"""
Rappels de pronos (notifications push), lances par .github/workflows/notify.yml.

Regles (choix de Piet, 07/11/2026) :
  1. veille de chaque jour de match (18h-23h, heure belge) : notification a chaque joueur a qui il manque un prono
     pour les matchs de ce jour-la ; filet de securite le jour meme des 7h, au plus tard 2 h avant le premier match ;
  2. mardi soir (18h-23h) : rappel pour les pronos manquants du week-end (samedi et dimanche) ; filet mercredi 7h-13h ;
  3. environ 1 h avant chaque match (entre 1h45 et 15 min avant) : rappel au joueur dont le prono manque pour ce match.
Une seule fois par partie et par rappel (trace dans Firebase hbpushlog/<partie>/<cle>,
cle = j-AAAA-MM-JJ, we-<lundi> ou h-AAAA-MM-JJ-HHMM pour l'heure de debut des matchs).
GitHub lance les taches programmees avec 4 a 5 h de retard (constate en novembre 2026) : reveil toutes les heures,
le premier passage qui tombe dans une bonne fenetre envoie. Reveil tous les quarts d'heure pour le rappel d'avant-match
(pas garanti : si GitHub ne lance aucun passage dans la fenetre, ce rappel-la ne part pas).

Donnees Firebase (lues avec FIREBASE_SECRET) :
  hbrooms/<partie>            : joueurs, matchs, pronos (comme l'app)
  hbpush/<partie>/<joueur>/<id> : abonnements {endpoint, keys, url} ecrits par l'app (illisibles depuis l'app)
Les dates et heures des matchs sont rafraichies depuis data/hockey-snapshot.json (matchs deja connus de la partie seulement).

Le journal GitHub du repo public est lisible par tous : on n'y ecrit ni nom de partie ni prenom, seulement des nombres.

Variables : FIREBASE_SECRET, VAPID_PRIVATE_KEY (texte PEM), MODE = rappel (defaut) | test | simulation, JOUEUR (pour test).
  test       : envoie tout de suite une notification d'essai a tous les appareils du JOUEUR (toutes parties).
  simulation : calcule les rappels comme si c'etait la veille du prochain premier match, sans rien envoyer.
"""
import datetime as dt
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

FIREBASE_URL = os.environ.get("FIREBASE_URL", "https://pronos-hockey-default-rtdb.europe-west1.firebasedatabase.app")
FIREBASE_SECRET = os.environ.get("FIREBASE_SECRET", "")
SNAPSHOT = os.environ.get("SNAPSHOT", "data/hockey-snapshot.json")
APP_URL = "https://pierresmons.github.io/pronos-hockey/championnat.html"
CLAIM_SUB = "https://pierresmons.github.io"  # contact du serveur pour Apple et Google (adresse du site)
BXL = ZoneInfo("Europe/Brussels")
EVENING = range(18, 23)  # veille du jour de match (et mardi pour le week-end) : 18h-22h59 (heure belge)
MORNING_FROM = 7         # filet : le jour du match des 7h si rien n'est parti la veille...
MORNING_GAP = dt.timedelta(hours=2)  # ... et au plus tard 2 h avant le premier match du jour
WE_FALLBACK_UNTIL = 13   # filet du rappel week-end : mercredi de 7h a 12h59
SOON_MIN = dt.timedelta(minutes=15)   # rappel d'avant-match : au plus tard 15 min avant...
SOON_MAX = dt.timedelta(minutes=105)  # ... et au plus tot 1h45 avant le debut


def fb(path, method="GET", value=None, query=""):
    q = "&".join(x for x in (query, "auth=" + urllib.parse.quote(FIREBASE_SECRET) if FIREBASE_SECRET else "") if x)
    url = f"{FIREBASE_URL}/{'/'.join(urllib.parse.quote(p, safe='') for p in path.split('/'))}.json" + ("?" + q if q else "")
    data = None if value is None else json.dumps(value).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def match_key(pid, a, b, leg):
    return re.sub(r"[.#$\[\]/]", "-", f"{pid}_{a}_{b}" + (f"_{leg}" if leg and leg > 1 else ""))


def load_snapshot():
    try:
        with open(SNAPSHOT, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for m in d.get("matches") or []:
        pid = d["pools"][m[0]][0]
        a, b = d["teams"][m[3]], d["teams"][m[4]]
        leg = m[9] if len(m) > 9 and m[9] else 1
        out[match_key(pid, a, b, leg)] = {"date": m[1], "time": m[2], "sa": m[5] if len(m) > 5 else None}
    return out


def room_matches(room, snap):
    ms = []
    for m in room.get("matches") or []:
        if not m or not m.get("id"):
            continue
        x = dict(m)
        s = snap.get(x["id"])
        if s:
            if s.get("date"):
                x["date"] = s["date"]
            if s.get("time"):
                x["time"] = s["time"]
            if s.get("sa") is not None and x.get("scoreA") is None:
                x["scoreA"] = s["sa"]
        ms.append(x)
    return ms


def start_of(m):
    try:
        t = m.get("time") or "00:00"
        return dt.datetime.fromisoformat(f"{m['date']}T{t[:5]}").replace(tzinfo=BXL)
    except (KeyError, TypeError, ValueError):
        return None


def is_open(m, now):
    if m.get("scoreA") is not None:
        return False
    s = start_of(m)
    return s is not None and s > now


def has_pred(room, mid, p):
    x = ((room.get("predictions") or {}).get(mid) or {}).get(p)
    return isinstance(x, dict) and x.get("a") is not None


def week_bounds(day):
    monday = day - dt.timedelta(days=day.weekday())
    return monday, monday + dt.timedelta(days=6)


def missing_count(room, ms, p):
    return sum(1 for m in ms if not has_pred(room, m["id"], p))


def day_plan(room, snap, now, day, same_day=False):
    """Rappel pour les matchs du jour `day` -> (cle, {joueur: (manquants du jour, manquants semaine)}, 1er match) ou None"""
    ms = room_matches(room, snap)
    monday, sunday = week_bounds(day)
    all_day = [m for m in ms if m.get("date") == day.isoformat()]
    opn_day = [m for m in all_day if is_open(m, now)]
    if not opn_day:
        return None
    if same_day:
        starts = [s for s in (start_of(m) for m in all_day) if s]
        if not starts or min(starts) - now < MORNING_GAP:
            return None  # trop tard : le premier match du jour est dans moins de 2 h (ou deja commence)
    opn_week = [m for m in ms if m.get("date") and monday.isoformat() <= m["date"] <= sunday.isoformat() and is_open(m, now)]
    missing = {}
    for p in room.get("players") or []:
        n = missing_count(room, opn_day, p)
        if n:
            missing[p] = (n, missing_count(room, opn_week, p))
    first = min((s for s in (start_of(m) for m in opn_day) if s), default=None)
    return "j-" + day.isoformat(), missing, first


def weekend_plan(room, snap, now):
    """Rappel du mardi pour les matchs du samedi et du dimanche de la semaine -> (cle, {joueur: manquants}, None) ou None"""
    monday, sunday = week_bounds(now.date())
    sat = (monday + dt.timedelta(days=5)).isoformat()
    we = [m for m in room_matches(room, snap) if m.get("date") in (sat, sunday.isoformat()) and is_open(m, now)]
    if not we:
        return None
    missing = {}
    for p in room.get("players") or []:
        n = missing_count(room, we, p)
        if n:
            missing[p] = n
    return "we-" + monday.isoformat(), missing, None


def soon_plans(room, snap, now):
    """Rappels d'avant-match : une liste de (cle, {joueur: [matchs sans prono]}, heure de debut), un par heure de debut"""
    slots = {}
    for m in room_matches(room, snap):
        s = start_of(m)
        if s and is_open(m, now) and SOON_MIN <= s - now <= SOON_MAX and (m.get("time") or "00:00")[:5] != "00:00":
            slots.setdefault(s, []).append(m)
    out = []
    for s, ms in sorted(slots.items()):
        missing = {}
        for p in room.get("players") or []:
            lst = [m for m in ms if not has_pred(room, m["id"], p)]
            if lst:
                missing[p] = lst
        out.append((f"h-{s:%Y-%m-%d-%H%M}", missing, s))
    return out


def plural(n):
    return f"{n} prono{'s' if n > 1 else ''}"


def message(kind, miss, first=None, same_day=False):
    if kind == "soon":
        h = first.strftime('%H:%M').replace(':', 'h')
        if len(miss) == 1:
            m = miss[0]
            return {"title": "Pronos Hockey", "body": f"{m.get('teamA', '?')} - {m.get('teamB', '?')} à {h} : il te manque ton prono !"}
        return {"title": "Pronos Hockey", "body": f"{len(miss)} matchs à {h} : il te manque tes pronos !"}
    if kind == "we":
        return {"title": "Pronos Hockey", "body": f"Il te manque {plural(miss)} pour le week-end. Pense à les remplir !"}
    n, tot = miss
    extra = f" ({tot} cette semaine)" if tot > n else ""
    h = f" Premier match à {first.strftime('%H:%M').replace(':', 'h')} !" if first and first.strftime("%H:%M") != "00:00" else ""
    return {"title": "Pronos Hockey",
            "body": f"Il te manque {plural(n)} pour {'aujourd' + chr(39) + 'hui' if same_day else 'demain'}{extra}.{h}"}


def jobs_for(now):
    """Rappels a tenter a cette heure : liste de (genre, jour vise, filet du matin)"""
    h, wd, today = now.hour, now.weekday(), now.date()
    jobs = [("soon", None, False)]  # avant-match : a toute heure (la fenetre depend de l'heure des matchs)
    if h in EVENING:
        jobs.append(("day", today + dt.timedelta(days=1), False))
        if wd == 1:
            jobs.append(("we", None, False))
    elif MORNING_FROM <= h < EVENING.start:
        jobs.append(("day", today, True))
        if wd == 2 and h < WE_FALLBACK_UNTIL:
            jobs.append(("we", None, True))
    return jobs


def run_job(room, snap, now, job):
    kind, day, same_day = job
    if kind == "soon":
        return soon_plans(room, snap, now)
    return weekend_plan(room, snap, now) if kind == "we" else day_plan(room, snap, now, day, same_day)


def send(sub, payload, key_file):
    from pywebpush import webpush, WebPushException
    info = {"endpoint": sub["endpoint"], "keys": sub.get("keys") or {}}
    try:
        webpush(subscription_info=info, data=json.dumps(payload, ensure_ascii=False), vapid_private_key=key_file,
                vapid_claims={"sub": CLAIM_SUB}, ttl=12 * 3600, timeout=20)
        return "ok"
    except WebPushException as e:
        code = getattr(e.response, "status_code", None)
        return "gone" if code in (404, 410) else f"erreur {code or ''}".strip()


def main():
    mode = (os.environ.get("MODE") or "rappel").strip().lower()
    pem = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if mode != "simulation" and "PRIVATE KEY" not in pem:
        sys.exit("Secret VAPID_PRIVATE_KEY absent ou incomplet")
    if not FIREBASE_SECRET:
        sys.exit("Secret FIREBASE_SECRET absent")
    now = dt.datetime.now(BXL)
    key_file = None
    if pem:
        fd, key_file = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(fd, "w") as f:
            f.write(pem + "\n")
    allsubs = fb("hbpush") or {}
    stats = {"ok": 0, "gone": 0, "err": 0}

    def deliver(room_code, p, payload):
        for sid, sub in list(((allsubs.get(room_code) or {}).get(p) or {}).items()):
            if not isinstance(sub, dict) or not sub.get("endpoint"):
                continue
            pl = dict(payload, url=sub.get("url") or APP_URL)
            r = send(sub, pl, key_file)
            if r == "ok":
                stats["ok"] += 1
            elif r == "gone":
                stats["gone"] += 1
                fb(f"hbpush/{room_code}/{p}/{sid}", "DELETE")
            else:
                stats["err"] += 1
                print("  envoi refuse :", r)

    if mode == "test":
        who = (os.environ.get("JOUEUR") or "").strip()
        if not who:
            sys.exit("Indique le joueur (exactement comme dans l'app)")
        n = 0
        for room_code, players in allsubs.items():
            if who in (players or {}):
                n += len(players[who] or {})
                deliver(room_code, who, {"title": "Pronos Hockey", "body": "Test : les rappels fonctionnent sur cet appareil.", "tag": "test"})
        print(f"Test : {n} appareil(s) trouve(s) pour ce joueur ; envoyes {stats['ok']}, expires {stats['gone']}, erreurs {stats['err']}")
        return

    if mode == "simulation":
        # les 7 prochains soirs a 19h : ce qui serait envoye (rien n'est envoye)
        moments = [dt.datetime.combine(now.date() + dt.timedelta(days=i), dt.time(19), tzinfo=BXL) for i in range(0, 8)]
        moments = [t for t in moments if t > now]
    else:
        moments = [now]
        # (le rappel d'avant-match peut tomber a toute heure : pas de "pas l'heure")
    snap = load_snapshot()
    rooms = list((fb("hbrooms", query="shallow=true") or {}).keys())
    print(f"Passage a {now:%a %H:%M} (heure belge), mode {mode} ; {len(rooms)} partie(s), "
          f"{sum(len(v or {}) for v in allsubs.values())} joueur(s) avec rappels")
    for i, room_code in enumerate(rooms, 1):
        if not allsubs.get(room_code):
            continue
        room = fb(f"hbrooms/{room_code}") or {}
        for t in moments:
            for job in jobs_for(t):
                if job[0] == "soon" and mode == "simulation":
                    continue
                res = run_job(room, snap, t, job)
                plans = res if job[0] == "soon" else ([res] if res else [])
                label0 = {"we": "week-end", "soon": "avant-match"}.get(job[0]) or f"matchs du {job[1]:%d/%m}"
                label0 += " (filet)" if job[2] else ""
                if not plans and job[0] != "soon" and mode != "simulation":
                    print(f"partie {i} : {label0} : rien a rappeler")
                for key, missing, first in plans:
                    label = label0 + (f" {first:%H:%M}" if job[0] == "soon" else "")
                    if mode == "rappel" and fb(f"hbpushlog/{room_code}/{key}"):
                        print(f"partie {i} : {label} : deja envoye")
                        continue
                    targets = [p for p in missing if (allsubs.get(room_code) or {}).get(p)]
                    print(f"partie {i} : {label} : {len(missing)} joueur(s) en retard dont {len(targets)} avec rappels"
                          + (f" (simulation {t:%a %d/%m %H:%M})" if mode == "simulation" else ""))
                    if mode == "simulation":
                        continue
                    for p in targets:
                        deliver(room_code, p, dict(message(job[0], missing[p], first, job[2]), tag="rappel-" + key))
                    fb(f"hbpushlog/{room_code}/{key}", "PUT", {"at": now.isoformat(timespec="minutes"), "late": len(missing), "sent": len(targets)})
    print(f"Envoyes {stats['ok']}, abonnements expires supprimes {stats['gone']}, erreurs {stats['err']}")


if __name__ == "__main__":
    main()

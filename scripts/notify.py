"""
Rappels de pronos (notifications push), lances par .github/workflows/notify.yml.

Regle (choix de Piet, 30/10/2026) : la veille du premier match de la semaine (lundi-dimanche), vers 19h,
une notification a chaque joueur a qui il manque encore des pronos pour les matchs ouverts de la semaine.
Une seule fois par partie et par semaine (trace dans Firebase hbpushlog/<partie>/<lundi>).

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
HOURS = range(19, 23)  # fenetre d'envoi (heure belge) ; GitHub lance parfois avec retard


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


def plan(room, snap, now):
    """-> (lundi, {joueur: nb de pronos manquants}, heure du 1er match) si demain est le 1er jour de match de la semaine, sinon None"""
    today = now.date()
    tomorrow = today + dt.timedelta(days=1)
    monday = tomorrow - dt.timedelta(days=tomorrow.weekday())
    sunday = monday + dt.timedelta(days=6)
    week = [m for m in room_matches(room, snap) if m.get("date") and monday.isoformat() <= m["date"] <= sunday.isoformat()]
    if not any(m["date"] == tomorrow.isoformat() for m in week):
        return None
    if any(m["date"] <= today.isoformat() for m in week):
        return None  # la semaine a deja commence : rappel deja fait (ou trop tard)
    opn = [m for m in week if is_open(m, now)]
    if not opn:
        return None
    first = min((start_of(m) for m in opn if m["date"] == tomorrow.isoformat()), default=None)
    missing = {}
    for p in room.get("players") or []:
        n = sum(1 for m in opn if not has_pred(room, m["id"], p))
        if n:
            missing[p] = n
    return monday.isoformat(), missing, first


def message(n, first):
    h = f" à {first.strftime('%H:%M').replace(':', 'h')}" if first and first.strftime("%H:%M") != "00:00" else ""
    return {"title": "Pronos Hockey",
            "body": f"Il te manque {n} prono{'s' if n > 1 else ''} cette semaine. Premier match demain{h} !"}


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

    if mode == "rappel" and now.hour not in HOURS:
        print(f"Pas l'heure ({now:%H:%M} a Bruxelles) : rien a faire")
        return
    snap = load_snapshot()
    rooms = list((fb("hbrooms", query="shallow=true") or {}).keys())
    print(f"{len(rooms)} partie(s), {sum(len(v or {}) for v in allsubs.values())} joueur(s) avec rappels")
    for i, room_code in enumerate(rooms, 1):
        if not allsubs.get(room_code):
            continue
        room = fb(f"hbrooms/{room_code}") or {}
        sim_now = now
        if mode == "simulation":
            # veille du prochain jour de match, 19h
            days = sorted({m["date"] for m in room_matches(room, snap) if m.get("date") and m["date"] > now.date().isoformat()})
            res = None
            for d in days:
                t = dt.datetime.fromisoformat(d).replace(hour=19, tzinfo=BXL) - dt.timedelta(days=1)
                if t < now:
                    continue
                res = plan(room, snap, t)
                if res:
                    sim_now = t
                    break
        else:
            res = plan(room, snap, now)
        if not res:
            print(f"partie {i} : pas de rappel aujourd'hui")
            continue
        monday, missing, first = res
        if mode == "rappel" and fb(f"hbpushlog/{room_code}/{monday}"):
            print(f"partie {i} : rappel deja envoye cette semaine")
            continue
        targets = [p for p in missing if ((allsubs.get(room_code) or {}).get(p))]
        print(f"partie {i} : semaine du {monday}, {len(missing)} joueur(s) en retard dont {len(targets)} avec rappels"
              + (f" (simulation : {sim_now:%d/%m %H:%M})" if mode == "simulation" else ""))
        if mode == "simulation":
            continue
        for p in targets:
            deliver(room_code, p, dict(message(missing[p], first), tag="rappel-" + monday))
        fb(f"hbpushlog/{room_code}/{monday}", "PUT", {"at": now.isoformat(timespec="minutes"), "late": len(missing), "sent": len(targets)})
    print(f"Envoyes {stats['ok']}, abonnements expires supprimes {stats['gone']}, erreurs {stats['err']}")


if __name__ == "__main__":
    main()

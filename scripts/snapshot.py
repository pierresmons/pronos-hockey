#!/usr/bin/env python3
"""
Robot Pronos Hockey : recupere TOUTES les poules de hockey.be (calendrier,
resultats, classements) et les range dans un seul fichier compact
data/hockey-snapshot.json, lu par l'onglet Matchs de l'app.

Lance automatiquement par GitHub Actions (.github/workflows/snapshot.yml).
Python standard uniquement, aucune dependance.

Ajoute aussi les matchs de l'Euro Hockey League (ehlhockey.tv), voir ehl.py.
  python scripts/snapshot.py             -> tout (hockey.be + EHL)
  python scripts/snapshot.py --ehl-only  -> seulement l'EHL (rapide, pendant les week-ends EHL)
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ehl  # noqa: E402

PAGE = "https://hockey.be/fr/competition/calendrier-resultats-et-classements/"
API = "https://hockey.be/wp-json/sportlink-api/cached"
SEASON_FROM = os.environ.get("SEASON_FROM", "2026-07-01")
SEASON_TO = os.environ.get("SEASON_TO", "2027-06-30")
OUT = os.environ.get("SNAPSHOT_OUT", "data/hockey-snapshot.json")
WORKERS = 6
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15 PronosHockeyFamille/1.0"


REQ = {"n": 0, "fail": 0, "examples": []}


def get(url, tries=3, timeout=45):
    for i in range(tries):
        REQ["n"] += 1
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/html"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            REQ["fail"] += 1
            if len(REQ["examples"]) < 3:
                REQ["examples"].append(f"{type(e).__name__}: {e}"[:150])
            time.sleep(2 * (i + 1))
    raise last


def strip(h):
    h = re.sub(r"<br\s*/?>", " | ", str(h or ""))
    h = re.sub(r"<[^>]+>", "", h)
    return re.sub(r"\s+", " ", html.unescape(h)).strip()


def team(h):
    t = re.sub(r"\s*\|\s*", " ", strip(h), count=1)
    return re.sub(r" (Outdoor|Indoor|Récréatif|Recreatif|Trimmers) Semaine", "", t, flags=re.I).strip()


LOGOS = {}  # nom du club (ex. "Old Club") -> code du logo hockey.be (logos/50x50/<code>.png)
LOGO_RE = re.compile(r"logos/50x50/([0-9A-Fa-f]{32})\.png")


def club_of(h):
    m = re.search(r"<span[^>]*>(.*?)</span>", str(h or ""), re.S)
    return strip(m.group(1)) if m else None


def note_logos(r):
    """Ligne hockey.be : [.., equipe A, logo A, .., logo B, equipe B] -> retient le logo de chaque club."""
    if len(r) < 6:
        return
    for team_html, logo_html in ((r[3], r[4]), (r[-1], r[-2])):
        c, m = club_of(team_html), LOGO_RE.search(str(logo_html or "").replace("\\/", "/"))
        if c and m:
            LOGOS[c] = m.group(1).upper()


LOGO_URL = "https://hockey.be/wp-content/uploads/logos/50x50/{}.png"
# Verification des images de logos : codes connus bons / introuvables (404), date de la derniere verification complete
LOGO_CHECK = {"ok": [], "missing": [], "full": None}
LOGO_FULL_DAYS = 7


LOGO_ERRORS = []


def logo_status(code):
    """Image presente -> True, 404/410 -> False, autre erreur (reseau, refus...) -> None (on reessaiera)."""
    for i in range(2):
        try:
            # GET (et pas HEAD : certains serveurs refusent HEAD) ; une image 50x50 pese quelques Ko
            req = urllib.request.Request(LOGO_URL.format(code), headers={"User-Agent": UA, "Accept": "image/*,*/*"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return 200 <= r.status < 300 and len(r.read(65536)) > 0
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return False
            err = f"HTTP {e.code}"
        except Exception as e:  # noqa
            err = f"{type(e).__name__}: {e}"
        if len(LOGO_ERRORS) < 3:
            LOGO_ERRORS.append(f"{code} -> {err}")
        time.sleep(2)
    return None


def check_logos():
    """Verifie seulement les nouveaux codes ; tout revoir une fois par semaine."""
    global LOGO_CHECK
    now = dt.datetime.now(dt.timezone.utc)
    codes = set(LOGOS.values())
    ok, missing = set(LOGO_CHECK.get("ok") or []) & codes, set(LOGO_CHECK.get("missing") or []) & codes
    full = LOGO_CHECK.get("full")
    try:
        due = not full or (now - dt.datetime.strptime(full, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)).days >= LOGO_FULL_DAYS
    except ValueError:
        due = True
    todo = sorted(codes if due else codes - ok - missing)
    errors = 0
    if todo:
        with ThreadPoolExecutor(WORKERS) as ex:
            for code, st in zip(todo, ex.map(logo_status, todo)):
                if st is None:
                    errors += 1
                    continue
                ok.discard(code); missing.discard(code)
                (ok if st else missing).add(code)
    if due and errors > len(todo) // 4:
        due = False  # trop d'erreurs : la verification complete sera refaite au prochain passage
    LOGO_CHECK = {"ok": sorted(ok), "missing": sorted(missing),
                  "full": now.strftime("%Y-%m-%dT%H:%M:%SZ") if due else full}
    print(f"Logos : {len(todo)} verifie(s){' (verification complete)' if due else ''}, "
          f"{len(missing)} introuvable(s), {errors} erreur(s)", file=sys.stderr)
    for e in LOGO_ERRORS:
        print(f"  exemple d'erreur logo : {e}", file=sys.stderr)


def logos_missing():
    miss = set(LOGO_CHECK.get("missing") or [])
    return sorted(c for c, code in LOGOS.items() if code in miss)


def iso(dmy):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", dmy or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def shift(d, days):
    return (dt.date.fromisoformat(d) + dt.timedelta(days=days)).isoformat()


def api(pid, endpoint):
    """hockey.be garde parfois une reponse VIDE en cache pour une requete precise :
    on essaie plusieurs variantes de dates avant d'accepter une liste vide."""
    today = dt.date.today().isoformat()
    if endpoint == "program":
        variants = [(shift(today, -1), SEASON_TO), (today, SEASON_TO), (SEASON_FROM, SEASON_TO), (shift(today, -2), shift(SEASON_TO, 1))]
    else:
        variants = [(SEASON_FROM, SEASON_TO), (shift(SEASON_FROM, 1), SEASON_TO), (shift(SEASON_FROM, -1), SEASON_TO), (shift(SEASON_FROM, 2), shift(SEASON_TO, 1))]
    ok = False
    for f, t in variants:
        q = urllib.parse.urlencode({"lang": "fr", "dump": "", "clubid": "", "facilityid": "", "poolid": pid, "from": f, "to": t, "endpoint": endpoint})
        try:
            data = json.loads(get(f"{API}?{q}", tries=2, timeout=20)).get("data") or []
            ok = True
            if data:
                return data
        except Exception:
            pass
        time.sleep(0.3)
    if ok:
        return []
    raise RuntimeError(f"hockey.be injoignable pour {pid}/{endpoint}")


def pool_list(previous):
    try:
        page = get(PAGE)
        sel = re.search(r'<select[^>]*name="poolid"[^>]*>(.*?)</select>', page, re.S)
        opts = re.findall(r'<option[^>]*value="(\d+)"[^>]*>(.*?)</option>', sel.group(1), re.S)
        pools = [[v, strip(n)] for v, n in opts]
        if len(pools) > 50:
            return pools
    except Exception as e:
        print("Liste des poules indisponible :", e, file=sys.stderr)
    if previous:
        print("On reutilise la liste du snapshot precedent", file=sys.stderr)
        return previous
    raise SystemExit("Impossible d'obtenir la liste des poules")


# ---------- Quelles poules lire a ce passage ? ----------
FIREBASE_URL = os.environ.get("FIREBASE_URL", "https://pronos-hockey-default-rtdb.europe-west1.firebasedatabase.app")
BXL = ZoneInfo("Europe/Brussels")
POOL_SEEN = {}        # poule -> date (AAAA-MM-JJ, heure belge) de la derniere lecture reussie
EMPTY_DAYS = 7        # poule vide : relue une fois par semaine
ACTIVE_BEFORE, ACTIVE_AFTER = 2, 3   # match il y a 2 jours ou moins, ou dans 3 jours ou moins : poule relue a chaque passage


def fb_get(path):
    req = urllib.request.Request(f"{FIREBASE_URL}/{path}.json", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def priority_pools(old):
    """Poules des parties (Firebase hbrooms/<partie>/pools) + poules des equipes favorites.
    On ne lit que des numeros de poules et des noms d'equipes ; rien n'est recopie dans le snapshot."""
    pools, fav_teams = set(), set()
    try:
        rooms = list((json.loads(urllib.request.urlopen(urllib.request.Request(
            f"{FIREBASE_URL}/hbrooms.json?shallow=true", headers={"User-Agent": UA}), timeout=20).read().decode("utf-8")) or {}).keys())
        for room in rooms:
            q = urllib.parse.quote(room, safe="")
            for p in (fb_get(f"hbrooms/{q}/pools") or []):
                pools.add(str(p))
            for teams in (fb_get(f"hbrooms/{q}/favs") or {}).values():
                fav_teams.update(teams or [])
    except Exception as e:  # noqa
        print("Parties Firebase illisibles (pas grave, toutes les poules actives sont lues) :", e, file=sys.stderr)
    if fav_teams and old:
        opools, oteams = [p[0] for p in old.get("pools", [])], old.get("teams", [])
        for m in old.get("matches", []):
            if oteams[m[3]] in fav_teams or oteams[m[4]] in fav_teams:
                pools.add(opools[m[0]])
    return pools


def plan_pools(pools, old):
    """Renvoie (a_lire, a_garder, comptes). a_garder = poules reprises telles quelles du snapshot precedent."""
    global POOL_SEEN
    today = dt.datetime.now(BXL).date()
    POOL_SEEN = dict((old or {}).get("poolSeen") or {})
    if old and not POOL_SEEN:
        # premier passage avec ce systeme : on considere le snapshot precedent comme lu ce jour-la
        try:
            gen = dt.datetime.strptime(old["generated"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
            day = gen.astimezone(BXL).date().isoformat()
            POOL_SEEN = {p[0]: day for p in pools}
        except Exception:  # noqa
            pass
    dates = {}
    if old:
        opools = [p[0] for p in old.get("pools", [])]
        for m in old.get("matches", []):
            if m[1]:
                dates.setdefault(opools[m[0]], []).append(dt.date.fromisoformat(m[1]))
    prio = priority_pools(old)
    late = set((old or {}).get("failed") or [])
    first, then, keep = [], [], []
    cnt = {"parties/favoris": 0, "en retard": 0, "actives": 0, "calmes (1x/jour)": 0, "vides (1x/semaine)": 0, "jamais lues": 0, "gardees": 0}
    for p in pools:
        pid, seen = p[0], POOL_SEEN.get(p[0])
        age = (today - dt.date.fromisoformat(seen)).days if seen else None
        if pid in prio:
            first.append(p); cnt["parties/favoris"] += 1
        elif pid in late:
            first.append(p); cnt["en retard"] += 1
        elif age is None:
            then.append(p); cnt["jamais lues"] += 1
        elif pid not in dates:
            if age >= EMPTY_DAYS:
                then.append(p); cnt["vides (1x/semaine)"] += 1
            else:
                keep.append(p); cnt["gardees"] += 1
        elif any(-ACTIVE_BEFORE <= (d - today).days <= ACTIVE_AFTER for d in dates[pid]):
            then.append(p); cnt["actives"] += 1
        elif age >= 1:
            then.append(p); cnt["calmes (1x/jour)"] += 1
        else:
            keep.append(p); cnt["gardees"] += 1
    return first + then, keep, cnt


START = time.time()
# GitHub coupe le robot apres timeout-minutes (snapshot.yml). On s'arrete avant, proprement :
# les poules pas encore lues gardent leurs donnees du snapshot precedent et passent en premier la fois suivante.
DEADLINE_MIN = float(os.environ.get("DEADLINE_MIN", "40"))
LATE = "temps depasse"
LATE_POOLS = set()


def too_late():
    return time.time() - START > DEADLINE_MIN * 60


def fetch_pool(pid):
    if too_late():
        return pid, [], [], [], LATE
    try:
        return pid, api(pid, "program"), api(pid, "results"), api(pid, "standing"), None
    except Exception as e:
        return pid, [], [], [], str(e)


EHL_STATUS = {"ok": None, "info": ""}


def add_ehl(pools, matches, standings, t_id, old):
    """Ajoute les poules EHL ; en cas d'echec, reprend celles du snapshot precedent."""
    try:
        res = ehl.collect(get, SEASON_FROM, SEASON_TO, log=lambda *a: print(*a, file=sys.stderr))
        EHL_STATUS.update(ok=True, info=f"{len(res)} poule(s), {sum(len(r[2]) for r in res)} match(s)")
    except Exception as e:  # noqa
        print("EHL indisponible :", e, file=sys.stderr)
        EHL_STATUS.update(ok=False, info=f"ehlhockey.tv indisponible : {e}"[:200])
        res = None
    if res is None:
        if old:
            opools = old.get("pools", [])
            oteams = old.get("teams", [])
            for i, p in enumerate(opools):
                if p[0].startswith("ehl-"):
                    pools.append(p)
            for om in old.get("matches", []):
                p = opools[om[0]] if om[0] < len(opools) else None
                if p and p[0].startswith("ehl-"):
                    pi = next(i for i, x in enumerate(pools) if x[0] == p[0])
                    a, b = oteams[om[3]], oteams[om[4]]
                    matches[(p[0], a, b)] = [pi, om[1], om[2], t_id(a), t_id(b)] + om[5:]
            for pid, st in old.get("standings", {}).items():
                if pid.startswith("ehl-"):
                    standings[pid] = st
        return False
    for pid, name, ms, st, venue in res:
        pools.append([pid, name])
        pi = len(pools) - 1
        for m in ms:
            status = None
            if m["so"]:
                status = "Shoot-out : " + (m["a"] if m["so"] == "A" else m["b"])
            matches[(pid, m["a"], m["b"])] = [pi, m["date"], m["time"], t_id(m["a"]), t_id(m["b"]), m["sa"], m["sb"], status, venue]
        if st:
            standings[pid] = st
    return True


def write(pools, teams, matches, standings, failed, generated=None):
    used = sorted({m[0] for m in matches.values()})
    remap = {old_i: new_i for new_i, old_i in enumerate(used)}
    out_pools = [pools[i] for i in used]
    # on ne garde que les equipes utilisees (le mode --ehl-only repart d'un index existant)
    out_matches = sorted(([remap[m[0]]] + m[1:] for m in matches.values()), key=lambda m: (m[1] or "9999", m[2] or "", m[0]))
    used_t = sorted({m[3] for m in out_matches} | {m[4] for m in out_matches})
    tmap = {o: n for n, o in enumerate(used_t)}
    for m in out_matches:
        m[3], m[4] = tmap[m[3]], tmap[m[4]]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap = {
        "generated": generated or now,
        "ehlGenerated": now,
        "ehlStatus": {"ok": EHL_STATUS["ok"], "info": EHL_STATUS["info"], "at": now},
        "season": [SEASON_FROM, SEASON_TO],
        "format": "matches = [poule, date, heure, equipeA, equipeB, butsA, butsB, statut, terrain] ; standings = [rang, equipe, J, G, P, N, BP, BC, Pts] ; logos = {club: code} -> https://hockey.be/wp-content/uploads/logos/50x50/<code>.png ; logosMissing = clubs dont l'image n'existe pas sur hockey.be",
        "pools": out_pools,
        "teams": [teams[i] for i in used_t],
        "matches": out_matches,
        "standings": {p[0]: standings[p[0]] for p in out_pools if p[0] in standings},
        "failed": failed,
        "logos": dict(sorted(LOGOS.items())),
        "logosMissing": logos_missing(),
        "logoCheck": LOGO_CHECK,
        "poolSeen": dict(sorted(POOL_SEEN.items())),
    }
    if len(out_matches) < 100:
        raise SystemExit(f"Seulement {len(out_matches)} matchs : hockey.be semble indisponible, snapshot non ecrit")
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    n_ehl = sum(1 for p in out_pools if p[0].startswith("ehl-"))
    print(f"OK : {len(out_pools)} poules (dont {n_ehl} EHL), {len(out_matches)} matchs, {len(snap['teams'])} equipes, "
          f"{len(failed)} echec(s), {os.path.getsize(OUT) // 1024} Ko", file=sys.stderr)


def main_ehl_only():
    """Remplace seulement la partie EHL du snapshot existant (quelques secondes)."""
    old = json.load(open(OUT, encoding="utf-8"))
    LOGOS.update(old.get("logos") or {})
    global LOGO_CHECK
    LOGO_CHECK = old.get("logoCheck") or LOGO_CHECK
    global POOL_SEEN
    POOL_SEEN = dict(old.get("poolSeen") or {})
    teams = list(old["teams"])
    tidx = {t: i for i, t in enumerate(teams)}

    def t_id(name):
        if name not in tidx:
            tidx[name] = len(teams)
            teams.append(name)
        return tidx[name]

    pools = [p for p in old["pools"]]
    matches = {}
    for om in old["matches"]:
        pid = pools[om[0]][0]
        if not pid.startswith("ehl-"):
            matches[(pid, teams[om[3]], teams[om[4]])] = list(om)
    standings = {k: v for k, v in old.get("standings", {}).items() if not k.startswith("ehl-")}
    keep = [i for i, p in enumerate(pools) if not p[0].startswith("ehl-")]
    remap = {o: n for n, o in enumerate(keep)}
    pools = [pools[i] for i in keep]
    for k, m in matches.items():
        m[0] = remap[m[0]]
    if not add_ehl(pools, matches, standings, t_id, None):
        # on note seulement l'echec (visible dans le fichier) ; ehlGenerated ne bouge pas -> nouvel essai au prochain reveil
        print("EHL indisponible : seul le statut est mis a jour, nouvel essai au prochain reveil", file=sys.stderr)
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        old["ehlStatus"] = {"ok": False, "info": EHL_STATUS["info"], "at": now}
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, separators=(",", ":"))
        return
    write(pools, teams, matches, standings, old.get("failed", []), generated=old.get("generated"))


def main():
    if "--ehl-only" in sys.argv:
        return main_ehl_only()
    previous = None
    old = None
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
            previous = old.get("pools")
        except Exception:
            pass
    global LOGO_CHECK
    if old:
        LOGOS.update(old.get("logos") or {})
        LOGO_CHECK = old.get("logoCheck") or LOGO_CHECK
    pools = [p for p in pool_list(previous) if not p[0].startswith("ehl-")]
    # ordre : parties/favoris, poules en retard, puis les autres ; certaines poules calmes ou vides sont gardees telles quelles
    pools, kept, cnt = plan_pools(pools, old)
    print(f"{len(pools) + len(kept)} poules connues, {len(pools)} a lire : " +
          ", ".join(f"{k} {v}" for k, v in cnt.items() if v), file=sys.stderr)
    kept_ids = {p[0] for p in kept}
    pools = pools + kept  # les poules gardees restent dans la liste (index), sans etre lues
    to_read = [p[0] for p in pools if p[0] not in kept_ids]
    today_s = dt.datetime.now(BXL).date().isoformat()

    teams, tidx = [], {}

    def t_id(name):
        if name not in tidx:
            tidx[name] = len(teams)
            teams.append(name)
        return tidx[name]

    matches, standings, failed = {}, {}, []
    with ThreadPoolExecutor(WORKERS) as ex:
        for n, (pid, program, results, standing, err) in enumerate(ex.map(fetch_pool, to_read), 1):
            if err:
                failed.append(pid)
                if err == LATE:
                    LATE_POOLS.add(pid)
            else:
                POOL_SEEN[pid] = today_s
            pi = next(i for i, p in enumerate(pools) if p[0] == pid)
            for r in program:
                note_logos(r)
                dv = strip(r[0]).split(" | ")
                a, b = team(r[3]), team(r[-1])
                key = (pid, a, b)
                matches[key] = [pi, iso(dv[0]), (r[1] or "")[:5], t_id(a), t_id(b), None, None, None, dv[1] if len(dv) > 1 else None]
            seen = {}
            for r in results:
                note_logos(r)
                a, b = team(r[3]), team(r[-1])
                sc = strip(r[5] if len(r) > 5 else "")
                m = re.search(r"(\d+)\s*-\s*(\d+)", sc)
                status = (re.sub(r"(\d+)\s*-\s*(\d+)", "", sc).strip() or None) if m else (sc or None)
                key = (pid, a, b)
                # match annule puis rejoue : hockey.be donne deux lignes pour le meme match.
                # On garde celle qui a un score, sinon celle a la date la plus recente.
                d = iso(r[0]) or ""
                # ligne annulee sans score alors que le calendrier donne deja la nouvelle date (match a rejouer)
                if not m and key not in seen and key in matches and (matches[key][1] or "") > d:
                    continue
                if key in seen:
                    had_score, had_date = seen[key]
                    if (had_score and not m) or (bool(m) == had_score and d <= had_date):
                        continue
                seen[key] = (bool(m), d)
                venue = matches[key][8] if key in matches else None
                matches[key] = [pi, iso(r[0]), (r[1] or "")[:5], t_id(a), t_id(b),
                                int(m.group(1)) if m else None, int(m.group(2)) if m else None, status, venue]
            if standing:
                standings[pid] = [[s[0], s[1], s[2], s[3], s[4], s[5], s[6], s[7], s[8]] for s in standing]
            if n % 50 == 0:
                print(f"  {n}/{len(to_read)}", file=sys.stderr)

    # Une poule en echec, ou pas lue a ce passage, garde ses donnees du snapshot precedent
    reuse = set(failed) | kept_ids
    if failed:
        print(f"{len(failed)} poule(s) en echec, reprise des anciennes donnees", file=sys.stderr)
    print(f"Demandes a hockey.be : {REQ['n']}, echecs : {REQ['fail']}, duree {int(time.time() - START)} s", file=sys.stderr)
    for e in REQ["examples"]:
        print(f"  exemple d'echec : {e}", file=sys.stderr)
    if reuse and old:
        oteams = old.get("teams", [])
        opools = [p[0] for p in old.get("pools", [])]
        for om in old.get("matches", []):
            opid = opools[om[0]] if om[0] < len(opools) else None
            if opid in reuse:
                pi = next((i for i, p in enumerate(pools) if p[0] == opid), None)
                if pi is None:
                    continue
                a, b = oteams[om[3]], oteams[om[4]]
                matches[(opid, a, b)] = [pi, om[1], om[2], t_id(a), t_id(b)] + om[5:]
        for pid in reuse:
            if pid in old.get("standings", {}):
                standings[pid] = old["standings"][pid]

    add_ehl(pools, matches, standings, t_id, old)
    n_late = sum(1 for p in failed if p in LATE_POOLS)
    if n_late:
        print(f"Temps depasse : {n_late} poule(s) non lues, reprises du snapshot precedent", file=sys.stderr)
    try:
        if too_late():
            raise RuntimeError("temps depasse, reportee au prochain passage")
        check_logos()
    except Exception as e:  # noqa : la verification des logos ne doit jamais bloquer le snapshot
        print(f"Verification des logos impossible : {e}", file=sys.stderr)
    write(pools, teams, matches, standings, failed)


if __name__ == "__main__":
    main()

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
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ehl  # noqa: E402

PAGE = "https://hockey.be/fr/competition/calendrier-resultats-et-classements/"
API = "https://hockey.be/wp-json/sportlink-api/cached"
SEASON_FROM = os.environ.get("SEASON_FROM", "2026-07-01")
SEASON_TO = os.environ.get("SEASON_TO", "2027-06-30")
OUT = os.environ.get("SNAPSHOT_OUT", "data/hockey-snapshot.json")
WORKERS = 6
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15 PronosHockeyFamille/1.0"


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/html"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (i + 1))
    raise last


def strip(h):
    h = re.sub(r"<br\s*/?>", " | ", str(h or ""))
    h = re.sub(r"<[^>]+>", "", h)
    return re.sub(r"\s+", " ", html.unescape(h)).strip()


def team(h):
    t = re.sub(r"\s*\|\s*", " ", strip(h), count=1)
    return re.sub(r" (Outdoor|Indoor|Récréatif|Recreatif|Trimmers) Semaine", "", t, flags=re.I).strip()


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
            data = json.loads(get(f"{API}?{q}")).get("data") or []
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


def fetch_pool(pid):
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
        "format": "matches = [poule, date, heure, equipeA, equipeB, butsA, butsB, statut, terrain] ; standings = [rang, equipe, J, G, P, N, BP, BC, Pts]",
        "pools": out_pools,
        "teams": [teams[i] for i in used_t],
        "matches": out_matches,
        "standings": {p[0]: standings[p[0]] for p in out_pools if p[0] in standings},
        "failed": failed,
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
        print("EHL indisponible : snapshot inchange, nouvel essai au prochain reveil", file=sys.stderr)
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
    pools = [p for p in pool_list(previous) if not p[0].startswith("ehl-")]
    print(f"{len(pools)} poules a recuperer", file=sys.stderr)

    teams, tidx = [], {}

    def t_id(name):
        if name not in tidx:
            tidx[name] = len(teams)
            teams.append(name)
        return tidx[name]

    matches, standings, failed = {}, {}, []
    with ThreadPoolExecutor(WORKERS) as ex:
        for n, (pid, program, results, standing, err) in enumerate(ex.map(fetch_pool, [p[0] for p in pools]), 1):
            if err:
                failed.append(pid)
            pi = next(i for i, p in enumerate(pools) if p[0] == pid)
            for r in program:
                dv = strip(r[0]).split(" | ")
                a, b = team(r[3]), team(r[-1])
                key = (pid, a, b)
                matches[key] = [pi, iso(dv[0]), (r[1] or "")[:5], t_id(a), t_id(b), None, None, None, dv[1] if len(dv) > 1 else None]
            for r in results:
                a, b = team(r[3]), team(r[-1])
                sc = strip(r[5] if len(r) > 5 else "")
                m = re.search(r"(\d+)\s*-\s*(\d+)", sc)
                status = (re.sub(r"(\d+)\s*-\s*(\d+)", "", sc).strip() or None) if m else (sc or None)
                key = (pid, a, b)
                venue = matches[key][8] if key in matches else None
                matches[key] = [pi, iso(r[0]), (r[1] or "")[:5], t_id(a), t_id(b),
                                int(m.group(1)) if m else None, int(m.group(2)) if m else None, status, venue]
            if standing:
                standings[pid] = [[s[0], s[1], s[2], s[3], s[4], s[5], s[6], s[7], s[8]] for s in standing]
            if n % 50 == 0:
                print(f"  {n}/{len(pools)}", file=sys.stderr)

    # Une poule en echec garde ses donnees du snapshot precedent
    if failed and old:
        print(f"{len(failed)} poule(s) en echec, reprise des anciennes donnees", file=sys.stderr)
        oteams = old.get("teams", [])
        opools = [p[0] for p in old.get("pools", [])]
        for om in old.get("matches", []):
            opid = opools[om[0]] if om[0] < len(opools) else None
            if opid in failed:
                pi = next((i for i, p in enumerate(pools) if p[0] == opid), None)
                if pi is None:
                    continue
                a, b = oteams[om[3]], oteams[om[4]]
                matches[(opid, a, b)] = [pi, om[1], om[2], t_id(a), t_id(b)] + om[5:]
        for pid in failed:
            if pid in old.get("standings", {}):
                standings[pid] = old["standings"][pid]

    add_ehl(pools, matches, standings, t_id, old)
    write(pools, teams, matches, standings, failed)


if __name__ == "__main__":
    main()

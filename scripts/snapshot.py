#!/usr/bin/env python3
"""
Robot Pronos Hockey : recupere TOUTES les poules de hockey.be (calendrier,
resultats, classements) et les range dans un seul fichier compact
data/hockey-snapshot.json, lu par l'onglet Matchs de l'app.

Lance automatiquement par GitHub Actions (.github/workflows/snapshot.yml).
Python standard uniquement, aucune dependance.
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


def main():
    previous = None
    old = None
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
            previous = old.get("pools")
        except Exception:
            pass
    pools = pool_list(previous)
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

    # On ne garde que les poules qui ont au moins un match
    used = sorted({m[0] for m in matches.values()})
    remap = {old_i: new_i for new_i, old_i in enumerate(used)}
    out_pools = [pools[i] for i in used]
    out_matches = sorted(([remap[m[0]]] + m[1:] for m in matches.values()), key=lambda m: (m[1] or "9999", m[2] or "", m[0]))
    snap = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "season": [SEASON_FROM, SEASON_TO],
        "format": "matches = [poule, date, heure, equipeA, equipeB, butsA, butsB, statut, terrain] ; standings = [rang, equipe, J, G, P, N, BP, BC, Pts]",
        "pools": out_pools,
        "teams": teams,
        "matches": out_matches,
        "standings": {p[0]: standings[p[0]] for p in out_pools if p[0] in standings},
        "failed": failed,
    }
    if len(out_matches) < 100:
        raise SystemExit(f"Seulement {len(out_matches)} matchs : hockey.be semble indisponible, snapshot non ecrit")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    print(f"OK : {len(out_pools)} poules, {len(out_matches)} matchs, {len(teams)} equipes, {len(failed)} echec(s), "
          f"{os.path.getsize(OUT) // 1024} Ko", file=sys.stderr)


if __name__ == "__main__":
    main()

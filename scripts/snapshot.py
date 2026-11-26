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
import fihpl  # noqa: E402
import nlhk  # noqa: E402

PAGE = "https://hockey.be/fr/competition/calendrier-resultats-et-classements/"
API = "https://hockey.be/wp-json/sportlink-api/cached"
SEASON_FROM = os.environ.get("SEASON_FROM", "2026-07-01")
SEASON_TO = os.environ.get("SEASON_TO", "2027-06-30")
OUT = os.environ.get("SNAPSHOT_OUT", "data/hockey-snapshot.json")
WORKERS = 6
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15 PronosHockeyFamille/1.0"


REQ = {"n": 0, "fail": 0, "lost": 0, "examples": []}


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
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                # 25/11/2026 : le nouveau site limite le nombre de demandes (429 Too Many Requests) : on attend plus
                REQ["429"] = REQ.get("429", 0) + 1
                try:
                    wait = int(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0
                time.sleep(min(max(wait, 15 * (i + 1)), 90))
                continue
            time.sleep(2 * (i + 1))
    REQ["lost"] += 1
    if len(REQ["examples"]) < 3:
        REQ["examples"].append(f"{type(last).__name__}: {last}"[:150])
    raise last


def strip(h):
    h = re.sub(r"<br\s*/?>", " | ", str(h or ""))
    h = re.sub(r"<[^>]+>", "", h)
    return re.sub(r"\s+", " ", html.unescape(h)).strip()


def team(h):
    t = re.sub(r"\s*\|\s*", " ", strip(h), count=1)
    t = re.sub(r" (Outdoor|Indoor|Récréatif|Recreatif|Trimmers) Semaine", "", t, flags=re.I).strip()
    # depuis le 05/11/2026 hockey.be ecrit "... U19B-1 Outdoor" (sans "Semaine")
    return re.sub(r" (Outdoor|Indoor|Récréatif|Recreatif|Trimmers)$", "", t, flags=re.I).strip()


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
        time.sleep(2)
    if len(LOGO_ERRORS) < 3:
        LOGO_ERRORS.append(f"{code} -> {err}")
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


HAD = {}  # poule -> endpoints qui avaient des donnees au snapshot precedent ({"program", "results", "standing"})


def note_had(old):
    if not old:
        return
    today = dt.date.today().isoformat()
    opools = [p[0] for p in old.get("pools", [])]
    for p in opools:
        HAD[p] = set()
    for m in old.get("matches", []):
        pid = opools[m[0]]
        HAD[pid].add("results" if m[5] is not None or (m[1] or "9999") < today else "program")
    for pid in old.get("standings", {}):
        if pid in HAD:
            HAD[pid].add("standing")


def api(pid, endpoint, variants=None):
    """hockey.be garde parfois une reponse VIDE en cache pour une requete precise :
    on essaie plusieurs variantes de dates avant d'accepter une liste vide."""
    today = dt.date.today().isoformat()
    if not variants:
        if endpoint == "program":
            variants = [(shift(today, -1), SEASON_TO), (today, SEASON_TO), (SEASON_FROM, SEASON_TO), (shift(today, -2), shift(SEASON_TO, 1))]
        else:
            variants = [(SEASON_FROM, SEASON_TO), (shift(SEASON_FROM, 1), SEASON_TO), (shift(SEASON_FROM, -1), SEASON_TO), (shift(SEASON_FROM, 2), shift(SEASON_TO, 1))]
        # hockey.be garde parfois une reponse vide en cache : on n'insiste (4 essais) que si cette poule
        # avait des donnees pour cet endpoint au passage precedent ; sinon 2 essais suffisent
        if (pid in HAD or pid in POOL_SEEN) and endpoint not in HAD.get(pid, ()):
            variants = variants[:2]
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


# ---------- Quelles poules lire a ce passage ? ----------
FIREBASE_URL = os.environ.get("FIREBASE_URL", "https://pronos-hockey-default-rtdb.europe-west1.firebasedatabase.app")
BXL = ZoneInfo("Europe/Brussels")
POOL_SEEN = {}        # poule -> date (AAAA-MM-JJ, heure belge) de la derniere lecture reussie
EMPTY_DAYS = 1        # poule vide : relue une fois par jour (calendriers publies en cours de saison, ex. indoor jeunes le 05/11)
ACTIVE_BEFORE, ACTIVE_AFTER = 2, 3   # match il y a 2 jours ou moins, ou dans 3 jours ou moins : poule relue a chaque passage


FIREBASE_SECRET = os.environ.get("FIREBASE_SECRET", "")  # cle secrete (GitHub secret) : lecture de toutes les parties


def fb_get(path, query=""):
    q = "&".join(x for x in (query, "auth=" + urllib.parse.quote(FIREBASE_SECRET) if FIREBASE_SECRET else "") if x)
    req = urllib.request.Request(f"{FIREBASE_URL}/{path}.json" + ("?" + q if q else ""), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


WATCH_POOLS = set()  # poules d'un match suivi (cloche, 21/11/2026) : chassees des 1h15 apres le debut


def priority_pools(old):
    """Poules des parties (Firebase hbrooms/<partie>/pools) + poules des equipes favorites + poules des matchs suivis
    + poules de toutes les equipes des clubs suivis (22/11/2026, choix de Piet).
    On ne lit que des numeros de poules et des noms d'equipes ou de clubs ; rien n'est recopie dans le snapshot."""
    pools, fav_teams, fav_clubs = set(), set(), set()
    try:
        rooms = list((fb_get("hbrooms", "shallow=true") or {}).keys())
        for room in rooms:
            q = urllib.parse.quote(room, safe="")
            for p in (fb_get(f"hbrooms/{q}/pools") or []):
                pools.add(str(p))
            for teams in (fb_get(f"hbrooms/{q}/favs") or {}).values():
                fav_teams.update(teams or [])
            for clubs in (fb_get(f"hbrooms/{q}/favClubs") or {}).values():
                fav_clubs.update(c for c in (clubs or []) if isinstance(c, str) and c)
            for marks in (fb_get(f"hbrooms/{q}/watch") or {}).values():
                for w in (marks or {}).values():
                    if isinstance(w, dict) and w.get("p"):
                        pools.add(str(w["p"]))
                        WATCH_POOLS.add(str(w["p"]))
    except Exception as e:  # noqa
        print("Parties Firebase illisibles (pas grave, toutes les poules actives sont lues) :", e, file=sys.stderr)
    if (fav_teams or fav_clubs) and old:
        opools, oteams = [p[0] for p in old.get("pools", [])], old.get("teams", [])
        club_of = lambda t: t.rsplit(" ", 1)[0] if " " in t else t  # "Old Club U19B-1" -> "Old Club"
        for m in old.get("matches", []):
            a, b = oteams[m[3]], oteams[m[4]]
            if a in fav_teams or b in fav_teams or club_of(a) in fav_clubs or club_of(b) in fav_clubs:
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


def fresh_variants():
    """Variantes de dates qui changent tous les quarts d'heure : hockey.be n'a pas encore de copie en cache
    pour ces demandes, il repond donc avec des donnees fraiches (07/11 : copie de 15h gardee jusqu'au soir)."""
    k = int(time.time() // 900) % 60
    return [(SEASON_FROM, shift(SEASON_TO, 3 + k)), (shift(SEASON_FROM, 1), shift(SEASON_TO, 70 + k))]


def last_scored(results):
    out = ""
    for r in results:
        if re.search(r"\d+\s*-\s*\d+", strip(r[5] if len(r) > 5 else "")):
            out = max(out, iso(r[0]) or "")
    return out


def stale_results(program, results):
    """Vrai si un match du programme est termine depuis plus de 1h30 mais plus recent que le dernier score recu."""
    now = dt.datetime.now(BXL)
    last = last_scored(results)
    for r in program:
        d = iso(strip(r[0]).split(" | ")[0])
        t = (r[1] or "")[:5]
        if not d or not re.match(r"\d\d:\d\d$", t) or d <= last:
            continue
        try:
            start = dt.datetime.fromisoformat(f"{d}T{t}").replace(tzinfo=BXL)
        except ValueError:
            continue
        if start + dt.timedelta(minutes=90) < now and (now - start).days < 3:
            return True
    return False


FRESH = {"n": 0, "ok": 0}


# =====================================================================================================
# Nouveau site hockey.be (25/11/2026) : l'ancienne adresse wp-json/sportlink-api ne repond plus.
# Les matchs viennent maintenant de la page publique d'impression du programme :
#   https://www.hockey.be/fr/competition/download-program-data?programType=3015|3019&pool[]=<poule>&period=<debut> - <fin>
# (3015 = outdoor, 3019 = indoor ; periode en secondes Unix ; tableau HTML complet, sans pages).
# La liste des poules (memes numeros qu'avant) vient des cases a cocher des pages « Programme & resultats ».
# Les classements ne sont plus publies tout faits : le robot les calcule (3 / 1 / 0 point).
# =====================================================================================================
NEW_BASE = "https://www.hockey.be/fr"
PROGRAM_TYPE = {"out": 3015, "in": 3019}
POOL_TYPE = {}      # poule -> "out" / "in"
CLUB_LOGOS = {}     # club -> adresse du logo sur le nouveau site (ecrit dans logoUrls, lu par l'app)


def load_club_logos(old):
    """Logos connus : liste de base scripts/logos-clubs.json (108 clubs le 25/11/2026) + ceux du snapshot precedent.
    26/11/2026 : le mode --ehl-only reecrivait logoUrls sans les logos des clubs (plus aucun logo dans l'app)."""
    try:
        CLUB_LOGOS.update(json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logos-clubs.json"), encoding="utf-8")))
    except (OSError, ValueError) as e:
        print(f"Liste de base des logos illisible : {e}", file=sys.stderr)
    CLUB_LOGOS.update({k: v for k, v in ((old or {}).get("logoUrls") or {}).items() if v and "hockey.be/files/" in v})
MOIS = {"janv": 1, "jan": 1, "févr": 2, "fevr": 2, "fév": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6, "juil": 7,
        "août": 8, "aout": 8, "sept": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12}


def unix_day(d, end=False):
    t = dt.datetime.combine(dt.date.fromisoformat(d), dt.time(23, 59, 59) if end else dt.time(0, 0), tzinfo=BXL)
    return int(t.timestamp())


def fr_date(txt):
    """« 05 sept. 2026 » -> « 05/09/2026 » (format de l'ancien site, compris par iso())"""
    m = re.match(r"\s*(\d{1,2})\s+([^\s.]+)\.?\s+(\d{4})", txt or "")
    if not m:
        return None
    mo = MOIS.get(m.group(2).lower().rstrip("."))
    return f"{int(m.group(1)):02d}/{mo:02d}/{m.group(3)}" if mo else None


def cell_team(td):
    """Cellule equipe : nom complet (<span>), nom du club et adresse du logo."""
    name = re.search(r"<span[^>]*>(.*?)</span>", td, re.S)
    club = re.search(r'font-semibold[^"]*">(.*?)</div>', td, re.S)
    logo = re.search(r"(https://[^\s\"']+/_48xAUTO_[^\s\"']+?\.png)", td) or re.search(r"background-image:\s*url\('([^']+)'\)", td)
    n = strip(name.group(1)) if name else strip(td)
    if club and logo:
        CLUB_LOGOS[strip(club.group(1))] = html.unescape(logo.group(1))
    return n


import threading  # noqa: E402
HB_SLOT = threading.Semaphore(2)   # au plus 2 demandes a la fois au nouveau site (429 avec 6 le 25/11/2026)
HB_PAUSE = 0.7                     # pause apres chaque demande (secondes)


def parse_rows(page):
    """Tableau de download-program-data -> liste de (poule affichee, ligne programme, ligne resultat ou None, A, B).
    Lignes au format de l'ancien site : programme [date | lieu, heure, '', A, '', '', '', B],
    resultats [date, heure, '', A, '', score ou statut, '', B]."""
    if "<table" not in page:
        # periode sans aucun match : petite page « Geen resultaten gevonden voor de geselecteerde filters. » (440 octets)
        if len(page) < 3000 and "Hockey Belgium" in page and re.search(r"resultaten|r[ée]sultat|results", page, re.I):
            return []
        raise RuntimeError("reponse inattendue de hockey.be (pas de tableau)")
    out = []
    body = page.split("<tbody", 1)[-1]
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 6:
            continue
        d = fr_date(strip(tds[0]))
        if not d:
            continue
        hour = strip(tds[1])[:5]
        a, b = cell_team(tds[3]), cell_team(tds[5])
        res = strip(tds[4])
        venue = strip(re.sub(r"<br\s*/?>", ", ", tds[6])) if len(tds) > 6 else ""
        prog = [d + (f" | {venue}" if venue else ""), hour, "", a, "", "", "", b]
        result = [d, hour, "", a, "", res, "", b] if res and res not in ("-", "–") else None
        out.append((norm_level(strip(tds[2])), prog, result, a, b))
    return out


def norm_level(t):
    return re.sub(r"\s+", " ", html.unescape(t or "")).strip()


def program_page(pt, period, pid=None):
    q = {"programType": pt}
    if pid:
        q["pool[]"] = pid
    q["period"] = period
    with HB_SLOT:
        page = get(f"{NEW_BASE}/competition/download-program-data?{urllib.parse.urlencode(q)}", tries=4, timeout=120)
        time.sleep(HB_PAUSE)
    return page


def fetch_rows(pid, d_from, d_to):
    """Matchs d'une poule entre deux dates -> (programme, resultats) au format de l'ancien site."""
    pt = PROGRAM_TYPE[POOL_TYPE.get(pid, "out")]
    rows = parse_rows(program_page(pt, f"{unix_day(d_from)} - {unix_day(d_to, True)}", pid))
    # 25/11/2026 : une poule SANS match dans la periode demandee fait ignorer le filtre par hockey.be, qui renvoie
    # alors les matchs de TOUTES les poules (56 poules gonflees a ~1900 matchs au premier passage). Une vraie reponse
    # n'a qu'une seule « Poule » (colonne 3) : sinon, la poule n'a aucun match dans la periode.
    if len({r[0] for r in rows}) > 1:
        return [], []
    return [r[1] for r in rows], [r[2] for r in rows if r[2]]


# ---------- Lecture groupee (26/11/2026) ----------
# Lire 500 poules une par une prenait 36 min avec des refus 429. download-program-data SANS numero de poule renvoie
# tous les matchs d'une periode (week-end outdoor du 21-22/11 : 1088 matchs en 11 s ; indoor du 28-30/11 : 363 en 5 s).
# La ligne ne donne pas le numero de poule : on le retrouve par les equipes deja connues dans le snapshot
# et par le nom de la poule affiche (« Nat. 2 A »), compare a celui de la liste des poules (POOL_LEVEL).
POOL_LEVEL = {}                 # poule -> nom affiche dans le tableau (ex. « Nat. 2 A »)
BULK_BEFORE, BULK_AFTER, BULK_CHUNK = 3, 10, 4   # lecture complete : de J-3 a J+10, par tranches de 4 jours
ROTATE_MAX = 80                 # au plus 80 poules relues une par une par lecture complete (verification de la saison)
BULK = {"req": 0, "err": 0, "rows": 0, "ok": 0, "unknown": 0, "ambiguous": 0}


def team_index(old, only=None):
    """(A, B) -> poules ; equipe -> poules (poules hockey.be du snapshot precedent)"""
    pair, by_team = {}, {}
    if not old:
        return pair, by_team
    pools, teams = old.get("pools") or [], old.get("teams") or []
    for m in old.get("matches") or []:
        pid = pools[m[0]][0]
        if not pid.isdigit() or (only is not None and pid not in only):
            continue
        a, b = teams[m[3]], teams[m[4]]
        pair.setdefault((a, b), set()).add(pid)
        by_team.setdefault(a, set()).add(pid)
        by_team.setdefault(b, set()).add(pid)
    return pair, by_team


def pool_of(level, a, b, pair, by_team, surf):
    cand = pair.get((a, b)) or (by_team.get(a, set()) & by_team.get(b, set()))
    cand = {p for p in cand if POOL_TYPE.get(p, surf) == surf}
    if len(cand) > 1 or (cand and level):
        exact = {p for p in cand if POOL_LEVEL.get(p) == level}
        if exact:
            cand = exact
        else:
            cand = {p for p in cand if p not in POOL_LEVEL}
    return next(iter(cand)) if len(cand) == 1 else (None if not cand else "?")


def bulk_read(old, d_from, d_to, only=None, surfs=("out", "in")):
    """Tous les matchs entre deux dates, ranges par poule -> {poule: (programme, resultats)}"""
    pair, by_team = team_index(old, only)
    out = {}
    day, end = dt.date.fromisoformat(d_from), dt.date.fromisoformat(d_to)
    while day <= end:
        stop = min(end, day + dt.timedelta(days=BULK_CHUNK - 1))
        for surf in surfs:
            if too_late():
                return out
            BULK["req"] += 1
            try:
                rows = parse_rows(program_page(PROGRAM_TYPE[surf], f"{unix_day(day.isoformat())} - {unix_day(stop.isoformat(), True)}"))
            except Exception as e:  # noqa
                BULK["err"] += 1
                print(f"  lecture groupee {surf} {day:%d/%m}-{stop:%d/%m} impossible : {e}", file=sys.stderr)
                continue
            for level, prog, res, a, b in rows:
                BULK["rows"] += 1
                pid = pool_of(level, a, b, pair, by_team, surf)
                if pid is None:
                    BULK["unknown"] += 1
                    continue
                if pid == "?":
                    BULK["ambiguous"] += 1
                    continue
                BULK["ok"] += 1
                p, r = out.setdefault(pid, ([], []))
                p.append(prog)
                if res:
                    r.append(res)
        day = stop + dt.timedelta(days=1)
    return out


FULL_SEASON = set()  # poules a relire sur toute la saison a ce passage (logos manquants)


def plan_rotation(pools, old):
    """Poules a relire une par une en plus de la lecture groupee : jamais lues (ou en echec), parties et favoris
    pas encore relus aujourd'hui, puis les autres pas relues depuis 7 jours (les plus anciennes d'abord)."""
    global POOL_SEEN
    POOL_SEEN = dict((old or {}).get("poolSeen") or {})
    today = dt.datetime.now(BXL).date()
    prio = priority_pools(old)
    never, daily, weekly = [], [], []
    for p in pools:
        pid = p[0]
        if not pid.isdigit():
            continue
        seen = POOL_SEEN.get(pid)
        age = (today - dt.date.fromisoformat(seen)).days if seen else None
        if age is None:
            never.append(pid)
        elif pid in prio and age >= 1:
            daily.append(pid)
        elif age >= 7:
            weekly.append((seen, pid))
    weekly = [pid for _, pid in sorted(weekly)]
    # 26/11/2026 : club sans logo (ex. Koksijde, aucun match lu depuis le nouveau site) -> une de ses poules relue
    # (saison complete), au plus 15 par passage. Seulement les poules des listes outdoor/indoor (pas Trimmers).
    logos = []
    if old:
        opools, oteams = old.get("pools") or [], old.get("teams") or []
        done = set(never + daily)
        for m in old.get("matches") or []:
            pid = opools[m[0]][0]
            if pid not in POOL_LEVEL or pid in done or len(logos) >= 15:
                continue
            for t in (oteams[m[3]], oteams[m[4]]):
                c = re.sub(r" \S+-\d+$", "", t)
                if c not in CLUB_LOGOS and pid not in done:
                    CLUB_LOGOS.setdefault(c, None)  # une seule poule par club
                    logos.append(pid)
                    FULL_SEASON.add(pid)
                    done.add(pid)
        for c in [c for c, v in CLUB_LOGOS.items() if v is None]:
            del CLUB_LOGOS[c]
    weekly = [p for p in weekly if p not in logos]
    room = max(0, ROTATE_MAX - len(never) - len(daily) - len(logos))
    return never + daily + logos + weekly[:room], {"jamais lues": len(never), "parties/favoris du jour": len(daily), "logos manquants": len(logos),
                                           "verification hebdo": min(room, len(weekly)), "hebdo reportees": max(0, len(weekly) - room)}


def pool_list(previous):
    """Poules des pages « Programme & resultats » outdoor et indoor (cases pool[]) ; noms gardes du snapshot
    precedent quand la poule est connue (l'app s'appuie sur l'ancien format « ... Outdoor - Nat. 2 - A »)."""
    prev = {p[0]: p[1] for p in (previous or [])}
    period = f"{unix_day(SEASON_FROM)} - {unix_day(SEASON_TO, True)}"
    pools = []
    for surf, slug in (("out", "program-outdoor"), ("in", "program-indoor")):
        try:
            page = get(f"{NEW_BASE}/competition-overview/{slug}/?" + urllib.parse.urlencode({"lang": "hbFr", "period": period}))
        except Exception as e:  # noqa
            print(f"Liste des poules {slug} indisponible :", e, file=sys.stderr)
            continue
        labels = {i: strip(t) for i, t in re.findall(r'<label[^>]*for="([^"]+)"[^>]*>(.*?)</label>', page, re.S)}
        for inp in re.findall(r"<input[^>]*name=\"pool\[\]\"[^>]*>", page):
            v = re.search(r'value="(\d+)"', inp)
            i = re.search(r'id="([^"]+)"', inp)
            if not v:
                continue
            pid = v.group(1)
            POOL_TYPE[pid] = surf
            lab0 = labels.get(i.group(1) if i else "", "")
            if " – " in lab0:
                POOL_LEVEL[pid] = norm_level(lab0.split(" – ", 1)[1])
            if pid in prev:
                name = prev[pid]
            else:
                lab = labels.get(i.group(1) if i else "", "") or pid
                comp, _, lvl = lab.partition(" – ")
                lvl = re.sub(r" ([A-Z]\d?)$", r" - \1", lvl.strip())
                name = f"{comp.strip()} {'Indoor' if surf == 'in' else 'Outdoor'} - {lvl}" if lvl else lab
            if pid not in {p[0] for p in pools}:
                pools.append([pid, name])
    if len(pools) > 50:
        # une poule connue mais absente des listes (coupe terminee...) reste dans le snapshot avec ses matchs
        ids = {p[0] for p in pools}
        for pid, name in prev.items():
            if pid not in ids and pid.isdigit():
                POOL_TYPE.setdefault(pid, "in" if re.search(r"\bIndoor\b", name) else "out")
                pools.append([pid, name])
        return pools
    if previous:
        print("Liste des poules du nouveau site indisponible : on reutilise celle du snapshot precedent", file=sys.stderr)
        for p in previous:
            POOL_TYPE.setdefault(p[0], "in" if re.search(r"\bIndoor\b", p[1]) else "out")
        return previous
    raise SystemExit("Impossible d'obtenir la liste des poules")


BONUS_24_11 = '{"12089": {"Leopold U19B-1": 1, "Uccle Sport U19B-1": 2, "Dragons U19B-1": 3, "Gantoise U19B-1": 2, "Oree U19B-1": 4, "Racing U19B-1": 2, "Louvain-La-Neuve U19B-1": 1}, "12092": {"Antwerp U19G-1": 1, "Leuven U19G-1": 1, "Waterloo Ducks U19G-1": 1, "Wellington U19G-1": 3, "Gantoise U19G-1": 2, "Uccle Sport U19G-1": 3, "Oree U19G-1": 1, "Dragons U19G-1": 1}, "12097": {"Gantoise U16B-1": 1, "Waterloo Ducks U16B-1": 2, "Uccle Sport U16B-1": 2, "Oree U16B-1": 1, "Pingouin U16B-1": 1, "Indiana U16B-1": 2}, "12100": {"Braxgata U16G-1": 1, "Leuven U16G-1": 1, "Victory U16G-1": 1, "White Star U16G-1": 4, "Rasante U16G-1": 1, "Dragons U16G-1": 1, "Leopold U16G-1": 1}}'
BONUS = {}  # poule -> {equipe: points de bonus} (shoot-outs des championnats nationaux jeunes, voir init_bonus)


def init_bonus(old):
    """U19/U16 Nat. 1 : un nul donne un shoot-out et 1 point de plus au vainqueur. Le nouveau site n'affiche que
    le score (ex. 1-1), pas le shoot-out. On fige donc une fois les points de bonus du dernier classement officiel
    (24/11/2026) ; les shoot-outs joues apres manquent (ecart d'au plus 1 point par nul)."""
    if old and old.get("standingBonus"):
        BONUS.update(old["standingBonus"])
        return
    # 26/11/2026 : le mode --ehl-only effacait standingBonus ; valeurs figees le 24/11 (classements officiels)
    BONUS.update(json.loads(BONUS_24_11))


def team_cat(t):
    """« Old Club U19B-1 » -> « U19B » ; « Ixelles D-1 » -> « D »"""
    m = re.match(r".* (\S+?)-\d+$", t or "")
    return m.group(1) if m else (t or "").rsplit(" ", 1)[-1]


REPAIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repair-2026-11-25.json")


def repair_bloated(old):
    """Poule « gonflee » (matchs de toutes les categories, voir fetch_rows) : ses matchs sont retires du snapshot
    precedent et remplaces par ceux de scripts/repair-2026-11-25.json (copie propre du 24/11) quand il existe.
    Une vraie poule n'a qu'une ou deux categories d'equipes ; les poules gonflees en avaient des dizaines."""
    pools, teams = old.get("pools") or [], old.get("teams") or []
    cats = {}
    for m in old.get("matches") or []:
        pid = pools[m[0]][0]
        if pid.isdigit():
            cats.setdefault(pid, set()).update((team_cat(teams[m[3]]), team_cat(teams[m[4]])))
    bad = {pid for pid, c in cats.items() if len(c) > 4}
    if not bad:
        return
    keep = [m for m in old["matches"] if pools[m[0]][0] not in bad]
    n_rep = 0
    try:
        rp = json.load(open(REPAIR, encoding="utf-8"))
        pidx = {p[0]: i for i, p in enumerate(pools)}
        tidx = {t: i for i, t in enumerate(teams)}
        for pid, a, b, *rest in rp.get("matches") or []:
            if pid in bad and pid in pidx:
                for x in (a, b):
                    if x not in tidx:
                        tidx[x] = len(teams)
                        teams.append(x)
                keep.append([pidx[pid], rest[0], rest[1], tidx[a], tidx[b]] + rest[2:])
                n_rep += 1
    except (OSError, ValueError) as e:
        print(f"Pas de fichier de reparation ({e})", file=sys.stderr)
    removed = len(old["matches"]) - (len(keep) - n_rep)
    old["matches"] = keep
    for pid in bad:
        (old.get("standings") or {}).pop(pid, None)
    print(f"Reparation : {len(bad)} poule(s) gonflee(s), {removed} match(s) retire(s), {n_rep} repris de la copie du 24/11", file=sys.stderr)


def compute_standings(rows, bonus=None):
    """rows = [(A, B, butsA, butsB)] -> classement [rang, equipe, J, G, P, N, BP, BC, Pts] (3 / 1 / 0 point).
    Attention : l'ordre des colonnes de hockey.be est J, G, P (perdus), N (nuls)."""
    t = {}
    for a, b, sa, sb in rows:
        for x in (a, b):
            t.setdefault(x, [0, 0, 0, 0, 0, 0, 0])  # J G P N BP BC Pts
        if sa is None or sb is None:
            continue
        for x, gf, ga in ((a, sa, sb), (b, sb, sa)):
            r = t[x]
            r[0] += 1; r[4] += gf; r[5] += ga
            if gf > ga:
                r[1] += 1; r[6] += 3
            elif gf < ga:
                r[2] += 1
            else:
                r[3] += 1; r[6] += 1
    # departage verifie sur les classements officiels du 24/11/2026 : points, victoires, difference de buts, buts marques
    for x, pts in (bonus or {}).items():
        if x in t:
            t[x][6] += pts
    order = sorted(t.items(), key=lambda kv: (-kv[1][6], -kv[1][1], -(kv[1][4] - kv[1][5]), -kv[1][4], kv[0]))
    return [[i + 1, name] + v for i, (name, v) in enumerate(order)]


NO_TABLE = re.compile(r"coupe|cup|beker|trophy|troph[ée]e|final", re.I)


def standings_from(matches, pools, teams, only=None, bonus=True):
    """Classements des poules hockey.be (numeros) calcules a partir des matchs [pi, date, heure, A, B, butsA, butsB, ...].
    Pas de classement pour les coupes. only = ensemble de poules a recalculer (toutes sinon)."""
    rows = {}
    for m in matches:
        pid = pools[m[0]][0]
        if not pid.isdigit() or (only is not None and pid not in only) or NO_TABLE.search(pools[m[0]][1]):
            continue
        if m[1] and m[1] < SEASON_FROM:
            continue
        rows.setdefault(pid, []).append((teams[m[3]], teams[m[4]], m[5], m[6]))
    return {pid: compute_standings(r, BONUS.get(pid) if bonus else None) for pid, r in rows.items()}


def fetch_pool(pid):
    """Nouveau site (25/11/2026) : fenetre courte (10 jours avant, 60 jours apres) pour une poule deja lue
    cette semaine, toute la saison sinon. Les matchs hors fenetre restent ceux du snapshot precedent."""
    if too_late():
        return pid, [], [], [], LATE
    try:
        today = dt.datetime.now(BXL).date()
        seen = POOL_SEEN.get(pid)
        short = seen and (today - dt.date.fromisoformat(seen)).days < 7 and pid not in FULL_SEASON
        d_from = max(SEASON_FROM, (today - dt.timedelta(days=10)).isoformat()) if short else SEASON_FROM
        d_to = min(SEASON_TO, (today + dt.timedelta(days=60)).isoformat()) if short else SEASON_TO
        program, results = fetch_rows(pid, d_from, d_to)
        return pid, program, results, [], None
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


FIHPL_STATUS = {"ok": None, "info": ""}
FLAGS = {}  # equipe Pro League -> url du drapeau (fih.hockey)
QUICK = ("ehl-", "fihpl-", "nlhk-")  # poules relues en mode rapide (--ehl-only), hors hockey.be


NLHK_STATUS = {"ok": None, "info": ""}


def add_league(prefix, collect, status, label, pools, matches, standings, t_id, old):
    """Ajoute les poules d'une ligue lue sur son site (Pro League, Hoofdklasse) ;
    en cas d'echec, reprend celles du snapshot precedent."""
    try:
        res, flags = collect(get, SEASON_FROM, SEASON_TO, log=lambda *a: print(*a, file=sys.stderr))
        status.update(ok=True, info=f"{sum(len(r[2]) for r in res)} match(s)")
        FLAGS.update(flags)
    except Exception as e:  # noqa
        print(f"{label} indisponible :", e, file=sys.stderr)
        status.update(ok=False, info=f"{label} indisponible : {e}"[:200])
        if old:
            FLAGS.update(old.get("logoUrls") or {})
            opools, oteams = old.get("pools", []), old.get("teams", [])
            for p in opools:
                if p[0].startswith(prefix):
                    pools.append(p)
            for om in old.get("matches", []):
                p = opools[om[0]] if om[0] < len(opools) else None
                if p and p[0].startswith(prefix):
                    pi = next(i for i, x in enumerate(pools) if x[0] == p[0])
                    a, b = oteams[om[3]], oteams[om[4]]
                    leg = om[9] if len(om) > 9 else 1
                    matches[(p[0], a, b, leg)] = [pi, om[1], om[2], t_id(a), t_id(b)] + om[5:]
            for pid, st in old.get("standings", {}).items():
                if pid.startswith(prefix):
                    standings[pid] = st
        return False
    for pid, name, ms, st in res:
        pools.append([pid, name])
        pi = len(pools) - 1
        for m in ms:
            stt = m.get("status")
            if m.get("so"):
                stt = f"Shoot-out {m['soa']}-{m['sob']}"
            matches[(pid, m["a"], m["b"], m["leg"])] = [pi, m["date"], m["time"], t_id(m["a"]), t_id(m["b"]),
                                                        m["sa"], m["sb"], stt, m.get("venue"), m["leg"]]
        standings[pid] = st
    return True


def add_fihpl(pools, matches, standings, t_id, old):
    return add_league("fihpl-", fihpl.collect, FIHPL_STATUS, "Pro League", pools, matches, standings, t_id, old)


UA_BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"


def get_browser(url, tries=2, timeout=45):
    """26/11/2026 : le site de la Hoofdklasse repond 404 au robot alors que la page s'ouvre dans un navigateur :
    on se presente comme un navigateur ordinaire (sans la mention PronosHockeyFamille)."""
    last = None
    for i in range(tries):
        REQ["n"] += 1
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA_BROWSER, "Accept-Language": "nl,en;q=0.8",
                                                       "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            time.sleep(3 * (i + 1))
    raise last


def add_nlhk(pools, matches, standings, t_id, old):
    def collect(_get, *a, **k):
        try:
            return nlhk.collect(get, *a, **k)
        except Exception as e:  # noqa
            print(f"Hoofdklasse : {e} ; nouvel essai comme un navigateur", file=sys.stderr)
            return nlhk.collect(get_browser, *a, **k)
    return add_league("nlhk-", collect, NLHK_STATUS, "Hoofdklasse", pools, matches, standings, t_id, old)


def write(pools, teams, matches, standings, failed, generated=None, ehl_generated=None):
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
        "ehlGenerated": ehl_generated or now,
        "ehlStatus": {"ok": EHL_STATUS["ok"], "info": EHL_STATUS["info"], "at": now},
        "season": [SEASON_FROM, SEASON_TO],
        "format": "matches = [poule, date, heure, equipeA, equipeB, butsA, butsB, statut, terrain ou adresse] ; standings = [rang, equipe, J, G, P, N, BP, BC, Pts] (calcules par le robot depuis le 25/11/2026) ; logoUrls = {club ou equipe: adresse de l'image} ; logos = anciens codes (images supprimees par hockey.be)",
        "pools": out_pools,
        "teams": [teams[i] for i in used_t],
        "matches": out_matches,
        "standings": {p[0]: standings[p[0]] for p in out_pools if p[0] in standings},
        "failed": failed,
        "logos": dict(sorted(LOGOS.items())),
        "logosMissing": logos_missing(),
        "logoCheck": LOGO_CHECK,
        "poolSeen": dict(sorted(POOL_SEEN.items())),
        "logoUrls": dict(sorted({**CLUB_LOGOS, **FLAGS}.items())),
        "standingBonus": BONUS,
        "poolLevels": dict(sorted(POOL_LEVEL.items())),
        "fihplStatus": {"ok": FIHPL_STATUS["ok"], "info": FIHPL_STATUS["info"], "at": now},
        "nlhkStatus": {"ok": NLHK_STATUS["ok"], "info": NLHK_STATUS["info"], "at": now},
    }
    if len(out_matches) < 100:
        raise SystemExit(f"Seulement {len(out_matches)} matchs : hockey.be semble indisponible, snapshot non ecrit")
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    n_ehl = sum(1 for p in out_pools if p[0].startswith("ehl-"))
    n_pl = sum(1 for p in out_pools if p[0].startswith("fihpl-"))
    n_nl = sum(1 for p in out_pools if p[0].startswith("nlhk-"))
    print(f"OK : {len(out_pools)} poules (dont {n_ehl} EHL, {n_pl} Pro League, {n_nl} Hoofdklasse), {len(out_matches)} matchs, {len(snap['teams'])} equipes, "
          f"{len(failed)} echec(s), {os.path.getsize(OUT) // 1024} Ko", file=sys.stderr)


def main_ehl_only():
    """Remplace seulement la partie EHL du snapshot existant (quelques secondes)."""
    old = json.load(open(OUT, encoding="utf-8"))
    LOGOS.update(old.get("logos") or {})
    load_club_logos(old)
    init_bonus(old)
    POOL_LEVEL.update(old.get("poolLevels") or {})
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
        if not pid.startswith(QUICK):
            matches[(pid, teams[om[3]], teams[om[4]])] = list(om)
    standings = {k: v for k, v in old.get("standings", {}).items() if not k.startswith(QUICK)}
    keep = [i for i, p in enumerate(pools) if not p[0].startswith(QUICK)]
    remap = {o: n for n, o in enumerate(keep)}
    pools = [pools[i] for i in keep]
    for k, m in matches.items():
        m[0] = remap[m[0]]
    ehl_ok = add_ehl(pools, matches, standings, t_id, old)
    pl_ok = add_fihpl(pools, matches, standings, t_id, old)
    if not add_nlhk(pools, matches, standings, t_id, old):
        print("Hoofdklasse indisponible : anciennes donnees gardees, nouvel essai au prochain reveil", file=sys.stderr)
    if not ehl_ok:
        # ehlGenerated ne bouge pas -> l'app sait que les scores EHL ne sont pas frais ; nouvel essai au prochain reveil
        print("EHL indisponible : anciennes donnees EHL gardees, nouvel essai au prochain reveil", file=sys.stderr)
    if not pl_ok:
        print("Pro League indisponible : anciennes donnees gardees, nouvel essai au prochain reveil", file=sys.stderr)
    write(pools, teams, matches, standings, old.get("failed", []), generated=old.get("generated"),
          ehl_generated=None if ehl_ok else (old.get("ehlGenerated") or old.get("generated")))


def main_scores():
    """Chasseur de scores (09/11/2026, choix de Piet) : relit seulement les poules des parties et des equipes
    favorites ou un match est fini depuis plus de 1h30 (et moins de 12 h) sans score. Quelques secondes.
    Ne touche pas a "generated" : les lectures completes gardent leurs horaires."""
    old = json.load(open(OUT, encoding="utf-8"))
    prio = priority_pools(old)
    now = dt.datetime.now(BXL)
    pools, teams = old["pools"], old["teams"]
    pending = {}
    for m in old["matches"]:
        pid = pools[m[0]][0]
        if pid not in prio or not pid.isdigit() or m[5] is not None or not m[1] or not re.match(r"\d\d:\d\d$", m[2] or ""):
            continue
        try:
            start = dt.datetime.fromisoformat(f"{m[1]}T{m[2]}").replace(tzinfo=BXL)
        except ValueError:
            continue
        if dt.timedelta(minutes=75 if pid in WATCH_POOLS else 90) < now - start < dt.timedelta(hours=12):
            pending[pid] = pending.get(pid, 0) + 1
    print(f"Chasseur de scores : {len(prio)} poule(s) de parties/favoris/matchs suivis, {len(pending)} avec un match fini sans score", file=sys.stderr)
    if not pending:
        return
    tidx = {t: i for i, t in enumerate(teams)}

    def t_id(name):
        if name not in tidx:
            tidx[name] = len(teams)
            teams.append(name)
        return tidx[name]

    def read(pid):
        try:
            today = now.date()
            _, res = fetch_rows(pid, (today - dt.timedelta(days=1)).isoformat(), today.isoformat())
            return pid, res, [], None
        except Exception as e:
            return pid, [], [], str(e)

    for p in pools:
        if p[0].isdigit():
            POOL_TYPE[p[0]] = "in" if re.search(r"\bIndoor\b", p[1]) else "out"
    POOL_LEVEL.update(old.get("poolLevels") or {})

    # 26/11/2026 : plus de 3 poules a chasser (samedi) -> une lecture groupee d'hier et d'aujourd'hui (1 ou 2 demandes)
    # au lieu d'une demande par poule ; poule par poule si la lecture groupee echoue
    todo = list(pending)
    found = []
    if len(todo) > 3:
        d0 = (now.date() - dt.timedelta(days=1)).isoformat()
        surfs = tuple(sorted({POOL_TYPE.get(p, "out") for p in todo}, reverse=True))
        bulk = bulk_read(old, d0, now.date().isoformat(), only=set(todo), surfs=surfs)
        print(f"Chasseur de scores, lecture groupee : {BULK['req']} demande(s) ({BULK['err']} en echec), "
              f"{BULK['ok']} matchs ranges, {BULK['unknown']} hors poules chassees, {BULK['ambiguous']} ambigus", file=sys.stderr)
        if BULK["err"] == 0:
            found = [(pid, bulk.get(pid, ([], []))[1], [], None) for pid in todo]
            todo = []

    rows = {}
    for i, m in enumerate(old["matches"]):
        rows[(pools[m[0]][0], teams[m[3]], teams[m[4]])] = i
    pidx = {p[0]: i for i, p in enumerate(pools)}
    got = 0
    with ThreadPoolExecutor(min(WORKERS, 6)) as ex:
        for pid, results, standing, err in found + list(ex.map(read, todo)):
            if err:
                print(f"  poule en echec : {err}", file=sys.stderr)
                continue
            for r in results:
                a, b = team(r[3]), team(r[-1])
                sc = strip(r[5] if len(r) > 5 else "")
                mm = re.search(r"(\d+)\s*-\s*(\d+)", sc)
                if not mm:
                    continue
                key = (pid, a, b)
                status = re.sub(r"(\d+)\s*-\s*(\d+)", "", sc).strip() or None
                if key in rows:
                    m = old["matches"][rows[key]]
                    if m[5] is None:
                        got += 1
                    m[1], m[2] = iso(r[0]) or m[1], (r[1] or "")[:5] or m[2]
                    m[5], m[6], m[7] = int(mm.group(1)), int(mm.group(2)), status
                else:
                    old["matches"].append([pidx[pid], iso(r[0]), (r[1] or "")[:5], t_id(a), t_id(b), int(mm.group(1)), int(mm.group(2)), status, None])
                    rows[key] = len(old["matches"]) - 1
    print(f"Chasseur de scores : {got} nouveau(x) score(s), demandes a hockey.be : {REQ['n']}", file=sys.stderr)
    if not got:
        return
    old["teams"] = teams
    init_bonus(old)
    old["standingBonus"] = BONUS
    old["standings"].update(standings_from(old["matches"], pools, teams, set(pending)))
    old["matches"].sort(key=lambda m: (m[1] or "9999", m[2] or "", m[0]))
    old["scoresGenerated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False, separators=(",", ":"))


def main_archive():
    """Archive de la saison (10/11/2026, demande de Piet) : data/archives/saison-AAAA-AA.json garde tous les
    matchs joues (score ou statut : forfait, remis...) et les classements de toutes les poules de la saison,
    y compris EHL, Pro League et Hoofdklasse. Donnees publiques de hockey.be ; rien sur les joueurs de l'app.
    Fusion avec l'archive precedente : un match deja archive n'est jamais perdu, meme si une poule disparait."""
    snap = json.load(open(OUT, encoding="utf-8"))
    y = int(SEASON_FROM[:4])
    path = os.path.join(os.path.dirname(OUT) or ".", "archives", f"saison-{y}-{str(y + 1)[2:]}.json")
    old = {}
    if os.path.exists(path):
        try:
            old = json.load(open(path, encoding="utf-8"))
        except Exception:
            old = {}
    pools = {p[0]: p[1] for p in (old.get("poules") or [])}
    matches, standings = {}, dict(old.get("classements") or {})
    ot, op = old.get("equipes") or [], [p[0] for p in (old.get("poules") or [])]
    for m in old.get("matchs") or []:
        pid, a, b = op[m[0]], ot[m[3]], ot[m[4]]
        matches[(pid, a, b, m[1])] = [pid, m[1], m[2], a, b] + m[5:]
    st, sp = snap["teams"], snap["pools"]
    n_new = 0
    for m in snap["matches"]:
        if m[5] is None and (not m[7] or re.match(r"planifi", m[7], re.I)):
            continue  # pas encore joue
        if m[1] and m[1] < SEASON_FROM:
            continue
        pid = sp[m[0]][0]
        pools[pid] = sp[m[0]][1]
        key = (pid, st[m[3]], st[m[4]], m[1])
        if key not in matches:
            n_new += 1
        matches[key] = [pid, m[1], m[2], st[m[3]], st[m[4]], m[5], m[6], m[7]]
    for pid, rows in (snap.get("standings") or {}).items():
        if rows:
            standings[pid] = rows
            if pid not in pools:
                pools[pid] = next((p[1] for p in sp if p[0] == pid), pid)
    pids = sorted(pools)
    pidx = {p: i for i, p in enumerate(pids)}
    teams = sorted({k[1] for k in matches} | {k[2] for k in matches})
    tidx = {t: i for i, t in enumerate(teams)}
    rows = sorted(([pidx[v[0]], v[1], v[2], tidx[v[3]], tidx[v[4]]] + v[5:] for v in matches.values()),
                  key=lambda r: (r[1] or "", r[2] or "", r[0]))
    out = {
        "saison": f"{y}-{y + 1}",
        "genere": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "hockey.be (Hockey Belgium), EHL, FIH Pro League, Hoofdklasse ; donnees publiques",
        "format": "poules = [id, nom] ; matchs = [poule, date, heure, equipeA (domicile), equipeB, butsA, butsB, statut] ; "
                  "classements = {id poule: [rang, equipe, J, G, P, N, BP, BC, Pts]}",
        "poules": [[p, pools[p]] for p in pids],
        "equipes": teams,
        "matchs": rows,
        "classements": {p: standings[p] for p in pids if p in standings},
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Archive {out['saison']} : {len(rows)} matchs joues ({n_new} nouveaux), {len(pids)} poules, "
          f"{len(out['classements'])} classements, {os.path.getsize(path) // 1024} Ko", file=sys.stderr)


def main():
    if "--archive" in sys.argv:
        return main_archive()
    if "--ehl-only" in sys.argv:
        return main_ehl_only()
    if "--scores" in sys.argv:
        return main_scores()
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
        repair_bloated(old)
        init_bonus(old)
    POOL_LEVEL.update((old or {}).get("poolLevels") or {})
    load_club_logos(old)
    pools = [p for p in pool_list(previous) if not p[0].startswith(QUICK)]
    today = dt.datetime.now(BXL).date()
    today_s = today.isoformat()
    # 26/11/2026 : d'abord la lecture groupee (quelques demandes pour tous les matchs de J-3 a J+10)...
    bulk = bulk_read(old, (today - dt.timedelta(days=BULK_BEFORE)).isoformat(), (today + dt.timedelta(days=BULK_AFTER)).isoformat())
    print(f"Lecture groupee : {BULK['req']} demande(s) ({BULK['err']} en echec), {BULK['rows']} matchs, {BULK['ok']} ranges dans "
          f"{len(bulk)} poules, {BULK['unknown']} sans poule connue, {BULK['ambiguous']} ambigus", file=sys.stderr)
    if BULK["req"] and BULK["err"] * 2 <= BULK["req"]:
        # ... puis quelques poules une par une (nouvelles, parties du jour, verification de la saison)
        one, cnt = plan_rotation(pools, old)
    else:
        print("Lecture groupee en echec : retour a la lecture poule par poule", file=sys.stderr)
        first, kept, cnt = plan_pools(pools, old)
        one = [p[0] for p in first]
    note_had(old)
    print(f"{len(pools)} poules connues, {len(one)} a lire une par une : " +
          ", ".join(f"{k} {v}" for k, v in cnt.items() if v), file=sys.stderr)
    to_read = list(dict.fromkeys(list(bulk) + one))
    kept_ids = {p[0] for p in pools if p[0] not in set(to_read)}

    def reads():
        for pid, (program, results) in bulk.items():
            yield pid, program, results, [], None, False
        with ThreadPoolExecutor(WORKERS) as ex:
            for pid, program, results, standing, err in ex.map(fetch_pool, one):
                yield pid, program, results, standing, err, True

    teams, tidx = [], {}

    def t_id(name):
        if name not in tidx:
            tidx[name] = len(teams)
            teams.append(name)
        return tidx[name]

    matches, standings, failed = {}, {}, []
    if True:
        for n, (pid, program, results, standing, err, single) in enumerate(reads(), 1):
            if err:
                if pid not in bulk:
                    failed.append(pid)
                if err == LATE:
                    LATE_POOLS.add(pid)
                continue
            elif single:
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
            if single and n % 50 == 0:
                print(f"  {n}/{len(to_read)}", file=sys.stderr)

    # Une poule en echec, ou pas lue a ce passage, garde ses donnees du snapshot precedent
    reuse = set(failed) | kept_ids
    if failed:
        print(f"{len(failed)} poule(s) en echec, reprise des anciennes donnees", file=sys.stderr)
    if FRESH["n"]:
        print(f"Resultats en retard chez hockey.be : {FRESH['n']} poule(s) redemandee(s), {FRESH['ok']} avec des scores plus recents", file=sys.stderr)
    print(f"Demandes a hockey.be : {REQ['n']}, essais rates puis refaits : {REQ['fail'] - REQ['lost']}, "
          f"abandonnees : {REQ['lost']}, refus 429 (trop de demandes) : {REQ.get('429', 0)}, duree {int(time.time() - START)} s", file=sys.stderr)
    for e in REQ["examples"]:
        print(f"  exemple de demande abandonnee : {e}", file=sys.stderr)
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

    # 22/11/2026 : hockey.be a renvoye pour la poule de coupe 12841 une copie qui ne contenait plus 3 des 4 matchs
    # (dont 2 avec score) : une lecture ne doit jamais faire disparaitre un match deja connu ni effacer un score.
    if old:
        oteams = old.get("teams", [])
        opools = [p[0] for p in old.get("pools", [])]
        read_ok = set(to_read) - set(failed)
        n_back = n_sc = 0
        for om in old.get("matches", []):
            opid = opools[om[0]] if om[0] < len(opools) else None
            if opid not in read_ok or (om[1] and om[1] < SEASON_FROM):
                continue
            a, b = oteams[om[3]], oteams[om[4]]
            key = (opid, a, b)
            cur = matches.get(key)
            if cur is None:
                pi = next((i for i, p in enumerate(pools) if p[0] == opid), None)
                if pi is not None:
                    matches[key] = [pi, om[1], om[2], t_id(a), t_id(b)] + om[5:]
                    n_back += 1
            elif om[5] is not None and cur[5] is None and (cur[1] or "") == (om[1] or ""):
                cur[5], cur[6], cur[7] = om[5], om[6], om[7] if len(om) > 7 else cur[7]
                n_sc += 1
        if n_back or n_sc:
            print(f"Copie incomplete chez hockey.be : {n_back} match(s) repris et {n_sc} score(s) gardes du snapshot precedent", file=sys.stderr)

    # 25/11/2026 : le nouveau site ne publie plus les classements, on les calcule (3 / 1 / 0 point)
    calc = standings_from(list(matches.values()), pools, teams)
    for pid in [p for p in standings if p.isdigit()]:
        if pid not in calc:
            del standings[pid]
    standings.update(calc)
    print(f"Classements calcules : {len(calc)} poules, logos de clubs : {len(CLUB_LOGOS)}", file=sys.stderr)
    add_ehl(pools, matches, standings, t_id, old)
    add_fihpl(pools, matches, standings, t_id, old)
    add_nlhk(pools, matches, standings, t_id, old)
    n_late = sum(1 for p in failed if p in LATE_POOLS)
    if n_late:
        print(f"Temps depasse : {n_late} poule(s) non lues, reprises du snapshot precedent", file=sys.stderr)
    # 25/11/2026 : les anciennes images (wp-content/uploads/logos) n'existent plus ; l'app prend logoUrls
    write(pools, teams, matches, standings, failed)


if __name__ == "__main__":
    main()

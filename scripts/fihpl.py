"""
Lecture des matchs de la FIH Hockey Pro League sur fih.hockey.

La page du calendrier contient toutes les rencontres de la saison dans une variable JavaScript :
  window.fixtureWidgetData = '{"matches":[...]}'   (texte JSON avec des \\ devant { [ - + ( ) . / ? ')
Chaque match : series_name "... (M)" ou "(W)", gender M/F, start_date (avec fuseau), participants [A, B]
avec "value" = buts, ou "2(4)" = 2 buts et 4 tirs au but reussis (shoot-out), event_state R = termine.

Deux poules : fihpl-m (hommes) et fihpl-w (femmes), classement calcule ici
(victoire 3 pts ; nul puis shoot-out gagne 2 pts, perdu 1 pt).
En Pro League, deux equipes se rencontrent deux fois de suite au meme endroit : le 2e match a "leg" = 2,
pour que l'app lui donne un identifiant different (pronos separes).
"""
import datetime as dt
import json
import re
from zoneinfo import ZoneInfo

URL = "https://www.fih.hockey/events/fih-pro-league/schedule-fixtures-results"
BXL = ZoneInfo("Europe/Brussels")
FLAG = "https://www.fih.hockey/static-assets/images/teams/{}.png"

# noms en francais ; (H) / (D) pour separer hommes et dames dans l'app
FR = {
    "Argentina": "Argentine", "Australia": "Australie", "Belgium": "Belgique", "China": "Chine",
    "England": "Angleterre", "France": "France", "Germany": "Allemagne", "Great Britain": "Grande-Bretagne",
    "India": "Inde", "Ireland": "Irlande", "Japan": "Japon", "Netherlands": "Pays-Bas", "New Zealand": "Nouvelle-Zélande",
    "Pakistan": "Pakistan", "South Africa": "Afrique du Sud", "Spain": "Espagne", "USA": "États-Unis",
}
POOL_NAME = {"M": "Pro League Men - {season}", "W": "Pro League Women - {season}"}


def extract(page):
    """Texte HTML de la page -> liste des matchs (dicts bruts de fih.hockey)"""
    i = page.find("fixtureWidgetData")
    if i < 0:
        raise ValueError("fixtureWidgetData absent de la page fih.hockey")
    s = page.find("'", i) + 1
    e = page.find("';", s)
    raw = re.sub(r"\\(.)", r"\1", page[s:e])
    return json.loads(raw).get("matches") or []


def team_name(p, g):
    n = (p.get("name") or "").strip()
    return f"{FR.get(n, n)} ({'D' if g == 'W' else 'H'})"


def score(v):
    m = re.match(r"\s*(\d+)\s*(?:\((\d+)\))?", v or "")
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else None) if m else (None, None)


def bxl(start):
    """'2026-12-09T03:30+05:30' -> ('2026-12-08', '23:00') heure belge"""
    try:
        d = dt.datetime.fromisoformat(start).astimezone(BXL)
        return d.date().isoformat(), d.strftime("%H:%M")
    except (TypeError, ValueError):
        return None, None


def parse(raw_matches, season_from, season_to):
    """-> {g: [match dicts]} avec g = 'M' ou 'W'"""
    out = {}
    for m in raw_matches:
        if "Pro League" not in (m.get("series_name") or "") + (m.get("parent_series_name") or ""):
            continue
        sd = m.get("series_start_date") or m.get("match_date") or ""
        if not (season_from <= sd <= season_to):
            continue
        ps = m.get("participants") or []
        if len(ps) != 2:
            continue
        g = "W" if (m.get("gender") or "").upper() in ("F", "W") or "(W)" in (m.get("series_name") or "") else "M"
        date, time = bxl(m.get("start_date"))
        if not date:
            date, time = m.get("date") or m.get("match_date"), (m.get("time") or "")[:5] or None
        done = (m.get("event_state") or "").upper() == "R"
        (sa, soa), (sb, sob) = score(ps[0].get("value")), score(ps[1].get("value"))
        if not done or sa is None or sb is None:
            sa = sb = soa = sob = None
        so = None
        if sa is not None and sa == sb and soa is not None and sob is not None and soa != sob:
            so = "A" if soa > sob else "B"
        status = None
        if (m.get("event_state") or "").upper() == "L":
            status = "En cours"
        elif re.search(r"cancel|abandon|postpon", m.get("event_status") or "", re.I):
            status = m.get("event_status")
        out.setdefault(g, []).append({
            "id": m.get("game_id"), "g": g, "date": date, "time": time,
            "a": team_name(ps[0], g), "b": team_name(ps[1], g),
            "fa": ps[0].get("short_name"), "fb": ps[1].get("short_name"),
            "sa": sa, "sb": sb, "so": so, "soa": soa, "sob": sob, "status": status,
            "venue": (m.get("venue_name") or "").strip() or None,
        })
    for ms in out.values():
        # 1re / 2e rencontre d'une paire : ordre des numeros de match FIH (stable meme si une date change)
        num = lambda x: int(x["id"]) if str(x["id"] or "").isdigit() else 0  # noqa: E731
        seen = {}
        for x in sorted(ms, key=num):
            k = frozenset((x["a"], x["b"]))
            seen[k] = seen.get(k, 0) + 1
            x["leg"] = seen[k]
        ms.sort(key=lambda x: (x["date"] or "9999", x["time"] or "", num(x)))
    return out


def standings(ms):
    """[rang, equipe, J, G, P, N, BP, BC, Pts] ; N = nuls (avant shoot-out)"""
    teams = []
    for m in ms:
        for t in (m["a"], m["b"]):
            if t not in teams:
                teams.append(t)
    s = {t: {"j": 0, "g": 0, "p": 0, "n": 0, "bp": 0, "bc": 0, "pts": 0} for t in teams}
    for m in ms:
        if m["sa"] is None:
            continue
        a, b = s[m["a"]], s[m["b"]]
        a["j"] += 1; b["j"] += 1
        a["bp"] += m["sa"]; a["bc"] += m["sb"]; b["bp"] += m["sb"]; b["bc"] += m["sa"]
        if m["sa"] > m["sb"]:
            a["g"] += 1; b["p"] += 1; a["pts"] += 3
        elif m["sa"] < m["sb"]:
            b["g"] += 1; a["p"] += 1; b["pts"] += 3
        else:
            a["n"] += 1; b["n"] += 1
            if m["so"] == "A":
                a["pts"] += 2; b["pts"] += 1
            elif m["so"] == "B":
                b["pts"] += 2; a["pts"] += 1
            else:
                a["pts"] += 1; b["pts"] += 1
    rows = sorted(teams, key=lambda t: (-s[t]["pts"], -(s[t]["bp"] - s[t]["bc"]), -s[t]["bp"], t))
    return [[i + 1, t, s[t]["j"], s[t]["g"], s[t]["p"], s[t]["n"], s[t]["bp"], s[t]["bc"], s[t]["pts"]] for i, t in enumerate(rows)]


def season_label(season_from):
    y = int(season_from[:4])
    return f"{y}-{str(y + 1)[2:]}"


def collect(get, season_from, season_to, log=print):
    """-> ([(pool_id, nom, [matchs], classement)], {equipe: url du drapeau})"""
    page = get(URL)
    by_g = parse(extract(page), season_from, season_to)
    flags, res = {}, []
    label = season_label(season_from)
    for g in ("M", "W"):
        ms = by_g.get(g) or []
        log(f"Pro League {'hommes' if g == 'M' else 'dames'} : {len(ms)} match(s), {sum(1 for m in ms if m['sa'] is not None)} joue(s)")
        if not ms:
            continue
        for m in ms:
            for t, f in ((m["a"], m["fa"]), (m["b"], m["fb"])):
                if f:
                    flags[t] = FLAG.format(f)
        res.append(("fihpl-" + g.lower(), POOL_NAME[g].format(season=label), ms, standings(ms)))
    return res, flags

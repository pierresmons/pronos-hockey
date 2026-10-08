"""
Lecture des matchs de l'Euro Hockey League (EHL) sur ehlhockey.tv.

Le site n'a pas d'acces de donnees officiel : on lit les pages HTML.
- https://ehlhockey.tv/events/        -> liste des tournois et leurs dates
- page de chaque tournoi               -> tableau "match-list" (date, heure, equipes, score)

Chaque tournoi de la saison devient une ou plusieurs poules :
- groupes en "tous contre tous" (ex : ROUND1, 4 groupes de 3) -> une poule par groupe, avec classement calcule
- sinon (elimination directe)                                   -> une seule poule, sans classement
"""
import datetime as dt
import html
import re

EVENTS = "https://ehlhockey.tv/events/"
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _txt(h):
    h = re.sub(r"<[^>]+>", " ", h or "")
    return re.sub(r"\s+", " ", html.unescape(h)).strip()


def _month(s):
    return MONTHS.get((s or "")[:3].lower())


def parse_events(page):
    """[(titre, url, debut_iso, fin_iso)] depuis la page /events/"""
    out, seen = [], set()
    for art in re.findall(r"<article[^>]*>(.*?)</article>", page, re.S):
        a = re.search(r'<h3[^>]*>\s*<a href="([^"]+)"[^>]*>(.*?)</a>', art, re.S)
        d = re.search(r'class="event-dates">(.*?)</div>', art, re.S)
        if not a or not d:
            continue
        url, title = a.group(1), _txt(a.group(2))
        m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s*-\s*(\d{1,2})\s+([A-Za-z]+),?\s*(\d{4})", _txt(d.group(1)))
        if not m or url in seen:
            continue
        seen.add(url)
        d1, m1, d2, m2, y2 = int(m.group(1)), _month(m.group(2)), int(m.group(3)), _month(m.group(4)), int(m.group(5))
        if not m1 or not m2:
            continue
        y1 = y2 - 1 if m1 > m2 else y2
        out.append((title, url, dt.date(y1, m1, d1).isoformat(), dt.date(y2, m2, d2).isoformat()))
    return out


def clean_team(n):
    n = re.sub(r"\s*\((Men|Women)\)\s*$", "", n.strip())
    return n


def parse_matches(page, start, end):
    """Matchs du tableau match-list : dicts {g, date, time, a, b, sa, sb, so}"""
    i = page.find('class="match-list"')
    if i < 0:
        return []
    table = page[i: page.find("</table>", i)]
    sy, ey = int(start[:4]), int(end[:4])
    out = []
    for cls, row in re.findall(r'<tr class="([^"]*)"[^>]*>(.*?)</tr>', table, re.S):
        dm = re.search(r'<td class="date">.*?<span>([^<]*)</span>\s*<span>([^<]*)</span>', row, re.S)
        hm = re.search(r'<td class="home"\s*>.*?<span class="team str-full">([^<]*)</span>', row, re.S)
        am = re.search(r'<td class="away"\s*>.*?<span class="team str-full">([^<]*)</span>', row, re.S)
        sm = re.search(r'<td class="score">(.*?)</td>', row, re.S)
        if not (dm and hm and am):
            continue
        dd = re.match(r"(\d{1,2})\s+([A-Za-z]+)", dm.group(1).strip())
        date = None
        if dd and _month(dd.group(2)):
            mo, day = _month(dd.group(2)), int(dd.group(1))
            year = sy
            if sy != ey and mo < int(start[5:7]):
                year = ey
            date = dt.date(year, mo, day).isoformat()
        time = dm.group(2).strip()[:5] if re.match(r"\d{1,2}:\d{2}", dm.group(2).strip()) else None
        score = _txt(sm.group(1)) if sm else ""
        sc = re.match(r"(\d+)\s*(\*?)\s*-\s*(\d+)\s*(\*?)", score)
        g = "W" if "women" in cls.lower() else ("M" if "men" in cls.lower() else None)
        out.append({
            "g": g, "date": date, "time": time,
            "a": clean_team(html.unescape(hm.group(1))), "b": clean_team(html.unescape(am.group(1))),
            "sa": int(sc.group(1)) if sc else None, "sb": int(sc.group(3)) if sc else None,
            "so": ("A" if sc.group(2) else ("B" if sc.group(4) else None)) if sc else None,
        })
    return out


def stage_of(title):
    m = re.search(r"(ROUND\s*\d+|KO\s*\d+(?:\s*-\s*KO\s*\d+)?|FINAL\s*\d+|RANKING CUP|FINAL)", title, re.I)
    return re.sub(r"\s+", "", m.group(1).upper()) if m else re.sub(r"[^A-Za-z0-9]+", " ", title).strip()[:30]


def gender_of(title):
    t = title.lower()
    return "W" if "women" in t else "M"


def _groups(matches):
    """Groupes d'equipes qui ne jouent qu'entre elles (composantes connexes)"""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for m in matches:
        ra, rb = find(m["a"]), find(m["b"])
        if ra != rb:
            parent[ra] = rb
    order, comp = [], {}
    for m in matches:
        r = find(m["a"])
        if r not in comp:
            comp[r] = []
            order.append(r)
        for t in (m["a"], m["b"]):
            if t not in comp[r]:
                comp[r].append(t)
    return [comp[r] for r in order]


def standings(matches, teams):
    """Classement : victoire 3 pts ; nul + shoot-out : 2 pts au vainqueur, 1 au perdant ; nul sans shoot-out : 1 pt"""
    s = {t: {"j": 0, "g": 0, "p": 0, "n": 0, "bp": 0, "bc": 0, "pts": 0} for t in teams}
    for m in matches:
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


def build(event_title, event_url, matches):
    """-> [(pool_id, pool_name, [matchs], classement ou None)]"""
    stage = stage_of(event_title)
    out = []
    by_g = {}
    for m in matches:
        by_g.setdefault(m["g"] or gender_of(event_title), []).append(m)
    for g, ms in by_g.items():
        label = "EHL " + ("Women" if g == "W" else "Men")
        base_id = "ehl-" + g.lower() + "-" + re.sub(r"[^a-z0-9]+", "", stage.lower())
        groups = _groups(ms)
        round_robin = len(groups) > 1 and all(
            3 <= len(gr) <= 6 and sum(1 for m in ms if m["a"] in gr) == len(gr) * (len(gr) - 1) // 2 for gr in groups)
        if round_robin:
            for k, gr in enumerate(groups):
                letter = "ABCDEFGHIJKLMNOP"[k]
                gm = [m for m in ms if m["a"] in gr]
                out.append((base_id + "-" + letter.lower(), f"{label} - {stage} - Pool {letter}", gm, standings(gm, gr)))
        else:
            out.append((base_id, f"{label} - {stage}", ms, None))
    return out


def collect(get, season_from, season_to, log=print):
    """Toutes les poules EHL de la saison : [(pool_id, nom, [matchs], classement|None, lieu)]"""
    events = [e for e in parse_events(get(EVENTS)) if season_from <= e[3] <= season_to]
    log(f"EHL : {len(events)} tournoi(s) cette saison")
    result = []
    for title, url, start, end in events:
        try:
            page = get(url)
        except Exception as e:  # noqa
            log(f"EHL : {title} illisible ({e})")
            raise
        ms = parse_matches(page, start, end)
        h1 = [t for t in (_txt(x) for x in re.findall(r"<h1[^>]*>(.*?)</h1>", page, re.S)) if t and not re.search(r"hockey league|\bEHL\b", t, re.I)]
        venue = h1[-1] if h1 else None
        log(f"EHL : {title} -> {len(ms)} match(s)")
        if not ms:
            continue
        for pid, name, pm, st in build(title, url, ms):
            result.append((pid, name, pm, st, venue))
    return result

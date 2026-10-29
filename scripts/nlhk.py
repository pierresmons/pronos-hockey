"""
Lecture du championnat hollandais (Hoofdklasse, hommes et dames) sur le site officiel de la ligue.

La page "matches-standings" contient toutes les rencontres (saison passee et saison en cours) sous forme de cartes :
  <a class="match-card card" data-type="men|women" data-status="final|scheduled|announced|..."
     data-date="2026-09-27T12:45:00+02:00" data-score="2|1|0|0" ...>
     <img alt="Logo: SCHC D1" src="https://images.static-hw.nl/...">  (equipe A puis equipe B)
     <span class="... col-span-3 text-white ..."> SCHC D1 </span><span ...> HGC D1 </span></a>
data-score = buts A | buts B | tirs au but A | tirs au but B (shoot-out, play-offs).

Deux poules : nlhk-m (hommes) et nlhk-w (dames). Noms d'equipes au format de l'app : "Amsterdam H1" -> "Amsterdam H-1".
Classement calcule sur la saison reguliere (premiere rencontre de chaque affiche A-B) : victoire 3, nul 1, defaite 0.
Les rencontres suivantes d'une meme affiche (play-offs) ont "leg" >= 2 et ne comptent pas au classement.
"""
import datetime as dt
import html
import re
from zoneinfo import ZoneInfo

URLS = ["https://staatsloterijhoofdklasse.com/en/matches-standings", "https://tulphoofdklasse.com/en/matches-standings"]
BXL = ZoneInfo("Europe/Brussels")
POOL_NAME = {"M": "Hoofdklasse Men - {season}", "W": "Hoofdklasse Women - {season}"}

CARD_RE = re.compile(r'<a\s+class="match-card[^"]*"([^>]*)>(.*?)</a>', re.S)
ATTR_RE = re.compile(r'data-([a-z-]+)="([^"]*)"')
LOGO_RE = re.compile(r'<img\s+alt="Logo:\s*([^"]+)"[^>]*\ssrc="([^"]+)"')
NAME_RE = re.compile(r'<span class="[^"]*col-span-3[^"]*">\s*([^<]+?)\s*</span>')


def team_name(n):
    """'Amsterdam H1' -> 'Amsterdam H-1' ; autre chose (t.b.d., 'Hoofdklasse Heren nr 4') -> None"""
    n = html.unescape(n or "").strip()
    m = re.match(r"^(.+?)\s+([HD])(\d+)$", n)
    return f"{m.group(1)} {m.group(2)}-{m.group(3)}" if m else None


def club_of(team):
    return re.sub(r"\s+[HD]-\d+$", "", team)


def parse(page, season_from, season_to):
    """-> ({g: [matchs]}, {club: url du logo})"""
    out, logos = {}, {}
    for attrs, body in CARD_RE.findall(page):
        a = dict(ATTR_RE.findall(attrs))
        names = NAME_RE.findall(body)
        if len(names) < 2:
            continue
        ta, tb = team_name(names[-2]), team_name(names[-1])
        if not ta or not tb:
            continue  # affiche de play-off pas encore connue
        try:
            d = dt.datetime.fromisoformat(a.get("date", "")).astimezone(BXL)
        except ValueError:
            continue
        date = d.date().isoformat()
        if not (season_from <= date <= season_to):
            continue
        status = (a.get("status") or "").lower()
        time = d.strftime("%H:%M") if (status != "announced" and d.strftime("%H:%M") != "00:00") else None
        sc = (a.get("score") or "").split("|")
        sa = sb = soa = sob = None
        if status == "final" and len(sc) >= 2 and sc[0].isdigit() and sc[1].isdigit():
            sa, sb = int(sc[0]), int(sc[1])
            if len(sc) >= 4 and sc[2].isdigit() and sc[3].isdigit() and (sc[2] != "0" or sc[3] != "0"):
                soa, sob = int(sc[2]), int(sc[3])
        st = None
        if status in ("live", "playing", "inprogress", "in_progress"):
            st = "En cours"
        elif status in ("postponed", "cancelled", "canceled", "abandoned"):
            st = {"postponed": "Remis", "abandoned": "Arrêté"}.get(status, "Annulé")
        if soa is not None and sa == sb and soa != sob:
            st = f"Shoot-out {soa}-{sob}"
        g = "W" if (a.get("type") or "").lower() == "women" else "M"
        for alt, src in LOGO_RE.findall(body):
            t = team_name(alt)
            if t and src.startswith("https://"):
                logos.setdefault(club_of(t), html.unescape(src))
        mid = a.get("match-id") or ""
        out.setdefault(g, []).append({"id": mid, "date": date, "time": time, "a": ta, "b": tb,
                                       "sa": sa, "sb": sb, "status": st})
    for ms in out.values():
        num = lambda x: int(x["id"]) if str(x["id"]).isdigit() else 0  # noqa: E731
        seen = {}
        for x in sorted(ms, key=lambda x: (x["date"], num(x))):
            k = (x["a"], x["b"])
            seen[k] = seen.get(k, 0) + 1
            x["leg"] = seen[k]
        ms.sort(key=lambda x: (x["date"], x["time"] or "", num(x)))
    return out, logos


def standings(ms):
    """[rang, equipe, J, G, P, N, BP, BC, Pts] sur la saison reguliere (leg 1)"""
    teams = []
    for m in ms:
        for t in (m["a"], m["b"]):
            if t not in teams:
                teams.append(t)
    s = {t: {"j": 0, "g": 0, "p": 0, "n": 0, "bp": 0, "bc": 0, "pts": 0} for t in teams}
    for m in ms:
        if m["sa"] is None or m["leg"] > 1:
            continue
        a, b = s[m["a"]], s[m["b"]]
        a["j"] += 1; b["j"] += 1
        a["bp"] += m["sa"]; a["bc"] += m["sb"]; b["bp"] += m["sb"]; b["bc"] += m["sa"]
        if m["sa"] > m["sb"]:
            a["g"] += 1; b["p"] += 1; a["pts"] += 3
        elif m["sa"] < m["sb"]:
            b["g"] += 1; a["p"] += 1; b["pts"] += 3
        else:
            a["n"] += 1; b["n"] += 1; a["pts"] += 1; b["pts"] += 1
    rows = sorted(teams, key=lambda t: (-s[t]["pts"], -(s[t]["bp"] - s[t]["bc"]), -s[t]["bp"], t))
    return [[i + 1, t, s[t]["j"], s[t]["g"], s[t]["p"], s[t]["n"], s[t]["bp"], s[t]["bc"], s[t]["pts"]] for i, t in enumerate(rows)]


def season_label(season_from):
    y = int(season_from[:4])
    return f"{y}-{str(y + 1)[2:]}"


def collect(get, season_from, season_to, log=print):
    """-> ([(pool_id, nom, [matchs], classement)], {club: url du logo})"""
    last = None
    for url in URLS:
        try:
            page = get(url)
            by_g, logos = parse(page, season_from, season_to)
            if by_g:
                break
            last = ValueError(f"aucun match de la saison sur {url}")
        except Exception as e:  # noqa
            last = e
    else:
        raise last or ValueError("Hoofdklasse illisible")
    res, label = [], season_label(season_from)
    for g in ("M", "W"):
        ms = by_g.get(g) or []
        log(f"Hoofdklasse {'hommes' if g == 'M' else 'dames'} : {len(ms)} match(s), {sum(1 for m in ms if m['sa'] is not None)} joue(s)")
        if ms:
            res.append(("nlhk-" + g.lower(), POOL_NAME[g].format(season=label), ms, standings(ms)))
    return res, logos

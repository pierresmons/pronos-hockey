#!/usr/bin/env python3
"""Robot ponctuel : cherche ou hockey.be range les logos des clubs (logos/50x50/<code>.png)
et quel nom de club est ecrit a cote. Ecrit un rapport texte (logos-rapport.txt)."""
import json, re, html, urllib.request, urllib.parse

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15 PronosHockeyFamille/1.0"
API = "https://hockey.be/wp-json/sportlink-api/cached"
PAGE = "https://hockey.be/fr/competition/calendrier-resultats-et-classements/"
LOGO = re.compile(r"logos/(?:\d+x\d+/)?([0-9A-Fa-f]{32})\.(?:png|jpg|svg)")
out = []

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", "replace")

def text(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()

def scan(label, body):
    hits = list(LOGO.finditer(body))
    out.append(f"\n===== {label} : {len(hits)} logo(s), {len(body)} caracteres")
    seen = set()
    for m in hits[:400]:
        code = m.group(1).upper()
        ctx = text(body[max(0, m.start() - 600): m.end() + 600])
        key = (code, ctx[:80])
        if key in seen: continue
        seen.add(key)
        out.append(f"{code} | {ctx[:300]}")

# 1. la page calendrier et quelques pages liees
try:
    page = get(PAGE)
    scan("page calendrier", page)
    links = sorted(set(re.findall(r'href="(https://hockey\.be/fr/[^"#?]*(?:club|equipe|team)[^"#?]*)"', page)))[:25]
    out.append("\nLiens 'club' trouves : " + ", ".join(links))
    for u in links:
        try: scan(u, get(u))
        except Exception as e: out.append(f"\n===== {u} : erreur {e}")
except Exception as e:
    out.append(f"page calendrier : erreur {e}")

# 2. l'API, avec differentes options (une poule U19 Boys Nat. 1, et un club)
for ep in ("program", "results", "standing", "clubs", "teams", "club"):
    for dump in ("", "1", "true"):
        q = urllib.parse.urlencode({"lang": "fr", "dump": dump, "clubid": "", "facilityid": "", "poolid": "12089",
                                    "from": "2026-07-01", "to": "2027-06-30", "endpoint": ep})
        try:
            body = get(f"{API}?{q}")
            scan(f"API endpoint={ep} dump={dump!r}", body)
            if ep in ("program", "results") and dump:
                out.append("Debut de la reponse : " + body[:1500])
        except Exception as e:
            out.append(f"\n===== API endpoint={ep} dump={dump!r} : erreur {e}")

open("logos-rapport.txt", "w", encoding="utf-8").write("\n".join(out))
print("\n".join(out)[:20000])

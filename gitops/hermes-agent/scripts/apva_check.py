#!/usr/bin/env python3
"""Read-only: fetch APVA published + planned calls, flag energy-storage / solar ones."""
import re, html, sys, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124"}
KW_STORAGE = ("kaupikl", "kaupimo", "akumuliat", "baterij")
KW_SOLAR = ("saulės elektrin", "saules elektrin")

def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read().decode("utf-8", "ignore")

def clean(s):
    s = re.sub(r"(?s)<(script|style).*?</\1>", "", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()

def flags(text):
    t = text.lower()
    return ("KAUPIKLIAI" if any(k in t for k in KW_STORAGE) else "", "SAULĖ" if any(k in t for k in KW_SOLAR) else "")

out, errors = [], []

# 1) Published calls (iframe, rows of 4 cells)
try:
    page = get("https://apvis.apva.lt/postinvitations/iframe")
    seen = set()
    rows = re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page)
    published = []
    for r in rows:
        cells = [clean(c) for c in re.findall(r"(?s)<td[^>]*>(.*?)</td>", r)]
        if len(cells) >= 3:
            key = tuple(cells)
            if key in seen: continue
            seen.add(key)
            published.append(cells)
    out.append(f"## PASKELBTI KVIETIMAI ({len(published)})")
    for c in published:
        s, k = flags(" ".join(c))
        out.append(f"- {' | '.join(c)}" + (f"  <<{k or s}>>" if (k or s) else ""))
    if not any(flags(" ".join(c))[0] for c in published):
        out.append("(paskelbtuose kvietimuose kaupiklių nerasta)")
    # Also list the 'individual' tab title links (fizinių asmenų) from slugs
    slugs = sorted(set(re.findall(r"paskelbti_kvietimai/([a-z0-9\-]+)", page)))
    slug_hits = [s for s in slugs if any(k in s for k in ("kaupi", "akumul", "baterij", "saules"))]
    out.append("slug hits: " + (", ".join(slug_hits) if slug_hits else "none"))
except Exception as e:
    errors.append(f"published: {e}")

# 2) Planned calls (static table on main page)
try:
    main = get("https://apva.lrv.lt/lt/aktualus-kvietimai/")
    m = re.search(r"(?s)Planuojami kvietimai.*?(<table.*?</table>)", main)
    planned = []
    if m:
        for r in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", m.group(1)):
            cells = [clean(c) for c in re.findall(r"(?s)<td[^>]*>(.*?)</td>", r)]
            if cells: planned.append(cells)
    upd = re.search(r"Atnaujinimo data:\s*([\d\-]+)", clean(main))
    out.append(f"\n## PLANUOJAMI KVIETIMAI ({len(planned)})  atnaujinta: {upd.group(1) if upd else '?'}")
    for c in planned:
        s, k = flags(" ".join(c))
        out.append(f"- {' | '.join(c)}" + (f"  <<{k or s}>>" if (k or s) else ""))
    if not planned: errors.append("planned table not found")
    # Site menu: current call groups for natural persons (names only)
    menu = [clean(x) for x in re.findall(r'(?s)<a[^>]*>([^<]*(?:saulės elektrin|kaupim)[^<]*)</a>', main)]
    menu = sorted(set(x for x in menu if x))
    out.append("\n## SVETAINĖS MENIU (ES projektai / kvietimų grupės)")
    out += [f"- {x}" + ("  <<KAUPIKLIAI>>" if flags(x)[0] else "") for x in menu]
except Exception as e:
    errors.append(f"planned: {e}")

for e in errors: out.append(f"KLAIDA: {e}")
print("\n".join(out))

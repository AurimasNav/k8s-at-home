#!/usr/bin/env python3
"""Read-only: APVA open (APVIS) and planned calls, tagged for home solar/storage.

Plain-text output is fed to the Hermes cron agent. No credentials needed.
"""
import html
import re
import urllib.request
from datetime import date, timedelta

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124"}
PUBLISHED_URL = "https://apvis.apva.lt/postinvitations/iframe"
PLANNED_URL = "https://apva.lrv.lt/lt/aktualus-kvietimai/"
TAGS = {
    "KAUPIKLIAI": ("kaupikl", "kaupimo", "akumuliat", "baterij"),
    "SAULĖ": ("saulės elektrin", "saules elektrin"),
    "FIZINIAMS": ("fiziniams asmenims", "fizinių asmenų", "fiziniai asmenys", "gyvenamųjų namų"),
}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")


def clean(s):
    s = re.sub(r"(?s)<(script|style).*?</\1>", "", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def tags(text):
    t = text.lower()
    found = [name for name, kws in TAGS.items() if any(k in t for k in kws)]
    return f"  [{', '.join(found)}]" if found else ""


def rows(table_html):
    for r in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", table_html):
        cells = [clean(c) for c in re.findall(r"(?s)<td[^>]*>(.*?)</td>", r)]
        if cells:
            yield cells


out, errors = [], []
storage_open = False

# 1) Open calls in APVIS: date | name | budget | remaining | "Liko N d."
try:
    seen, published = set(), []
    for cells in rows(get(PUBLISHED_URL)):
        if len(cells) >= 3 and tuple(cells) not in seen:
            seen.add(tuple(cells))
            published.append(cells)
    out.append(f"## AKTYVŪS KVIETIMAI APVIS ({len(published)})")
    for c in published:
        line = " | ".join(c)
        left = re.search(r"Liko (\d+) d", line)
        if left:
            line += f" | pabaiga ~{date.today() + timedelta(days=int(left.group(1)))}"
        out.append(f"- {line}{tags(line)}")
        storage_open |= "KAUPIKLIAI" in tags(line)
    if not published:
        errors.append("APVIS lentelė tuščia arba pasikeitė struktūra")
except Exception as e:
    errors.append(f"APVIS: {e}")

out.insert(0, "NAMŲ KAUPIKLIŲ KVIETIMAS ATIDARYTAS: " + ("TAIP" if storage_open else "NE"))

# 2) Planned calls table on apva.lrv.lt
try:
    page = get(PLANNED_URL)
    m = re.search(r"(?s)Planuojami kvietimai.*?(<table.*?</table>)", page)
    planned = list(rows(m.group(1))) if m else []
    upd = re.search(r"Atnaujinimo data:\s*([\d\-]+)", clean(page))
    out.append(f"\n## PLANUOJAMI KVIETIMAI ({len(planned)}), atnaujinta {upd.group(1) if upd else '?'}")
    for c in planned:
        line = " | ".join(c)
        out.append(f"- {line}{tags(line)}")
    if not planned:
        errors.append("planuojamų kvietimų lentelė nerasta")
    # Past call groups (menu links): reference only, NOT open calls
    past = sorted({clean(x) for x in re.findall(
        r"(?s)<a[^>]*>([^<]*kaupimo įrenginių[^<]*)</a>", page)} - {""})
    if past:
        out.append("\n## ANKSTESNI KAUPIKLIŲ KVIETIMAI (tik nuoroda, ne aktyvūs)")
        out += [f"- {x}" for x in past]
except Exception as e:
    errors.append(f"apva.lrv.lt: {e}")

out += [f"KLAIDA: {e}" for e in errors]
out.append(f"\nŠaltiniai: {PUBLISHED_URL} , {PLANNED_URL}")
print("\n".join(out))

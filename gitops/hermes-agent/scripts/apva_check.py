#!/usr/bin/env python3
"""Read-only: APVA open (APVIS) and planned calls, printed as the finished APVA topic report.

Runs as a no_agent cron job: the scheduler delivers stdout as the message body (inside its
usual "Cronjob Response" header and footer) with no LLM turn, so the same pages always give
the same message. Open calls come only from the APVIS
"Fiziniams asmenims" tab. Forests, invasive species, farm machinery, biomethane and calls
for business use are left out. Planned calls are kept when individuals can apply. The
first line says whether a household battery storage call is open.
Columns are found by their headers. A missing tab, an unknown table layout or an
unreachable page prints a warning in the report (NEŽINOMA instead of NE for APVIS), so a
broken parser can't keep saying NE.
--raw prints every row with its filter decision instead, to check the filters.
Exit code 1 (the scheduler then posts a failure alert) only when neither page could be
read. No credentials needed.
"""
import html
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124"}
PUBLISHED_URL = "https://apvis.apva.lt/postinvitations/iframe"
PLANNED_URL = "https://apva.lrv.lt/lt/aktualus-kvietimai/"
TODAY = datetime.now(ZoneInfo("Europe/Vilnius")).date()

# Header text (lowercase substring) -> field. Every field must be found, or the layout changed.
APVIS_COLUMNS = {"name": "pavadinimas", "remaining": "likutis", "left": "priimamos iki"}
PLANNED_COLUMNS = {"name": "pavadinimas", "start": "skelbti", "budget": "suma", "who": "pareišk"}
EMPTY_TAB = "nėra aktyvių kvietimų"

STORAGE = ("kaupikl", "kaupim", "akumuliat", "baterij")
SOLAR = ("saulės elektrin", "saules elektrin")
INDIVIDUALS = ("fizini", "gyventoj", "namų ūk")
OFF_TOPIC = ("mišk", "invazin", "savaiminuk", "medžiojam", "pelki", "durpž", "žemės ūkio", "biometan", "biodujų")
ICONS = ((STORAGE, "🔋"), (SOLAR, "☀️"), (("elektromobil",), "🚗"), (("namų", "namo", "butų"), "🏠"))


def get(url):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")


def clean(s):
    s = re.sub(r"(?s)<(script|style).*?</\1>", "", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", html.unescape(s)).strip()
    # A literal "<tag" in the message makes Telegram's sender switch to HTML parse mode.
    return s.replace("<", "‹").replace(">", "›")


def why(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return clean(str(e.reason))
    return clean(str(e))


def has(text, words):
    t = text.lower()
    return any(w in t for w in words)


def table(table_html, columns, what):
    """Rows of one <table> as dicts, with columns located by header text."""
    heads = [clean(h).lower() for h in re.findall(r"(?s)<th(?:\s[^>]*)?>(.*?)</th>", table_html)]
    if not heads:  # header row written with <td>
        first = re.search(r"(?s)<tr[^>]*>(.*?)</tr>", table_html)
        heads = [clean(c).lower() for c in re.findall(r"(?s)<td[^>]*>(.*?)</td>", first.group(1))] if first else []
    idx = {}
    for field, label in columns.items():
        hits = [i for i, h in enumerate(heads) if label in h]
        if len(hits) != 1:
            raise ValueError(f"{what}: pasikeitė lentelės stulpeliai ({' | '.join(heads) or 'antraščių nėra'})")
        idx[field] = hits[0]
    out = []
    for r in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", table_html):
        cells = [clean(c) for c in re.findall(r"(?s)<td[^>]*>(.*?)</td>", r)]
        if not cells or [c.lower() for c in cells] == heads:
            continue
        if len(cells) != len(heads):
            raise ValueError(f"{what}: eilutėje {len(cells)} langeliai, antraščių {len(heads)}")
        out.append({f: cells[i] for f, i in idx.items()})
    return out


def is_storage(name):
    """Electricity storage; heat storage ('šilumos kaupimo talpos') doesn't count."""
    return has(re.sub(r"šilumos kaup\w*", "", name.lower()), STORAGE)


def icon(text):
    if is_storage(text):
        return "🔋"
    return next((i for words, i in ICONS[1:] if has(text, words)), "•")


def eur(raw):
    """'1225000 Eur.' -> '1 225 000 €', '1225000.50 Eur.' -> '1 225 000,50 €'; else as is."""
    m = re.fullmatch(r"(-?\d+)(?:[.,](\d{1,2}))?\s*(?:Eur\.?|€)?", re.sub(r"[\s\xa0]", "", raw), re.I)
    if not m:
        return raw
    whole = f"{int(m.group(1)):,}".replace(",", " ")
    cents = m.group(2)
    return f"{whole},{cents.ljust(2, '0')} €" if cents and int(cents) else f"{whole} €"


def ends(left):
    """APVIS 'Liko N d.' / 'Liko N val.' -> end date text."""
    m = re.search(r"Liko (\d+) d", left)
    if m:
        return f"pabaiga ~{TODAY + timedelta(days=int(m.group(1)))}"
    m = re.search(r"Liko (\d+) val", left)
    if m:
        return f"baigiasi per {m.group(1)} val."
    return left or "pabaiga nežinoma"


def published_calls():
    """Open calls on the APVIS 'Fiziniams asmenims' tab (empty list when the tab says none)."""
    page = get(PUBLISHED_URL)
    start = re.search(r'<div[^>]*\bid="individual"[^>]*>', page)
    if not start:
        raise ValueError("nerastas skirtukas „Fiziniams asmenims“")
    pane = div_body(page, start.end())
    tab = re.search(r"(?s)<table.*?</table>", pane)
    if not tab:
        if EMPTY_TAB in clean(pane).lower():
            return []
        raise ValueError("skirtuke „Fiziniams asmenims“ nėra lentelės")
    calls, seen = [], set()
    for c in table(tab.group(0), APVIS_COLUMNS, "APVIS"):
        c["name"] = re.sub(r"^Nauja\s+", "", c["name"])
        if c["name"] not in seen:
            seen.add(c["name"])
            calls.append(c)
    if not calls and EMPTY_TAB not in clean(pane).lower():
        raise ValueError("APVIS: lentelė tuščia")
    return calls


def div_body(page, pos):
    """Inner HTML of the <div> whose opening tag ends at pos, up to its own closing </div>."""
    depth = 1
    for m in re.finditer(r"(?i)<div\b|</div\s*>", page[pos:]):
        depth += -1 if m.group(0).startswith("</") else 1
        if depth == 0:
            return page[pos:pos + m.start()]
    raise ValueError("skirtuko „Fiziniams asmenims“ pabaiga nerasta")


def planned_calls():
    """apva.lrv.lt 'Planuojami kvietimai' table."""
    page = get(PLANNED_URL)
    m = re.search(r"(?s)Planuojami kvietimai.*?(<table.*?</table>)", page)
    if not m:
        raise ValueError("planuojamų kvietimų lentelė nerasta")
    calls = table(m.group(1), PLANNED_COLUMNS, "planuojami kvietimai")
    if not calls:
        raise ValueError("planuojamų kvietimų lentelė tuščia")
    upd = re.search(r"Atnaujinimo data:\s*([\d\-]+)", clean(page))
    return calls, upd.group(1) if upd else None


def for_business(text):
    """Legal entities only, or individuals only for business use ('... ūkinei veiklai').

    Calls for both ('fiziniams ir/bei juridiniams asmenims', either order) are kept.
    """
    t = text.lower()
    return "juridin" in t and ("ūkin" in t or "fizini" not in t)


def open_for_households(call):
    return not (for_business(call["name"]) or has(call["name"], OFF_TOPIC))


def planned_for_households(call):
    who = call["who"]
    return has(who, INDIVIDUALS) and not for_business(who) and not has(call["name"], OFF_TOPIC)


def raw(published, planned, updated, errors):
    def mark(keep):
        return "  <- ataskaitoje" if keep else ""

    out = [f"## APVIS „Fiziniams asmenims“ ({len(published or [])})"]
    out += [f"- {c['name']} | {c['remaining']} | {c['left']}{mark(open_for_households(c))}"
            for c in published or []]
    out.append(f"\n## PLANUOJAMI KVIETIMAI ({len(planned or [])}), atnaujinta {updated or '?'}")
    out += [f"- {c['name']} | {c['start']} | {c['budget']} | {c['who']}{mark(planned_for_households(c))}"
            for c in planned or []]
    out += [f"KLAIDA: {e}" for e in errors]
    return "\n".join(out)


def report(published, planned, updated, errors):
    if published is None:
        out = ["🔋 **Namų kaupiklių kvietimas: NEŽINOMA** (APVIS nepavyko nuskaityti)"]
    else:
        mine = [c for c in published if open_for_households(c)]
        storage = any(is_storage(c["name"]) for c in mine)
        out = ["🔋 **Namų kaupiklių kvietimas: " + ("TAIP** ✅" if storage else "NE** (šiuo metu neatidarytas)")]
        out.append("\n✅ **Aktyvūs kvietimai fiziniams asmenims:**")
        out += [f"- {icon(c['name'])} {c['name']}: liko {eur(c['remaining'])}, {ends(c['left'])}"
                for c in mine] or ["- nėra"]

    if planned is not None:
        when = f" (sąrašas atnaujintas {updated})" if updated else ""
        out.append(f"\n📅 **Planuojami kvietimai fiziniams asmenims**{when}:")
        out += [f"- {icon(c['name'])} {c['name']}: pradžia {c['start']}, {c['budget']}"
                for c in planned if planned_for_households(c)] or ["- nėra"]

    out += [f"\n⚠️ Nepavyko nuskaityti: {e}" for e in errors]
    out.append(f"\n🔗 [APVIS kvietimai]({PUBLISHED_URL}) · [APVA aktualūs kvietimai]({PLANNED_URL})")
    return "\n".join(out)


def main():
    errors, published, planned, updated = [], None, None, None
    try:
        published = published_calls()
    except Exception as e:
        errors.append(f"APVIS ({why(e)})")
    try:
        planned, updated = planned_calls()
    except Exception as e:
        errors.append(f"apva.lrv.lt ({why(e)})")

    print((raw if "--raw" in sys.argv else report)(published, planned, updated, errors))
    return 1 if published is None and planned is None else 0


if __name__ == "__main__":
    sys.exit(main())

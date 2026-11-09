#!/usr/bin/env python3
"""Read-only: APVA open (APVIS) and planned calls, printed as the finished APVA topic report.

Runs as a no_agent cron job: stdout is posted to Telegram as is, with no LLM step, so the
same pages always give the same message. Only calls households can apply for are kept
(forests, invasive species, business-only calls and the like are left out). The first
line says whether a household battery storage call is open.
--raw prints every row with its filter decision instead, to check the filters.
Exit code 1 (the scheduler then posts a failure alert) only when neither page could be
read. No credentials needed.
"""
import html
import re
import sys
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124"}
PUBLISHED_URL = "https://apvis.apva.lt/postinvitations/iframe"
PLANNED_URL = "https://apva.lrv.lt/lt/aktualus-kvietimai/"
TODAY = datetime.now(ZoneInfo("Europe/Vilnius")).date()

STORAGE = ("kaupikl", "kaupimo", "akumuliat", "baterij")
SOLAR = ("saulės elektrin", "saules elektrin")
HOUSEHOLD = ("fiziniams asmenims", "fizinių asmenų", "fiziniai asmenys", "gyventoj", "namų ūk")
OFF_TOPIC = ("mišk", "invazin", "savaiminuk", "medžiojam", "pelki", "durpž", "žemės ūkio", "biometan", "biodujų")
ICONS = ((STORAGE, "🔋"), (SOLAR, "☀️"), (("elektromobil",), "🚗"), (("namų", "namo", "butų"), "🏠"))


def get(url):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")


def clean(s):
    s = re.sub(r"(?s)<(script|style).*?</\1>", "", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def rows(table_html):
    for r in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", table_html):
        cells = [clean(c) for c in re.findall(r"(?s)<td[^>]*>(.*?)</td>", r)]
        if cells:
            yield cells


def has(text, words):
    t = text.lower()
    return any(w in t for w in words)


def icon(text):
    return next((i for words, i in ICONS if has(text, words)), "•")


def eur(raw):
    """'1225000 Eur.' -> '1 225 000 €'; anything else is passed through."""
    digits = re.sub(r"[^\d-]", "", raw)
    if not re.fullmatch(r"-?\d+", digits):
        return raw
    return f"{int(digits):,}".replace(",", " ") + " €"


def ends(left):
    """APVIS 'Liko N d.' / 'Liko N val.' -> end date text."""
    m = re.search(r"Liko (\d+) d", left)
    if m:
        return f"pabaiga ~{TODAY + timedelta(days=int(m.group(1)))}"
    if re.search(r"Liko \d+ val", left):
        return "baigiasi šiandien"
    return left or "pabaiga nežinoma"


def published_calls():
    """Open APVIS calls: date | name | budget | remaining | 'Liko N d.'"""
    seen, calls = set(), []
    for cells in rows(get(PUBLISHED_URL)):
        if len(cells) < 5 or tuple(cells) in seen:
            continue
        seen.add(tuple(cells))
        calls.append({"name": re.sub(r"^Nauja\s+", "", cells[1]), "budget": cells[2],
                      "remaining": cells[3], "left": cells[4]})
    if not calls:
        raise ValueError("lentelė tuščia arba pasikeitė jos struktūra")
    return calls


def planned_calls():
    """apva.lrv.lt 'Planuojami kvietimai': name | start | budget | applicants"""
    page = get(PLANNED_URL)
    m = re.search(r"(?s)Planuojami kvietimai.*?(<table.*?</table>)", page)
    calls = [{"name": c[0], "start": c[1], "budget": c[2], "who": c[3]}
             for c in (rows(m.group(1)) if m else []) if len(c) >= 4]
    if not calls:
        raise ValueError("planuojamų kvietimų lentelė nerasta")
    upd = re.search(r"Atnaujinimo data:\s*([\d\-]+)", clean(page))
    return calls, upd.group(1) if upd else None


def open_for_households(call):
    """Household calls; storage/solar calls count even when the name doesn't say who applies."""
    n = call["name"]
    if "juridiniams asmenims" in n.lower() or has(n, OFF_TOPIC):
        return False
    return has(n, HOUSEHOLD + STORAGE + SOLAR)


def planned_for_households(call):
    return "fizini" in call["who"].lower() and not has(call["name"], OFF_TOPIC)


def raw(published, planned, updated, errors):
    def mark(keep):
        return "  <- ataskaitoje" if keep else ""

    out = [f"## AKTYVŪS KVIETIMAI APVIS ({len(published)})"]
    out += [f"- {c['name']} | {c['budget']} | {c['remaining']} | {c['left']}{mark(open_for_households(c))}"
            for c in published]
    out.append(f"\n## PLANUOJAMI KVIETIMAI ({len(planned)}), atnaujinta {updated or '?'}")
    out += [f"- {c['name']} | {c['start']} | {c['budget']} | {c['who']}{mark(planned_for_households(c))}"
            for c in planned]
    out += [f"KLAIDA: {e}" for e in errors]
    return "\n".join(out)


def report(published, planned, updated, errors):
    if published:
        mine = [c for c in published if open_for_households(c)]
        storage = [c for c in mine if has(c["name"], STORAGE)]
        out = ["🔋 **Namų kaupiklių kvietimas: " + ("TAIP** ✅" if storage else "NE** (šiuo metu neatidarytas)")]
        out.append("\n✅ **Aktyvūs kvietimai fiziniams asmenims:**")
        out += [f"- {icon(c['name'])} {c['name']}: liko {eur(c['remaining'])}, {ends(c['left'])}"
                for c in mine] or ["- nėra"]
    else:
        out = ["🔋 **Namų kaupiklių kvietimas: NEŽINOMA** (APVIS nepavyko nuskaityti)"]

    if planned:
        when = f" (sąrašas atnaujintas {updated})" if updated else ""
        out.append(f"\n📅 **Planuojami kvietimai fiziniams asmenims**{when}:")
        out += [f"- {icon(c['name'])} {c['name']}: pradžia {c['start']}, {c['budget']}"
                for c in planned if planned_for_households(c)] or ["- nėra"]

    out += [f"\n⚠️ Nepavyko nuskaityti: {e}" for e in errors]
    out.append(f"\n🔗 [APVIS kvietimai]({PUBLISHED_URL}) · [APVA aktualūs kvietimai]({PLANNED_URL})")
    return "\n".join(out)


def main():
    errors, published, planned, updated = [], [], [], None
    try:
        published = published_calls()
    except Exception as e:
        errors.append(f"APVIS ({e})")
    try:
        planned, updated = planned_calls()
    except Exception as e:
        errors.append(f"apva.lrv.lt ({e})")

    print((raw if "--raw" in sys.argv else report)(published, planned, updated, errors))
    return 1 if not published and not planned else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Read-only: NIBE F1245 + room temperature summary from Home Assistant history."""
import json, os, sys, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

URL = os.environ.get("HA_URL", "").rstrip("/")
TOK = os.environ.get("HA_TOKEN", "")
if not URL or not TOK:
    print("KLAIDA: HA_URL/HA_TOKEN nepasiekiami skriptui")
    sys.exit(0)

def api(path):
    req = urllib.request.Request(URL + path, headers={"Authorization": "Bearer " + TOK})
    return json.loads(urllib.request.urlopen(req, timeout=40).read())

now = datetime.now(timezone.utc)
states = {s["entity_id"]: s for s in api("/api/states")}

def hist(entity, days):
    start = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    end = urllib.parse.quote(now.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    q = urllib.parse.urlencode({"filter_entity_id": entity, "minimal_response": "", "no_attributes": "", "significant_changes_only": "0"})
    try:
        data = api(f"/api/history/period/{start}?{q}&end_time={end}")
    except Exception as e:
        return None
    pts = []
    for x in (data[0] if data else []):
        try:
            pts.append(float(x["state"]))
        except (ValueError, KeyError):
            pass
    return pts

def stat(entity, days, unit=""):
    p = hist(entity, days)
    if not p:
        return f"n/d ({days}d)"
    return f"min {min(p):.1f} / vid {sum(p)/len(p):.1f} / max {max(p):.1f}{unit}  (n={len(p)}, {days}d)"

def cur(entity):
    s = states.get(entity)
    if not s: return "nėra entiteto"
    return f"{s['state']} {s['attributes'].get('unit_of_measurement','')}".strip()

# Supply temp includes hot-water charging peaks; report median-ish too
def supply_stat(days):
    p = hist("sensor.bt2_supply_temp_s1_40008", days)
    if not p: return f"n/d ({days}d)"
    low = sorted(p)[: max(1, int(len(p) * 0.8))]  # drop top 20% (hot water charging)
    return f"vid be karšto vandens viršūnių {sum(low)/len(low):.1f}, max {max(p):.1f} (n={len(p)}, {days}d)"

print("## NIBE F1245 dabar")
for label, e in [
    ("Lauko temp.", "sensor.bt1_outdoor_temperature_40004"),
    ("Tiekimas S1", "sensor.bt2_supply_temp_s1_40008"),
    ("Grįžtamoji", "sensor.eb100_ep14_bt3_return_temp_40012"),
    ("Laipsnių minutės DM", "number.degree_minutes_16_bit_43005"),
    ("Poslinkis S1", "number.heat_offset_s1_47011"),
    ("Kompresoriaus startai/d.", "sensor.nibe_compressor_starts_daily"),
    ("Kompresoriaus startai iš viso", "sensor.compressor_starts_eb100_ep14_43416"),
    ("El. šildytuvas", "sensor.int_el_add_power_43084"),
]:
    print(f"- {label}: {cur(e)}")

print("\n## NIBE istorija")
for days in (1, 3):
    print(f"- Lauko: {stat('sensor.bt1_outdoor_temperature_40004', days, ' °C')}")
    print(f"- Tiekimas: {supply_stat(days)}")
    print(f"- DM: {stat('number.degree_minutes_16_bit_43005', days)}")
    print(f"- Kompresoriaus startai/d. (max per langą): {stat('sensor.nibe_compressor_starts_daily', days)}")

print("\n## Kambarių temperatūros")
rooms = [e for e in states if e.startswith("sensor.ac_") and e.endswith("_temperature")]
extra = [e for e in states if e.startswith("sensor.") and any(k in e for k in ("extract_air",)) and "ac_" not in e]
ok = 0
for e in rooms + extra:
    s = states[e]
    if s["state"] in ("unavailable", "unknown"):
        print(f"- {e}: UNAVAILABLE")
        continue
    ok += 1
    print(f"- {e}: dabar {cur(e)}; 1d {stat(e,1,' °C')}; 3d {stat(e,3,' °C')}")
print(f"(kambarių jutiklių su duomenimis: {ok} iš {len(rooms)+len(extra)})")

print("\n## Šildymo automatizacijos / poslinkio keitimai (72 val.)")
try:
    start = (now - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    q = urllib.parse.urlencode({"entity": "number.heat_offset_s1_47011"})
    ev = api(f"/api/logbook/{start}?{q}")
    if ev:
        for e in ev[-6:]:
            print(f"- {e.get('when','')[:16]} {e.get('message', e.get('state',''))}")
    else:
        print("- poslinkis nesikeitė per 72 val.")
except Exception as e:
    print(f"- logbook nepasiekiamas: {e}")

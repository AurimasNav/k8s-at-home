#!/usr/bin/env python3
"""Read-only NIBE F1245 + room climate summary from Home Assistant.

Plain-text output is fed to the Hermes cron agent. Needs HA_URL and HA_TOKEN
in the environment. Never changes any setting.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, UTC

URL = os.environ.get("HA_URL", "").rstrip("/")
TOK = os.environ.get("HA_TOKEN", "")
if not URL or not TOK:
    print("KLAIDA: HA_URL/HA_TOKEN nepasiekiami skriptui")
    sys.exit(0)

E = {
    "outdoor": "sensor.bt1_outdoor_temperature_40004",
    "outdoor_avg": "sensor.bt1_average_40067",
    "supply": "sensor.bt2_supply_temp_s1_40008",
    "supply_calc": "sensor.calc_supply_s1_43009",
    "return": "sensor.eb100_ep14_bt3_return_temp_40012",
    "brine_in": "sensor.eb100_ep14_bt10_brine_in_temp_40015",
    "brine_out": "sensor.eb100_ep14_bt11_brine_out_temp_40016",
    "hw_top": "sensor.bt7_hw_top_40013",
    "hw_load": "sensor.bt6_hw_load_40014",
    "hw_mode": "select.hot_water_comfort_mode_47041",
    "dm": "number.degree_minutes_16_bit_43005",
    "offset": "number.heat_offset_s1_47011",
    "prio": "sensor.prio_43086",
    "compressor": "sensor.compressor_state_ep14_43427",
    "starts_daily": "sensor.nibe_compressor_starts_daily",
    "starts_total": "sensor.compressor_starts_eb100_ep14_43416",
    "el_add": "sensor.int_el_add_power_43084",
    "el_add_hours": "sensor.tot_op_time_add_43081",
    "alarm": "sensor.alarm_45001",
    "energy": "sensor.heat_pump_energy_estimate",
}
ROOMS = {"Svetainė": "living_room", "Miegamasis": "bedroom", "Žaidimų k.": "gaming"}
EXTRACT_AIR = "sensor.flexit_nordic_extract_air_temperature"
BAD = ("unavailable", "unknown", "", None)
now = datetime.now(UTC)


def api(path):
    req = urllib.request.Request(URL + path, headers={"Authorization": "Bearer " + TOK})
    return json.loads(urllib.request.urlopen(req, timeout=40).read())


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


states = {s["entity_id"]: s for s in api("/api/states")}


def cur(entity):
    s = states.get(entity)
    if not s:
        return "nėra entiteto"
    if s["state"] in BAD:
        return "nepasiekiama"
    return f"{s['state']} {s['attributes'].get('unit_of_measurement', '')}".strip()


# One history request (3 days) for every entity; shorter windows are sliced from it.
history_entities = [E[k] for k in ("outdoor", "supply", "supply_calc", "return", "brine_out",
                                   "hw_top", "dm", "prio", "compressor", "starts_daily", "energy")]
history_entities += [f"sensor.ac_{r}_temperature" for r in ROOMS.values()] + [EXTRACT_AIR]
H = {}
try:
    start = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    # end_time is required: HA defaults to start + 1 day
    q = urllib.parse.urlencode({"filter_entity_id": ",".join(history_entities),
                                "end_time": now.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                                "minimal_response": "", "no_attributes": "",
                                "significant_changes_only": "0"})
    for series in api(f"/api/history/period/{start}?{q}"):
        if series:
            H[series[0]["entity_id"]] = [(datetime.fromisoformat(x["last_changed"]), x["state"])
                                         for x in series]
except Exception as e:  # current values still print without history
    print(f"PASTABA: HA istorija nepasiekiama ({e})")


def segments(entity, days):
    """[(start, end, state)] clipped to the window. HA stores changes only."""
    since = now - timedelta(days=days)
    seq = H.get(entity, [])
    out = []
    for i, (t, v) in enumerate(seq):
        end = seq[i + 1][0] if i + 1 < len(seq) else now
        if end > since:
            out.append((max(t, since), end, v))
    return out


def stat(entity, days, unit="", skip=None, only=None):
    """Time-weighted average plus min/max; skip/only = periods to exclude/keep."""
    total = weighted = 0.0
    vals = []
    for a, b, v in segments(entity, days):
        x = num(v)
        if x is None or (skip and any(sa <= a < sb for sa, sb in skip)):
            continue
        if only is not None and not any(sa <= a < sb for sa, sb in only):
            continue
        dur = (b - a).total_seconds()
        total += dur
        weighted += x * dur
        vals.append(x)
    if not vals or total == 0:
        return "n/d"
    return f"min {min(vals):.1f} / vid {weighted / total:.1f} / max {max(vals):.1f}{unit}"


def periods(entity, value, days):
    return [(a, b) for a, b, v in segments(entity, days) if v == value]


def hours(iv):
    return sum((b - a).total_seconds() for a, b in iv) / 3600


def energy_delta(days):
    p = [x for x in (num(v) for _, _, v in segments(E["energy"], days)) if x is not None]
    return f"{p[-1] - p[0]:.1f} kWh" if len(p) >= 2 else "n/d"


print("## NIBE F1245 dabar")
rows = [
    ("Lauko temp. / vidurkis", f"{cur(E['outdoor'])} / {cur(E['outdoor_avg'])}"),
    ("Tiekimas S1 / skaičiuotas", f"{cur(E['supply'])} / {cur(E['supply_calc'])}"),
    ("Grįžtamoji", cur(E["return"])),
    ("Gruntas į / iš (BT10/BT11)", f"{cur(E['brine_in'])} / {cur(E['brine_out'])}"),
    ("KV viršus / apkrova (BT7/BT6)", f"{cur(E['hw_top'])} / {cur(E['hw_load'])}"),
    ("KV komforto režimas", cur(E["hw_mode"])),
    ("Laipsnių minutės DM", cur(E["dm"])),
    ("Poslinkis S1", cur(E["offset"])),
    ("Prioritetas", cur(E["prio"])),
    ("Kompresorius", cur(E["compressor"])),
    ("Kompresoriaus startai šiandien / iš viso", f"{cur(E['starts_daily'])} / {cur(E['starts_total'])}"),
    ("El. šildytuvas dabar / val. iš viso", f"{cur(E['el_add'])} / {cur(E['el_add_hours'])}"),
    ("Aliarmo kodas", cur(E["alarm"])),
]
for label, val in rows:
    print(f"- {label}: {val}")

print("\n## NIBE istorija")
for days in (1, 3):
    hw = periods(E["prio"], "HOT WATER", days)
    run = periods(E["compressor"], "RUNNING", days)
    print(f"### {days} d.")
    print(f"- Lauko: {stat(E['outdoor'], days, ' °C')}")
    print(f"- Tiekimas be KV kaitinimo: {stat(E['supply'], days, ' °C', skip=hw)}")
    print(f"- Skaičiuotas tiekimas: {stat(E['supply_calc'], days, ' °C')}")
    print(f"- Grįžtamoji be KV kaitinimo: {stat(E['return'], days, ' °C', skip=hw)}")
    # idle brine reads boiler-room temperature, so only count running periods
    print(f"- Gruntas iš (BT11) kompresoriui dirbant: {stat(E['brine_out'], days, ' °C', only=run)}")
    print(f"- KV viršus (BT7): {stat(E['hw_top'], days, ' °C')}")
    print(f"- DM: {stat(E['dm'], days)}")
    print(f"- Kompresorius dirbo: {hours(run):.1f} h "
          f"(KV prioritetas {hours(hw):.1f} h, šildymas {hours(periods(E['prio'], 'HEAT', days)):.1f} h)")
    print(f"- Kompresoriaus startai per dieną: {stat(E['starts_daily'], days)}")
    print(f"- Energija (įvertinimas): {energy_delta(days)}")

print("\n## Patalpos")
ok = 0
for name, slug in ROOMS.items():
    t, h = f"sensor.ac_{slug}_temperature", f"sensor.ac_{slug}_humidity"
    if cur(t) in ("nėra entiteto", "nepasiekiama"):
        print(f"- {name}: {cur(t)}")
        continue
    ok += 1
    print(f"- {name}: {cur(t)}, drėgnis {cur(h)}; 1d {stat(t, 1, ' °C')}; 3d {stat(t, 3, ' °C')}")
if cur(EXTRACT_AIR) not in ("nėra entiteto", "nepasiekiama"):
    ok += 1
    print(f"- Ištraukiamas oras (Flexit): {cur(EXTRACT_AIR)}; 1d {stat(EXTRACT_AIR, 1, ' °C')}")
print(f"(jutiklių su duomenimis: {ok} iš {len(ROOMS) + 1})")

print("\n## Poslinkio S1 keitimai (72 val.)")
try:
    since = (now - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    ev = api(f"/api/logbook/{since}?" + urllib.parse.urlencode({"entity": E["offset"]}))
    for e in ev[-6:]:
        print(f"- {e.get('when', '')[:16]} {e.get('message', e.get('state', ''))}")
    if not ev:
        print("- nesikeitė")
except Exception as e:
    print(f"- logbook nepasiekiamas: {e}")

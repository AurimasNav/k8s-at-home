# 0006 — DIY light-automation sensor bundle (Thread, mmWave + PIR + lux, cat-aware)

- **Status:** **Proposed** 2026-10-11
- **Scope:** Hardware bundle and fusion logic for a mains-powered sensor node that decides "someone
  is here and it is dark" for light automation, in rooms the two cats use. ESPHome config, once
  built, lives in `src/smart-home/esphome/` — not duplicated here.
- **Related:** [ADR 0003](0003-bathroom-presence-radar-retrofit.md) (same mains/enclosure pattern, pet-immune
  tuning notes), [Sensor selection](../sensor-selection.md), [Matter-over-Thread setup](../../matter-thread-setup.md)

## Context

Light automation needs three signals that fail in different ways, so the node combines them:

| Signal | Good at | Fails at |
|---|---|---|
| mmWave presence | Still people (reading, sitting), through plastic | Cats; fans/curtains; sees through thin walls |
| PIR | Fast, cheap, clear "someone just moved" edge | Still people; cats; sunlit windows, heaters |
| Ambient light | Deciding whether a light is needed at all | Sees the lamp it controls (feedback) |

Constraints already settled: DIY, mains 230 V → 5 V via the existing Hi-Link HLK-PM01 module
(0.6 A / 3 W, see ADR 0003), Thread network already running (OTBR + Nabu Casa ZBT-2), two cats in
the house.

## Decision

| Part | Pick | Why |
|---|---|---|
| MCU | **ESP32-C6 SuperMini** (Seeed XIAO ESP32-C6 only if a node needs an external antenna) | 802.15.4 for Thread, supported by ESPHome's `openthread` component. Reuses `boards/esp32c6-supermini.yaml` (already ESP-IDF, no WiFi in it). XIAO adds a u.FL connector, at the cost of a second board profile |
| Thread role | **FTD** (`device_type: FTD`, the default) | Mains-powered, so it can be promoted to a router and strengthens the mesh. No sleep needed |
| Presence radar | **HLK-LD2410C** by default; **HLK-LD2450** only where [zones pay off](#when-to-pick-the-ld2450) | LD2410C is cheaper, smaller, better with still people and already in the repo; LD2450 adds X/Y positions and exclusion zones |
| PIR | **Panasonic EKMC1601111** | Production-grade part named in [Sensor selection](../sensor-selection.md), clean digital output, 3–6 V, ~170 µA, ~5 m range |
| Ambient light | **Vishay VEML7700** (I²C, 0x10) | Calibrated lux, RoHS, ESPHome-native `veml7700` with automatic gain/integration, ~0–120 000 lux. Replaces the LD2410's unitless 0–255 `light` value |
| Power | Existing **HLK-PM01** → 5 V into the board's `5V` pin | Same as ADR 0003. Re-check headroom, see [Power](#power) |

### Thread, not WiFi, for this node

- The ESPHome `openthread` component runs on C5/C6/H2/H4/H21/S31 with ESP-IDF and carries the normal
  ESPHome native API and OTA over Thread. It needs an OpenThread border router, which we have.
- The official ESP32-C6 example is **Thread-only: no `wifi:` block**, so drop `packages/wifi.yaml` for
  this device.
- This means a Thread node is not a drop-in copy of the existing WiFi nodes: it needs `network:
  enable_ipv6: true` plus an `openthread:` block with the dataset (channel/network key/PAN ID or a single
  `tlv` string from Home Assistant's Thread integration).
- **ESP32-H2** is the Thread-only alternative (no WiFi at all). C6 is preferred here because it keeps
  the repo's board profile, and a node can fall back to WiFi if Thread misbehaves. H2 is fine if cost matters.
- Whether the C6 can run WiFi and Thread at the same time is unconfirmed (see [Open questions](#open-questions)),
  so this ADR assumes one radio per node.
- ESPHome's Thread support is relatively new (introduced in 2025.6). Sleepy end device support does
  not matter for a mains node.

### Radar choice

| | LD2410C | LD2450 |
|---|---|---|
| Output | 1-D distance, 9 gates (~0.75 m each) | Up to 3 targets with X/Y position |
| Cat handling | Per-gate move/still energy thresholds, set between cat and human energy | Up to 3 rectangular zones; zone type **Filter** excludes a floor-plan region (people in it too) |
| Still person | Good — reports a still target | Weaker — tracks moving targets best (see [Open questions](#open-questions)) |
| Price / size (retail, approx.) | ~€3–6, ~22 × 16 mm, 2.54 mm header | ~€8–15, ~44 × 15 mm |
| ESPHome | `ld2410`, already in `features/presence-mmwave.yaml` | `ld2450` built in; `TillFleisch/ESPHome-HLK-LD2450` is an external alternative |

The LD2412 (14 gates, with background-correction helpers in ESPHome) is a third option, not picked
because its gate spacing and range are unconfirmed.

**Recommendation:** use the **LD2410C** by default. Light automation only needs "someone is here", which
it detects well, including people sitting still. Cats are handled by the height-gated PIR (see
[Cat strategy](#cat-strategy)), not by the radar. LD2410C caveats to handle in config:

- Cap the max gate at the room's size, because it sees through thin walls and open doors.
- Keep fans, curtains and plants near heater vents out of its view, because they can keep it "occupied".
- Turn off its Bluetooth (ESPHome `ld2410` `bluetooth` switch), which is on by default with a well-known
  password. `features/presence-mmwave.yaml` does not do this yet.

Both modules are UART 256000 baud 8N1. If one room ever needs both, interference between two 24 GHz radars
in one enclosure has not been tested.

#### When to pick the LD2450

Only where *where* someone is changes what should happen:

- **Open-plan rooms** (living + kitchen/dining): light only the zone someone is in; one module replaces 2–3 sensors.
- **A false-trigger spot to exclude** that people never use: cat tree, windowsill, ceiling fan, curtains.
- **Position-based scenes**: sofa → dim + TV backlight, desk → full brightness, in bed → night light.
- **Counting people** (up to 3 targets) or **direction of travel** (entering vs leaving, stairs up vs down).

Not for the entrance hall, bathroom, toilet, corridors or storage, where one area means one light.

### Fusion logic (on the node, not in Home Assistant)

Do the fusion in ESPHome with a `template` binary sensor. HA automations then only react to one
"light needed" entity, the decision does not depend on HA latency, and the per-room tuning stays with
the hardware.

The node still cannot switch a light by itself, so **automation needs HA (and the OTBR) up**. The lights
are switched one of two ways:

- **Aqara smart switch** in front of the fixture. HA turns the relay on/off. If HA is down, automation
  stops but the wall switch still works by hand.
- **Always-powered smart bulb** (e.g. the Nanoleaf GU10 in ADR 0003). HA sends on/off to the bulb. If HA
  is down, automation stops and there is no manual fallback beyond the bulb's own app/controller.

- **ON (light needed):** PIR rising edge **and** a radar moving target within ~2 s of it **and**
  lux below the on-threshold. (LD2450 rooms: the target must be outside any Filter zone.)
- **HOLD:** radar presence (moving or still). PIR is not required to hold.
- **OFF:** radar clear for the timeout (start at 2–5 min, tune per room). In transit spaces also cap the
  time since the last PIR edge, see [Ignoring cats by height](#ignoring-cats-by-height-entrance-hall-transit-spaces).
- **Lux gates ON only.** "Dark enough" (e.g. below 20 lx, tuned per room) is checked only when deciding to
  turn on. While the light is on, lux is ignored and only radar-clear turns it off. Otherwise the lamp lights
  the sensor, the room reads as "bright", and the light turns itself off. No off threshold is needed.
  Daylight takeover (turning off because the sun came up) is not worth the complexity here. If it is ever
  wanted, record the lux jump when the lamp switches on and subtract it.
- Face the VEML7700 away from the fixture anyway, so the "dark enough" check isn't skewed by other lamps
  already on in the same space.
- **The lux threshold is relative, not absolute.** Behind a light pipe or diffuser the VEML7700 sees a fraction
  of the real light, and mounted at 2+ m it measures near the ceiling, not at desk or floor level. Calibrate
  each room's threshold from history ("the reading when I'd want the light on"), not from lux tables.
- Expose the raw signals (PIR, radar targets, lux) and the fused result separately, so a wrong
  decision can be debugged from history.

## Cat strategy

**No sensor here is cat-proof.** Treat the points below as layers, and test with the real cats before trusting any of them.

1. **Home occupancy (in HA).** If no `person.*` entity is home, the automation does nothing. This is the
   only layer that is fully reliable against cats, and it covers the commonest false trigger: cats alone in
   an empty house. It lives in the HA automation, not on the node, so the node's entities stay honest.
2. **Placement.** Mount high (2–2.7 m), tilted away from the floor and from the cats' favourite spots.
   Pet-immune commercial PIRs get their immunity mostly this way, by having no "look-down" zone; at 2.3 m
   the detection range starts about 1 m from the wall. Higher mounting also leaves a bigger blind spot under the unit.
3. **LD2410C gate thresholds.** With Engineering Mode on, watch per-gate move/still energy while a cat and a
   person each cross, and set each gate's threshold between the two. Gate 0 (closest) is the usual one to lower the sensitivity of.
4. **LD2450 Filter zone (LD2450 rooms only).** Draw a rectangle (mm, X −3000…3000, Y 0…6000) over the cats' lounging
   area and set `zone_type` to Filter. Only **3** zones exist, so one is spent on the cats. The zone is a
   floor-plan rectangle, not a height band: **a person in that rectangle is ignored too**. It only fits a cat
   spot people never use (a cat tree, a windowsill). It does not fit a sofa, or a hallway the cats walk along.
5. **PIR as the height gate.** An unmasked PIR cannot tell a cat from a person. With the lower half of the lens
   masked it ignores the floor (see [Ignoring cats by height](#ignoring-cats-by-height-entrance-hall-transit-spaces)),
   so it is the main thing that keeps cats from turning lights on. Still require the radar to agree; never use the PIR alone.
6. **Hold ≠ on.** A cat lying still can keep lights *on* once a person turned them on, but cannot turn them on if
   ON needs the height-gated PIR edge plus a radar moving target.
7. **Per-room tuning stays in HA entities.** Gate thresholds and zone coordinates are `number`/`select`
   entities, so they can be changed without reflashing.

### Ignoring cats by height (entrance hall, transit spaces)

In the entrance hall there is no cat spot to exclude: the cats walk the same floor as people, so the
rule is simply "**anything low on the floor is a cat**". The node sits in a corner at about 2 m.

Neither radar can do this. The LD2410C reports distance only, and the LD2450 reports X/Y in its own
plane with no elevation, so a cat and a person at the same distance look alike. **Height has to come
from the PIR's geometry**, the same trick as commercial pet-immune PIRs:

- **Aim the PIR level or slightly down, and mask the lower half of its lens** (opaque tape on the inside
  of the Fresnel lens), so no detection zone looks down at the floor near the unit.
- Example geometry: at 2 m, if the lowest unmasked zone points 10° below horizontal, its lower edge
  reaches cat height (~0.3 m) only about 9–10 m away. That is beyond the end of the hall. A person's torso
  (1.2–1.8 m) still crosses it up to about 4.5 m away. Tune the mask on the bench with the real cats.
- **ON then requires that height-gated PIR edge** plus a radar moving target within ~2 s, so a cat
  walking through cannot turn the light on. The radar does not need any cat handling for ON.
- **Cap HOLD in transit spaces.** A cat sitting in the hall can keep the radar "present" after the person
  left. In the hall, turn off when the radar clears **or** after a max time since the last PIR edge (start at
  5 min), whichever comes first. Anyone actually walking around re-triggers the PIR and extends it.
- What still defeats it: a cat on a shoe rack, stairs or a shelf at torso height. Mask those directions too,
  or accept them.
- The LD2410C (already in `features/presence-mmwave.yaml`) is enough here. The LD2450 Filter zone adds
  nothing in a hall.

An untested idea worth a bench try: ceiling-mounted pointing down, the radar's distance gates become
height bands, so the gate holding the floor (cat height) can be zeroed. At 2.5 m with ~0.75 m gates this
is only 3–4 gates, so resolution is coarse.

## Enclosure

ADR 0003 hides the node behind a blind filler with one ~2.5 mm light pipe. That works for the radar (24 GHz
passes through plastic) and the light sensor, **but not for the PIR**:

- A PIR only sees far-infrared through its own Fresnel lens; plastic, glass and acrylic block it. The EKMC
  lens dome must stick out through the cover. Drill a hole matching the lens diameter, or use a surface-mount
  enclosure where a bigger hole in the faceplate is not acceptable.
- The lens mask from [Ignoring cats by height](#ignoring-cats-by-height-entrance-hall-transit-spaces) goes on the
  inside of the lens, so it stays invisible.
- Keep the radar's face clear of metal (box, screws, the PSU) and of the PIR's lens dome.
- The light pipe stays as in ADR 0003; see the lux calibration note under [Fusion logic](#fusion-logic-on-the-node-not-in-home-assistant).

## Power

- HLK-PM01 is 0.6 A / 3 W. ADR 0003 judged that fine for a C6 plus a small radar.
- Retail figures for the LD2410C range from ~79 mA to "use a supply above 200 mA"; there is no manufacturer
  figure for the LD2450 or for the C6's Thread TX peaks. **Measure the real draw of the assembled node
  before sealing the enclosure.** If two radars are fitted, move to **HLK-10M05** (5 V / 2 A) as ADR 0003
  suggests.
- Mains side: keep the PSU's AC pins physically separated from the low-voltage side and from the metal box,
  and have the mains work checked by a qualified electrician (ADR 0003 applies unchanged).

## Why not the alternatives

- **Buy Everything Presence / Aqara FP2.** Rejected earlier (ADR 0003): faceplates do not fit continental
  round 60 mm boxes, and bought units are not tunable to this level.
- **LD2410's own `light` value as the light sensor.** It is an arbitrary 0–255 number and only exists in
  Engineering Mode (the repo's interval hack exists to keep it on). The VEML7700 gives calibrated lux and uses I²C, not an ADC pin.
- **HC-SR501 PIR module.** Trimpot-tuned and prone to false triggers near a radio, per
  [Sensor selection](../sensor-selection.md).
- **CdS photoresistor.** Slow, uncalibrated, RoHS problem.
- **WiFi + Thread on one C6.** Not documented to work, see above.
- **ST STHS34PF80 as the PIR.** It detects stationary people over IR, but ESPHome support is unconfirmed
  (see [Open questions](#open-questions)); EKMC is the simpler first node.

## Consequences

- Thread nodes join the existing mesh and, being FTD, add router capacity, but they depend on
  the OTBR/ZBT-2 setup (see the Thread doc) being healthy.
- One more network package (`packages/thread.yaml` instead of `packages/wifi.yaml`) to maintain in
  `src/smart-home/esphome/`. The board profile is reused.
- Lights are automated only while HA and the OTBR are up (see [Fusion logic](#fusion-logic-on-the-node-not-in-home-assistant)).
- Bundle needs per-room calibration with the cats present. Budget an evening per room.

## Open questions

1. **Does Home Assistant discover an ESPHome-over-Thread device here?** IPv6 routing from HA into the
   Thread mesh is already proven: HA, OTBR and matter-server run with `hostNetwork` and the RA/route sysctls
   from the Thread doc, and Matter devices work. What is untested is HA's ESPHome integration picking up the
   node's SRP-registered `_esphomelib._tcp` service through the OTBR. If that fails, add the node by its IPv6
   address. **Prototype one node first.**
2. **Does the LD2450 lose a person sitting still?** The sources suggest the LD2410 is the better still-person
   detector, but I did not find a head-to-head test. Only matters for rooms that pick the LD2450; verify on the bench there.
3. **Which EKMC lens and mounting gives acceptable coverage?** The retail listing's two angles (94° / 82°)
   are unlabelled; read the Panasonic datasheet for the lens part numbers and the horizontal/vertical split.
4. **Coexistence margin.** A C6 Thread border-router project reports instability below about −85 dBm on WiFi;
   Thread-only nodes should be unaffected, but check RSSI to the nearest router.
5. **Current draw of the assembled node** (above).
6. **Can a C6 run WiFi and Thread at the same time?** The official example is Thread-only and a forum report
   says the shared RF makes it impractical; no authoritative statement found.
7. **LD2412 gate spacing and range**, from a manufacturer source, if it is ever reconsidered.
8. **STHS34PF80 in ESPHome.** It would cover stationary people over IR as a radar-free alternative; check
   whether ESPHome supports it natively or needs an external component.
9. **PIR lens exposure in a flush box.** See [Enclosure](#enclosure).

## Implementation notes

- New files, following the existing layout:
  `packages/thread.yaml` (replaces `wifi.yaml` for Thread nodes),
  `features/pir.yaml`, `features/light-veml7700.yaml`, and a
  `devices/presence-<room>.yaml` that composes them and defines the fusion `template` sensor.
  The radar reuses `features/presence-mmwave.yaml` (LD2410C); add `features/presence-ld2450.yaml` only
  when a room picks the LD2450.
- `esp32: framework: type: esp-idf` is required for `openthread`; `boards/esp32c6-supermini.yaml` already
  sets it. Use `network: enable_ipv6: true`.
- Get the Thread dataset TLV from Home Assistant's Thread integration; use `force_dataset: false` unless re-provisioning.
- If a XIAO ESP32-C6 is used (needs its own `boards/esp32c6-xiao.yaml`), the external antenna is selected
  by firmware (RF switch control, then the antenna pin high — GPIO14 in Seeed's example); without that it
  uses the onboard antenna.
- Keep the PIR a few cm from the C6's antenna and away from the PSU.
- VEML7700: I²C at 0x10, use the component's automatic mode.

## Where to buy

Prices checked 2026-10-11, single unit, EUR incl. VAT, shipping excluded; they move often. AliExpress links
are searches, because individual listings come and go and many are clones. Pick a listing with a high sold
count, or Hi-Link's own store for Hi-Link parts.

| Part | kaina24.lt (LT shops) | Amazon.de | AliExpress (listed) | AliExpress landed, 1 pc |
|---|---|---|---|---|
| ESP32-C6 SuperMini | Not listed | [Ulegqin ESP32-C6 SuperMini](https://www.amazon.de/dp/B0GY84262T) ~€15 | [search](https://www.aliexpress.com/w/wholesale-ESP32-C6-SuperMini.html) ~€4–5 | ~€7–8 |
| HLK-LD2410C | Not listed | [DollaTek LD2410C](https://www.amazon.de/dp/B0F25WTY1W) ~€9; [1 pc](https://www.amazon.de/dp/B0F8Q26ZTJ) ~€8 | [search](https://www.aliexpress.com/w/wholesale-HLK-LD2410C.html) ~€3–5 | ~€6–8 |
| HLK-LD2450 (only where needed) | Not listed | [LD2450](https://www.amazon.de/dp/B0HDCRBRK9) ~€9; [LD2450](https://www.amazon.de/dp/B0DSJJZ3V8) ~€12 | [search](https://www.aliexpress.com/w/wholesale-HLK-LD2450.html) ~€6–9 | ~€9–12 |
| Panasonic EKMC1601111 | [Electrobase.lt](https://electrobase.lv/lt/node/798132) ~€17.80 (check the part number on the page) | [EKMC1601111](https://www.amazon.de/dp/B0F7HZQ61N) ~€26 | [search](https://www.aliexpress.com/w/wholesale-EKMC1601111.html) ~€5–11, see note | n/a (buy from a distributor) |
| VEML7700 breakout | [Anodas.lt, DFRobot Gravity](https://www.anodas.lt/dfrobot-gravity-skaitmeninis-aplinkos-sviesos-jutiklis-veml7700-i2c) ~€11.50 | [Adafruit 4162](https://www.amazon.de/dp/B07QJG43FB) ~€11; [Ausi](https://www.amazon.de/dp/B0FMR4XWYZ) ~€8 | [search](https://www.aliexpress.com/w/wholesale-VEML7700.html) ~€2–3 | ~€5–6 |
| HLK-PM01 | [Anodas.lt](https://www.anodas.lt/maitinimo-saltinis-hi-link-hlk-pm01-100v-240vac-5vdc-0-6a) ~€8.20; [Varle.lt](https://www.varle.lt/atviro-kodo-elektronika/acdc-itampos-keitiklis-is-230v-i-5v-600ma-step-down--14235174.html) ~€11.50 | [JZK, 2 pcs](https://www.amazon.de/dp/B073QH1XT8) ~€10 | [search](https://www.aliexpress.com/w/wholesale-HLK-PM01.html) ~€2–5, see note | ~€5–8 |

**EU import charges on AliExpress** (the "landed" column):

- **VAT (21 % in LT)** is already in the AliExpress price (IOSS; listings say "Price includes VAT").
- **€3 customs duty** since 2026-07-01, on parcels under €150 from outside the EU, charged once per
  *distinct product type* (tariff classification) in the parcel, not per unit. It runs until mid-2028.
  Every part here is a different classification, and different sellers ship separate parcels, so in
  practice it is **+€3 per part type per order**. AliExpress adds it at checkout as "import charges".
- **Handling fee:** a further fee per parcel (reported as €2, some sources say €3) is due to start by
  2026-11-01. Not included in the table; add it per seller/parcel once it is live.
- **Buying several units of the same listing in one order pays the €3 once.** For example 4 × LD2410C at
  ~€4 is ~€19 landed, ~€4.75 each. For single units the duty roughly erases the AliExpress advantage:
  LD2410C, LD2450 and HLK-PM01 then cost about the same as Amazon.de or Anodas.lt, which deliver
  faster and are easier to return.
- Rule of thumb: buy from AliExpress only in batches for several nodes (SuperMini, radars, VEML7700);
  buy single units, the PIR and the PSU in the EU.

- **EKMC1601111:** the AliExpress listings at a fraction of the distributor price are likely pulls or fakes.
  For the PIR, which carries the cat logic, buy from a distributor: Electrobase.lt above, or Mouser/DigiKey
  via [Panasonic's stock search](https://na.industrial.panasonic.com/stock-search/EKMC). kaina24.lt
  also lists other EKMC lens variants at [Itwork.lt](https://itwork.lt/panasonic-pir-bewegungssensor-1-st-ekmc1600100-3-6-v-ekmc1600100) (~€27–29).
- **HLK-PM01:** it sits on mains, and cloned PM01s are common. Prefer the Anodas.lt one (also used for
  ADR 0003) or Hi-Link's own store over the cheapest listing.
- **XIAO ESP32-C6 and HLK-10M05:** not on kaina24.lt; AliExpress has both if needed.

## Sources

- [OpenThread component — ESPHome](https://esphome.io/components/openthread/)
- [ESPHome 2025.6.0 release notes (OpenThread, C6/H2)](https://esphome.io/blog/2025/06/18/esphome-2025-6/)
- [LD2450 sensor — ESPHome](https://esphome.io/components/sensor/ld2450/)
- [LD2412 sensor — ESPHome](https://esphome.io/components/sensor/ld2412/)
- [VEML7700 and VEML6030 — ESPHome](https://esphome.io/components/sensor/veml7700/)
- [LD2410 vs LD2450 vs LD2420 vs RD-03D comparison (esp32.co.uk)](https://esp32.co.uk/ld2410-vs-ld2450-vs-ld2420-vs-rd-03d-best-mmwave-sensor/)
- [EKMC1601111 listing (Conrad)](https://www.conrad.com/en/p/panasonic-pir-motion-detector-ekmc1601111-3-6-v-1-pc-s-1541129.html)
- [EKMC1601111 listing (RS)](https://uk.rs-online.com/p/proximity-sensor-ics/1357078)
- [Seeed XIAO ESP32-C6 (CNX Software)](https://www.cnx-software.com/2024/05/22/tiny-xiao-esp32c6-wifi-ble-and-802-15-4-iot-board-offers-up-to-16-gpio-pins/)
- [XIAO ESP32-C6 pinout (espboards.dev)](https://www.espboards.dev/esp32/xiao-esp32c6/)
- [What is a pet-immune motion detector? (Alarm Grid)](https://alarmgrid.com/faq/what-is-a-pet-immune-motion-detector)
- [TillFleisch/ESPHome-HLK-LD2450](https://github.com/TillFleisch/ESPHome-HLK-LD2450)
- [C6 Thread border router project (WiFi coexistence notes)](https://github.com/p01di/esp32c6-thread-border-router)
- [Commission guidance on the €3 low-value import duty](https://transition-pathways.europa.eu/retail/news/commission-publishes-guidance-new-eu3-duty-low-value-imports)
- [EU imposes €3 duty on parcels from China (LRT, 2026-07-01)](https://www.lrt.lt/en/news-in-english/19/2977091/eu-imposes-eur3-duty-on-parcels-from-china)
- [Parcels from outside the EU to cost more from July (LRT)](https://www.lrt.lt/en/news-in-english/19/2962349/parcels-from-outside-the-eu-to-cost-more-from-july)

# 0003 — Bathroom presence + ambient-light retrofit

- **Status:** **Proposed** 2026-08-02
- **Scope:** *How* the bathroom light switch is being retrofitted into a presence/ambient-light
  sensor node (hardware, wiring, power). The ESPHome device config and HA automations, once
  built, live in code (`gitops/home-assistant/automations/`) — not duplicated here.
- **Related:** entrance hall presence node (same ESPHome pattern) — see
  `gitops/home-assistant/automations/lights.yaml`

## Why

The bathroom fixture (**Nanoleaf Essentials GU10**, `light.essentials_gu10`) is already a
Matter/Thread bulb — it doesn't need a wall switch to *switch* it, it needs to stay powered so
Thread/Matter can reach it. Right now `light.essentials_gu10` reads **`unavailable`** in HA,
consistent with the mechanical switch currently cutting its power. The fix: stop the switch from
cutting power, and repurpose that box's wiring to run a presence + ambient-light sensor instead —
the same role the entrance hall's ESP32 mmWave node already plays for
`light.entrance_hall_entrance_*`.

## Hardware in play

| Component | Role |
|---|---|
| **Nanoleaf Essentials GU10** (existing, `light.essentials_gu10`) | the Matter/Thread bulb — stays, just needs to stay *powered* |
| Existing mechanical wall switch | **removed** — was cutting power to the bulb |
| **ESP32-C6 SuperMini** | runs ESPHome, same role as the entrance hall's ESP32-C6 DevKitC-1 |
| mmWave presence + ambient-light radar module | same family/ESPHome component as the entrance hall sensor |
| **Hi-Link HLK-PM01** (100–240VAC → 5VDC, 0.6A / 3W) | isolated mains PSU for the ESP32 + radar |
| Enclosure sized for the wall box | houses PSU + ESP32 + radar, keeps the mains side clear of the metal box |
| Blind/blank filler module (same switch series as the rest of the house) | covers the wall opening once the toggle is removed — clips into the existing frame/plate |
| Panel-mount acrylic light pipe (~2.5mm, e.g. RS/DigiKey "LMC" series) | small drilled hole in the filler so the light sensor can read ambient light |

## The retrofit (the decision)

- **Bridge Line straight to Load** in the switch box (Wago lever connectors / wire nuts) so the
  GU10 fixture is permanently live. Cap the switch opening with a blank plate.
- Power the sensor node off the same box's **L/N**: HLK-PM01 → 5V into the ESP32-C6 SuperMini's
  `5V` pin (**not** `3V3` — that pin is the board's own regulator *output*). The board's onboard
  regulator drops 5V to the 3.3V the chip needs, same as powering it over USB.
- **Verify the SuperMini variant actually breaks out a `5V` header pin** before wiring it in —
  some clones only accept power via the USB-C connector, with no 5V pin on the header. If so, tap
  the USB-C 5V/GND lines directly instead.
- Presence + ambient-light logic feeds HA automations the same way the entrance hall's do:
  presence + "dark enough" → `light.turn_on` on `light.essentials_gu10`; presence clear →
  `light.turn_off`.

## Enclosure — covering the wall opening

The toggle switch comes out of the **same mounting frame/plate** the rest of the house's switch
series uses (Schneider/Legrand/ABB/Gira-style frame + module system) — that frame stays. Cover the
resulting gap with a **blind/blank filler module** from the same series (every switch range sells
one for unused positions; ask for a "blind cover" / "aklinas dangtelis" locally). No special
enclosure or wall-box product needed.

- **The mmWave radar needs no opening at all** — 24GHz passes through plastic fine. Confirmed by
  the OEM Tuya ZY-M100 (same sensor family as the entrance hall node): its case only has two small
  drilled holes — one for the status LED, one for the light sensor — not for the radar.
- **The light sensor does need a small opening**: drill one ~2.5mm hole in the blind filler and
  press-fit a **panel-mount acrylic light pipe** (e.g. the "LMC" style sold by RS Components /
  DigiKey) — the same off-the-shelf part used for indicator LEDs, works identically in reverse for
  a photoresistor/ambient-light sensor. No need for a full clear pane.

**Considered and rejected:** a ready-made sensor board (**Everything Presence One/Lite** —
ESPHome-native, combines mmWave + BH1750 + PIR, USB-C/5V powered) with a 3D-printed wall-box
faceplate. The community only publishes faceplates for **UK single-gang** and **US low-voltage
gang boxes** so far, not the continental-EU round 60mm box used here, and switching to it would
mean dropping the ESP32-C6 + entrance-hall-style module plan for a different board. Sticking with
the blind-filler + light-pipe approach keeps both bathroom and entrance-hall nodes on the same
build pattern.

## Why not the alternatives

- **A smart relay/switch module in the box instead of bridging L→Load** — redundant. The bulb is
  already Matter-smart; adding a second switching layer is another point of failure for no
  benefit.
- **Battery-powered presence sensor instead of mains-fed** — mains is available right there in the
  box (that's the whole reason to reuse it); no reason to take on battery swaps for a fixed
  in-wall install.
- **Capacitive dropper instead of HLK-PM01** — no isolation, more failure-prone. HLK-PM01 is the
  same class of module used inside most commercial WiFi switches (Sonoff/Tuya) for exactly this job.

## Consequences / gotchas

- **Current headroom:** HLK-PM01 is rated 0.6A/3W at 5V — fine for an ESP32-C6 + a small mmWave
  module under normal load; momentary WiFi/Thread TX spikes have enough margin. Only reconsider
  (e.g. HLK-10M05, 5V/2A) if a relay or other power-hungry peripheral ever gets added to the same rail.
- **Mains-side clearance:** keep the HLK-PM01's AC input pins (100–240VAC) physically separated
  from the low-voltage output side and from the metal wall box, per its datasheet spacing.
- **Bathroom electrical zoning (IEC 60364-7-701):** a wall-switch position is normally already
  outside the restricted splash zones (0–2), which is presumably why a mechanical switch was
  allowed there — but this is mains work, so get a qualified electrician to sign off the bridge +
  new enclosure rather than treat it as pure DIY.

## Future options — pet-immune gate tuning

The entrance hall's mmWave module (ZY-M100-style) has no per-gate sensitivity/energy config, so a
cat lounging nearby is indistinguishable from a person standing there — confirmed from its
presence history, no duration/distance signal separates the two. If the bathroom node hits the
same problem, don't replicate that module — swap the radar choice at build time instead:

- **Hi-Link LD2410C** (or plain LD2410/2410B) — ESPHome's official `ld2410` component exposes 9
  distance gates (0–8), each with independent **move/still energy** (0–100) visible live once
  **Engineering Mode** is on, plus a matching **per-gate threshold** `number` entity. A cat's
  radar cross-section reads lower energy than a person's; set the threshold between the two.
  Same UART wiring (5V/GND/TX/RX) as the current module — drop-in on the radar side only.
- **Hi-Link LD2450** — multi-target X/Y tracking, via the `TillFleisch/ESPHome-HLK-LD2450`
  external component. Supports up to 3 rectangular **zones** in Filter mode — draw an exclusion
  box in mm coordinates around wherever the cat lounges, ignored regardless of energy level. More
  surgical than gate-energy tuning if the lounging spot is consistent; a different config paradigm
  (spatial, not energy-based).

Either is a straight swap for "mmWave presence + ambient-light radar module" in the hardware table
above — no change to the mains/enclosure decisions.

## Sources

- [Hi-Link HLK-PM01, anodas.lt listing](https://www.anodas.lt/maitinimo-saltinis-hi-link-hlk-pm01-100v-240vac-5vdc-0-6a)
- Entrance hall precedent: `gitops/home-assistant/automations/lights.yaml`, device
  "Entrance Hall Presence" (Espressif ESP32-C6-DevKitC-1)
- [ZY-M100 Tuya Human Presence Sensor Review (Blakadder)](https://blakadder.com/zy-m100/) — case has
  two small drilled holes (status LED, light sensor), none for the radar
- [Light Pipe Products (RS Components)](https://us.rs-online.com/optoelectronics/light-pipe-products/)
- [Everything Presence One - ESPHome Devices](https://devices.esphome.io/devices/everything-presence-one/)
- [Everything Presence One - UK Single Back Box Faceplate (Cults3D)](https://cults3d.com/en/3d-model/gadget/everything-presence-one-uk-single-back-box-faceplate)
- [Everything Presence One - USA Low Voltage Gang Box Faceplate (Cults3D)](https://cults3d.com/en/3d-model/gadget/everything-presence-one-usa-low-voltage-gang-box-faceplate)
- [LD2410 Sensor - ESPHome](https://esphome.io/components/sensor/ld2410/)
- [Adjusting LD2410C presence sensitivity - Home Assistant Community](https://community.home-assistant.io/t/adjusting-ld2410c-presence-sensitivity/945437)
- [LD2450 Sensor - ESPHome](https://esphome-docs.pages.dev/components/sensor/ld2450/)
- [GitHub - TillFleisch/ESPHome-HLK-LD2450](https://github.com/TillFleisch/ESPHome-HLK-LD2450)

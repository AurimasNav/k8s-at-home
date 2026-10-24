# 0007 — DSC alarm panel into Home Assistant over the Keybus (ESPHome)

- **Status:** **Proposed** 2026-10-24
- **Scope:** Read the house's existing wired DSC PowerSeries alarm into Home Assistant: every detector
  as a sensor, plus panel status and troubles. Arming/disarming from HA is out of scope for now.
  ESPHome config, once built, lives in `src/smart-home/esphome/` — not duplicated here.
- **Related:** [ADR 0006](0006-thread-presence-light-sensor-bundle.md) (the entrance-hall node takes its PIR
  signal through this node), [Matter-over-Thread setup](../../matter-thread-setup.md)

## Context

- The house has a wired **DSC PowerSeries** alarm. So far only the keypad is known: a PK5500-style LCD
  keypad ("Message Display Security Station"; the RFK5500 has the same face). The control panel itself
  (a metal cabinet with the main board and a lead-acid backup battery) has **not been located**.
- Found 2026-10-24 while preparing the ADR 0006 hall node: the planned spot holds a **Crow Swan Quad**
  pet-immune PIR wired to the panel: 12 V (red +, white −), a relay zone and a tamper loop, both with
  end-of-line resistors. There is no mains there; the 12 V is the panel's AUX output.
- The Swan Quad kept its 12 V through a deliberate mains cut, so the panel's battery works. After mains
  came back, `*2` showed only **loss of time** and **zone tamper** (the opened Swan Quad). There were no
  communication troubles, so the panel either is not set up to report anywhere or reports fine; which one
  is unknown.
- Every room with an alarm detector already has a pet-immune PIR that Home Assistant cannot see.

## Decision

| Part | Pick | Why |
|---|---|---|
| Interface | ESPHome external component **`dsc_alarm_panel`** ([Dilbert66/esphome-components](https://github.com/Dilbert66/esphome-components), built on [taligentx/dscKeybusInterface](https://github.com/taligentx/dscKeybusInterface)) | Reads the Keybus directly: zone open/closed, partition state, troubles, events. Native ESPHome API, so it fits the rest of `src/smart-home/esphome/` |
| MCU / network | **ESP32-C6 SuperMini on Thread** first; fall back to the same C6 on WiFi, then a classic ESP32 on WiFi | See [Thread or WiFi](#thread-or-wifi) |
| Mode | **Read-only**: no write transistor, `dscwritepin` unset | Zone and status reads need no writes, and a node that cannot write cannot disarm the alarm |
| Tap point | The panel's Keybus terminals once it is found; **any keypad's terminal block** until then | All keypads share the same 4-wire bus (RED, BLK, YEL, GRN) |
| Power | Keybus RED/BLK → **Pololu D45V5F5** (12 V → 5 V) → board `5V` pin | Same converter as the ADR 0006 hall node; on the alarm's battery backup |
| Bus inputs | YEL (clock) and GRN (data) each through a **33 kΩ / 10 kΩ divider** to a GPIO | Upstream wiring. The lines swing to the bus voltage; the divider brings ~13 V down to ~3 V |

### Thread or WiFi

The first draft of this plan defaulted to a classic ESP32 on WiFi, because nobody reports `dsc_alarm_panel`
on a C6. Reading the component source (`main` at `33911af`, 2026-10-17) moves that from "unknown" to
"plausible":

- On ESP32 targets it uses plain ESP-IDF APIs: a `gptimer` to sample the data line and
  `gpio_isr_handler_add` for the clock edge (`use_esp_idf_timer` defaults to true and pulls in
  `esp_driver_gptimer`). Both exist on the C6, and nothing in it is Xtensa-specific. The C6 needs ESP-IDF
  anyway for `openthread`, and `boards/esp32c6-supermini.yaml` already sets it.
- Its pin validator only knows classic-ESP32 pin numbers, so C6 pins outside that list log a warning
  (GPIO4/5 do not). A warning, not an error.
- Upstream supports "ESP32" and dropped the ESP8266 as too small. No C6 or Thread report exists in the
  issues, the discussions, the Home Assistant forum or Reddit (searched 2026-10-24).
- **The closest proxy is the ESP32-C3**: the same single-core RISC-V family, with WiFi instead of Thread.
  The maintainer runs the component on one ("I use this code on the esp32c3 which is a risc device with
  only one core and it has no issues", 2025-08-28, esphome-dsckeybus #180).
- Every non-classic-chip failure reported so far was timer-related and is fixed on the ESP-IDF path the C6
  uses: S2 garbled data (#180, fixed by the ESP-IDF `gptimer` default from 2025-11-15), C3 cycle-counter
  timing (esphome-components PR #43, 2025-12), S2 on the Arduino framework (esphome-components #64,
  2026-10-17, fixed by switching to ESP-IDF).
- **Build verified 2026-10-24:** a minimal C6 config with `openthread` and `dsc_alarm_panel` at `33911af`
  compiles on ESPHome 2026.7.3 (ESP-IDF 5.5.5) to a 774 KB firmware. Only runtime behaviour is unproven.

The real risk is timing. Keybus decoding is interrupt-driven: a clock-edge interrupt, then a timer that
samples the data line shortly after (upstream notes keypad data arriving up to 160 µs after the clock
falls). The C6 has one core, shared with the 802.15.4 radio and OpenThread. The classic ESP32 has two
cores and is what upstream users run.

Thread first anyway, because:

- It keeps one board profile and one network package for all DIY nodes (ADR 0006).
- A mains-powered FTD at the panel adds a Thread router wherever the panel turns out to be.
- Failing costs a reflash (C6 on WiFi), then about €5 of hardware (classic ESP32).

**Acceptance test:** walk the house and check that every zone flips in HA when the keypad's open-zone list
does; no decode errors in the ESPHome log over a week; PIR-to-HA latency measured (see
[Open questions](#open-questions)). On failure, try the same C6 on WiFi (separates "Thread" from "C6"),
then a classic ESP32.

## Why not the alternatives

- **Envisalink EVL-4 + HA's `envisalink` integration.** Works, but it is a commercial module that needs
  Ethernet at the panel and costs many times an ESP32, for the same data.
- **DSC IT-100 serial module + an ESP.** Extra DSC hardware for the same result.
- **One ESP per detector reading its relay.** One node per room instead of one per house, and the alarm
  loses each zone that is taken over.
- **Removing the alarm and reusing its cables.** Loses the security function and the battery-backed 12 V
  the ADR 0006 hall node now runs on.
- **Classic ESP32 on WiFi from the start.** The proven setup; kept as the fallback, see above.

## Consequences

- Every wired detector becomes an HA `binary_sensor`. The PIRs are pet-immune by lens geometry, so in
  rooms that have one it can be the PIR half of the ADR 0006 bundle instead of a masked EKMC.
- Detector settings now serve the alarm and the lights at once. Changing a Swan Quad's PET or PULSE
  jumper for the lights also changes the alarm.
- The panel's AUX output (700 mA per the DSC install guide for PC1616/1832/1864) now also feeds this node
  and the hall node. Budget it, see [Open questions](#open-questions).
- Opening detectors or keypads logs tamper troubles. If the panel reports to a monitoring company, they see them.
- Write mode (arm/disarm from HA), if ever enabled, makes this node and HA a way to disarm the house. That
  needs API encryption, the access code only in `secrets.yaml`, and a separate decision.

## Open questions

1. **Where is the panel, and which model?** Look for a metal cabinet, roughly 25–30 cm square, with a
   lead-acid battery inside; a plug-in AC transformer in a socket nearby; or follow the keypad cable.
   The model sets the zone count and confirms the AUX rating.
2. **Is the panel monitored?** Decides whether tamper events from this work reach anyone.
3. **Codes.** The master code is needed to set the clock (`*6`, master code, `1`) and for any arming;
   the installer code for programming (zone bypass, zone types). Is either known?
4. **Does `dsc_alarm_panel` decode reliably on a C6 running Thread?** It builds; decoding needs the
   acceptance test above on the real bus.
5. **PIR-to-HA latency.** ADR 0006's ON rule wants the PIR edge within ~2 s of a radar moving target.
   Measure detector trip → HA state change on the hall zone; widen that window if needed.
6. **AUX budget and battery.** Sum the keypads, detectors and both ESP nodes against 700 mA (a reseller
   rule of thumb is to stay 10 % under). Check the battery's age; sealed lead-acid alarm batteries last
   a few years.
7. **Which zone is the hall PIR?** From the keypad's open-zone list while the detector is tripped, or
   from the node's log.
8. **Antenna vs a steel cabinet.** A node inside the panel cabinet will barely reach the network. Mount
   it outside the cabinet, or use the XIAO ESP32-C6 with an external antenna (its own board profile,
   see ADR 0006).

## Implementation notes

- New files following the existing layout: `features/dsc-keybus.yaml` (external component and the
  `dsc_alarm_panel` block) and `devices/alarm-dsc.yaml` composing `boards/esp32c6-supermini.yaml`,
  `packages/core.yaml` and `packages/thread.yaml` (from ADR 0006; `packages/wifi.yaml` for the fallback).
- Pin the external component to a commit; `main` moves often:

  ```yaml
  external_components:
    - source: github://Dilbert66/esphome-components@33911affa459361900176d8688426111cba453e2
      components: [dsc_alarm_panel]
  ```

- Component keys: `dscclockpin`, `dscreadpin`, `maxzones`; leave `dscwritepin` and `accesscode` unset
  while read-only, and set `trouble_fetch: false` (it defaults to true and sends `*21`/`*27` to the panel,
  which needs write). Zones are `binary_sensor` entries on the `dsc_alarm_panel` platform with
  `id_code: z<N>` (see upstream's `DscAlarm.yaml`); give PIR zones `device_class: motion`.
- Build requirements found in the 2026-10-24 test build: `api: custom_services: true` (the component
  `#error`s without it), at least one `text_sensor` on the `dsc_alarm_panel` platform (e.g. `id_code: ss`,
  system status), and a component `id` other than `dsc`, which clashes with a global in the library.
- Pick C6 GPIOs that are not strapping, flash or USB pins (USB is GPIO12/13 on the C6). Reported
  failures: an S2 on its USB pins got no data (esphome-dsckeybus #157); a C3 on flash-related GPIO6/7
  upset the bus (discussion #206).
- When keypads show bus trouble after the ESP is attached, the maintainer's usual causes are a bad
  ground or a misconfigured write line. Read-only wiring has no write line.
- Build from PowerShell or cmd: ESP-IDF refuses to install under Git Bash ("MSys/Mingw is not supported").
- Wiring, read-only:

  ```
  Keybus RED (+) ── D45V5F5 VIN        D45V5F5 VOUT ── C6 5V
  Keybus BLK (−) ── D45V5F5 GND ────── C6 GND
  Keybus YEL ── 33k ──┬── C6 GPIO (dscclockpin)
                      └── 10k ── GND
  Keybus GRN ── 33k ──┬── C6 GPIO (dscreadpin)
                      └── 10k ── GND
  ```

- Write mode later (upstream circuit): NPN (2N3904, BC547) collector to GRN, emitter to GND, base through
  1 kΩ to `dscwritepin`.
- At a PK5500: the front unclips from the wall plate with a flat screwdriver in the slot(s) on the
  bottom edge. The terminal block takes a second wire under each screw. There is little room inside, so
  put the node in a small box beside the keypad.
- The D45V5F5 has no reverse-polarity protection: check RED/BLK with a meter before connecting.
- Do not plug USB into the board while it runs from the Keybus. Flash on the bench, then update OTA.
- Upstream suggests powering the ESP from its own supply. Powering it from the panel is common and keeps
  it on the alarm battery; the ground must be shared with the panel either way.

## Sources

- [Dilbert66/esphome-components](https://github.com/Dilbert66/esphome-components) (component source read at `33911af`)
- [Dilbert66/esphome-dsckeybus](https://github.com/Dilbert66/esphome-dsckeybus) (project docs: ESP32 only, write circuit)
- [taligentx/dscKeybusInterface](https://github.com/taligentx/dscKeybusInterface) (wiring, resistor values, power notes)
- [esphome-dsckeybus #180: S2 timing, maintainer on C3](https://github.com/Dilbert66/esphome-dsckeybus/issues/180)
- [esphome-dsckeybus #157: S2 USB pin conflict](https://github.com/Dilbert66/esphome-dsckeybus/issues/157)
- [esphome-dsckeybus discussion #206: C3 on GPIO6/7](https://github.com/Dilbert66/esphome-dsckeybus/discussions/206)
- [esphome-components PR #43: ESP32 variant timing](https://github.com/Dilbert66/esphome-components/pull/43)
- [esphome-components #64: S2 on Arduino framework](https://github.com/Dilbert66/esphome-components/issues/64)
- [esphome-components #58: maintainer uses a C3](https://github.com/Dilbert66/esphome-components/issues/58)
- [HA forum: ESP8266 into existing alarm DSC System](https://community.home-assistant.io/t/esp8266-into-existing-alarm-dsc-system/225224?page=41)
- [DSC PowerSeries PC1616/PC1832/PC1864 manual (manualsdir)](https://www.manualsdir.com/manuals/72556/dsc-powerseries-pc1616-powerseries-pc1864-powerseries-pc1832.html) (AUX 700 mA)
- [Alarm System Store: AUX budget for PowerSeries](https://www.alarmsystemstore.com/pages/will-i-need-an-additional-power-supply-to-use-the-ptk5507)
- [OpenThread component — ESPHome](https://esphome.io/components/openthread/)
- [Pololu D45V5F5](https://www.pololu.com/product/5433)

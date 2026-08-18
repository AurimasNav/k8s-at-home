# Smart-home follow-ups

Open work, newest context first. Decisions live in [decisions/](decisions/).

## Sungrow / energy

- [ ] **Rework EV surplus charging — the first attempt was based on a wrong
      assumption.** "Eco mode + grid power draw permission denied" does **not**
      mean charge-from-surplus-only; denying that register (8049 / Sungrow 8050)
      stops the charger operating at all. Proven 2026-08-16: car plugged at 87%
      all day, export steady at 4488–6123 W from 09:03–09:17 (14 min, no dips,
      well above the 4100 W / 6 A-per-phase floor), charger drew 0 W and the
      surplus went to the grid. The automation was removed and the permission
      restored to 0xAA; `switch.charger_allow_grid_charging` remains as a manual
      control. Solar priority comes from Eco mode itself. A real "never import"
      policy needs a wider write scope — mode (8047) and/or charger enable
      (8048) — which was deliberately excluded, so re-scoping is a decision, not
      an increment. **Verify any next attempt against a real sunny day before
      trusting it.**

- [x] ~~Backup does not transfer automatically~~ **Resolved 2026-08-18 — nothing was
      wrong with the installation.** The ETI SSQ 340 changeover was simply left in the
      grid-direct position at handover; nobody was told which position keeps the house
      fed through the inverter's `LOAD` output. Flipping it was the whole fix. Islanding
      then verified by opening the grid breaker: seamless transfer, no reboot, all three
      grid CTs at zero, house running on solar. See
      [ADR 0004](decisions/0004-whole-home-backup-not-automatic.md).
      Side effect: the k3s node is now on the backed-up side, so HA survives an outage —
      which is what makes `automations/backup-power.yaml` possible.
      **Still untested:** RCD operation while islanded (N-PE bonding). Deferred; needs a
      plug-in RCD tester pressed during an outage.

- [x] ~~Sweep the iHomeManager's register map~~ **Done 2026-08-14.** The useful
      map is at `192.168.1.168:502` **unit 247** (registers 8000–8600, the iHM's
      own EMS map): live meter power, per-phase powers/voltages, lifetime grid
      import/export, battery state, and EV charger status/mode/enable. Unit 1 on
      the same port is only a WiNet-mirror of the inverter map whose meter
      registers all read zero — that was the earlier "mostly NaN/zero" result.
      Port 503 (community default) is closed here and not needed. Vendored as
      `gitops/home-assistant/packages/modbus_ihomemanager.yaml` (read-only;
      upstream Jam3s97/sungrow_ihomemanager also has write entities — EMS mode,
      forced charge/discharge, charger enable — add deliberately if wanted).
- [ ] **Decide where grid import/export should come from — now settleable against
      the actual bill.** The `eso` integration (vendored 2026-08-16, Ignitis
      "Energy Smart" path) imports the DSO's own metered consumption and export,
      i.e. what you are actually billed on. Compare it against
      `ihm_grid_import_energy` and the inverter's `total_imported_energy` over a
      few days and pick whichever matches the meter, rather than choosing on
      principle. Data is previous-day only, so allow a couple of days. It also
      exposes the **kaupimas storage-bank balance**, which is not derivable from
      any local measurement.
- [ ] **Decide where grid import/export should come from.** Now actionable: the
      iHM serves the meter's lifetime counters (`8175/8177`, e.g. 7937.0 kWh
      import / 5664.7 kWh export on 2026-08-14) as
      `sensor.ihm_grid_import_energy` / `ihm_grid_export_energy`. Compare a few
      days against the inverter's `total_imported_energy` /
      `total_exported_energy` before switching the Energy dashboard source —
      switching needs care to avoid double-counting.
- [x] ~~EV charger real power~~ **Solved 2026-08-14 — no hardware needed.**
      Diffing every readable unit-247 register between an idle baseline and a
      live charge session found **8593 (total) and 8595/8597/8599 (per phase)**,
      undocumented (the community map stops at 8573). Validated live: 8593 held
      4056–4071 W while a load-derived estimate wandered 4107–4687 W and chased
      an unrelated household spike that 8593 ignored. Now read in
      `packages/modbus_ihomemanager.yaml`; the whole estimate layer was deleted.
      **No energy counter exists** — scanned 8574–8773 across 7 min of charging
      at 4.06 kW and nothing accumulated (a 1 Wh counter would tick ~470), so
      kWh comes from integrating the measured power. The RS485/ESP32 work below
      is therefore **no longer needed for power**; it would only add session
      energy and control, which the iHM does not expose.
- [ ] ~~EV charger telemetry — needs a local RS485 path.~~ **Superseded, kept
      for the topology findings.** Status/mode/enable come from the iHM
      (`8551/8047/8048`). Established 2026-08-14:
      - charger `:516` only (SSL, iHM-reserved); every other port actively
        *refused*. It rate-limits fast scans — scan gently or it looks dead.
      - not reachable through the WiNet-S either: units 2/247 answer on
        `.119` but the `21xxx` charger registers return ILLEGAL_DATA_ADDRESS,
        and units 3/248 don't exist. Community setups reach the wallbox that
        way only when it hangs off the *inverter's* RS485; here the
        **iHomeManager owns it over Ethernet**, so there is nothing to re-serve.
      - **evcc needs the same missing local Modbus** (its driver reads 21299/
        21307/21316 at unit 248), so it is not a workaround. Its sponsorship
        gate is gone though — removed 2025-09-03, evcc-io/evcc `065bb7ae`;
        the issue claiming otherwise was filed later against a stale build.
      - working community fix is **RS485 straight off the wallbox into a
        serial→Ethernet gateway** (9600 8N1), wallbox set to EMS mode via its
        WLAN hotspot. Blocked here: no power/Ethernet at the wallbox for the
        freed Waveshare (`192.168.1.99`, currently unplugged). An **ESP32 +
        RS485 over WiFi** (same pattern as the T-CAN485 nibegw node, ESPHome
        `modbus_controller`) would sidestep the cabling — still needs power at
        the wallbox and a free RS485 port on it (unverified; plausible since
        the iHM link is Ethernet).
      - watch for a **two-masters conflict**: reports say the wallbox cannot be
        driven by the iHM and an external controller at once. Read-only polling
        may coexist; the EMS-mode change is the risky part.
- [ ] **Disable the WiNet-S WiFi interface** (`192.168.1.116`; same dongle as
      wired `.119`). The dongle tolerates one Modbus client, so a second reachable
      path invites contention — a plausible cause of intermittent modbus errors.

## NIBE F1245

- [ ] **DHCP reservation for `192.168.1.149`** (nibegw). The `nibe_heatpump`
      integration is pinned to that address; a lease change breaks writes to the
      pump.
- [x] ~~Calibrate the heat pump power estimate.~~ **Done 2026-08-14** —
      `compressor_w` 1600 → **1970 W**, derived from recorder history rather
      than watched by eye: 44 transitions over 8 days, median house load 4 min
      before vs after each start/stop, keeping the 19 with a quiet house on both
      sides. Median step 2014 W (stdev 219), minus the model's own 45 W pump
      delta. Derivation is in the `packages/nibe.yaml` header. **Recheck in
      winter** — the immersion heater read zero for the whole sample, so its
      contribution is untested, and a colder brine temperature may shift the
      compressor draw.
- [ ] **Consider real metering** instead of the estimate: a Shelly EM on the
      pump's breaker (local push, no cloud, Matter not needed — Shelly has no
      Matter-over-Thread and does not plan to), or NIBE's own pulse energy meter
      on input board X22/X23, which needs input board version 35+.
- [ ] **Tune automation thresholds after a winter of data**: brine-out −8 °C,
      30 compressor starts/day, immersion heater 0.5 kW for 30 min.
- [ ] **Evaluate the dummy RMU40** (`acknowledge: RMU40_S4`) for faster register
      updates. Left out because the pump then expects RMU-specific replies too.
- [ ] Optionally add *Heat pump energy estimate* under **Settings → Energy → Add
      device** once the daily figures look right.

## Flexit Nordic

- [ ] **Fix the real cause of the post-outage dropout** (`192.168.1.134`): a DHCP
      reservation or a static address on the unit, plus a UPS on the switch so the
      network is up before the unit. The replug ritual points at its link or DHCP
      not recovering when the switch comes up after it. The HA-side auto-reload in
      `automations/flexit.yaml` only fixes a stale connection, never an absent
      device.
- [ ] **Verify that auto-reload actually recovers it** at the next outage, and
      drop the retry count if it proves noisy.

## Presence nodes

- [ ] **Reflash `presence-entrance-hall`** with the engineering-mode retry loop
      (`features/presence-mmwave.yaml`, needs the Windows/esphome toolchain).
      After the 2026-08-13 outage the one-shot `on_boot` enable lost the race
      against the radar's cold boot, the ambient-light reading stayed `unknown`,
      and the light-on automation never passed its `below: 50` condition. The
      HA-side guard now reconciles every 5 min as a stopgap, but the node
      should self-heal without HA.
- [ ] **HA 2026.7.2 oddity worth an upstream look:** the old edge-triggered
      guard (`state` trigger, `to: 'off'`, `for: 10s`) verifiably did not fire
      on the `unavailable → off` transition after the outage (twice), even
      though the trigger source says `MATCH_ALL` matches any old state. If it
      reproduces on a future HA restart, file it upstream with the recorder
      timeline.

## Cameras / CCTV

Current kit (nothing here is old — both lines are still in Hikvision's 2026
catalogue, installed ~June 2023):

| | |
|---|---|
| NVR | `DS-7604NXI-K1/4P`, fw V4.76.015 — 4-ch AcuSense |
| Cameras ×3 | `DS-2CD2346G2-IU` — 4 MP AcuSense turret, Darkfighter, 120 dB WDR, mic, IP66 |

- [ ] **Keep Frigate in mind — it is the answer if the Hikvision integration
      dies, and it fixes a gap we already have.** The problem is software, not
      hardware: `hikvision_next` is dormant (last real commit 2024-12-15, 111
      open issues) and warns it stops working in HA 2027.2. Replacing cameras
      would fix nothing.

      [Frigate](https://frigate.video) keeps the existing cameras (they do RTSP)
      and replaces the NVR's *software* role, running in this k3s cluster. It
      brings a maintained HA integration, local AI object detection, and
      **notifications carrying video clips** — which is exactly why the
      Hikvision native app currently beats HA, and why the motion notifications
      were dropped from HA in the first place (see
      `automations/hikvision.yaml`).

      Free and open source (AGPL-3.0). Frigate+ (~$50/yr, custom model
      training) is optional and not needed. Real costs are **inference**
      — free if the k3s node has an Intel iGPU (OpenVINO), else a Coral TPU
      at ~€30–70 — and **storage**, which is the bigger one for three 4 MP
      streams. Check the node for an Intel iGPU first; that decides whether
      this costs anything at all.

      Trade-off worth naming: it is another service to run, update and back up,
      versus an appliance that just works. The NVR keeps recording regardless,
      so it can be trialled alongside rather than as a cutover.

- [ ] **Lighter fallback if Frigate is not wanted:** core `onvif` gives streams
      and motion/tamper events from the same cameras, vendor-neutral and
      maintained. The only thing lost is the NVR reboot service, which becomes a
      `rest_command` against the ISAPI endpoint. Our exposure is already small —
      since motion notifications moved to the native app, HA only uses
      tamper/video-loss sensors plus that reboot.

- [ ] **Update camera firmware.** All three are on V5.7.13 build 230706 (July
      2023) — three years stale on network-facing devices.

- [ ] **Keep the NVR and cameras off the internet** (no port forwarding, LAN or
      VLAN only). Hikvision is on the US FCC covered list and barred from UK
      government sites; for a private home that is a personal call, but it is a
      strong argument against any remote/cloud exposure.

## Housekeeping

- [ ] `plans/esphome-directory-structure.md` is stale — it names an ESP32-C6 as
      the nibegw board, but the gateway runs on the LilyGo T-CAN485. Rewrite or
      delete.
- [ ] Reloader now watches all namespaces (`gitops/reloader/values.yaml`), which
      grants it cluster-wide read on Secrets as well as ConfigMaps. Revisit if
      that is too broad; the alternative is dropping
      `disableNameSuffixHash: true` so ConfigMap content changes roll the pod by
      themselves (verified to work — kustomize rewrites the Helm-rendered volume
      references).

# Smart-home follow-ups

Open work, newest context first. Decisions live in [decisions/](decisions/).

## Sungrow / energy

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
- [ ] **Decide where grid import/export should come from.** Now actionable: the
      iHM serves the meter's lifetime counters (`8175/8177`, e.g. 7937.0 kWh
      import / 5664.7 kWh export on 2026-08-14) as
      `sensor.ihm_grid_import_energy` / `ihm_grid_export_energy`. Compare a few
      days against the inverter's `total_imported_energy` /
      `total_exported_energy` before switching the Energy dashboard source —
      switching needs care to avoid double-counting.
- [ ] **EV charger real power/energy — needs a local RS485 path.** Status/mode/
      enable come from the iHM (`8551/8047/8048`); charging power and session
      energy are **not exposed anywhere locally**. Established 2026-08-14:
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
- [ ] **Hunt for an undecoded charging-power register on iHM unit 247.** An
      idle/unplugged baseline of all 2800 readable registers was captured
      2026-08-14 21:41. Re-snapshot *while the car is charging* and diff: any
      register tracking the charge is the one we want. Tooling and baseline are
      session-scratch — recreate with a block scan of the live regions
      (fc4 2500/4300–4899/7900–8699/40500, fc3 4100/8000/33100/39000–40799) if
      not still around.
- [ ] **Disable the WiNet-S WiFi interface** (`192.168.1.116`; same dongle as
      wired `.119`). The dongle tolerates one Modbus client, so a second reachable
      path invites contention — a plausible cause of intermittent modbus errors.

## NIBE F1245

- [ ] **DHCP reservation for `192.168.1.149`** (nibegw). The `nibe_heatpump`
      integration is pinned to that address; a lease change breaks writes to the
      pump.
- [ ] **Calibrate the heat pump power estimate.** `packages/nibe.yaml` assumes a
      flat 1600 W while the compressor runs. Watch *House load* step up when the
      compressor starts at night and set the real figure.
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

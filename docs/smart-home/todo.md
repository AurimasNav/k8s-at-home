# Smart-home follow-ups

Open work, newest context first. Decisions live in [decisions/](decisions/).

## Sungrow / energy

- [ ] **Sweep the iHomeManager's register map** (`192.168.1.168:502`, plain Modbus,
      already open — no device setting needs changing). Find which addresses it
      actually populates; most of the inverter map reads NaN because it only
      re-serves what it collects. Then vendor
      `gitops/home-assistant/packages/modbus_ihomemanager.yaml` next to the
      Sungrow one.
- [ ] **Decide where grid import/export should come from.** The Energy dashboard
      currently uses `sensor.total_imported_energy` / `total_exported_energy`
      read from the inverter, but with an iHomeManager in the system it is the
      authority at the meter. If they disagree, self-sufficiency and cost figures
      are skewed. Switching sources needs care to avoid double-counting.
- [ ] **EV charger has no local Modbus** — `:516` on it is SSL and reserved for
      the iHomeManager. If charger state/control in HA is wanted, the options are
      whatever the iHomeManager exposes on `:502`, or **evcc**.
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

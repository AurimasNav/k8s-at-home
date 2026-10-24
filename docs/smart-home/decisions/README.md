# Smart-home / IoT / home-network decisions

Lightweight decision records (ADR-style) for smart-home, IoT and home-network
choices in this homelab — the "why", not just the "how". Setup/how-to docs live one level up in
[`docs/`](../..) (e.g. [Matter-over-Thread setup](../../matter-thread-setup.md)),
and component-choice guidance in
[Sensor selection](../sensor-selection.md).

## Format

One file per decision, `NNNN-short-slug.md`, with a header:

- **Status** — Proposed / Accepted / Superseded (with date)
- **Context** — the problem and constraints
- **Decision** — what we chose
- **Why not the alternatives** — options considered and rejected
- **Consequences** — trade-offs we accepted
- **Implementation notes** — enough to reproduce

Keep them short and self-contained. Supersede rather than rewrite: mark the old one
`Superseded by NNNN` and add a new file.

## Index

| # | Decision | Status |
|---|---|---|
| [0001](0001-nibe-f1245-integration-path.md) | NIBE F1245 integration path (ESP32 NibeGW over WiFi, retire Waveshare) | Accepted 2026-07-22 |
| [0002](0002-sungrow-inverter-modbus-path.md) | Sungrow integration path (WiNet-S native Modbus TCP; iHomeManager unit 247 for meter + EV charger) | Deployed 2026-07-24, extended 2026-08-14 |
| [0003](0003-bathroom-presence-radar-retrofit.md) | Bathroom presence + ambient-light retrofit (ESP32-C6 SuperMini + mmWave, mains-fed) | Proposed 2026-08-02 |
| [0004](0004-whole-home-backup-not-automatic.md) | Whole-home backup: changeover was in the wrong position, not miswired | **Resolved 2026-08-18** |
| [0005](0005-opnsense-firewall-hardware.md) | OPNsense firewall hardware (Intel N150 mini-PC, not an official Deciso appliance) | Proposed 2026-09-12 |
| [0006](0006-thread-presence-light-sensor-bundle.md) | DIY light-automation sensor bundle (ESP32-C6 on Thread, mmWave + PIR + lux, cat-aware) | Proposed 2026-10-11, hall amended 2026-10-24 |
| [0007](0007-dsc-alarm-keybus-integration.md) | DSC alarm into Home Assistant over the Keybus (ESPHome `dsc_alarm_panel`, C6 on Thread first) | Proposed 2026-10-24 |

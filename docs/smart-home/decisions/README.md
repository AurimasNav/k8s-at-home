# Smart-home / IoT decisions

Lightweight decision records (ADR-style) for smart-home / IoT choices in this
homelab — the "why", not just the "how". Setup/how-to docs live one level up in
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

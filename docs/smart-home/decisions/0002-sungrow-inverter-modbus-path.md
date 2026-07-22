# 0002 — Sungrow inverter integration path

- **Status:** Accepted (2026-07-22)
- **Scope:** Smart-home / IoT — solar / battery / EV telemetry into Home Assistant
- **Related:** [0001 — NIBE F1245 integration path](0001-nibe-f1245-integration-path.md)

## Context

Our energy stack is all Sungrow: SH-series hybrid inverter, battery, and EV charger.
We want full, reliable telemetry (and where supported, control) in Home Assistant,
self-hosted, without depending on the iSolarCloud cloud.

The community-standard integration is
[`mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant`](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant).
It offers three transports:

| Transport | Trade-offs |
|---|---|
| **WiNet-S dongle** (WiFi or its LAN port) | No extra hardware, but slower, **some Modbus registers are missing**, and Modbus is low-priority on the dongle so polling is flaky (needs `sungrow_modbus_wait_milliseconds` ≥ 20). |
| **Internal LAN port** (only some models) | Fast (5 ms), full registers — but not all inverters expose it. |
| **RS485 → the inverter COM2 / "logger" terminals** | Full register access, independent of the WiNet-S, stable. Needs an RS485-to-network bridge. |

We already own a **Waveshare RS485-to-ETH** converter, freed from the NIBE role
(see [0001](0001-nibe-f1245-integration-path.md) — it cannot meet NIBE's ACK timing
over the network, but it is a great fit for a normal Modbus RTU slave where Home
Assistant is the polling master).

## Decision

Use the **Waveshare RS485-to-ETH wired into the inverter's COM2 / logger RS485
terminals**, exposed to Home Assistant as **Modbus RTU-over-TCP** and read by the
`mkaiser/Sungrow-SHx` integration.

- Full register set, independent of the WiNet-S dongle's limitations.
- Reuses hardware we already own — €0.
- Home Assistant is the Modbus **master** polling the inverter **slave**: a clean
  request→response cycle with no unsolicited real-time ACK requirement, which is
  exactly what the Waveshare does well.

## Why not the alternatives

| Option | Why rejected |
|---|---|
| **WiNet-S dongle Modbus TCP only** | Missing registers, slower, unstable Modbus service. Fine as a fallback, not as the primary. |
| **Internal LAN port** | Not reliably available across models; RS485 is model-agnostic. |
| **New dedicated ESP32 Modbus gateway** | Unnecessary — the Waveshare already does transparent RS485↔TCP and is free. |

## Consequences

- The **single UTP drop** shared with the NIBE is assigned to this Waveshare; the
  NIBE gateway therefore runs on WiFi (see [0001](0001-nibe-f1245-integration-path.md)).
- Battery telemetry comes **through** the SH inverter's Modbus map (covered by the
  same connection).
- The **EV charger may sit on a separate Modbus map** — its registers are not
  guaranteed to be exposed via the inverter. Verify before assuming one link covers
  all three devices; it may need its own integration.

## Implementation notes

**Inverter side (RS485):**

- Wire the Waveshare A/B/GND to the inverter's **COM2 / logger RS485 terminals**
  (the port where the inverter acts as a Modbus **slave** for a data logger). Exact
  pin labels vary by model (SH-RT / SH-RS / SG string) — follow the
  [mkaiser wiring guide](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant)
  for the specific unit.
- Serial: **9600 baud, 8N1**; Modbus slave/unit ID typically **1**.
- The COM2 logger port is generally independent of the WiNet-S, but avoid two
  masters polling the **same** port simultaneously.

**Waveshare side:**

- Work mode **TCP Server**, serial **9600 8N1**, transparent bridge (not the
  Modbus-gateway conversion mode).
- Note its IP/port; keep the serial framing / packing interval low.

**Home Assistant (`mkaiser/Sungrow-SHx`):**

- Connection type **Modbus RTU-over-TCP**, host = Waveshare IP, **port 502**.
- Tune `sungrow_modbus_wait_milliseconds` (LAN/RS485 can use a low value, ~5 ms).

## Sources

- [mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant)
- [FAQ: connection problems / transport comparison](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant/wiki/FAQ:-Problems-with-the-connection)

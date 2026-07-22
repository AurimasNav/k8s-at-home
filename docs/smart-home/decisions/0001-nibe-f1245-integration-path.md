# 0001 — NIBE F1245 integration path

- **Status:** Accepted (2026-07-22)
- **Scope:** Smart-home / IoT — heat pump telemetry into Home Assistant
- **Related:** [Matter-over-Thread setup](../../matter-thread-setup.md)

## Context

We want the NIBE F1245 (ground-source heat pump) readable and controllable from
Home Assistant, without buying the NIBE MODBUS 40 accessory and without relying on
NIBE Uplink / myUplink cloud.

The F1245 is a **non-S-series** pump. It does **not** speak Modbus RTU on its
accessory bus. The only local integration path is the NIBE proprietary protocol on
the RS485 accessory bus, using **NibeGW**, which emulates the MODBUS 40 accessory:

- The pump broadcasts telegrams on the bus.
- **Every telegram must be ACK/NAK'd in near-real-time**, or the pump raises an
  alarm and stops communicating.
- A gateway running NibeGW handles the ACK and relays decoded telegrams to Home
  Assistant over UDP (and forwards read/write requests back).

We already own a Waveshare RS485-to-ETH converter and initially tried to use it as
the transport. That does not work: the Waveshare is a **dumb byte pipe**. In TCP or
UDP mode it forwards raw RS485 bytes but runs no protocol logic, so nothing ACKs the
pump and Home Assistant never receives valid NibeGW telegrams. Both "TCP mode" and
"UDP mode" attempts failed for this reason — the missing element is the NibeGW
process itself, and the ACK timing is too tight to reliably survive a network
round-trip (`pump → Waveshare → TCP → gateway → back`).

## Decision

Use **Option A: a dedicated microcontroller running NibeGW, wired directly to the
F1245 RS485 bus.**

- **Hardware:** LilyGo **T-CAN485** (ESP32 with built-in RS485 transceiver).
- **Firmware:** [`elupus/esphome-nibe`](https://github.com/elupus/esphome-nibe) —
  an ESPHome component wrapping NibeGW. Emulates MODBUS 40 (plus a dummy RMU40 for
  faster register updates) and forwards telegrams to Home Assistant over UDP.
- **Home Assistant:** the built-in
  [`nibe_heatpump`](https://www.home-assistant.io/integrations/nibe_heatpump/)
  integration in **nibegw** mode.

The Waveshare is **retired from the NIBE role** and reserved for real Modbus RTU
devices (see index README for candidate uses).

## Why not the alternatives

| Option | Why rejected |
|---|---|
| **Waveshare + HA pointed directly at it (TCP/UDP)** | Impossible — no NibeGW process, nothing ACKs the pump, HA receives raw bytes not telegrams. This is what failed. |
| **Waveshare + `socat`/`ser2net` → NibeGW binary on Linux** | Workable but fragile. The ACK now traverses the network; Waveshare buffering + LAN latency intermittently trips the pump's alarm. Not "tried and proven" enough for an always-on heat pump. |
| **Buy the NIBE MODBUS 40 accessory** | Costs money and still needs a gateway/logic on our side; no advantage over the ESP32 for our purpose. |
| **NIBE Uplink / myUplink cloud** | Cloud dependency, rate-limited, out of scope (we self-host everything). |

The dedicated-microcontroller approach keeps the tight ACK loop entirely **off the
network**, which is precisely why it is the community-standard, reliable path.

## Consequences

- ~€25 of new hardware (T-CAN485) instead of reusing the Waveshare for this.
- The gateway runs on ESPHome, so it is managed/updated like our other ESPHome
  nodes, and appears to HA via the official integration (no custom container).
- The Waveshare is freed for a genuine Modbus RTU device.
- The heat pump stays fully local — no cloud, no MODBUS 40 purchase.

## Implementation notes

**Pump side (NIBE F1245 service menu):**

- Enable the **MODBUS 40** accessory in service menu **5.2** (NibeGW impersonates
  it — this is required even though we do not own the physical unit; it is what
  makes the pump broadcast telegrams and expect ACKs).
- RS485 wiring per the MODBUS 40 installation manual: **A→A, B→B, GND→GND**.
- Serial parameters: **9600 baud, 8N1**.

**Gateway (T-CAN485 + `elupus/esphome-nibe`):**

- Flash ESPHome with the `nibe` external component.
- Point the UDP target at the Home Assistant host.

**Home Assistant (`nibe_heatpump`, nibegw mode):**

- Listening port (telegrams from gateway): **UDP 9999**
- Remote read port: **UDP 10000**
- Remote write port: **UDP 10000**
- Ensure the HA host firewall allows inbound UDP 9999/10000.

**Verification / troubleshooting:**

- Pump shows red/alarm → MODBUS 40 not enabled, or gateway not ACKing.
- Gateway RS485 activity LED solid on → A/B lines swapped (must be A→A, B→B).
- Garbage / no telegrams → serial not set to 9600 8N1.
- HA configured but no entities → wrong UDP ports or host firewall blocking.

## Sources

- [Home Assistant — Nibe Heat Pump integration](https://www.home-assistant.io/integrations/nibe_heatpump/)
- [elupus/esphome-nibe](https://github.com/elupus/esphome-nibe)
- [openHAB — Nibe Heatpump binding (NibeGW protocol, ACK, ports)](https://www.openhab.org/addons/bindings/nibeheatpump/)
- [HA Community — "Connect to Nibe without the cloud"](https://community.home-assistant.io/t/how-to-connect-to-nibe-heat-pump-without-the-cloud/381099)

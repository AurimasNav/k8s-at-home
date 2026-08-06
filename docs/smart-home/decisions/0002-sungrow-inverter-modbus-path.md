# 0002 — Sungrow inverter integration path

- **Status:** **Deployed** 2026-07-24 — read over the WiNet-S dongle via native Modbus TCP
- **Scope:** *How* the Sungrow system is physically/logically connected to Home Assistant
  (devices, modules, transport). The actual sensor/register definitions live in code
  (`gitops/home-assistant/packages/modbus_sungrow.yaml`) and are not duplicated here.
- **Related:** [0001 — NIBE F1245 integration path](0001-nibe-f1245-integration-path.md)

## Hardware in play

| Device / module | Role |
|---|---|
| **Sungrow SH15T** (three-phase hybrid inverter) | the Modbus slave we read |
| **Sungrow SBR160** (16 kWh HV battery) | battery data, via the inverter's map |
| **Sungrow WiNet-S dongle** (on the inverter) | **the connection module we use** — native Modbus TCP + iSolarCloud uplink |
| **Waveshare RS485-to-ETH (B)** (`192.168.1.99`, on inverter COM2) | evaluated then **abandoned** — now free for another Modbus RTU device |
| **Sungrow iHomeManager** (`192.168.1.168`, WiFi) | energy manager / smart meter. Serves **plain Modbus TCP `:502`** with a *partial* map — not yet read by HA |
| **Sungrow EV charger AC011E** (`192.168.1.191`, wired) | serves **`:516` only** (SSL, reserved for the iHomeManager) — no plain-Modbus port to read locally |
| **k3s node `ubuntu-k8s`** | runs Home Assistant (container) |

Identified by scanning for who listens on 502 vs 516 and comparing register reads
(2026-08-07): `.116` and `.119` return the same inverter serial and are the *same*
WiNet-S on WiFi and Ethernet; `.168` mirrors a subset of the inverter map with
live values tracking `.119`, which is an energy manager re-serving what it
collects.

## The connection (the decision)

HA reads the inverter through the **WiNet-S dongle over native Modbus TCP**:

- **host `192.168.1.119`, port `502`, unit id `1`, `type: tcp`**
- **SSL/encryption OFF** on the WiNet-S — HA/pymodbus speaks *plain* Modbus TCP; Modbus-over-TLS is unsupported.
- Use the WiNet-S **wired Ethernet** uplink (more stable than WiFi); that same link carries the iSolarCloud connection.

Where the config lives (GitOps, no manual PVC drift): the mkaiser register package is vendored
at `gitops/home-assistant/packages/modbus_sungrow.yaml`, shipped as a **ConfigMap** and mounted
at `/config/packages` (chart `additionalVolumes` / `additionalMounts`), and included from
`configuration.templateConfig` in `values.yaml`.

## Why the WiNet-S and not the Waveshare (RS485 → COM2)

The Waveshare bridges the inverter's **COM2 RS485** to the network. The data path works (we
pulled live values through it), but under sustained polling it is **unstable on HA 2026.7 /
pymodbus 3.13**:

- transparent mode + `rtuovertcp` → RTU frame desync → pymodbus hangs HA startup;
- gateway mode (`Modbus TCP to RTU`) + `tcp` → flood of `ModbusIOException: Unable to decode request`.

That's the known cheap-serial-gateway framing problem. The **WiNet-S is a native Modbus TCP
device** — no RTU↔MBAP translation — so it's clean and stable. Chosen; the Waveshare is freed.

**Waveshare config learnings (if ever reused):** Work Mode must be **TCP Server**; gateway mode's
Instruction Timeout only persists with **Multi-host = Yes** and must be a **multiple of 32**
(e.g. 1024); transparent + `rtuovertcp` hung under load.

## Hardware gotchas

- **WiNet-S SSL must be off** for plain Modbus TCP.
- **Single Modbus client** on the WiNet-S — the iSolarCloud app can contend for it.
- The WiNet-S serves **a subset** of the inverter's registers (the internal LAN port / COM2 expose
  more). Registers it doesn't serve were trimmed from the package to stop error spam.
- The **EV charger (AC011E)** is **not** on this Modbus map. Port `516` on it is the channel the
  *iHomeManager dials into*, SSL-encrypted — not a port HA can usefully poll. For local charger
  data the realistic paths are the iHomeManager's own `:502` map or **evcc** — see
  [todo](../todo.md).
- The **WiNet-S is reachable twice** (wired `.119`, WiFi `.116`, same serial). Given it tolerates
  only one Modbus client, the WiFi interface is worth disabling.
- **iHomeManager settings to leave alone:** RS485 mode stays **collection** (the manager polling
  its own bus) and **O&M off** (Sungrow/installer remote access). Its manual is explicit that
  RS485 *and* WiNet must not both be used for the network connection (p.17), and that RS485 to the
  inverter is only required when feed-in power limitation is (p.18) — we are on the WiNet path.

## HA reliability

A `startupProbe` was added to the HA chart (`values.yaml`) so a slow modbus setup on boot can't
trip the liveness probe and crash-loop HA.

## Sources

- [mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant)
- [FAQ: connection problems ("Unable to decode request")](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant/wiki/FAQ:-Problems-with-the-connection)

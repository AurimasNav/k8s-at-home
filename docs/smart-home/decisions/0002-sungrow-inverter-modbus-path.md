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
| **k3s node `ubuntu-k8s`** | runs Home Assistant (container) |

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
- The **EV charger (AC011E)** is **not** on this Modbus map — it reports RS485 → inverter →
  iSolarCloud. For local EV-charger / heat-pump data the path is the **Sungrow iHomeManager**
  (Modbus TCP `:516`, SSL) or **evcc** — see follow-ups.

## HA reliability

A `startupProbe` was added to the HA chart (`values.yaml`) so a slow modbus setup on boot can't
trip the liveness probe and crash-loop HA.

## Sources

- [mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant)
- [FAQ: connection problems ("Unable to decode request")](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant/wiki/FAQ:-Problems-with-the-connection)

# 0002 — Sungrow inverter integration path

- **Status:** **Deployed 2026-07-24** — WiNet-S dongle, native Modbus TCP, lean sensor set, via GitOps
- **Scope:** Smart-home / IoT — solar / battery telemetry into Home Assistant
- **Related:** [0001 — NIBE F1245 integration path](0001-nibe-f1245-integration-path.md)

> **TL;DR:** Reading the SH15T over the **WiNet-S dongle (native Modbus TCP, `192.168.1.119:502`,
> unit 1, SSL off)** with a **lean ~15-sensor set** defined in git (`values.yaml` →
> `templateConfig`). Clean and stable — **zero framing errors**, sensors refresh every
> 15–60 s. The earlier **Waveshare RS485→ETH on COM2** attempt was abandoned: the data path
> worked but sustained polling destabilised the cheap gateway on pymodbus 3.13. The Waveshare
> is now free for another Modbus device.

## Context

Energy stack is all Sungrow: **SH15T** three-phase hybrid inverter, **SBR160** 16 kWh
battery, Sungrow EV charger. Goal: local telemetry in Home Assistant (k3s container),
no iSolarCloud dependency. Register map / definitions from
[`mkaiser/Sungrow-SHx`](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant)
(built-in HA `modbus`, no add-on/HACS). Transports considered: **WiNet-S dongle** (native
Modbus TCP), **internal LAN port** (not exposed on all models), **RS485→COM2** via a bridge.

## What we tried

### ❌ Waveshare RS485-to-ETH (B) on COM2 — abandoned
Data path proven (pulled firmware `PEARL-H_01011.01.31` + live values), but the sustained
poll destabilised the gateway on HA 2026.7 / pymodbus 3.13:

| Waveshare mode | HA `type` | Result |
|---|---|---|
| Transparent (`None`) | `rtuovertcp` | RTU framing desyncs → pymodbus `Repeating…` loop → **hangs HA startup** |
| Gateway (`Modbus TCP to RTU`) | `tcp` | Transaction-ID drift fixed via gateway serialization, but then a flood of `ModbusIOException: Unable to decode request` (malformed MBAP) → sensors freeze (~64/120 stale) |

`Unable to decode request` is the exact symptom in mkaiser's connection FAQ — a known
cheap-gateway + strict-pymodbus-3.13 issue. `message_wait` tuning (5→50→100 ms) and the
RS485 Conflict Time Gap did not resolve it.

### ✅ WiNet-S dongle — deployed
Native Modbus TCP → HA connects with plain `type: tcp`, **no gateway translation**, so none
of the framing flakiness. **14/15 sensors live, 0 decode errors, 0 drift.**

## Decision (deployed)

- **Transport:** WiNet-S at **`192.168.1.119:502`**, native Modbus TCP (`type: tcp`), unit **1**,
  **SSL/encryption OFF** (HA/pymodbus speaks plain Modbus TCP; Modbus-over-TLS is unsupported).
- **Scope:** **lean ~15-sensor set** (battery SoC/power/health, total DC power, daily+total PV,
  total active power, export power, load power, running state, inverter temp, daily+total
  import/export energy). Registers/types/scaling copied from mkaiser and validated live.
- **Location:** in git at `gitops/home-assistant/values.yaml` under
  `configuration.templateConfig` (a `modbus:` block). ArgoCD renders it into the
  `hass-configuration` configmap; the chart's init container merges it into the PVC
  `configuration.yaml`. **No manual PVC drift.**
- **Tuning:** `message_wait_milliseconds: 30` (WiNet is slower), `delay: 3`, `timeout: 10`.

### Why not the full ~120-sensor mkaiser set
Would need the big file vendored + a configmap volume mount, WiNet exposes a subset and is
slower, and heavier polling is riskier. Lean covers what we care about and is sturdy. Can
expand later if wanted.

## HA hardening (in git)

The pajikos chart had **no `startupProbe`** (liveness gives only ~60 s), so any slow/stalled
integration boot crash-looped the pod. Added a `startupProbe` (10-min boot grace) in
`values.yaml` (commit `13429ef`). Kept — general hardening.

## Notes / gotchas

- **`sensor.export_power`** uses `nan_value: 0x7FFFFFFF`, so it reads `unavailable` when
  **not exporting** (register returns "no value"). For an always-on signed grid figure, use
  **Meter active power** (reg 5600) instead.
- **`running_state`** is exposed raw (a status bitfield, e.g. `16384`); mkaiser decodes it to
  text via template sensors — add later if the friendly state is wanted.
- **Entity-registry orphans:** the earlier full-mkaiser attempt left ~105 `restored`/
  `unavailable` entities in the registry — harmless; bulk-delete under Settings → Entities.
- **WiNet-S single Modbus client** — if the iSolarCloud app polls at the same time, HA can
  get bumped. Prefer the dongle's **wired Ethernet**, not WiFi, for stability.

## Waveshare config learnings (kept for reference; device now free)

- Work Mode must be **TCP Server** (it shipped as TCP Client dialing `192.168.1.129:9999`,
  the NIBE nibegw port).
- Gateway mode: Protocol `Modbus TCP to RTU`; **Instruction Timeout** only persists with
  `Enable Multi-host = Yes` and must be a **multiple of 32** (`1024`, not `1000`).
- Transparent mode: Protocol `None` + HA `rtuovertcp` (hung under load).

## Future options

1. **Full mkaiser set** — vendor the file + configmap mount if you want all registers.
2. **Meter active power (5600)** for reliable signed grid flow; decode `running_state`.
3. **Battery charge/discharge energy** registers for the HA Energy dashboard battery lane.
4. **EV charger** likely on a separate Modbus map — not covered by the inverter link.

## Sources

- [mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant)
- [FAQ: connection problems ("Unable to decode request")](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant/wiki/FAQ:-Problems-with-the-connection)

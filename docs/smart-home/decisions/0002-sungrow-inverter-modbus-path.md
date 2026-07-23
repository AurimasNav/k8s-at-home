# 0002 — Sungrow inverter integration path

- **Status:** Attempted 2026-07-22 → **PAUSED / not in service** (2026-07-24)
- **Scope:** Smart-home / IoT — solar / battery / EV telemetry into Home Assistant
- **Related:** [0001 — NIBE F1245 integration path](0001-nibe-f1245-integration-path.md)

> **TL;DR:** The Waveshare RS485→ETH data path to the SH15T works (we pulled live
> values), but the full ~120-sensor `mkaiser` poll destabilises this cheap gateway on
> HA 2026.7 / pymodbus 3.13 (`Unable to decode request` flood → sensors freeze).
> Integration is **paused** and all manual config was removed. Revisit via a **lean
> sensor set** or the **WiNet-S dongle**, and do it **through GitOps** (see below).

## Context

Energy stack is all Sungrow: **SH15T** three-phase hybrid inverter, **SBR160** 16 kWh
battery, and a Sungrow EV charger. Goal: full, reliable telemetry in Home Assistant,
self-hosted, no iSolarCloud dependency. Community-standard integration:
[`mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant`](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant)
(YAML on HA's built-in `modbus` — no add-on/HACS needed; works on our k3s container HA).

Transports it supports: **WiNet-S dongle** (native Modbus TCP; some registers missing,
Modbus low-priority/flaky), **internal LAN port** (fast/full but not on all models),
**RS485 → COM2/logger terminals** via a bridge (full registers, needs an RS485↔network box).

We own a **Waveshare RS485-to-ETH (B)** (fw V1.523, `192.168.1.99`), freed from the NIBE
role (see [0001](0001-nibe-f1245-integration-path.md)), wired to the inverter's **COM2**.
It shares the single UTP drop near the NIBE; NIBE therefore runs on WiFi.

## Original decision (attempted)

Waveshare on COM2 → HA `modbus` via `mkaiser/Sungrow-SHx`. Serial **9600 8N1**, unit id
**1**, SBR battery device address **200**, battery max power ~**9600 W**.

## Outcome — what actually happened

**The path works; sustained heavy polling does not (on this hardware+stack).**

- ✅ Single reads succeed — pulled the inverter firmware string (`PEARL-H_01011.01.31`)
  and live values (battery **77 %**, total active power **621 W**, grid export/import **0**).
- ❌ The full `mkaiser` poll (~120 sensors) destabilises the gateway. Two modes tried:

| Waveshare mode | HA `type` | Result |
|---|---|---|
| **Transparent** (Protocol `None`) | `rtuovertcp` | RTU frame delimitation over the bridge desyncs under load → pymodbus stuck in `Repeating…` retry loop → **hangs HA startup entirely** |
| **Gateway** (`Modbus TCP to RTU`) | `tcp` | Transaction-ID drift fixed (see settings below), but then a flood of `pymodbus … ModbusIOException: Unable to decode request` (malformed MBAP frames) → sensors get one initial value then **freeze** (~64/120 stale) |

`Unable to decode request` is the exact symptom in [mkaiser's connection FAQ](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant/wiki/FAQ:-Problems-with-the-connection)
— a known cheap-gateway + strict-pymodbus-3.13 interaction. Neither `message_wait`
tuning (5→50→100 ms) nor the RS485 Conflict Time Gap resolved it.

## Waveshare config learnings (for whoever revisits)

- **Work Mode must be `TCP Server`** — it shipped from the NIBE experiment as `TCP Client`
  dialing `192.168.1.129:9999` (HA's nibegw port); useless for Modbus.
- **Gateway mode:** Protocol `Modbus TCP to RTU`. Multi-host **auto-enables** for this
  protocol; to make the **Instruction Timeout** persist you must set `Enable Multi-host = Yes`,
  and the value must be a **multiple of 32** (use `1024`; `1000` is silently rejected).
  Instruction Timeout `1024` is what killed the transaction-ID drift.
- **Transparent mode:** Protocol `None`, pair with HA `type: rtuovertcp` (but this hung — see above).
- HA side: `message_wait_milliseconds` of `5` caused a transaction-ID flood; `50`–`100`
  fixed the drift but not the decode errors. `delay: 3` is fine.

## HA hardening kept (in git)

The pajikos HA chart had **no `startupProbe`** — the liveness probe gives only ~60 s, so a
slow/stalled integration boot (exactly what modbus did) got the pod killed → crash loop.
Added a `startupProbe` (10-min boot grace) to `gitops/home-assistant/values.yaml`
(commit `13429ef`). **Kept** — it's general hardening, independent of Sungrow.

## Current state (paused)

- Integration **disabled**; HA runs purely from the git-managed config template.
- **All manual PVC edits removed** (`modbus_sungrow.yaml`, `secrets.yaml`, the
  `configuration.yaml` include) — no drift. This was the mistake to avoid repeating:
  the whole attempt was done as manual PVC edits, not GitOps.
- Waveshare left in **gateway mode** (Protocol `Modbus TCP to RTU`, Multi-host `Yes`,
  Instruction Timeout `1024`), physically wired to COM2 — just not polled.

## Future choices (when revisiting)

1. **Lean sensor set (try first).** Keep the Waveshare (full-register *access* retained);
   poll only ~15 core registers — battery SoC/power, PV power, grid import/export, load,
   daily + total energy. Drastically less framing stress; the usual way to make cheap
   gateways stable.
2. **WiNet-S dongle.** Point HA at the dongle's native Modbus TCP (enable Modbus on it).
   mkaiser's *recommended* transport — no gateway translation, so no decode errors.
   Fewer registers, dongle can be slower; frees the Waveshare.
3. **Internal LAN port** if the SH15T exposes one — fast + full, mkaiser's other recommended path.

**Do it via GitOps, not manual PVC edits.** Put the modbus YAML into the chart's
`configuration.templateConfig` or a git-managed package mounted via configmap, and any
connection params in a git-managed secret — so there's no manual drift to clean up.

## Sources

- [mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant)
- [FAQ: connection problems ("Unable to decode request")](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant/wiki/FAQ:-Problems-with-the-connection)

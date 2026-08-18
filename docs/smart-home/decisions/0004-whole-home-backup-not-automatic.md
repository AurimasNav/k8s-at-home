# 0004 — Whole-home backup does not transfer automatically

- **Status:** **RESOLVED 2026-08-18 — no electrician required.** The wiring was already
  correct; the ETI SSQ 340 changeover was simply parked in the grid-direct position. Moving
  the lever to the other position feeds the house through the inverter's `LOAD` output, and
  automatic islanding then works. See *Resolution* below.
- ~~**Status:** Diagnosed 2026-08-15 — cause identified, fix is electrician work~~
- **Scope:** *Why* the SH15T does not carry the house automatically during a grid outage, and what
  would have to change. Electrical work itself is out of scope for this repo — this exists so the
  evidence is written down when the installer is challenged.
- **Related:** [0002 — Sungrow integration path](0002-sungrow-inverter-modbus-path.md)

## Symptom

On a grid outage the house goes dark. Power is restored only by manually flipping a switch in the
meter box, after which the house runs from battery. The installer intended whole-home automatic
backup and did not achieve it; the manual switch is a fallback, not a design choice. There is no
critical-loads subpanel.

## What was measured

All figures from live Modbus reads and Home Assistant recorder history, not inference:

- **Backup mode is enabled.** `switch.backup_mode` = on (reg 13074 = 0xAA), backup reserve SoC 20%,
  battery well above it. Nothing is misconfigured on the inverter.
- **The backup output is live but idle.** Median **9 W**, while the house draws ~650 W idle. Across
  **516 hours** of history the backup output carried ≥60% of house load in **zero** hours.
- **It can carry the house.** During the manual switch on 2026-08-13 backup power tracked house load
  almost exactly (870 W vs 896 W; 616 vs 652) for ~70 minutes. Hardware and battery are fine.
- **The 2026-08-13 outage itself is not in the data** — HA runs on the k3s node, which is not backed
  up, so it died with the grid. Recorder shows a 15.2 min gap (10:23:10 → 10:38:21) and an HA start
  at 10:38:15, i.e. HA only returned *after* the manual switch. Everything recorded during that
  "backup window" is post-switch.

## Cause

**A manual changeover switch sits between the inverter's backup output and the house.**

- The AC connector under the inverter is labelled **`LOAD`** and **`GRID`**, and **both are wired** —
  the backup output was connected, so this is not a missing cable.
- The switch in the meter box is an **ETI SSQ 340**: a *center-off change-over switch*, 3-pole, 40 A,
  400 V, I-0-II. Its entire purpose is that a human selects the source.
- No inverter setting can operate that switch, which is why "make backup work" attempts failed while
  it remained in the load path.

The SH15T is designed to need no such switch: nameplate `AC-Backup` **43000 W / 63 A** pass-through,
15000 W / 15000 VA off-grid, with a **built-in 63 A bypass** and **<10 ms** transfer. The house is on
a **40 A** supply (SSQ 340 40 A, ETIMAT6 C40), comfortably inside the 63 A bypass — so capacity was
never the blocker either.

## What would have to change

Target topology, for the installer/electrician to implement and certify:

```
Grid ──▶ inverter GRID port ──▶ [internal 63 A bypass] ──▶ inverter LOAD port ──▶ house DB
```

- Feed the house distribution board **permanently** from `LOAD`. The inverter passes grid through in
  normal operation and opens its grid relay on failure, unattended.
- **Keep the SSQ 340, repurposed as a maintenance bypass** (grid-direct, past the inverter) so the
  house can still be powered if the inverter fails or needs service. It stops being the everyday path.
- **Neutral–earth bonding while islanded** must be handled — an off-grid inverter has to establish the
  N-PE reference, and getting it wrong breaks RCD protection downstream.
- Keep the **DTSU666 CTs on the grid side** so import/export metering still reads correctly.
- Verify the `LOAD`→DB cable is rated for the full 40 A.

## The EV charger problem this creates

The distribution board puts the **EV charger on a 25 A three-pole** breaker and the **heat pump on
another**. Whole-home backup takes both with it:

- Heat pump ~2 kW — fine.
- EV at 11 kW against a **15 kW off-grid limit** and ~12.8 kWh usable battery (16 kWh less the 20%
  reserve) — an outage mid-charge drains the entire reserve in roughly **an hour**, versus 15+ hours
  at house-only load.

So the rework must also decide one of:

- keep the EV circuit **upstream of the transfer point**, so it is simply dead during an outage; or
- have the iHomeManager **disable charging when off-grid** (charger enable is reg 8049 / 8048 on unit
  247 — currently vendored read-only, so this would mean deliberately adding a write path).

The first is simpler and fails safe. The second keeps the option of slow solar charging during a long
outage.

## Consequences / gotchas

- **HA is not on backup.** The k3s node dies with the grid, so monitoring, automations and logging are
  lost exactly when they are most wanted — and there is no record of what the inverter did during an
  outage. Putting the node and network gear on the backed-up side (or a small UPS) both fixes that and
  makes the next outage self-documenting, which is the evidence needed to prove the fix worked.
- **Unexplained detail:** the backup output reads a steady ~9 W and has peaked at 492 W with the
  changeover in the normal position, which should not happen if that port is open-circuit. Possibly
  measurement offset, possibly a small circuit tapped ahead of the switch. Worth asking about; not
  load-bearing for the diagnosis.
- This is mains work involving islanding and earthing. It belongs with a qualified electrician, and
  it is reasonable to hold the original installer to finishing what was intended.

## Sources

- [Sungrow SH15/20/25T datasheet V3](https://www.pvo-int.com/wp-content/uploads/2024/01/EN_DS_20231201_SH15_20_25T_Datasheet_V3_EN%EF%BC%88IEC%EF%BC%89.pdf)
  — backup ratings, 63 A bypass, <10 ms switch time
- [ETI SSQ 340 product page](https://www.etigroup.eu/products-services/002421435) and
  [SSQ series overview](https://www.etigroup.eu/media-center/eti-news/ssq-modular-changeover-switch)
  — center-off change-over switch, I-0-II
- Inverter nameplate, S/N A2511008987: `AC-Backup` 43000 W / 63 A, 15000 W / 15000 VA
- Measurements: HA recorder (`sensor.total_backup_power`, `sensor.load_power`) and direct Modbus
  reads on the iHomeManager, unit 247


## Resolution — 2026-08-18

The diagnosis was right about *what* was wrong (a manual changeover in the load path) but
wrong about the *remedy*: it assumed the load side needed re-terminating. It did not. The
`LOAD` output was already wired to the changeover, so flipping the lever was the whole fix.

Measured before and after, house otherwise unchanged:

| | grid-direct position | via inverter `LOAD` |
|---|---|---|
| Backup output total | **8 W** | **650 W** ≈ house load |
| Backup phases A/B/C | −1 / 5 / 4 W | 160 / 83 / 389 W |
| House load | 651 W | 651 W |

Then the grid breaker was opened deliberately to test islanding:

- **Transfer was seamless** — lights did not blink, HA and the k3s node did not reboot
  (0 restarts), consistent with the datasheet's <10 ms switch time. Flipping the lever
  *slowly* does drop power; flipping it quickly does not.
- **Genuinely islanded** — all three grid CTs read exactly `0 W` (they had read −49/−117/+193
  moments earlier), grid meter 0 W.
- **Ran on solar, not battery** — house 650 W while the battery still *charged* at −2050 W
  from 3 kW of PV; SoC rose through the test.
- **Inverter state register flipped**, which is the signal the automations now use:
  `sensor.running_state_raw` **16384 (0x4000) grid-connected → 4096 (0x1000) islanded**.

### Consequences

- **HA now survives an outage.** The k3s node is fed through `LOAD`, so Home Assistant stays
  up and can both act and record during a grid failure. That closes the "put the node on the
  backed-up side" item and makes the automations below possible at all.
- `automations/backup-power.yaml` blocks EV charging while islanded and restores it when the
  grid returns, and notifies on both transitions.
- The SSQ 340 keeps its value as a **manual bypass** — flip back to grid-direct to run the
  house straight from the grid if the inverter ever fails or needs service.

### Still untested

- **RCD operation while islanded** (the N-PE bonding question) — unproven, and not inferable
  from any register. Until tested with a plug-in RCD tester *while off-grid*, treat
  earth-fault protection as unknown in backup mode.
- **Behaviour under real load at night** — the test ran with PV exceeding house load. The
  stress case is no sun, battery near the 20% reserve.
- Island output measured ~221 V phase / 382 V line, a few percent below nominal. Fine, but
  worth watching on a longer outage.

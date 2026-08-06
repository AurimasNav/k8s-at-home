# NIBE F1245 — entities to keep enabled

Companion to [decision 0001](decisions/0001-nibe-f1245-integration-path.md).

## Why this list is a document and not config

The `nibe_heatpump` integration creates **~1535 entities**, nearly all disabled by
default. Which ones are enabled is stored in HA's `.storage/core.entity_registry`
on the config volume — runtime state owned by HA. There is **no YAML or GitOps
surface for it**: the only programmatic path is the admin-only WebSocket command
`config/entity_registry/update` (`disabled_by: null` to enable), which needs a
long-lived access token. Not worth automating for a one-time action, so the list
lives here instead.

**Don't enable everything.** Every enabled coil is polled over UDP through the
ESPHome gateway; a few dozen keeps updates responsive and the pump's RS485 bus
quiet.

## Finding them in the UI

The device page lists disabled entities by **display name** only, and NIBE's
names are terse (`Prio`, `Calc. Supply S1`), so both columns are given below —
the display name is what you click, the entity ID is what automations reference.

Easier than scrolling: go to *Settings → Devices & Services → **Entities***,
filter to the Nibe Heat Pump integration, enable "show disabled entities", and
**search the coil number** (e.g. `45001`). Every entity ID ends in its coil
address, so the number is unique and unambiguous — which the display names are
not. Select the matches and use *Enable selected*.

This only needs redoing if the HA config volume is rebuilt from scratch.

## Operation

| Shown in UI as | Entity ID | Why |
|---|---|---|
| Alarm | `sensor.alarm_45001` | Alarm code. The one that makes fault alerting possible. |
| Prio | `sensor.prio_43086` | What the pump is doing now (heat / hot water / off). |
| Compressor State EP14 | `sensor.compressor_state_ep14_43427` | Compressor running or not. |
| Int. el.add. Power | `sensor.int_el_add_power_43084` | Immersion heater kW — the expensive bit, and the first place a fault shows up as cost. |
| Degree Minutes (16 bit) | `number.degree_minutes_16_bit_43005` | Heat demand. F1245 uses the 16-bit register; if it reads nonsense use *Degree Minutes (32 bit)* / `number.degree_minutes_32_bit_40940`. |

## Temperatures

| Shown in UI as | Entity ID | Why |
|---|---|---|
| BT1 Outdoor Temperature | `sensor.bt1_outdoor_temperature_40004` | Outdoor temp. |
| BT1 Average | `sensor.bt1_average_40067` | Averaged outdoor temp — what the heat curve actually follows. |
| BT2 Supply temp S1 | `sensor.bt2_supply_temp_s1_40008` | Supply temp. |
| EB100-EP14-BT3 Return temp | `sensor.eb100_ep14_bt3_return_temp_40012` | Return temp; with supply gives delta-T. |
| Calc. Supply S1 | `sensor.calc_supply_s1_43009` | Target supply. Compared against BT2 it shows whether the pump is keeping up. |
| BT7 HW Top | `sensor.bt7_hw_top_40013` | Hot water top. |
| BT6 HW Load | `sensor.bt6_hw_load_40014` | Hot water charge sensor. |
| EB100-EP14-BT10 Brine In Temp | `sensor.eb100_ep14_bt10_brine_in_temp_40015` | Brine in. |
| EB100-EP14-BT11 Brine Out Temp | `sensor.eb100_ep14_bt11_brine_out_temp_40016` | Brine out — ground loop health, the key number on a GSHP. |

## Runtime counters

Useful over months rather than minutes.

| Shown in UI as | Entity ID |
|---|---|
| Compressor starts EB100-EP14 | `sensor.compressor_starts_eb100_ep14_43416` |
| Tot. op.time compr. EB100-EP14 | `sensor.tot_op_time_compr_eb100_ep14_43420` |
| Tot. HW op.time compr. EB100-EP14 | `sensor.tot_hw_op_time_compr_eb100_ep14_43424` |
| Tot. op.time add. | `sensor.tot_op_time_add_43081` |

## Pumps

| Shown in UI as | Entity ID |
|---|---|
| Supply Pump Speed EP14 | `sensor.supply_pump_speed_ep14_43437` |
| EP14-GP2 Brine Pump Status EP14 | `sensor.ep14_gp2_brine_pump_status_ep14_43439` |

## Controls (writable — needed by automations)

| Shown in UI as | Entity ID | Why |
|---|---|---|
| Temporary Lux | `select.temporary_lux_48132` | One-shot hot water boost, e.g. driven by PV surplus. |
| Hot water comfort mode | `select.hot_water_comfort_mode_47041` | Economy / normal / luxury. |
| Heat Offset S1 | `number.heat_offset_s1_47011` | Heat curve offset, for forecast- or tariff-driven nudging. |
| Max int add. power | `number.max_int_add_power_47212` | Cap the immersion heater. |

Writes go out on the gateway's write port (UDP 10000), so these depend on the
remote write port being configured correctly in the integration.

## Deliberately left disabled

The refrigerant-circuit sensors (*EB100-EP14-BT14 Hot Gas Temp*,
*EB100-EP14-BT15 Liquid Line*, *EB100-EP14-BT17 Suction*) are diagnostics —
enable them temporarily when investigating compressor behaviour, not
permanently. Everything else in the 1500 is either for accessories we don't have
(ERS/FLM ventilation, pool, cooling, systems S2-S8) or settings better changed on
the pump's own panel.

Enabled out of the box by the integration, no action needed: *Climate System S1*
(`climate.f1245_climate_system_s1`), *Hot Water*
(`water_heater.f1245_hot_water`), *Alarm Reset* (`button.f1245_alarm_reset`).

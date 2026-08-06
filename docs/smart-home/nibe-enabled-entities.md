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

Enable them from the device page: *Settings → Devices & Services → Nibe Heat
Pump → the "+N entities not shown" link → select → Enable*. This only needs
redoing if the HA config volume is rebuilt from scratch.

**Don't enable everything.** Every enabled coil is polled over UDP through the
ESPHome gateway; a few dozen keeps updates responsive and the pump's RS485 bus
quiet.

Entity IDs carry the coil address as a suffix, so they are stable.

## Operation

| Entity | Why |
|---|---|
| `sensor.alarm_45001` | Alarm code. The one that makes fault alerting possible. |
| `sensor.prio_43086` | What the pump is doing now (heat / hot water / off). |
| `sensor.compressor_state_ep14_43427` | Compressor running or not. |
| `sensor.int_el_add_power_43084` | Immersion heater kW — the expensive bit, and the first place a fault shows up as cost. |
| `number.degree_minutes_16_bit_43005` | Heat demand. F1245 uses the 16-bit register; if it reads nonsense use `number.degree_minutes_32_bit_40940`. |

## Temperatures

| Entity | Why |
|---|---|
| `sensor.bt1_outdoor_temperature_40004` | Outdoor temp. |
| `sensor.bt1_average_40067` | Averaged outdoor temp — what the heat curve actually follows. |
| `sensor.bt2_supply_temp_s1_40008` | Supply temp. |
| `sensor.eb100_ep14_bt3_return_temp_40012` | Return temp; with supply gives delta-T. |
| `sensor.calc_supply_s1_43009` | Target supply. Compared against BT2 it shows whether the pump is keeping up. |
| `sensor.bt7_hw_top_40013` | Hot water top. |
| `sensor.bt6_hw_load_40014` | Hot water charge sensor. |
| `sensor.eb100_ep14_bt10_brine_in_temp_40015` | Brine in. |
| `sensor.eb100_ep14_bt11_brine_out_temp_40016` | Brine out — ground loop health, the key number on a GSHP. |

## Runtime counters

Useful over months rather than minutes: `sensor.compressor_starts_eb100_ep14_43416`,
`sensor.tot_op_time_compr_eb100_ep14_43420`,
`sensor.tot_hw_op_time_compr_eb100_ep14_43424`, `sensor.tot_op_time_add_43081`.

## Pumps

`sensor.supply_pump_speed_ep14_43437`, `sensor.ep14_gp2_brine_pump_status_ep14_43439`.

## Controls (writable — needed by automations)

| Entity | Why |
|---|---|
| `select.temporary_lux_48132` | One-shot hot water boost, e.g. driven by PV surplus. |
| `select.hot_water_comfort_mode_47041` | Economy / normal / luxury. |
| `number.heat_offset_s1_47011` | Heat curve offset, for forecast- or tariff-driven nudging. |
| `number.max_int_add_power_47212` | Cap the immersion heater. |

Writes go out on the gateway's write port (UDP 10000), so these depend on the
remote write port being configured correctly in the integration.

## Deliberately left disabled

The refrigerant-circuit sensors (`BT14 hot gas`, `BT15 liquid line`,
`BT17 suction`) are diagnostics — enable them temporarily when investigating
compressor behaviour, not permanently. Everything else in the 1500 is either for
accessories we don't have (ERS/FLM ventilation, pool, cooling, systems S2-S8) or
settings better changed on the pump's own panel.

Enabled out of the box by the integration, no action needed:
`climate.f1245_climate_system_s1`, `water_heater.f1245_hot_water`,
`button.f1245_alarm_reset`.

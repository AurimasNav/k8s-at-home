# Sensor selection — parts to avoid and what to use instead

Reference list of seven common hobby sensors that fail once they leave the breadboard, and the
parts to reach for instead. Sourced from Predictable Designs' *Production-Grade Sensor Swap List*
(John Teel, doc SNS-07 rev A) — see [Sources](#sources).

Consult this **before** picking a sensor for a new node. Every part in it works fine on a bench,
which is exactly what makes each one a trap.

## At a glance

| If a design uses | Start the search with | The core problem |
|---|---|---|
| DHT11 / DHT22 | Sensirion SHT40 or SHT41 | Accuracy and consistency |
| HC-SR04 ultrasonic module | ST VL53 time-of-flight family | Integration and drift |
| HC-SR501 PIR module | Panasonic EKMC / EKMB series | Production repeatability |
| MQ-2 / MQ-135 gas sensors | Sensirion SGP40 / SCD40 | Power and calibration |
| MPU-6050 IMU | TDK ICM-42688-P | Discontinued part |
| CdS photoresistor (LDR) | Vishay VEML7700 | RoHS compliance |
| ACS712 module on mains | Shunt + TI AMC1302 isolated amp | Shock and fire safety |

Each entry below lists alternates — read the full entry before locking in a part.

## DHT11 and DHT22 — temperature and humidity

**Why it fails:** accuracy is only rated to ±2°C and humidity is worse, so two sensors from the same
batch will noticeably disagree. The nonstandard one-wire protocol has strict timing requirements, so
readings randomly get corrupted. The parts come from countless factories of unknown origin. DHT22
shares the same protocol, variance, and supply-chain problems.

| Replacement | Why this part |
|---|---|
| **Sensirion SHT40 / SHT41** | Factory calibrated, standard I²C, accuracy specified across the full operating range |
| **TI HDC3020** | Another fully specified option from a major manufacturer |
| **Bosch BME280** | Adds barometric pressure sensing in the same package |

## HC-SR04 ultrasonic module — distance sensing

**Why it fails:** a 5V module in a 3.3V world, so it needs level shifting to connect safely to modern
MCUs. It is a large through-hole module rather than a component you place in your own layout. There
is no temperature compensation, and since the speed of sound changes with temperature, readings
drift with the weather. Condensation, fragile exposed transducers, and the acoustic changes caused
by an enclosure or grille shift readings further.

| Replacement | Why this part |
|---|---|
| **ST VL53 family** (VL53L1X, VL53L4CD) | Time-of-flight — excellent for short-range distance sensing |
| **MaxBotix ultrasonic sensors** | Not cheap, but the spec sheets are exactly what you are paying for |
| **Reflective IR sensor** | Does the job for pennies if all you need is basic presence detection |

Ultrasonic sensing itself is a perfectly valid approach — the problem is this specific two-dollar
module.

## HC-SR501 PIR module — motion detection

**Why it fails:** sensitivity and timing are set by two screw-adjusted trimpots, which cannot be
replicated consistently across units. Retrigger behavior is inconsistent, the onboard regulator has
its own quirks, and false triggers are a constant complaint — **especially with a WiFi antenna
nearby**. The module form factor has no place on a real board.

| Replacement | Why this part |
|---|---|
| **Panasonic EKMC series** | Clean digital output, specified sensitivity, range of lens options, solderable package |
| **Panasonic EKMB series** | Made for battery products — current draw of just a few microamps |
| **ST STHS34PF80** | True presence detection — still knows a person is there after they stop moving |

## MQ-series gas sensors (MQ-2, MQ-135) — gas and air quality

**Why it fails:** the internal heater burns roughly 750–900mW continuously, so a battery-powered node
is dead on arrival. They need a burn-in period of a day or more before readings settle, they drift
constantly over their lifetime, and they respond to practically every gas at once. Without
calibration against reference gases — which you cannot realistically do — the numbers mean almost
nothing.

**Never use these for a safety function.** Gas and carbon monoxide detection is a certified category
with strict standards, and that liability lands directly on you.

| Replacement | Why this part |
|---|---|
| **Sensirion SGP40 / SGP41** | Volatile organic compounds — the right choice for general air-quality sensing |
| **Sensirion SCD40 / SCD41** | Real CO₂ measurement in a tiny package |
| **SPEC Sensors / Figaro electrochemical cells** | An electrochemical cell is the way to detect one specific target gas |
| **Certified detection modules** | Costs more, but comes with the testing and documentation any safety claim demands |

## MPU-6050 IMU — motion tracking

**Why it fails:** the MPU-6050 actually works pretty well, which is what makes it sneaky. TDK
InvenSense flagged it not-recommended-for-new-designs years ago and the part is **discontinued**. A
lot of what ships on hobby modules today is remarked or outright counterfeit silicon, so every new
reel can behave differently and firmware tuning becomes guesswork.

| Replacement | Why this part |
|---|---|
| **TDK ICM-42688-P** | Modern successor from the same company, better specs and lower power |
| **Bosch BMI270** | Optimized for low power — strong choice for wearables and battery products |
| **ST LSM6 family** (e.g. LSM6DSO) | Wide range of options, mature software support, all actually in production |

When a manufacturer marks a part NRND, that part is living on borrowed time. The source cites a
design built around the 9-axis MPU-9250 that had to switch parts mid-development at EOL — a
non-trivial swap, because the replacement ran its data lines at 1.8V and needed level shifting added.

## CdS photoresistors (LDRs) — ambient light

**Why it fails:** the classic photoresistor is made from cadmium sulfide, and cadmium is restricted
under RoHS — so these cannot go into a product sold in the EU. Beyond compliance, they respond
slowly, nobody fully specifies which wavelengths they react to, and part-to-part variance is so wide
they barely qualify as sensors.

| Replacement | Why this part |
|---|---|
| **Vishay VEML7700** | Popular first choice, wide dynamic range |
| **TI OPT3001** | Designed to match the response of the human eye |
| **Lite-On LTR-303** | Compact, low-cost option |

All three give calibrated brightness readings in lux over I²C, are fully RoHS compliant, and cost
around a dollar or less in volume.

## ACS712 modules on mains voltage — AC current sensing

**Why it fails:** the Allegro chip itself is a legitimate part, but the hobby breakout board it sits
on is not. These modules route mains voltage through PCB traces with nowhere near the creepage and
clearance safety standards require. There is no reinforced isolation, and the screw terminals are
often completely exposed — a shock hazard, a fire hazard, and an automatic certification failure
rolled into one.

| Replacement | Why this part |
|---|---|
| **Shunt resistor + TI AMC1302** | Shunt plus isolated amplifier on your own board with proper spacing — the standard approach |
| **Current transformer** | Clamps around a wire, isolated by design, keeps mains off your board entirely |
| **Allegro ACS37800** | Newer part, made to be integrated correctly into your own layout |
| **Certified energy metering modules** | The safe option when you would rather buy the safety engineering than design it |

> **Safety.** Treat anything that touches mains voltage with the respect it deserves — this is the one
> category where a bad sensor choice can actually hurt somebody.

## The five production filters

Run any new sensor through these before committing to it:

1. **Accuracy** — the datasheet must specify performance across the full operating range, not just at
   a comfortable room temperature.
2. **Lifecycle** — the part must be in active production; anything marked NRND is living on borrowed
   time.
3. **Compliance** — every substance must clear RoHS and the regulations of every market involved.
4. **Power** — the sensor has to fit the power budget, which matters most in battery nodes.
5. **Safety** — anything touching mains, or making a safety claim, needs proper isolation, spacing,
   and certification behind it.

## How this applies here

The source is written for products going into manufacture, so two filters land softer in a homelab:
**compliance** (nothing here is sold, so RoHS is not a legal gate) and **production repeatability**
(a trimpot you adjust once on a one-off node is not the same problem as one adjusted across thousands
of units). The other three — accuracy, lifecycle, and especially **safety** — apply unchanged. Mains
safety arguably applies *harder* to DIY: there is no certification lab to catch the mistake.

Where this repo stands today — no node uses any of the seven:

- **Air quality** ([`features/air-quality-sen55.yaml`](../../src/smart-home/esphome/features/air-quality-sen55.yaml))
  — Sensirion SEN55, same family as the SGP40 recommended over the MQ-series, and it covers PM, VOC,
  NOx, temperature and humidity. It does **not** measure CO₂; if that is ever wanted, the answer is an
  **SCD40/SCD41**, not an MQ part.
- **Presence** ([`features/presence-mmwave.yaml`](../../src/smart-home/esphome/features/presence-mmwave.yaml))
  — HLK-LD2410C mmWave, which sidesteps the HC-SR501 entry entirely. Note the PIR module's
  false-triggering "especially with a WiFi antenna nearby" — directly relevant, since every node here
  is an ESP32 with a radio in the same enclosure.
- **Ambient light** — the LD2410C already reports a `light` value (in engineering mode), so no
  separate light sensor is fitted. Worth flagging against
  [ADR 0003](decisions/0003-bathroom-presence-radar-retrofit.md), which describes drilling a light
  pipe for "a photoresistor/ambient-light sensor": if that node ever gets a dedicated part rather
  than reusing the radar's reading, use a **VEML7700 / OPT3001 / LTR-303** over a CdS LDR — they are
  I²C and calibrated in lux, which also means no ADC pin and no per-node calibration curve.
- **Energy metering** — mains current is read from the Sungrow inverter over Modbus TCP
  ([ADR 0002](decisions/0002-sungrow-inverter-modbus-path.md)), so no current sensor is fitted at all.
  If per-circuit metering is ever added, the ACS712 entry above is the one to re-read first.

## Sources

- [Production-Grade Sensor Swap List (PDF)](https://predictabledesigns.com/sensor-swap-list.pdf) —
  Predictable Designs, John Teel, doc SNS-07 rev A
- [PredictableDesigns.com](https://predictabledesigns.com/)

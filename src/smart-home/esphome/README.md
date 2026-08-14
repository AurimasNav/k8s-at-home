# ESPHome Smart Home Node Configurations

This directory contains the ESPHome configurations for all microcontrollers (ESP32 family) deployed in the smart home.

Before picking a sensor for a new node, see [Sensor selection — parts to avoid and what to use instead](../../../docs/smart-home/sensor-selection.md).

## Directory Layout

- **`boards/`**: Reusable hardware profiles (chip variant, board model, framework settings, any onboard peripherals like the T-CAN485's RS485 transceiver enable pins).
- **`packages/`**: Common software configurations (WiFi, API/OTA, uptime/status sensors).
- **`features/`**: Modular integrations and protocols (NibeGW, mmWave presence, etc.), parameterized via substitutions.
- **`devices/`**: One file per physical node, composing a board + packages + feature(s) with real substitution values (pins, name, target IPs). `secrets.yaml` lives here too — see below for why.

A device is not self-contained; it's an assembly of the other three layers:

```yaml
packages:
  board: !include ../boards/esp32c6-supermini.yaml
  core: !include ../packages/core.yaml
  wifi: !include ../packages/wifi.yaml
  presence: !include ../features/presence-mmwave.yaml
```

## Getting Started

1. Copy the secrets template — **it must live in `devices/`**, not the repo root. ESPHome only resolves `!secret` against the directory of the file you invoke, plus (as a fallback) the directory of whatever included file references it — it does not walk up the tree:
   ```bash
   cp devices/secrets.yaml.example devices/secrets.yaml
   ```
2. Fill in `devices/secrets.yaml`: WiFi credentials, `ha_ip_address`, and generate real values for `api_encryption_key` (32-byte base64), `ota_password`, `ap_password`. It's gitignored — every device shares this one file.
3. Validate a config (fast, no compiler needed):
   ```bash
   esphome config devices/<name>.yaml
   ```

## Building firmware

**On Windows, `esphome compile`/`esphome run` fails natively** — ESP-IDF's toolchain requires MSYS, which ESPHome does not support on native Windows (`ERROR: MSys/Mingw is not supported`). Use the official Docker image instead:

```bash
docker run --rm -v "<path-to-this-esphome-dir>":/config esphome/esphome compile devices/<name>.yaml
```

The first compile per board target downloads and caches the ESP-IDF toolchain (~15-20 min); subsequent compiles reuse the cache (`.esphome/idf`, `.esphome/platformio` at this directory's root) and only take a few minutes. Compiling two different devices at once is fine once the toolchain is cached — it's not recommended for a cold cache, since both containers hit the same download step.

**Known quirk**: because the Docker container mounts this directory as `/config`, ESPHome's cache files (`devices/.esphome/idedata/<name>.json`, `devices/.esphome/storage/<name>.yaml.json`) can end up storing that container path. If a subsequent native `esphome upload` fails with a path like `\config\devices\...\firmware.factory.bin` not found, delete those two cache files and retry — the actual compiled binaries under `devices/.esphome/build/<name>/build/` are unaffected and don't need to be rebuilt.

## Flashing

**First flash must be over USB** (OTA needs firmware with WiFi+API already running, which doesn't exist yet). Do this with the natively-installed `esphome` CLI, not Docker — Docker Desktop on Windows can't reach host USB/serial devices:

```bash
esphome upload devices/<name>.yaml --device COM<N>
```

Find the port in Device Manager, or via PowerShell: `[System.IO.Ports.SerialPort]::GetPortNames()`. Boards with native USB (e.g. ESP32-C6/S3) enumerate as `USB\VID_303A&PID_1001...` — if it doesn't show up despite being plugged in, try unplugging/replugging (fixes most `CM_PROB_PHANTOM` states) or a different cable/port.

**Every flash after the first can go over WiFi** — no cable needed:

```bash
esphome upload devices/<name>.yaml --device <name>.local
```

## Viewing logs

Every board here sets `logger.baud_rate: 0` (see `packages/core.yaml`), which disables the raw serial log stream. Use the network/API log stream instead — works whether flashing was USB or OTA:

```bash
esphome logs devices/<name>.yaml --device <name>.local
```

## Active Nodes

- [`devices/nibegw.yaml`](devices/nibegw.yaml) — LilyGo T-CAN485 (plain ESP32, onboard RS485 transceiver) running NibeGW for Nibe F1245 heat pump integration. See [ADR 0001](../../../docs/smart-home/decisions/0001-nibe-f1245-integration-path.md). Flashed, powered off the pump's AA3-X4 12V, and verified forwarding pump telegrams to HA's node. At `192.168.1.149` (DHCP — needs a router reservation, since HA's `nibe_heatpump` integration is configured with this IP). Its onboard WS2812 is driven as a status LED: dark = no power, blue = booting, red = no WiFi, yellow = no HA API, green = connected.
- [`devices/presence-entrance-hall.yaml`](devices/presence-entrance-hall.yaml) — ESP32-C6 SuperMini + HLK-LD2410C mmWave presence sensor, entrance hall. Flashed and on the network. **Has an unflashed change pending** — `features/presence-mmwave.yaml` now retries the LD2410 engineering-mode enable on an interval instead of once at boot, because a cold start races the radar and loses the command (which is how the 2026-08-13 outage killed the ambient-light reading). HA reconciles it every 5 min meanwhile; see [todo](../../../docs/smart-home/todo.md).

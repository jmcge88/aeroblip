# Flashing & setup guide

Everything needed to build, flash, provision, and troubleshoot a flight-info
display — dev bench or retail unit. Commands are PowerShell, run from the repo
root unless noted. PlatformIO is invoked as `python -m platformio` (no `pio`
on PATH needed).

## Prerequisites

```bash
pip install platformio pyserial
```

Device: Waveshare ESP32-S3-Touch-AMOLED-2.16 or ESP32-C6-Touch-AMOLED-2.16 on
USB (shows up as a COM port, usually `COM5`; on macOS `/dev/cu.usbmodem*`).
Opening the serial port auto-resets the board — that's normal.

## Build environments (esp32/platformio.ini)

| Env (S3 / C6) | Server baked in | PRODUCT_BUILD | Use for |
|---|---|---|---|
| `amoled216` / `amoled216-c6` | `http://192.168.1.100:8000` (LAN, example — set your server's IP) | no | personal/dev units |
| `product` / `product-c6` | `https://api.aeroblip.com` (TLS, pinned ISRG Root X1) | yes | retail units |
| `product-dev` / `product-dev-c6` | `http://192.168.1.100:8000` (LAN, example) | yes | testing product behaviour on the bench |

`PRODUCT_BUILD` means: photos default OFF, OTA self-update enabled, pinned CA
for HTTPS. Every build lets the owner override the server URL in the portal.
Firmware version comes from `fw_version` in the `[common]` section — bump it
before an OTA release.

### The two boards

Same 480x480 CO5300 AMOLED, CST9220 touch, AXP2101 PMU, QMI8658 IMU and
ES8311 codec; the firmware picks the GPIO map from the chip it's built for
(`esp32/src/pin_config.h`). What differs on the **ESP32-C6** board:

- **No PSRAM** (328 KB of heap total), so the `-c6` envs build with
  `NO_FRAMEBUFFER`: the UI draws straight to the panel instead of through a
  460 KB canvas. Repaints are visible rather than page-flipped, aircraft
  photos and airline logos (PSRAM-only buffers) are skipped, and IMU
  auto-rotation is off (the panel stays upright).
- **Keys**: BOOT is GPIO9. Holding it at *power-on* enters ROM download mode
  (that's the chip, not us) — to force the setup portal, hold BOOT once the
  CONNECTING splash is up instead. The side KEY button's GPIO isn't in any
  vendor example yet; it's unmapped (`KEY_USER -1`). To find it, open a
  serial monitor, hold KEY and send `GPIOS` — the spare pin reading `0` is
  it — then build with `-DKEY_USER=<n>`.
- **No speaker-amp enable pin**; the codec drives the speaker directly.
- Single core: the network task and UI loop share one CPU. Watch the heap
  figures the boot log prints (`[boot] setup done, heap ...` and
  `[ws] connected, heap ...`). After TLS the largest free block is ~100 KB;
  the websocket library needs roughly 2x a frame's size to receive it, so
  devices ask the server for `rows=10` on the airport board (the full BNE
  board is ~44 KB and aborted the C6 with `bad_alloc` every connect). A
  server older than 0.10.5 ignores that parameter - upgrade the server
  before putting a C6 on it.
- OTA: the device asks `/api/fw/latest?variant=esp32c6` and the server only
  ever answers with a C6 image (see *Publishing an OTA release*).

## Dev flash (bench unit, no provisioning)

```bash
cd esp32; python -m platformio run -e amoled216 -t upload --upload-port COM5
```

Optional but recommended — mirror the build into the second OTA slot so the
crash-loop rollback always has somewhere to land (also resets the OTA boot
selector so what you just flashed is what runs):

```bash
python $env:USERPROFILE\.platformio\packages\tool-esptoolpy\esptool.py --chip esp32s3 --port COM5 --baud 460800 erase_region 0xe000 0x2000
python $env:USERPROFILE\.platformio\packages\tool-esptoolpy\esptool.py --chip esp32s3 --port COM5 --baud 460800 write_flash 0x650000 esp32\.pio\build\amoled216\firmware.bin
```

(`tools/flash_product.py` does both steps automatically.)

## Retail unit — one command per device

```bash
python tools\flash_product.py --port COM5 --name batch1-003 --server https://api.aeroblip.com --admin-token <ADMIN_TOKEN>
```

ESP32-C6 board: add `--env product-c6` (the script derives the esptool chip
and the release filename from the env name). `--port` defaults to `COM5` on
Windows and to the first `/dev/cu.usbmodem*` / `/dev/ttyACM*` elsewhere.

What it does, in order:

1. builds the `product` env (`--skip-build` to reuse the last build,
   `--env product-dev` to target the LAN server instead)
2. flashes over USB, then mirrors the firmware into the second OTA slot and
   clears the otadata selector
3. generates a unique device token and provisions it over serial
4. waits for the boot banner as a smoke test
5. registers the token with the server (needs `--server` + `--admin-token`)
6. appends the unit to `tools/devices_manifest.csv` (gitignored — it holds
   tokens; back it up somewhere private)

**Provision an already-flashed device** (skip build and flash, just token +
registration — e.g. after enabling `REQUIRE_DEVICE_TOKEN` on an existing unit):

```bash
python tools\flash_product.py --no-flash --port COM5 --name bench-dev --server http://192.168.1.100:8000 --admin-token <ADMIN_TOKEN>
```

## Publishing an OTA release

```bash
cd esp32; python -m platformio run -e product; cd ..
python tools\flash_product.py --release
```

Copies the build to `fw/product-s3-<version>.bin` and rewrites
`fw/manifest.json`. `fw/` is tracked in git, so deploying a release to prod is
commit + push + `git pull` on the server (docker-compose mounts `fw/`
read-only, no container rebuild needed). Devices check `/api/fw/latest` on
boot and daily, and self-update when the version differs from theirs.
**Bump `fw_version` in platformio.ini `[common]` first** or devices will see
"same version" and skip it.

Two boards, one manifest: release each board's build separately —

```bash
cd esp32; python -m platformio run -e product; python -m platformio run -e product-c6; cd ..
python tools\flash_product.py --release                    # -> fw/product-s3-<ver>.bin, top-level + variants.esp32s3
python tools\flash_product.py --release --env product-c6   # -> fw/product-c6-<ver>.bin, variants.esp32c6
```

The manifest keeps a per-chip entry under `variants`; devices ask
`/api/fw/latest?variant=<chip>` and get their own entry or a 404 — never the
other board's image. Pre-variant S3 units (≤ 0.10.3) send no `variant` and
read the top-level fields, which the S3 release keeps updating. Releasing
only one board leaves the other's entry untouched.

Safety net: 3 consecutive crash reboots (panics/watchdogs, not power cycles)
without a minute of stable running flips the device back to the previous
firmware in the other slot. Kill switch: delete `fw/manifest.json` to stop a
rollout. Soak every release on the bench unit for a day before `--release`.

## Serial provisioning protocol (115200 baud)

| Command | Reply | Purpose |
|---|---|---|
| `PROVISION <token>` | `PROVISIONED <token>` | store the device token in NVS |
| `DEVINFO` | `DEVINFO fw=... mac=... token=set|unset server=... board=... variant=... heap=...` | identity check |
| `GPIOS` | `GPIOS 10=1 14=1 18=0` | levels of the unmapped GPIOs (pull-ups on) — find the C6 KEY button |
| `REBOOT` | `REBOOTING` | restart |

Note: the MAC reads as zeros until WiFi comes up (~2 s after boot) — query
DEVINFO again if you need it.

## Server flags (.env — restart with `docker compose up -d --build` after code changes, `docker compose up -d` after .env-only changes)

| Flag | Dev | Hosted product | Meaning |
|---|---|---|---|
| `PRODUCT_MODE` | `false` | `true` | commercially-licensed data sources only (adsb.lol + AeroDataBox, no photos) |
| `REQUIRE_DEVICE_TOKEN` | `false` | `true` | 403 all data endpoints + websocket without a registered token |
| `ADMIN_TOKEN` | any secret | strong secret | protects `/admin`, device registration and fleet listing |
| `MAX_LOCATIONS` | `50` | sized to fleet | cap on concurrent per-location poll loops |
| `AERODATABOX_API_KEY` | free tier | paid plan | board always; metadata/routes in product mode |
| `LOGO_API_KEY` | *(blank)* | logostream key | enables the cached `/api/logo/{iata}` upstream |

## Who sends which token

- **Devices** send `X-Device-Token` (baked in at flash time) on every request
  and the websocket — automatic, nothing to configure.
- **Browsers/tablets** can't set headers: append `?token=<any registered
  token>` to the dashboard URL, or paste it into the `⌖` location panel
  (saved in the browser). Only needed when `REQUIRE_DEVICE_TOKEN=true`.
- **Admins** send `X-Admin-Token` — used by `/admin`, the flash script, and
  the register/list endpoints.

## Device gestures

| Action | Effect |
|---|---|
| Hold either side key 3 s (while running) | open the setup portal (QR screen) |
| Hold USER key during the "connecting" splash | open the setup portal at boot |
| Side keys short-press / horizontal swipe | switch pages |
| Vertical swipe | device-info screen (fw version, IP, settings URL) |
| `/param` page → Reboot / Factory reset | reset keeps the device token, wipes everything else |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "WAITING FOR DATA" forever | `REQUIRE_DEVICE_TOKEN=true` but the device has no registered token (server logs show 403s / ws close 4403) | provision it: `flash_product.py --no-flash ...` |
| Dashboard stuck "reconnecting…" | same, browser has no token | add `?token=...` to the URL or the `⌖` panel |
| Splash "FLIGHT INFO / CONNECTING" flashing forever | firmware crash loop (each flash is a reboot) | capture serial at 115200 for the backtrace; on OTA'd units rollback kicks in after 3 crashes |
| C6 unit: `abort()` right after `[ws] connected`, 3 times, then rollback, then OTA, forever | out of memory receiving a websocket frame - the server is sending the full airport board | upgrade the server to ≥ 0.10.5 so it honours the device's `rows=` cap |
| Black screen after holding a button at power-on | that was BOOT (GPIO0) — chip is in ROM download mode | unplug, replug without holding anything |
| USB flash "succeeds" but old firmware still runs | otadata still points at the other OTA slot | `esptool erase_region 0xe000 0x2000` (the flash script does this) |
| Wrong city pair on a spotlighted flight | stale adsbdb route the board couldn't correct | expected for callsign≠flight-number carriers in dev mode; product mode resolves live |
| Board empty for a new airport | cache created on first request, AeroDataBox fetch takes ~10-30 s | wait and re-poll |
| No photos | photos are off by default everywhere (planespotters is personal-use) | owner opt-in: `/param` → "Aircraft photos" |
| adsb.lol 429/420 in logs | too many location pollers vs. `POLL_SECONDS` | raise `POLL_SECONDS`, lower `MAX_LOCATIONS` (a global throttle already spaces calls) |

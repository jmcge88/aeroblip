# Flashing & setup guide

Everything needed to build, flash, provision, and troubleshoot a flight-info
display — dev bench or retail unit. Commands are PowerShell, run from the repo
root unless noted. PlatformIO is invoked as `python -m platformio` (no `pio`
on PATH needed).

## Prerequisites

```bash
pip install platformio pyserial
```

Device: Waveshare ESP32-S3-Touch-AMOLED-2.16 on USB (shows up as a COM port,
usually `COM5`). Opening the serial port auto-resets the board — that's normal.

## Build environments (esp32/platformio.ini)

| Env | Server baked in | PRODUCT_BUILD | Use for |
|---|---|---|---|
| `amoled216` | `http://192.168.1.100:8000` (LAN, example — set your server's IP) | no | personal/dev units |
| `product` | `https://api.aeroblip.com` (TLS, pinned ISRG Root X1) | yes | retail units |
| `product-dev` | `http://192.168.1.100:8000` (LAN, example) | yes | testing product behaviour on the bench |

`PRODUCT_BUILD` means: photos default OFF, OTA self-update enabled, pinned CA
for HTTPS. Every build lets the owner override the server URL in the portal.
Firmware version comes from `-DFW_VERSION` in the `[base]` section — bump it
before an OTA release.

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

Copies the build to `fw/product-<version>.bin` and rewrites
`fw/manifest.json`. `fw/` is tracked in git, so deploying a release to prod is
commit + push + `git pull` on the server (docker-compose mounts `fw/`
read-only, no container rebuild needed). Devices check `/api/fw/latest` on
boot and daily, and self-update when the version differs from theirs.
**Bump `FW_VERSION` in platformio.ini first** or devices will see "same
version" and skip it.

Safety net: 3 consecutive crash reboots (panics/watchdogs, not power cycles)
without a minute of stable running flips the device back to the previous
firmware in the other slot. Kill switch: delete `fw/manifest.json` to stop a
rollout. Soak every release on the bench unit for a day before `--release`.

## Serial provisioning protocol (115200 baud)

| Command | Reply | Purpose |
|---|---|---|
| `PROVISION <token>` | `PROVISIONED <token>` | store the device token in NVS |
| `DEVINFO` | `DEVINFO fw=... mac=... token=set|unset server=...` | identity check |
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
| Black screen after holding a button at power-on | that was BOOT (GPIO0) — chip is in ROM download mode | unplug, replug without holding anything |
| USB flash "succeeds" but old firmware still runs | otadata still points at the other OTA slot | `esptool erase_region 0xe000 0x2000` (the flash script does this) |
| Wrong city pair on a spotlighted flight | stale adsbdb route the board couldn't correct | expected for callsign≠flight-number carriers in dev mode; product mode resolves live |
| Board empty for a new airport | cache created on first request, AeroDataBox fetch takes ~10-30 s | wait and re-poll |
| No photos | photos are off by default everywhere (planespotters is personal-use) | owner opt-in: `/param` → "Aircraft photos" |
| adsb.lol 429/420 in logs | too many location pollers vs. `POLL_SECONDS` | raise `POLL_SECONDS`, lower `MAX_LOCATIONS` (a global throttle already spaces calls) |

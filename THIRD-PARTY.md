# Third-party software and data

This project is licensed under the [GNU AGPL-3.0](LICENSE). It also includes or
depends on the work below. All of these licences are compatible with AGPL-3.0 as
inbound dependencies.

## Vendored in this repository

| Component | Where | Licence | Notes |
|---|---|---|---|
| Leaflet 1.9.4 | `static/vendor/` | BSD-2-Clause | (c) 2010–2023 Volodymyr Agafonkin, (c) 2010–2011 CloudMade. Licence preserved in the `@preserve` header of `leaflet.js`; provenance and SRI hashes in `static/vendor/README.md`. |
| Roboto (glyph data) | `esp32/src/fonts/roboto_s*.h` | Apache-2.0 | (c) 2011 Google Inc. Rasterised into Adafruit-GFX font tables by `tools/gen_fonts.py`; attribution retained in each generated header. |
| ES8311 codec driver | `esp32/src/es8311.{c,h}`, `es8311_reg.h` | Apache-2.0 | (c) 2015–2022 Espressif Systems. `SPDX-License-Identifier` headers intact. |

## Server dependencies (Python)

| Package | Licence |
|---|---|
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| httpx | BSD-3-Clause |
| Pillow | MIT-CMU (HPND) |

## Firmware dependencies (PlatformIO)

| Library | Licence | Notes |
|---|---|---|
| ArduinoWebsockets | **GPL-3.0** | The strongest constraint on this project. Because the firmware links it, the firmware must be GPL-3.0 or AGPL-3.0 — no permissive or source-available licence is possible without replacing it first. |
| GFX Library for Arduino (Arduino_GFX) | BSD-2-Clause | Derived from Adafruit_GFX, (c) 2012 Adafruit Industries. The published PlatformIO package omits the licence file; the terms are in `license.txt` in the upstream repository. |
| ArduinoJson | MIT |  |
| WiFiManager | MIT |  |
| JPEGDEC | Apache-2.0 |  |
| SensorLib | MIT |  |
| XPowersLib | MIT |  |
| QRCode | MIT |  |
| Arduino core for ESP32 (pioarduino) | LGPL-2.1 / Apache-2.0 | Espressif toolchain and framework. |

## Data sources

Licensing of *data* is separate from licensing of code, and some of it restricts
what a commercial deployment may use. See [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md)
for the full breakdown; in summary:

| Source | Used for | Licence / terms |
|---|---|---|
| [adsb.lol](https://adsb.lol) | Aircraft positions, global squawk-7700 watch | ODbL — **attribution required**, and it is displayed in the web footer, on the device, and in `/api/config`. Rate limits are dynamic; please [feed a receiver](https://adsb.lol/feed/). |
| [adsb.fi](https://adsb.fi) | Position fallback | Non-commercial terms — excluded when `PRODUCT_MODE=true`. |
| [VRS standing-data](https://github.com/vradarserver/standing-data) | Routes, airlines, airports, airframes | CC0-1.0. |
| [adsbdb](https://adsbdb.com) | Routes/airframes/photos in personal mode | Not licensed for commercial redistribution — excluded when `PRODUCT_MODE=true`. |
| planespotters.net (via adsbdb) | Aircraft photos | Non-commercial. Off by default; opt-in per device. |
| [AeroDataBox](https://aerodatabox.com) | Airport arrivals/departures board | Commercial API, paid plan. |
| [BigDataCloud](https://www.bigdatacloud.com) | Reverse geocoding for 7700 alerts | Free client tier. |
| CARTO basemaps | Map tiles on the emergency view | CARTO terms; OpenStreetMap data (ODbL). |

If you deploy this, the data licences are yours to honour — keep the ODbL
attribution visible.

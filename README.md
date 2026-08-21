# flight-info

Wall-tablet flight board: shows aircraft flying over your house and switches
to your local airport's departures/arrivals board when the sky is quiet.
Also drives a companion **ESP32-S3 AMOLED desk display** — see
[esp32/README.md](esp32/README.md).

## Views (auto-switching)

1. **Spotlight** — a flight is inside the overhead ring (default 5 NM):
   big callsign, airline logo + name, route (codes and cities), aircraft
   model/registration, altitude/speed/heading/distance, a photo of the actual
   airframe (when available), and a live mini radar map. When several planes
   are overhead the spotlight sticks to the first one (no flip-flopping),
   shows a "+N MORE" tag, and draws the others dim on the map. When the
   highlighted plane exits the ring it hands over to the next overhead plane
   within one poll (~5 s); if none remain it lingers 15 s, then drops back to
   the nearby-traffic view.
2. **Nearby traffic** — anything within the area radius (default 60 NM):
   card list with logos, routes, altitude/speed/distance, climb/descent
   phase, direction arrows, and an amber **"OVERHEAD IN m:ss"** countdown for
   flights that will cross the overhead ring.
3. **Airport board** — when the sky is clear: departures/arrivals for your
   airport as separate pages, with airline logos and status colours.
4. **Emergency (squawk 7700)** — a global watch polls the aggregators'
   squawk-7700 endpoint every 60 s, so emergencies show up wherever they are
   in the world: airline, route, aircraft, altitude/speed/heading, reverse
   geocoded location, and a live map. A new 7700 takes over the screen for
   2 minutes (footer shows the remaining hold), then joins the normal page
   rotation until it clears. A 7700 inside your area radius stays pinned.

### Page rotation & manual control

Pages (emergency / air / departures / arrivals) rotate every 30 s with a
"`>> Ns`" countdown and clickable dots in the footer; swipe left/right on a
touchscreen to change pages. A manual choice holds its slot before automatic
rotation resumes — including during an emergency takeover, which reclaims the
screen after your slot expires. When a flight is overhead the spotlight pins;
an active alert and an overhead flight alternate every 15 s.

Data sources:

- **Radar** — free community ADS-B aggregators, no API key:
  [adsb.lol](https://adsb.lol) (default) with auto-fallback to
  [adsb.fi](https://adsb.fi). adsb.lol rate-limits at roughly 1 req/10 s, so
  calls to each aggregator are spaced globally and the poller sticks to
  whichever source last worked. Please
  [feed a receiver](https://adsb.lol/feed/) if you can — these are volunteer
  networks and this project is a pure consumer of them.
- **Global 7700 watch** — adsb.lol's squawk endpoint, every 60 s. No fallback
  source exists for this one (see [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md)).
- **Enrichment** — routes, airlines and aircraft details/photos from
  [adsbdb.com](https://adsbdb.com), cached per flight. In product mode these
  come from [VRS standing-data](https://github.com/vradarserver/standing-data)
  (CC0) synced into a local SQLite database instead (no photos).
- **Airport board** — [AeroDataBox](https://aerodatabox.com) FIDS, cached and
  refreshed every 20 min (free tier friendly), paused overnight.

## Run

```sh
cp .env.example .env    # edit lat/long, airport, key
docker compose up -d --build
```

The container runs as uid 10001, so on Linux make the bind-mounted state
directory writable by it once (Docker Desktop on Windows/macOS needs nothing):

```bash
sudo chown -R 10001:10001 ./data
```

Open `http://<host>:8000` on the tablet (binds 0.0.0.0, reachable on your LAN).

## Demo mode

Set `DEMO_MODE=true` to fabricate realistic overhead traffic and board data —
no API keys needed. Two buttons appear in the footer:

- **SIMULATE FLYOVER** — a Singapore Airlines A350 spawns 6 NM out, crosses
  overhead ~10 s later (triggering the spotlight), and exits after ~2 min.
  Press it repeatedly for multiple simultaneous flyovers.
- **SIMULATE 7700** — a mid-Tasman Air New Zealand 787 squawks 7700 for
  5 minutes: full emergency takeover, 2-minute hold countdown, then demotion
  into the page rotation.

When `DEMO_MODE=false`, data is **never** made up: without a board key the
board honestly shows "NO BOARD DATA".

## Viewing a different location (web/tablet)

The server polls one sky per distinct location; any browser view can watch any
of them. Two ways to set it:

- **Location panel**: tap the `⌖` button in the footer — paste coordinates
  ("-33.8688, 151.2093", exactly what Google Maps copies on long-press), or use
  **Use my location** (browser GPS; needs HTTPS or localhost, so on plain LAN
  HTTP paste instead). Saved in the browser (localStorage), so a kiosk tablet
  keeps its location across restarts.
- **URL query string** (shareable/kiosk-pinnable, wins over saved settings):

```text
http://<server>:8000/?lat=-33.8688&lon=151.2093&radius=5&area=60&airport=SYD
```

| Param | Range | Purpose |
|---|---|---|
| `lat` / `lon` | ±90 / ±180 | view centre (both required, else server default) |
| `radius` | 1–30 NM | overhead spotlight ring |
| `area` | 10–250 NM | nearby-traffic radius |
| `airport` | IATA or ICAO | arrivals/departures board |

Precedence: URL query > saved panel settings > server `.env` defaults. The
ESP32 display has the same settings in its WiFi portal, sent the same way.
Each distinct location costs one upstream poll loop (see `MAX_LOCATIONS`).

## Test / debug URLs

| URL | Purpose |
|---|---|
| `/?view=spotlight` | force the spotlight view (shows nearest aircraft) |
| `/?view=nearby` | force the nearby-traffic list |
| `/?view=board` | force the airport board |
| `/?lat=&lon=&radius=&area=&airport=` | view another location (see above) |
| `/admin` | device-fleet admin page (needs `ADMIN_TOKEN` set) |
| `/locate` | phone GPS helper for portal setup (HTTPS only) |
| `/api/health` | health + product-mode flag |
| `/api/overhead` | raw aircraft snapshot JSON (accepts location params) |
| `/api/board` | cached board JSON (accepts `airport=`) |
| `/api/alerts` | current global squawk-7700 aircraft (accepts `lat`/`lon`) |
| `/api/config` | server default config + data attribution |
| `/api/logo/{iata}` | cached airline logo |
| `/api/fw/latest` | OTA manifest (404 until a release is published) |
| `POST /api/demo/flyover` | spawn a demo flyover (400 unless `DEMO_MODE=true`) |
| `POST /api/demo/emergency` | spawn a demo 7700 (400 unless `DEMO_MODE=true`) |

The footer status line shows the active provider, overhead/nearby counts and
last update time. Static assets are cache-busted with `?v=N` — bump the
version in [static/index.html](static/index.html) when editing JS/CSS.

## Run without Docker (dev)

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
$env:DEMO_MODE="true"; .venv\Scripts\uvicorn app.main:app --port 8001
```

## Tablet kiosk setup

- **Android**: install [Fully Kiosk Browser](https://www.fully-kiosk.com/) and
  set the start URL, or Chrome → menu → "Add to Home screen" and use a
  screen-on app.
- **iPad**: Settings → Accessibility → Guided Access, then open the page in
  Safari fullscreen.
- Keep the tablet plugged in and disable screen timeout. The page also
  requests a screen wake lock (works on HTTPS/localhost; on plain LAN HTTP
  use the kiosk app's screen-on setting).

## Configuration

All via `.env` — see [.env.example](.env.example). Key settings:

| Variable | Default | Purpose |
|---|---|---|
| `DEMO_MODE` | `false` | fabricate overhead + board data for demos; when false, data is never made up |
| `HOME_LAT` / `HOME_LON` | Brisbane NW | your coordinates |
| `OVERHEAD_RADIUS_NM` | `5` | spotlight radius: one flight, big display |
| `AREA_RADIUS_NM` | `60` | nearby-traffic radius, list display |
| `AIRPORT_ICAO` | `YBBN` | board airport |
| `ADSB_PROVIDER` | `adsblol` | radar source (`adsblol`, `adsbfi`) |
| `POLL_SECONDS` | `10` | radar poll interval — be kind to the free aggregators |
| `AERODATABOX_API_KEY` | *(empty = board shows no data)* | FIDS data key |
| `BOARD_QUIET_START/END` | `23` / `5` | skip board refreshes overnight |
| `PRODUCT_MODE` | `false` | hosted/commercial mode: commercially-licensed data sources only |
| `MAX_LOCATIONS` | `50` | cap on concurrently-polled device/view locations (LRU-evicted at the cap) |
| `MAX_AIRPORTS` | `20` | cap on concurrently-refreshed airport boards — this is an AeroDataBox spend limit |
| `REQUIRE_DEVICE_TOKEN` | `false` | gate data endpoints on provisioned device tokens |
| `ADMIN_TOKEN` | *(empty = admin disabled)* | protects `/admin` + device registration |
| `LOGO_URL_TEMPLATE` / `LOGO_API_KEY` | kiwi (personal) / logostream (product) | upstream for the cached `/api/logo/{iata}` |
| `FRAME_ANCESTORS` | *(empty = embedding blocked)* | space-separated origins allowed to iframe the dashboard, e.g. a Home Assistant dashboard - sent as `Content-Security-Policy: frame-ancestors`, superseding `X-Frame-Options: DENY` |

## API

Route and airframe lookups go through one process-wide cache persisted to
`data/meta_cache.json`, shared by every location and surviving restarts —
these are properties of a callsign or hex, not of a location, so the fleet
must never buy the same lookup twice. `GET /api/health` reports its hit rate.

Data endpoints accept optional `?lat=&lon=&radius=&area=&airport=` — each
distinct location gets its own poll loop (idle ones are reaped). With
`REQUIRE_DEVICE_TOKEN=true` they also require an `X-Device-Token` header.

- `GET /api/overhead` — aircraft currently within the area radius
- `GET /api/board` — cached arrivals/departures (`airport=` IATA or ICAO)
- `GET /api/alerts` — aircraft squawking 7700 worldwide (distances from `lat`/`lon`)
- `GET /api/config` — server default radii/airport + ODbL data attribution
- `GET /api/health` — health + product-mode flag
- `GET /api/logo/{iata}` — airline logo, cached server-side for 30 days
- `GET /api/fw/latest` — OTA manifest; firmware images under `/fw/`
- `POST /api/devices/register` — admin: register a device token (`X-Admin-Token`)
- `GET /api/devices` — admin: fleet list (also rendered at `/admin`)
- `POST /api/demo/flyover` — demo mode only: spawn a scripted flyover
- `POST /api/demo/emergency` — demo mode only: spawn a scripted 7700
- `WS /ws` — push updates; accepts the same location params (used by the frontend and devices)

## Licence

[GNU AGPL-3.0](LICENSE). In short: it's free software — use it, modify it, run it
at home, sell it if you want. But if you distribute a modified version, or run
one as a network service for other people, you have to give them the source to
your changes under the same licence.

The **name and logo are trademarks and are not covered by the AGPL** — forks
must rebrand. See [TRADEMARK.md](TRADEMARK.md).

Contributions require a CLA so the project can keep offering a paid hosted
option; see [CONTRIBUTING.md](CONTRIBUTING.md).

Third-party code and the (separate, stricter) **data source terms** are listed in
[THIRD-PARTY.md](THIRD-PARTY.md) and [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md).
The position data is ODbL: **keep the attribution visible**, and please
[feed a receiver](https://adsb.lol/feed/) if you run this at any scale.

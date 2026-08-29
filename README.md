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
5. **Following** — when you're following a flight (✈ in the footer), a page
   joins the rotation with a world map, great-circle route, progress bar and
   ETA. See [Follow a flight](#follow-a-flight).

Flights in good evening/morning light get a **☀ GOLDEN** tag (sun elevation
is computed locally), orbiting aircraft get a blinking **CIRCLING** badge,
the airport board carries the station's **METAR/TAF**, and a clear sky
advertises the next naked-eye **ISS pass**.

### Page rotation & manual control

Pages (emergency / air / departures / arrivals) rotate every 30 s with a
"`>> Ns`" countdown and clickable dots in the footer; swipe left/right on a
touchscreen to change pages. A manual choice holds its slot before automatic
rotation resumes — including during an emergency takeover, which reclaims the
screen after your slot expires. When a flight is overhead the spotlight pins;
an active alert and an overhead flight alternate every 15 s.

## Spotting log & stats

The server remembers what flies over (SQLite under `data/`): every flyover,
an all-time airframe **life list**, and thinned track points for the last few
days. Tap **▤** in the footer for the stats page: today's flyover count and
hourly histogram, busiest hour, top types/airlines/routes, rarest and newest
types ever seen, plus a 24-hour track heatmap with a time-sweep **replay**.
Stats are kept per location cell; demo mode logs to a separate database so
fake traffic never contaminates a real life list. Disable with
`SIGHTINGS_ENABLED=false`. Track points are purged after
`TRACK_RETENTION_HOURS` (default 72); flyovers and the life list are forever.

**MONTHLY WRAP** (button on the stats page, or `/api/wrapped?month=YYYY-MM`)
is the month-in-review: flyovers vs the month before, daily histogram,
busiest day/hour, first-ever types, rarest catch and the month's top
types/airlines/routes — browsable back through any month the log covers.
With `NTFY_URL` (or `WEBHOOK_URL`) set, the server also pushes last month's
wrap-up as a digest on the 1st of each month (from 09:00 local, sent once).

## Watch list & phone notifications (no Home Assistant needed)

Tap **◉** to manage server-side watch rules: aircraft type, airline, callsign
prefix, registration or hex — or the built-in detectors: **circling
aircraft** (orbiting helicopters, holding stacks), **any emergency squawk**,
**first-ever aircraft type** (from the spotting log), **military** /
**notable aircraft** (the aggregators' tar1090-style `dbFlags` — these also
get a red MIL/NOTABLE tag wherever they appear), or a **go-around at the
airport** (an aircraft established on approach to the board airport that
suddenly climbs away — detected from altitude/vertical-rate history against
the airport's standing-data coordinates). Modifiers: within N NM, overhead
only, golden light only. Matches:

- toast on every connected dashboard (and are spoken when voice is on);
- push to your phone via [ntfy](https://ntfy.sh) — set
  `NTFY_URL=https://ntfy.sh/<your-secret-topic>` on the server, install the
  free ntfy app, subscribe to the topic. No accounts, no Home Assistant;
- POST as JSON to a generic `WEBHOOK_URL` for everything else.

The same aircraft won't re-notify the same rule for 6 hours.

Watch rules (and follows, below) are namespaced per caller: each device
token — or an unauthenticated caller, when `REQUIRE_DEVICE_TOKEN` is off —
manages and sees only its own rules and matches. One shared poll loop still
does the actual detection work once per location regardless of how many
tokens have rules; only visibility is split.

## Follow a flight

Tap **✈** and enter a callsign. Type the IATA flight number as shown on a
boarding pass or Google Flights (`JQ59`) and the server figures out the rest:
if the airline code is unambiguous it corrects it immediately (`JQ` →
Jetstar's `JST`); if the code covers several real airlines under one brand
(`QF` alone covers six — Qantas mainline plus five QantasLink regional
partners) it can't guess safely, so instead it tries each real candidate
against live traffic, one per poll, and adopts whichever one is actually
flying right now — same idea as typing the ICAO form yourself (`QFA12`), just
automatic and safe against every airline with this problem, not only the
famous ones. A route can resolve even before any live position does (already
landed, not yet departed, a coverage gap) — position and route are looked up
independently.

The server tracks a follow anywhere in the world via adsb.lol's callsign
endpoint — follows are polled round-robin, one upstream request per minute
total, and dead-reckoned between polls. The FOLLOWING page shows a world map
with the dashed great-circle route, live position, progress bar, distance to
go and ETA. Landings are detected and announced; oceanic coverage gaps
honestly show "NO COVERAGE" with the last known position. Follows expire
after 24 h and are capped by `MAX_FOLLOWS` **per token**, not fleet-wide.

Follows also raise **alerts**, toasted/spoken on dashboards and pushed via
the same ntfy/webhook channels as watch matches: **landed** (with a
"landed away from destination" variant when touchdown is 80+ NM out —
a diversion), **holding** (the flight is flying circles — same turn-integral
detection as the circling detector, on the follow's own samples),
**descending far from destination** (low and descending 150+ NM short — the
classic diversion signature), and **running late** (the live ETA has drifted
45+ min past the first estimate).

## "What's that plane?" (phone)

Hear a jet, grab your phone: `http://<server>:8000/whatsthat` shows a compass
arrow and "LOOK NW · UP 55°" pointing your eyes at the nearest aircraft, its
route and details, and a tap-list of everything else nearby. GPS and compass
need HTTPS (or an iOS permission tap); on plain LAN HTTP it falls back to the
saved dashboard location and a north-up arrow.

## Extras

- **Voice announcements** — the 🔇 footer toggle speaks flyovers, watch
  matches, follow landings and new 7700s (browser speech synthesis; off by
  default, remembered per device).
- **Airport weather** — the board shows the airport's decoded METAR summary
  and the raw METAR/TAF strings (aviationweather.gov: free, keyless, public
  domain; cached 10 min).
- **ISS passes** — CelesTrak orbital elements propagated locally (sgp4);
  `/api/sky` lists the next 48 h of passes with naked-eye-visible ones
  flagged.
- **Rain radar overlay** — the 🌧 RAIN toggle on the stats map adds
  [RainViewer](https://www.rainviewer.com)'s latest observed radar frame to
  every map view (stats/replay, follow, emergency); the setting is remembered
  per device. Fetched by the browser, keyless, and it explains at a glance
  why the sky went quiet.
- **Local receiver** — parked design for polling your own readsb/dump1090 at
  1 s intervals: [docs/LOCAL-RECEIVER.md](docs/LOCAL-RECEIVER.md).

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
- **Airport weather** — [aviationweather.gov](https://aviationweather.gov)
  METAR/TAF (US Government work, public domain), cached 10 min per station.
- **ISS elements** — [CelesTrak](https://celestrak.org) GP data, cached and
  refreshed twice daily; pass geometry computed locally.
- **Rain radar** — [RainViewer](https://www.rainviewer.com) public tile API,
  fetched directly by the browser only while the overlay is toggled on.

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
| `/?view=stats` | open the spotting-log stats page directly |
| `/?lat=&lon=&radius=&area=&airport=` | view another location (see above) |
| `/admin` | device-fleet admin page (needs `ADMIN_TOKEN` set) |
| `/whatsthat` | phone page: compass arrow + elevation to the nearest aircraft |
| `/locate` | phone GPS helper for portal setup (HTTPS only) |
| `/api/health` | health + product-mode flag |
| `/api/overhead` | raw aircraft snapshot JSON (accepts location params) |
| `/api/board` | cached board JSON (accepts `airport=`) |
| `/api/alerts` | current global squawk-7700 aircraft (accepts `lat`/`lon`) |
| `/api/config` | server default config + data attribution |
| `/api/stats` | spotting-log statistics (accepts location params) |
| `/api/wrapped` | monthly wrap-up (`month=YYYY-MM`, default current month) |
| `/api/history/tracks` | recent track points for the stats map (`hours=`) |
| `/api/watches` | watch rules + recent matches (GET/POST/DELETE) |
| `/api/follow` | followed flights (GET/POST/DELETE) |
| `/api/wx` | board airport METAR/TAF (accepts `airport=`) |
| `/api/sky` | sun/light state + upcoming ISS passes |
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
| `SIGHTINGS_ENABLED` | `true` | spotting log (flyovers, life list, tracks) in `data/sightings.db` |
| `TRACK_RETENTION_HOURS` | `72` | how long stats-map track points are kept |
| `NTFY_URL` | *(empty)* | ntfy topic URL for watch push notifications |
| `WEBHOOK_URL` | *(empty)* | generic JSON webhook for watch notifications |
| `MAX_FOLLOWS` | `5` | cap on concurrently followed flights |
| `FOLLOW_POLL_SECONDS` | `60` | follow-a-flight poll interval (round-robin, one request total) |
| `MAX_LOCATIONS` | `50` | cap on concurrently-polled device/view locations (LRU-evicted at the cap) |
| `MAX_AIRPORTS` | `20` | cap on concurrently-refreshed airport boards — this is an AeroDataBox spend limit |
| `REQUIRE_DEVICE_TOKEN` | `false` | gate data endpoints on provisioned device tokens |
| `ADMIN_TOKEN` | *(empty = admin disabled)* | protects `/admin` + device registration |
| `LOGO_URL_TEMPLATE` / `LOGO_API_KEY` | kiwi (personal) / logostream (product) | upstream for the cached `/api/logo/{iata}` |
| `CARTO_API_KEY` | *(empty = watermarked once CARTO's free tier is exhausted)* | dark map tiles on the emergency/follow/stats views - free key at [carto.com/basemaps/apikey](https://carto.com/basemaps/apikey/) |
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
- `GET /api/stats` — spotting-log stats for the location's grid cell
- `GET /api/wrapped` — monthly spotting wrap-up (`month=YYYY-MM`)
- `GET /api/history/tracks` — recent track points (`hours=`, capped at retention)
- `GET/POST /api/watches`, `DELETE /api/watches/{id}` — watch rules + recent matches
- `GET/POST /api/follow`, `DELETE /api/follow/{callsign}` — followed flights
- `GET /api/wx` — board airport METAR/TAF (10-min cache)
- `GET /api/sky` — sun/light state + next 48 h of ISS passes
- `GET /api/health` — health + product-mode flag
- `GET /api/logo/{iata}` — airline logo, cached server-side for 30 days
- `GET /api/fw/latest` — OTA manifest; firmware images under `/fw/`
- `POST /api/devices/register` — admin: register a device token (`X-Admin-Token`)
- `GET /api/devices` — admin: fleet list (also rendered at `/admin`)
- `POST /api/demo/flyover` — demo mode only: spawn a scripted flyover
- `POST /api/demo/emergency` — demo mode only: spawn a scripted 7700
- `WS /ws` — push updates; accepts the same location params (used by the
  frontend and devices). Frame types: `overhead` (includes `sun` light state
  and recent `watch_events`), `board`, `alerts`, `follow`.

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

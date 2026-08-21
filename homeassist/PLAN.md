# Aeroblip Home Assistant Integration — Implementation Plan

**Branch:** `homeassist` · **Lives in:** `homeassist/` · **HA domain:** `aeroblip`

> **Status (21 Aug 2026): COMPLETE.** All four waves shipped and QA'd. Live smoke
> test passed against the server in DEMO_MODE on HA 2025.1.4 (config flow, WS push,
> all 12 registry entities + map markers, flyover event fired via
> `POST /api/demo/flyover`, clean unload). Offline suite: 27 tests passing
> (`homeassist/tests/`, see README → Development). Notable QA fixes along the way:
> deprecated `ws_connect` timeout kwarg removed; airline-name key bug in the event
> payload trimmer; geo markers now purge on WS disconnect (stale positions);
> README event payload docs corrected to the raw nested shape.

## 1. What we're building

A Home Assistant custom integration that connects to an Aeroblip server and turns its
flight data into HA entities: aircraft overhead your house, the nearest airport's
arrivals/departures board, and worldwide squawk-7700 emergencies — all usable in
automations ("announce on the kitchen speaker when a Qantas A380 is about to fly over").

### Why these choices

| Decision | Choice | Rationale |
|---|---|---|
| Transport | **WebSocket push** (`/ws`), REST only for setup validation | One connection delivers all three snapshot types (`overhead`, `board`, `alerts`); overhead updates every `POLL_SECONDS` (5 s default) — polling three REST endpoints at that rate is strictly worse. A live WS also marks the server-side poller `busy`, protecting it from idle-reaping. `iot_class: local_push`. |
| Location | Default to **HA's own home coordinates** (`hass.config.latitude/longitude`), overridable in config flow | The server accepts per-client `?lat=&lon=&radius=&area=&airport=` — no reason to make the user type coordinates HA already knows. |
| Coordinator | Single `DataUpdateCoordinator` in push mode (`async_set_updated_data`), holding `{overhead, board, alerts}` | One WS feed, many entities. Entities go `unavailable` when the socket is down. |
| Auth | Optional device token field (sent as `X-Device-Token` header, `?token=` on WS) | Matches `REQUIRE_DEVICE_TOKEN` deployments; personal installs leave it blank. |
| Packaging | `homeassist/custom_components/aeroblip/` + `hacs.json` at `homeassist/` | Standard layout, ready to split into a dedicated repo for HACS later (HACS needs `custom_components/` at repo root, so for now: manual copy / symlink install, documented in README). |
| Attribution | `attribution` attribute on all entities: *"Flight data © adsb.lol contributors, ODbL"* | ODbL requires it; the server's `/api/config` says so explicitly. |

## 2. Server API contract (verified against `app/main.py`, branch point `b6ecc22`)

- `GET /api/health` → `{ok, product, meta_cache}` — used by config flow to validate host.
- `GET /api/config` → server defaults (radii, airport, poll seconds, `data_credit`).
- `WS /ws?lat=&lon=&radius=&area=&airport=[&token=]` → JSON frames
  `{"type": "overhead"|"board"|"alerts", "data": {...}}`. Sends all three on connect,
  then overhead every poll, board/alerts only when changed. Close codes: `4403` bad
  token, `4429` too many locations.
- **Overhead snapshot:** `{aircraft: [...], updated, provider, overhead_count,
  overhead_radius_nm, area_radius_nm}`. Each aircraft: `hex, callsign, registration,
  type, description, lat, lon, altitude_ft, ground_speed_kt, track, heading_cardinal,
  vertical_rate_fpm, phase, distance_nm, bearing_from_home, squawk, emergency,
  overhead (bool), route {origin, origin_name, destination, destination_name, airline,
  airline_iata}, airline, info {manufacturer, model, owner, country, photo, photo_thumb}`.
  Sorted nearest-first.
- **Board snapshot:** `{arrivals: [...], departures: [...], updated, airport {icao,
  iata, name}, mock, unavailable}`. Rows (verified against `providers/board.py`):
  `flight, airline, city, code, scheduled, estimated, terminal, gate, status,
  aircraft, direction` — times are ISO local strings, `estimated` nullable.
- **Alerts snapshot:** `{aircraft: [...], count, updated}` — worldwide 7700s, distances
  recomputed from *our* lat/lon, plus `place` ("Tasman Sea") and `route`.
- Query param bounds (server clamps): radius 1–30 NM, area 10–250 NM, airport
  `[A-Z0-9]{3,4}`.

## 3. Entity model

One HA device per config entry (e.g. "Aeroblip (Brisbane)"). Unique IDs keyed on
`{entry_id}_{key}`.

### Sensors (`sensor.py`)
| Entity | State | Notable attributes |
|---|---|---|
| Overhead count | `overhead_count` | radii |
| Nearby aircraft count | `len(aircraft)` | — |
| Nearest aircraft | callsign (or registration/hex) | full aircraft dict: altitude, distance, route, airline, aircraft type, photo URL |
| Next arrival | flight number | airline, origin city/code, scheduled, estimated, timestamp device class where sane |
| Next departure | flight number | same, destination |
| Emergency alerts count | `count` | list of alerts (callsign, place, distance) |
| Data provider (diagnostic) | `provider` | `updated` timestamp |

### Binary sensors (`binary_sensor.py`)
- **Flight overhead** — `overhead_count > 0`. The flagship automation trigger.
- **Emergency active** — `alerts.count > 0`.
- **Connected** (diagnostic) — WS link up.

### Geo-location (`geo_location.py`)
One `GeolocationEvent` per nearby aircraft (source `aeroblip`) so planes appear on the
HA map, added/removed as they enter/leave the area. Distance in km (HA convention —
convert from NM), attributes carry callsign/altitude/route.

### Events (`event.py`)
- **Flyover** event entity: fires when an aircraft's `overhead` flips false→true
  (tracked by `hex` in the coordinator), event data = the aircraft dict. Also fired on
  the bus as `aeroblip_flyover` for device-independent automations.
- **Emergency** event entity: fires when a new `hex` appears in alerts.

### Explicitly out of scope (v1)
Camera/image entity for aircraft photos, board list as `todo`/custom card, demo-mode
service buttons, config entry per additional location (works already — just add the
integration twice), Lovelace card. Note them in README as future ideas.

## 4. File layout

```
homeassist/
├── PLAN.md                      # this file
├── hacs.json                    # name, min HA version
├── README.md                    # install (copy/symlink), config, entity docs, attribution
├── install.sh                   # convenience: symlink into a HA config dir
└── custom_components/aeroblip/
    ├── manifest.json            # domain, version 0.1.0, iot_class local_push, aiohttp dep (ships with HA — no requirements)
    ├── const.py                 # DOMAIN, CONF_*, defaults mirroring server clamps
    ├── api.py                   # AeroblipClient: REST validate + WS listen loop w/ backoff reconnect
    ├── coordinator.py           # AeroblipCoordinator: owns client, merges snapshots, flyover edge detection
    ├── __init__.py              # async_setup_entry / unload, platform forwarding
    ├── config_flow.py           # user step (host, token, location w/ HA defaults) + options flow (radii, airport)
    ├── entity.py                # AeroblipEntity base: device_info, attribution, availability
    ├── sensor.py
    ├── binary_sensor.py
    ├── geo_location.py
    ├── event.py
    ├── diagnostics.py           # redact token + precise lat/lon
    ├── strings.json
    └── translations/en.json
tests/                           # inside homeassist/, pytest-homeassistant-custom-component
    ├── conftest.py              # fixtures: mock client, canned snapshots (from real demo-mode captures)
    ├── test_config_flow.py
    ├── test_coordinator.py
    └── test_sensors.py
```

## 5. Execution plan — wrangler + worker agents

Fable orchestrates and QAs; implementation farmed to cheaper agents. Each task ships
with the API contract from §2 pasted into its prompt so workers never guess shapes.

| Wave | Task | Agent | Notes |
|---|---|---|---|
| 1 | Scaffold: manifest, const, hacs.json, strings, translations, install.sh, README skeleton | **Haiku** | Mechanical; exact file contents specified in prompt |
| 1 | `api.py` client + `coordinator.py` | **Sonnet** | The hard part: WS reconnect w/ exponential backoff, close-code handling (4403 → reauth, 4429 → retry later), flyover edge detection |
| 2 | `config_flow.py`, `__init__.py`, `entity.py` | **Sonnet** | Depends on wave 1 interfaces |
| 3 | `sensor.py` + `binary_sensor.py` | **Sonnet** | Parallel with next row |
| 3 | `geo_location.py` + `event.py` | **Sonnet** | Dynamic entity add/remove is the fiddly bit |
| 4 | Tests + diagnostics + README completion | **Sonnet** (tests) / **Haiku** (docs) | Fixtures from real demo-mode captures |

**QA gates (me, after every wave):**
1. Read every produced file; check against §2 contract and HA dev standards
   (unique IDs, `_attr_*` idiom, no I/O in properties, async everywhere).
2. `ruff check` + `python -m compileall`.
3. After wave 3: live smoke test — run the server in `DEMO_MODE=true`, drive the
   coordinator against it in a real HA dev container (or at minimum a scripted
   `aiohttp` session replaying `/ws`), confirm flyover event fires via
   `POST /api/demo/flyover`.
4. After wave 4: full pytest run; hassfest validation if available.

**Risks / watch-fors**
- Board row field names in §2 are inferred from consumer code — wave 1 agent must
  verify against `app/providers/board.py` and update this file if wrong.
- WS frames can be large (60 NM of traffic); recorder spam — keep big lists out of
  sensor *state*, put them in attributes, and exclude the nearest-aircraft attributes
  from recorder via `_unrecorded_attributes`.
- `updated: null` board/overhead before first poll — entities must tolerate it.
- HA min version: target 2025.x; use `entry.runtime_data`, `_async_setup` style.

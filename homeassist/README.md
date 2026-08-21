# Aeroblip

Home Assistant custom integration for Aeroblip, a self-hosted server that tracks aircraft overhead your location, displays airport arrivals and departures, and monitors worldwide squawk-7700 emergencies. The integration connects to your Aeroblip server over WebSocket for real-time updates.

## Installation

### Manual Installation

1. Download the `custom_components/aeroblip` directory.
2. Copy it to your Home Assistant `custom_components` directory (create it if it doesn't exist).
3. Restart Home Assistant.

### Automated Installation

Use the included `install.sh` script:

```bash
./install.sh /path/to/homeassistant/config
```

This creates a symlink to the integration in your Home Assistant config directory and is useful for development.

### HACS

HACS support is planned once this integration is published as a standalone repository.

## Embedding the Aeroblip web app

To embed the Aeroblip web app in a Home Assistant Webpage card or iframe, the server must allow your Home Assistant host as a frame ancestor. Start the Aeroblip server with the `FRAME_ANCESTORS` environment variable (space-separated origins):

```bash
FRAME_ANCESTORS="http://<your-ha-host>:8123" uvicorn app.main:app --host 0.0.0.0 --port 8000
```

(or add `FRAME_ANCESTORS` to the server's environment in `docker-compose.yml`).

By default, frame embedding is blocked for security. Without this setting, iframe requests from Home Assistant will be rejected.

## Configuration

The integration is configured via the Home Assistant UI:

1. Go to **Settings** → **Devices & Services**.
2. Click **Add Integration** and search for **Aeroblip**.
3. Fill in the configuration fields:

| Field | Description | Default |
|---|---|---|
| **Server URL** | Address of your Aeroblip server (e.g. `http://192.168.1.10:8000`) | Required |
| **Device Token** | Optional authentication token; only needed if `REQUIRE_DEVICE_TOKEN` is set on the server | — |
| **Latitude** | Latitude for overhead/nearby aircraft queries | Home Assistant home location |
| **Longitude** | Longitude for overhead/nearby aircraft queries | Home Assistant home location |
| **Overhead Radius** | Aircraft within this radius (NM) count as overhead | 5 NM (range: 1–30) |
| **Area Radius** | Aircraft within this radius (NM) are tracked as nearby | 60 NM (range: 10–250) |
| **Airport Code** | IATA or ICAO airport code for arrivals/departures board (e.g. `BNE`, `YBBN`) | — |

Options can be updated at any time via **Settings** → **Devices & Services** → **Aeroblip** → **Options**.

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| Aircraft overhead | Sensor | Count of aircraft currently inside the overhead radius |
| Aircraft nearby | Sensor | Count of aircraft currently inside the area radius |
| Nearest aircraft | Sensor | Callsign of the closest tracked aircraft, with altitude, distance, route, airline, and photo URL as attributes |
| Nearest aircraft bearing | Sensor | Bearing from home to the closest aircraft in degrees, with 16-point cardinal direction as attribute |
| Next flyover | Sensor | Timestamp of when the next aircraft is projected to enter the overhead radius (straight-line projection of track/speed), with callsign, airline, route, aircraft type, distance, and ETA in seconds as attributes |
| Next arrival | Sensor | Next non-cancelled arrival at the configured airport, with airline, city, scheduled/estimated times, terminal, and gate as attributes |
| Next departure | Sensor | Next non-cancelled departure from the configured airport, with the same attributes as arrivals |
| Emergency alerts | Sensor | Count of aircraft worldwide squawking 7700, with a summary list of active emergencies as an attribute |
| Data provider | Sensor (diagnostic) | Which ADS-B data source is feeding the data |
| Flyovers today | Sensor | Count of flyover events recorded since local midnight (persists across restarts) |
| Unique aircraft today | Sensor | Count of distinct airframes seen since local midnight (persists across restarts) |
| Busiest hour today | Sensor | Local hour with the most flyovers, formatted "14:00" (persists across restarts) |
| Nearest aircraft photo | Image | Photo of the closest aircraft where available |
| Watch sensors | Sensor (dynamic) | One per watched callsign, with states `not_seen`, `nearby`, `overhead`, or `gone`; aircraft details available as attributes |
| Flight overhead | Binary Sensor | On while any aircraft is inside the overhead radius — the main automation trigger |
| Emergency active | Binary Sensor | On while any 7700 alert is active worldwide |
| Server connection | Binary Sensor (diagnostic) | WebSocket link state; remains available during outages to report connection loss |
| Flyover | Event | Fires when an aircraft newly enters the overhead radius |
| Flyover imminent | Event | Fires once per approach when an aircraft is projected to be overhead within ~90 seconds |
| Emergency | Event | Fires when a new 7700 alert appears |
| Map markers | Geo Location | Every tracked aircraft appears on the Home Assistant map as a moving marker, updating live |

**Availability note:** All entities except the server connection sensor become unavailable whilst the Aeroblip server is unreachable. Map markers are removed during outages to prevent stale position data on the map.

**Daily statistics note:** The three daily-statistics sensors (`Flyovers today`, `Unique aircraft today`, and `Busiest hour today`) retain their values across Home Assistant restarts and server outages, as their state is persisted to disk.

## Events

The integration publishes five Home Assistant bus events:

- **`aeroblip_flyover`**: Triggered when an aircraft enters the overhead radius.
- **`aeroblip_flyover_imminent`**: Triggered once per approach when an aircraft is projected to enter the overhead radius within ~90 seconds. Includes the same aircraft payload as `aeroblip_flyover` plus `eta_s` (seconds until projected overhead; the event *entity* exposes the same value rounded, as an `eta_seconds` attribute).
- **`aeroblip_emergency`**: Triggered when a new squawk-7700 emergency is detected worldwide.
- **`aeroblip_rare_aircraft`**: Triggered the first time an aircraft type is ever seen (all-time registry, persisted across restarts). The first frame after install seeds the registry silently without firing this event. Payload: `{aircraft, entry_id, first_seen: true}`.
- **`aeroblip_watched_flight`**: Triggered on watch status transitions (e.g. `not_seen` → `nearby` → `overhead` → `gone`). Payload: `{callsign, status, previous_status, aircraft|null, entry_id}`.

The `aeroblip_flyover`, `aeroblip_flyover_imminent`, `aeroblip_emergency`, and `aeroblip_rare_aircraft` events carry the full aircraft dict as the server sent it (route details are nested under `route`; `phase` is one of `climbing`, `descending`, or `level`):

```json
{
  "aircraft": {
    "callsign": "QFA551",
    "registration": "VH-VZR",
    "type": "B738",
    "description": "BOEING 737-800",
    "altitude_ft": 6500,
    "distance_nm": 2.1,
    "ground_speed_kt": 285,
    "phase": "descending",
    "route": {
      "origin": "SYD",
      "origin_name": "Sydney",
      "destination": "BNE",
      "destination_name": "Brisbane",
      "airline": "Qantas",
      "airline_iata": "QF"
    }
  },
  "entry_id": "<config-entry-id>"
}
```

The **Flyover**/**Emergency** event entities carry a flattened, trimmed version of the same data (`aircraft_type`, `origin`, `destination`, `airline` at the top level).

### Example automation using a bus event

This automation announces a flyover on a text-to-speech speaker:

```yaml
alias: Announce aircraft flyover
trigger:
  platform: event
  event_type: aeroblip_flyover
action:
  service: tts.google_translate_say
  target:
    entity_id: media_player.living_room
  data:
    message: "Aircraft {{ trigger.event.data.aircraft.callsign }} overhead, altitude {{ trigger.event.data.aircraft.altitude_ft }} feet"
```

Alternatively, trigger automations using the **Flyover** or **Emergency** event entities in the Home Assistant UI without needing to write YAML.

## Services

The integration provides two services for monitoring specific flights:

- **`aeroblip.watch_flight`**: Start monitoring a specific ICAO callsign. A dynamic sensor is created for the watched flight, showing its live status (`not_seen`, `nearby`, `overhead`, or `gone`), and the `aeroblip_watched_flight` bus event fires on status transitions. Watches persist across Home Assistant restarts.
- **`aeroblip.unwatch_flight`**: Stop monitoring a previously watched callsign. The sensor is removed and watch state is persisted.

**Service call schema:** Both services require one parameter:
- `callsign` (string): The ICAO callsign to watch/unwatch (normalised to uppercase)

**Example YAML service call:**

```yaml
service: aeroblip.watch_flight
data:
  callsign: QFA551
```

To unwatch:

```yaml
service: aeroblip.unwatch_flight
data:
  callsign: QFA551
```

## Blueprints

Ready-made automations live in [`blueprints/`](blueprints/) - no YAML editing
required, just fill in the inputs via **Settings** → **Automations & Scenes**
→ **Blueprints**.

| Blueprint | What it does |
|---|---|
| [`announce_flyover.yaml`](blueprints/announce_flyover.yaml) | Announces an overhead (or about-to-arrive) aircraft on a speaker via text-to-speech - airline or callsign, aircraft description, route, and altitude. |
| [`emergency_alert.yaml`](blueprints/emergency_alert.yaml) | Runs your own notification actions when a worldwide squawk-7700 emergency is reported, with `callsign`, `place` and `distance_nm` exposed as template variables for your notification text, and an optional light flash. |
| [`aircraft_spotter.yaml`](blueprints/aircraft_spotter.yaml) | Runs your own actions only when a flyover matches a field you choose - airline (IATA), aircraft type (ICAO), or callsign prefix. |

### Installing a blueprint

**Manual:**

1. Copy the blueprint file(s) you want into your Home Assistant config under
   `<config>/blueprints/automation/aeroblip/` (create the folders if they
   don't exist).
2. In Home Assistant, go to **Settings** → **Automations & Scenes** →
   **Blueprints** and click the refresh icon, or restart Home Assistant.
3. Click **Create Automation** on the blueprint and fill in the inputs.

**My Home Assistant import:** once this repository is published, each
blueprint can be imported directly via a "My Home Assistant" link (Settings →
Automations & Scenes → Blueprints → **Import Blueprint**, pasting the raw
GitHub URL of the blueprint file) - no manual file copying needed.

## Development

The test suite runs fully offline against canned server snapshots:

```bash
python3 -m venv .venv
.venv/bin/pip install --prefer-binary homeassistant pytest-homeassistant-custom-component
cd homeassist
PYTHONPATH=. ../.venv/bin/pytest tests --asyncio-mode=auto
```

The `custom_components/__init__.py` marker file exists so the repo's package wins
the import race against the test library's bundled one — installs only ever copy
the `aeroblip` subdirectory, so it never reaches a Home Assistant instance.

## Attribution

Flight data sourced from [adsb.lol](https://adsb.lol) contributors under the ODbL licence. Aeroblip server licence terms apply to the server component.

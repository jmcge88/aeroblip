"""Constants for the Aeroblip integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "aeroblip"

# Config entry keys (lat/lon/host reuse homeassistant.const CONF_* where they exist)
CONF_BASE_URL: Final = "base_url"
CONF_DEVICE_TOKEN: Final = "device_token"
CONF_RADIUS_NM: Final = "radius_nm"
CONF_AREA_NM: Final = "area_nm"
CONF_AIRPORT: Final = "airport"

# Server-side clamps (app/main.py parse_location) - mirrored in the UI so the
# user can't configure a value the server would silently override.
DEFAULT_RADIUS_NM: Final = 5.0
MIN_RADIUS_NM: Final = 1.0
MAX_RADIUS_NM: Final = 30.0
DEFAULT_AREA_NM: Final = 60.0
MIN_AREA_NM: Final = 10.0
MAX_AREA_NM: Final = 250.0

# ODbL requires this credit wherever the data is displayed
ATTRIBUTION: Final = "Flight data © adsb.lol contributors, ODbL"

# Hass bus events (event entities mirror these)
EVENT_FLYOVER: Final = "aeroblip_flyover"
EVENT_EMERGENCY: Final = "aeroblip_emergency"
EVENT_FLYOVER_IMMINENT: Final = "aeroblip_flyover_imminent"
EVENT_RARE_AIRCRAFT: Final = "aeroblip_rare_aircraft"
EVENT_WATCHED_FLIGHT: Final = "aeroblip_watched_flight"

# Flyover prediction: fire the imminent event this many seconds before an
# aircraft is projected to enter the overhead ring; ignore projections
# further out than the horizon (course changes make them fiction).
IMMINENT_SECONDS: Final = 90.0
PREDICTION_HORIZON_S: Final = 900.0

# Services
SERVICE_WATCH_FLIGHT: Final = "watch_flight"
SERVICE_UNWATCH_FLIGHT: Final = "unwatch_flight"

# Persistent storage (helpers.storage.Store); suffixed with entry_id per entry
STORAGE_VERSION: Final = 1
STORAGE_KEY_STATS: Final = "aeroblip_stats"
STORAGE_KEY_WATCHES: Final = "aeroblip_watches"

# WebSocket close codes sent by the server
WS_CLOSE_AUTH: Final = 4403
WS_CLOSE_BUSY: Final = 4429

NM_TO_KM: Final = 1.852

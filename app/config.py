"""Configuration via environment variables."""
import os


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


# When true, overhead traffic and the airport board are fabricated so every
# view can be demoed without API keys or live traffic. When false, data is
# never made up: no radar data means "clear skies", no board key means an
# empty board.
DEMO_MODE = _bool("DEMO_MODE")

# The hosted/commercial deployment. Restricts data to commercially-licensed
# sources: positions from adsb.lol only (ODbL, attribution required), aircraft
# metadata and routes via AeroDataBox (paid plan), no planespotters photos,
# airline logos via the configured logo API. Personal/self-hosted installs
# leave this off and keep the hobby data sources.
PRODUCT_MODE = _bool("PRODUCT_MODE")

HOME_LAT = float(os.getenv("HOME_LAT", "-27.3842"))  # default: Brisbane Airport
HOME_LON = float(os.getenv("HOME_LON", "153.1175"))

# Aircraft within OVERHEAD_RADIUS_NM get the big spotlight view; anything
# within AREA_RADIUS_NM shows on the "nearby traffic" list; otherwise the
# airport board is shown.
OVERHEAD_RADIUS_NM = float(os.getenv("OVERHEAD_RADIUS_NM", "5"))
AREA_RADIUS_NM = float(os.getenv("AREA_RADIUS_NM", "60"))

AIRPORT_ICAO = os.getenv("AIRPORT_ICAO", "YBBN")
AIRPORT_IATA = os.getenv("AIRPORT_IATA", "BNE")
AIRPORT_NAME = os.getenv("AIRPORT_NAME", "Brisbane")

# Radar (overhead) polling. Product mode ignores this and always uses adsb.lol
# (adsb.fi's terms are non-commercial).
ADSB_PROVIDER = os.getenv("ADSB_PROVIDER", "adsblol")  # adsblol | adsbfi
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "10"))

# AeroDataBox: airport board only (metadata comes from standing-data)
AERODATABOX_API_KEY = os.getenv("AERODATABOX_API_KEY", "")
AERODATABOX_MARKET = os.getenv("AERODATABOX_MARKET", "rapidapi")  # rapidapi | apimarket
BOARD_REFRESH_MINUTES = float(os.getenv("BOARD_REFRESH_MINUTES", "20"))
# During quiet hours the board is not refreshed (saves free-tier API calls).
BOARD_QUIET_START = int(os.getenv("BOARD_QUIET_START", "23"))  # local hour, inclusive
BOARD_QUIET_END = int(os.getenv("BOARD_QUIET_END", "5"))       # local hour, exclusive

# VRS standing-data (CC0): bulk routes/airlines/airports/airframes, synced
# into a local SQLite DB so product-mode metadata lookups cost nothing.
STANDING_DATA_URL = os.getenv(
    "STANDING_DATA_URL",
    "https://github.com/vradarserver/standing-data/archive/refs/heads/main.tar.gz")
STANDING_DATA_REFRESH_HOURS = float(os.getenv("STANDING_DATA_REFRESH_HOURS", "24"))

# Airline logos are served from /api/logo/{iata} with a server-side cache so
# upstream sees a handful of requests, not one per client per flight.
# {code} is replaced with the uppercase IATA code, {key} with LOGO_API_KEY.
_DEFAULT_LOGO_TEMPLATE = (
    # logostream only accepts the key as a query parameter, not a header
    "https://airlines-api.logostream.dev/airlines/iata/{code}?key={key}"
    if PRODUCT_MODE else
    "https://images.kiwi.com/airlines/64/{code}.png"
)
LOGO_URL_TEMPLATE = os.getenv("LOGO_URL_TEMPLATE", _DEFAULT_LOGO_TEMPLATE)
LOGO_API_KEY = os.getenv("LOGO_API_KEY", "")
LOGO_API_KEY_HEADER = os.getenv("LOGO_API_KEY_HEADER", "X-API-Key")

# Space-separated origins allowed to iframe the dashboard (e.g.
# "http://homeassistant.local:8123 http://192.168.1.54:8123"); empty (default)
# keeps embedding blocked.
FRAME_ANCESTORS = os.getenv("FRAME_ANCESTORS", "").strip()

# Device fleet (product mode): per-device tokens provisioned at flash time by
# tools/flash_product.py. REQUIRE_DEVICE_TOKEN gates the device endpoints;
# ADMIN_TOKEN protects registration/listing.
REQUIRE_DEVICE_TOKEN = _bool("REQUIRE_DEVICE_TOKEN")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# Writable state (device registry DB, logo cache) and OTA firmware images
DATA_DIR = os.getenv("DATA_DIR", "data")
FW_DIR = os.getenv("FW_DIR", "fw")

# Devices send their own location/airport as query params; locations are
# pooled onto one upstream poll loop per ~5 km grid cell (idle ones are
# reaped). This caps concurrent cells so a fleet can't hammer adsb.lol
# unboundedly - raise it deliberately, alongside POLL_SECONDS, as the fleet
# grows.
#
# At the cap, the least-recently-used location with no live websocket client is
# evicted to make room, rather than refusing the newcomer: an unauthenticated
# caller walking 50 arbitrary coordinates must not be able to lock the fleet
# out for a whole idle period. 429 is returned only when every slot is actively
# streaming to someone.
MAX_LOCATIONS = int(os.getenv("MAX_LOCATIONS", "50"))

# Each distinct airport code spawns a board refresh loop against AeroDataBox -
# a *paid* API. Unlike locations, board codes come straight from a query param
# with ~1.7M valid-looking values, so this cap is what stops an anonymous
# caller from spawning loops that bill you. Least-recently-used boards are
# evicted at the cap.
MAX_AIRPORTS = int(os.getenv("MAX_AIRPORTS", "20"))

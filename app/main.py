"""Flight-info dashboard: overhead radar + airport board for a wall tablet."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import config
from app.providers.meta import AdsbdbMeta, LayeredMeta
from app.providers.standing_data import StandingDataMeta
from app.services.alerts import GlobalAlerts
from app.services.devices import DeviceRegistry
from app.services.hub import LocationHub, TooManyLocations
from app.services.meta_cache import CachedMeta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
LOGO_DIR = Path(config.DATA_DIR) / "logos"
LOGO_MAX_AGE = 30 * 24 * 3600
LOGO_NEG_TTL = 3600  # remember upstream misses this long

hub: LocationHub
alerts: GlobalAlerts
meta_cache: CachedMeta
http_client: httpx.AsyncClient
devices = DeviceRegistry(str(Path(config.DATA_DIR) / "devices.db"))
_logo_misses: dict[str, float] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global hub, alerts, meta_cache, http_client
    async with httpx.AsyncClient(headers={"User-Agent": "flight-info-dashboard"}) as client:
        http_client = client
        # Standing-data (CC0) is the sole metadata source in product mode and
        # the gap-filler behind adsbdb in personal mode (adsbdb lacks IATA
        # codes for many regional airlines, so their logos never resolved).
        standing = StandingDataMeta(
            client, Path(config.DATA_DIR) / "standing_data.db",
            config.STANDING_DATA_URL, config.STANDING_DATA_REFRESH_HOURS)
        if config.PRODUCT_MODE:
            # Commercially-licensed sources only: adsb.lol positions (ODbL),
            # standing-data metadata (CC0), AeroDataBox for the board only,
            # no planespotters photos.
            meta = standing
            log.info("PRODUCT_MODE enabled - adsb.lol positions, standing-data "
                     "metadata, AeroDataBox board only, no photos")
        else:
            meta = LayeredMeta(AdsbdbMeta(client), standing)
        if not standing.ready and not config.DEMO_MODE:
            # One-time first boot: block until the metadata DB exists so
            # early lookups don't poison the poller caches with misses.
            log.info("standing-data: no local database yet, running first sync")
            try:
                await standing.sync()
            except Exception:
                log.exception("initial standing-data sync failed - "
                              "retrying in background")
        # One shared, disk-backed cache for every location poller: routes and
        # airframes don't vary by location, so the fleet must not re-buy them
        # per device, and they must survive reaping and restarts.
        meta = meta_cache = CachedMeta(meta, Path(config.DATA_DIR) / "meta_cache.json")
        hub = LocationHub(client, meta)
        alerts = GlobalAlerts(client, meta=meta, product=config.PRODUCT_MODE)
        if config.DEMO_MODE:
            log.warning("DEMO_MODE enabled - overhead traffic and board data are fabricated")
        elif not config.AERODATABOX_API_KEY:
            log.warning("AERODATABOX_API_KEY not set - airport board will be empty "
                        "(set DEMO_MODE=true for fake data)")
        # Pre-start the default (env-configured) location for the web dashboard
        await hub.poller_for(*DEFAULT_LOCATION)
        tasks = [asyncio.create_task(alerts.run()), asyncio.create_task(hub.reaper()),
                 asyncio.create_task(standing.run())]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            meta.flush()  # don't throw away lookups on shutdown
            log.info("metadata cache on exit: %s", meta.stats())


app = FastAPI(title="flight-info", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Headers that matter for a page holding a device token.

    no-referrer above all: map tiles are fetched from a third-party CDN, and
    without this the full dashboard URL travels in the Referer header. The
    token is scrubbed from the URL client-side too (static/app.js), so this is
    the belt to that braces.
    """
    response = await call_next(request)
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if config.FRAME_ANCESTORS:
        # CSP frame-ancestors supersedes X-Frame-Options; sending both DENY
        # and an allowlist would still block, so skip X-Frame-Options here.
        response.headers.setdefault(
            "Content-Security-Policy", f"frame-ancestors {config.FRAME_ANCESTORS}")
    else:
        response.headers.setdefault("X-Frame-Options", "DENY")
    return response

DEFAULT_LOCATION = (config.HOME_LAT, config.HOME_LON, config.OVERHEAD_RADIUS_NM,
                    config.AREA_RADIUS_NM, config.AIRPORT_IATA)


def parse_location(params) -> tuple[float, float, float, float, str]:
    """Device-supplied location from query params, env defaults otherwise.

    Devices send ?lat=&lon=&radius=&area=&airport= (set in their WiFi portal);
    the web dashboard sends nothing and gets the server's configured location.
    """
    def num(name: str, default: float, lo: float, hi: float) -> float:
        raw = params.get(name)
        if raw is None:
            return default
        try:
            return min(hi, max(lo, float(raw)))
        except ValueError:
            return default

    lat = num("lat", config.HOME_LAT, -90, 90)
    lon = num("lon", config.HOME_LON, -180, 180)
    if params.get("lat") is None or params.get("lon") is None:
        lat, lon = config.HOME_LAT, config.HOME_LON  # both or neither
    radius = num("radius", config.OVERHEAD_RADIUS_NM, 1, 30)
    area = num("area", config.AREA_RADIUS_NM, 10, 250)
    airport = (params.get("airport") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,4}", airport):
        airport = config.AIRPORT_IATA
    return lat, lon, radius, area, airport


async def poller_or_429(params):
    try:
        return await hub.poller_for(*parse_location(params))
    except TooManyLocations:
        raise HTTPException(status_code=429, detail="too many active locations")


def board_or_429(airport: str):
    try:
        return hub.board_for(airport)
    except TooManyLocations:
        raise HTTPException(status_code=429, detail="too many active airports")


# ---- device auth ---------------------------------------------------------

async def require_device(request: Request) -> None:
    """Gate device/data endpoints on a provisioned token (product fleets).

    Off by default so personal installs and the web dashboard keep working;
    the hosted deployment sets REQUIRE_DEVICE_TOKEN=1.
    """
    if not config.REQUIRE_DEVICE_TOKEN:
        return
    # Devices send a header; browsers/kiosks can't, so ?token= also works
    token = request.headers.get("X-Device-Token") or request.query_params.get("token")
    if not devices.valid(token):
        raise HTTPException(status_code=403, detail="unknown device token")
    devices.touch(token, request.headers.get("X-FW-Version"))


def require_admin(request: Request) -> None:
    # compare_digest, not ==: a plain compare leaks the matching prefix length
    # through timing, and this token is the only thing guarding the fleet.
    supplied = request.headers.get("X-Admin-Token") or ""
    if not config.ADMIN_TOKEN or not secrets.compare_digest(supplied, config.ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="admin token required")


@app.get("/api/health")
async def health():
    # meta_cache stats make the upstream lookup spend visible at a glance
    return {"ok": True, "product": config.PRODUCT_MODE, "meta_cache": meta_cache.stats()}


@app.get("/api/overhead", dependencies=[Depends(require_device)])
async def overhead(request: Request):
    poller = await poller_or_429(request.query_params)
    return poller.snapshot


@app.get("/api/board", dependencies=[Depends(require_device)])
async def board_endpoint(request: Request):
    _, _, _, _, airport = parse_location(request.query_params)
    return board_or_429(airport).snapshot


@app.get("/api/alerts", dependencies=[Depends(require_device)])
async def alerts_endpoint(request: Request):
    """Aircraft anywhere in the world currently squawking 7700."""
    lat, lon, _, _, _ = parse_location(request.query_params)
    return alerts.snapshot_for(lat, lon)


@app.post("/api/demo/flyover")
async def demo_flyover():
    """Demo mode only: spawn a one-shot flight that passes directly overhead."""
    if not config.DEMO_MODE:
        return JSONResponse({"error": "DEMO_MODE is disabled"}, status_code=400)
    poller = await hub.poller_for(*DEFAULT_LOCATION)
    poller.trigger_flyover()
    await poller.poll_now()  # push it to clients immediately
    return {"ok": True}


@app.post("/api/demo/emergency")
async def demo_emergency():
    """Demo mode only: fabricate a far-away squawk-7700 alert for a few minutes."""
    if not config.DEMO_MODE:
        return JSONResponse({"error": "DEMO_MODE is disabled"}, status_code=400)
    alerts.trigger_demo()
    poller = await hub.poller_for(*DEFAULT_LOCATION)
    await poller.poll_now()  # ws clients pick up the alerts change on this push
    return {"ok": True}


@app.get("/api/config", dependencies=[Depends(require_device)])
async def client_config():
    """Server-side defaults - devices without a local location use these."""
    return {
        "overhead_radius_nm": config.OVERHEAD_RADIUS_NM,
        "area_radius_nm": config.AREA_RADIUS_NM,
        "airport": {"icao": config.AIRPORT_ICAO, "iata": config.AIRPORT_IATA,
                    "name": config.AIRPORT_NAME},
        "poll_seconds": config.POLL_SECONDS,
        # ODbL attribution for the position data - clients must display this
        "data_credit": "Flight data (c) adsb.lol contributors, ODbL",
    }


# ---- airline logos (server-side cache) ------------------------------------

@app.get("/api/logo/{code}")
async def airline_logo(code: str, size: int = 0):
    """Serve an airline logo by IATA code, cached on disk so upstream sees a
    handful of requests instead of one per client per flight.

    ?size=N returns an N x N JPEG on white - ESP32 devices decode JPEG only,
    and flattening alpha server-side beats doing it per device."""
    code = code.upper().removesuffix(".PNG")
    if not re.fullmatch(r"[A-Z0-9]{2,3}", code):
        raise HTTPException(status_code=404)
    if size:
        size = max(8, min(128, size))
        sized = LOGO_DIR / f"{code}_{size}.jpg"
        if sized.exists() and time.time() - sized.stat().st_mtime < LOGO_MAX_AGE:
            return FileResponse(sized, media_type="image/jpeg")
        raw = await _logo_raw(code)
        # SVG here means the upstream "unknown airline" placeholder - skip it
        if raw is None or _logo_media_type(raw[:64]) == "image/svg+xml":
            raise HTTPException(status_code=404)
        jpg = await asyncio.to_thread(_logo_to_jpeg, raw, size)
        if jpg is None:
            raise HTTPException(status_code=404)
        LOGO_DIR.mkdir(parents=True, exist_ok=True)
        sized.write_bytes(jpg)
        return Response(jpg, media_type="image/jpeg")
    raw = await _logo_raw(code)
    if raw is None:
        raise HTTPException(status_code=404)
    return Response(raw, media_type=_logo_media_type(raw[:64]))


async def _logo_raw(code: str) -> bytes | None:
    """Original upstream logo bytes, disk-cached; None when unavailable."""
    cached = LOGO_DIR / f"{code}.img"
    if cached.exists() and time.time() - cached.stat().st_mtime < LOGO_MAX_AGE:
        return cached.read_bytes()
    if not config.LOGO_URL_TEMPLATE:
        return None
    if time.time() - _logo_misses.get(code, 0) < LOGO_NEG_TTL:
        return None
    headers = ({config.LOGO_API_KEY_HEADER: config.LOGO_API_KEY}
               if config.LOGO_API_KEY and "{key}" not in config.LOGO_URL_TEMPLATE
               else {})
    url = config.LOGO_URL_TEMPLATE.format(code=code, key=config.LOGO_API_KEY)
    try:
        resp = await http_client.get(url, headers=headers, timeout=10,
                                     follow_redirects=True)
        # Some providers 200 an HTML page on auth failures - only cache images
        if resp.status_code == 200 and _is_image(resp.content,
                                                 resp.headers.get("content-type", "")):
            LOGO_DIR.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(resp.content)
            return resp.content
    except Exception as exc:
        log.warning("logo fetch failed for %s: %s", code, exc)
    _logo_misses[code] = time.time()
    if cached.exists():  # stale beats none
        return cached.read_bytes()
    return None


def _logo_to_jpeg(raw: bytes, size: int) -> bytes | None:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        img.thumbnail((size, size))
        canvas = Image.new("RGB", (size, size), (255, 255, 255))
        canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2),
                     mask=img)
        out = io.BytesIO()
        canvas.save(out, "JPEG", quality=88)
        return out.getvalue()
    except Exception:
        log.warning("logo conversion failed", exc_info=True)
        return None


def _logo_media_type(head: bytes) -> str:
    return "image/svg+xml" if head.lstrip()[:4] in (b"<svg", b"<?xm") else "image/png"


def _is_image(body: bytes, content_type: str) -> bool:
    if not body:
        return False
    if content_type.lower().split(";")[0].strip().startswith("image/"):
        return True
    head = body.lstrip()
    return (body[:4] == b"\x89PNG" or body[:3] == b"\xff\xd8\xff"  # png/jpeg
            or head[:4] == b"<svg")


# ---- device fleet (product mode) -------------------------------------------

@app.post("/api/devices/register", dependencies=[Depends(require_admin)])
async def register_device(payload: dict):
    token = (payload.get("token") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
        raise HTTPException(status_code=400, detail="bad token")
    devices.register(token, (payload.get("name") or "").strip())
    return {"ok": True}


@app.get("/api/devices", dependencies=[Depends(require_admin)])
async def list_devices():
    return {"devices": devices.all()}


# ---- firmware OTA ----------------------------------------------------------

@app.get("/api/fw/latest")
async def fw_latest():
    """OTA manifest: {"version": "1.0.1", "url": "/fw/product-1.0.1.bin"}.
    Maintained by tools/flash_product.py --release."""
    manifest = Path(config.FW_DIR) / "manifest.json"
    if not manifest.exists():
        raise HTTPException(status_code=404)
    # A half-written or hand-edited manifest is a missing release, not a 500 -
    # devices poll this on a loop and must not be handed server errors.
    try:
        j = json.loads(manifest.read_text())
        version, file = j["version"], j["file"]
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("fw manifest unreadable: %s", manifest, exc_info=True)
        raise HTTPException(status_code=404)
    # Both fields are compared against / fetched by devices on a loop: a junk
    # version reads as "newer" and triggers an OTA attempt every check, and the
    # filename is echoed into a URL under /fw, so keep it a plain basename.
    if (not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9._+-]{1,32}", version)
            or not isinstance(file, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", file)):
        log.warning("fw manifest is malformed: version=%r file=%r", version, file)
        raise HTTPException(status_code=404)
    return {"version": version, "url": f"/fw/{file}"}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    if config.REQUIRE_DEVICE_TOKEN:
        token = (websocket.headers.get("x-device-token")
                 or websocket.query_params.get("token"))
        if not devices.valid(token):
            await websocket.close(code=4403)
            return
        devices.touch(token)
    location = parse_location(websocket.query_params)
    lat, lon, _, _, airport = location
    try:
        poller = await hub.poller_for(*location)
        board = hub.board_for(airport)
    except TooManyLocations:
        await websocket.close(code=4429)
        return
    await websocket.accept()
    queue = poller.subscribe()
    last_board_update = None
    last_alerts_update = None
    try:
        await websocket.send_json({"type": "overhead", "data": poller.snapshot})
        await websocket.send_json({"type": "board", "data": board.snapshot})
        await websocket.send_json({"type": "alerts", "data": alerts.snapshot_for(lat, lon)})
        last_board_update = board.snapshot.get("updated")
        last_alerts_update = alerts.snapshot.get("updated")
        while True:
            snapshot = await queue.get()
            poller.touch()
            await websocket.send_json({"type": "overhead", "data": snapshot})
            if board.snapshot.get("updated") != last_board_update:
                last_board_update = board.snapshot.get("updated")
                board.touch()
                await websocket.send_json({"type": "board", "data": board.snapshot})
            if alerts.snapshot.get("updated") != last_alerts_update:
                last_alerts_update = alerts.snapshot.get("updated")
                await websocket.send_json(
                    {"type": "alerts", "data": alerts.snapshot_for(lat, lon)})
    except WebSocketDisconnect:
        pass
    finally:
        poller.unsubscribe(queue)


@app.get("/")
async def index():
    # Always revalidate the shell so ?v= asset bumps reach long-lived dashboards
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/admin")
async def admin_page():
    """Fleet admin page - the data behind it requires ADMIN_TOKEN."""
    return FileResponse(STATIC_DIR / "admin.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/locate")
async def locate():
    """Phone helper: shows GPS coordinates to paste into the device's WiFi
    portal. Geolocation needs a secure context, so this only works on the
    HTTPS-hosted domain - which is exactly where customers will use it."""
    return FileResponse(STATIC_DIR / "locate.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
if Path(config.FW_DIR).is_dir():
    app.mount("/fw", StaticFiles(directory=config.FW_DIR), name="fw")

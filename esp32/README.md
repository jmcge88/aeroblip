# Flight-info AMOLED display

Firmware for the Waveshare **ESP32-S3-Touch-AMOLED-2.16** (480x480 CO5300 AMOLED,
CST9220 touch) and its RISC-V sibling, the **ESP32-C6-Touch-AMOLED-2.16**
(same panel, touch, PMU and codec on different GPIOs; build the `-c6` envs -
see [docs/FLASHING.md](../docs/FLASHING.md#the-two-boards) for what the
PSRAM-less C6 leaves out). Shows live data from the flight-info server on the LAN:

- **Overhead / nearby view** — when a flight is inside the overhead ring, a
  fullscreen spotlight on that one aircraft (airline, registration, big callsign,
  route, airframe, ALT/SPD/DIST/HDG stack and a large radar scope). Otherwise a
  "NEARBY TRAFFIC" layout: nearest-aircraft summary with an "OVERHEAD IN m:ss"
  countdown when one is inbound, a small radar, and a traffic list. The radar
  shows rings at 5/10/15 NM, the focused aircraft as an arrow on its track, and
  other traffic as dots.
- **Device info screen** — swipe vertically (up or down) to see WiFi, IP, the
  settings-page URL, server URL, screen mode and uptime. Tap, swipe, or press a
  button to close (auto-hides after 60 s).
- **Departures / Arrivals views** — the airport board with time, flight, city, gate
  and colour-coded status.
- **Following view** — joins the rotation whenever a flight is followed
  (up to 5 at once): status, route, progress bar, ETA, and the same embedded
  world map used for far-away emergencies. Cycles every 15 s if more than one
  flight is followed - double-tap anywhere on the page to jump to the next
  one immediately, which holds for 2 minutes before automatic cycling
  resumes. Add or remove followed flights from the web dashboard's ✈ panel,
  or right from the device's own settings page - see
  [Follows and watch rules](#follows-and-watch-rules-on-device).
- **Emergency view (squawk 7700)** — the server's global 7700 watch feeds a
  red-alert screen: callsign, airline, route, aircraft, altitude/speed/heading,
  location and distance. A new global 7700 takes over the screen for 2 minutes
  (footer shows the remaining hold), then joins the normal rotation until it
  clears; a 7700 inside the area radius stays pinned.

Switch views by swiping left/right on the touchscreen, or with the two side keys
(BOOT = previous, USER/GPIO18 = next). A manual choice holds for 2 minutes, then
automatic behaviour resumes (during an alert takeover a manual choice holds for
one 30 s slot before the takeover reclaims the screen):

- Anything inside the overhead ring forces the **overhead view** (and wakes the
  screen). An active 7700 and an overhead flight alternate every 15 s.
- Otherwise, if board screens are enabled, departures/arrivals alternate every
  30 s (an empty side is skipped).
- If there's nothing to show - no board rows and no area traffic - or it's
  quiet hours (default 22:00-06:00), the screen drops to near-off with a faint
  "NO FLIGHTS" + clock that drifts around to avoid burn-in. A tap, button, or
  an overhead flight wakes it.

## Watch-list toasts, golden light and circling

Server-side watch-rule matches (type/airline/callsign/registration/hex, or the
circling/emergency-squawk/first-ever-type detectors - managed from the web
dashboard's ◉ panel, or from the device's own settings page, see below) pop up
as an amber banner across the bottom of whatever screen is showing, for about
8 seconds. A circling aircraft (orbiting
helicopters, holding patterns) is flagged inline wherever it appears - amber
callsign and a CIRC/CIRCLING tag - and the overhead spotlight shows a GOLDEN
LIGHT tag when the sun is at a photo-friendly angle. The board screens gain a
one-line METAR summary above the table when the server has one, and the
"ALL QUIET" empty-sky screen (and the near-black night screensaver) advertise
the next naked-eye-visible ISS pass when one is coming up.

## Sound

The onboard ES8311 codec plays a drawn-out three-tone airport chime when a
flight enters the overhead ring (suppressed during quiet hours) and a siren
alarm when a new 7700 appears (never suppressed). Watch-list matches reuse
these same two sounds: a squawk match alarms, everything else chimes (both
still honour their own toggle and the chime's quiet-hours rule). Each sound
can be toggled and has its own volume slider in the settings page, with a
live "test" preview.

Which screens are used (overhead only / board only / both), which sounds play
and at what volume, the timezone (15 presets, DST-aware) and the quiet-hours
window are all chosen in the setup portal and can be changed anytime at
**http://&lt;device-ip&gt;/param** - the device keeps a small web settings page
running (also lets you change the server URL without reflashing).

## Follows and watch rules, on device

The settings page (**http://&lt;device-ip&gt;/param**) also lists the flights
you're currently following and your watch rules, each with a **Remove**
button, plus an **Add** control for both - a callsign field for follows, and
a field-type dropdown (callsign/registration/hex/type/airline, or the
circling/emergency-squawk/first-ever-type detectors) with a value box for
watch rules. No account needed - it talks to the same server your device is
already configured against, using the device's own token, so what you add or
remove here is exactly what shows up on the web dashboard's ✈ and ◉ panels.

Every list refresh and every add/remove is a single request fired only when
you click something on the page - the device never polls these in the
background. (0.10.0 briefly shipped unconditional background polling for
other data and it crash-looped the board within a few minutes; 0.10.1 removed
it, and every screen added since follows the same on-demand rule.)

## Setup

1. Set `upload_port` in `platformio.ini` if the board isn't on COM5. You do not
   need to edit `SERVER_BASE_URL` in `src/config.h` — it is only the value the
   portal's **Flight-info server URL** field is pre-filled with, and whatever you
   enter there is stored in NVS and wins from then on. Change it in `config.h`
   only to save yourself typing across a batch of boards.
2. Build and flash:

```bash
pio run -t upload
pio device monitor
```

3. **WiFi setup happens on-device**: on first boot the display shows a
   `WIFI SETUP` screen with a **join QR code, the hotspot name, and its
   password in plain text**. Scan the QR and your phone joins
   `FlightInfo-Setup` without typing anything; otherwise join it manually with
   the password shown. A captive portal (or `http://192.168.4.1`) then lets you
   pick your 2.4 GHz network and enter its password. The portal also has a
   **Flight-info server URL** field, pre-filled from `config.h`. Both are stored
   in the ESP32's flash and reused on every boot; the hotspot only reappears if
   the network becomes unreachable for a while.

   The hotspot is **WPA2-protected with a password unique to each device**,
   generated randomly on first boot and kept for the life of the unit (a factory
   reset preserves it). It is deliberately not derived from the MAC address or
   baked into the source: the portal exposes an unauthenticated firmware-upload
   page, and since the hotspot reopens by itself whenever your home WiFi is
   down, an open AP would let anyone in radio range reflash the device. If you
   lose the password, read it off the setup screen or the serial log.
4. To change the WiFi network or server URL later, **hold the USER key
   (GPIO18 side button) while powering on** - the setup hotspot reopens.

## Notes

- Data comes from `GET /api/overhead` (every 5 s), `/api/board` (every 60 s),
  `/api/alerts` (every 60 s), `/api/follow` (every 60 s, HTTP fallback only -
  normally rides the websocket), `/api/wx` (10 min), `/api/sky` (30 min) and
  `/api/config` (once at boot). The device sends its provisioned token as
  `X-Device-Token` on every request when it has one, which is what scopes its
  follows and watch rules to it on servers with `REQUIRE_DEVICE_TOKEN=true`;
  otherwise it talks to the server unauthenticated. Either way the server must
  be reachable on the LAN.
- The display dims during quiet hours and static labels drift by a couple of
  pixels to slow AMOLED burn-in. For a 24/7 installation consider a nightly deep
  sleep or screen-off window - AMOLED panels showing a mostly static board will
  age visibly over months.
- Clock is NTP-synced; timezone and quiet hours are set in the portal
  (defaults in `src/config.h`).

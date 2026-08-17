#pragma once

// Base URL of the flight-info server. Product builds pin the hosted API via
// PRODUCT_SERVER_URL (platformio.ini); dev builds default to the LAN server.
// Either way the user can override it in the settings portal.
#ifdef PRODUCT_SERVER_URL
#define SERVER_BASE_URL PRODUCT_SERVER_URL
#else
#define SERVER_BASE_URL "http://192.168.1.100:8000"
#endif

#ifndef FW_VERSION
#define FW_VERSION "dev"
#endif

// How often to ask the server for a firmware update. The first check runs as
// soon as WiFi is up after boot (a reboot is the natural "check now" gesture);
// then daily.
#define OTA_CHECK_MS (24UL * 3600UL * 1000UL)
#define OTA_FIRST_CHECK_MS 3000UL

#define POLL_OVERHEAD_MS 5000UL
#define POLL_BOARD_MS 60000UL
#define POLL_ALERTS_MS 60000UL
// Global 7700 data older than this no longer triggers the takeover
#define ALERTS_FRESH_MS 200000UL
// A global (far-away) alert owns the screen this long, then joins the normal
// rotation - they can stay active for hours and would burn the AMOLED
#define GLOBAL_ALERT_TAKEOVER_MS 120000UL
#define HTTP_TIMEOUT_MS 6000

// Brisbane: AEST, no DST (default - changeable in the settings portal)
#define TZ_STRING "AEST-10"
#define NTP_SERVER_1 "pool.ntp.org"
#define NTP_SERVER_2 "time.google.com"

// AMOLED brightness (0-255) - kept moderate to slow burn-in
#define BRIGHT_DAY 170
#define BRIGHT_NIGHT 40
#define BRIGHT_SLEEP 8
#define NIGHT_START_HOUR 22 // quiet hours default - changeable in the settings portal
#define NIGHT_END_HOUR 6

// Display font default: 1 = smooth (Roboto), 0 = classic bitmap.
// Changeable in the settings portal.
#define UI_FONT_DEFAULT 1

// Auto-cycle active screens while nothing is overhead
#define BOARD_FLIP_MS 30000UL
// When both a squawk alert and an overhead flight are active, alternate pages
#define ALERT_ALTERNATE_MS 15000UL
// After a swipe/button press, stay on the chosen view this long
#define MANUAL_HOLD_MS 120000UL

// Consider data stale when the last successful fetch is older than this
#define STALE_AFTER_MS 20000UL

// Fall back to HTTP polling when the websocket has been silent this long
#define WS_SILENCE_MS 20000UL

// Spotlight aircraft photo box (thumbnails are typically 200x133)
#define PHOTO_W 200
#define PHOTO_H 133

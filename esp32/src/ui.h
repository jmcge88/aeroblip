#pragma once
#include <Arduino_GFX_Library.h>
#include "flight_data.h"

enum View : int {
  VIEW_OVERHEAD = 0,
  VIEW_DEPARTURES = 1,
  VIEW_ARRIVALS = 2,
  VIEW_EMERGENCY = 3, // squawk 7500/7600/7700 alert page
  // Own page, not a fallback layout of VIEW_OVERHEAD: in busy airspace (LAX)
  // the ring is never empty, so a shared page would pin the spotlight and
  // make the nearby list unreachable
  VIEW_NEARBY = 4,
  VIEW_FOLLOW = 5, // followed flights (only in rotation while one is active)
  VIEW_COUNT = 6,
};

// Decoded aircraft photo for the spotlight (buf: packed RGB565, w x h)
struct PhotoState {
  char hex[8];
  uint16_t *buf;
  int w, h;
  bool valid;
};

// Extra feeds for the newer screens; any pointer may be null and issLine /
// toast may be empty - the UI degrades to the classic layouts.
struct UiExtras {
  const FollowData *follow;  // VIEW_FOLLOW content
  const WxData *wx;          // METAR strip on the board screens
  const char *issLine;       // "ISS PASS 19:42 W>NE MAX 45" or ""
  const char *toast;         // transient watch-match banner or ""
  int followIdx;             // which follow to show on VIEW_FOLLOW
};

// spotIdx: index into oh.aircraft of the flight to spotlight fullscreen,
// or -1 for the normal nearby-traffic layout.
// emIdx: index of the local aircraft with an active squawk alert, or -1.
// galert: global 7700-watch aircraft to show when there's no local alert.
void uiDraw(Arduino_GFX *g, int view, const OverheadData &oh, const BoardData &bd,
            const AppConfig &cfg, bool wifiOk, int spotIdx, int emIdx,
            const Aircraft *galert, int pageCount, int pageIdx, int flipInSec,
            const PhotoState *photo, const UiExtras *ex);

// 0 = classic scaled bitmap font, 1 = smooth (Roboto). Settable in the portal.
void uiSetFont(uint8_t font);

// Near-black screensaver: faint "NO FLIGHTS" + clock drifting position each
// minute, plus the next visible ISS pass when one is coming up
void uiDrawSleep(Arduino_GFX *g, const char *issLine);

// Device info screen (vertical swipe): network, addresses, settings URL,
// enabled screens, battery, uptime
struct DeviceInfo {
  char ssid[34];
  int rssi;
  char ip[18];
  char server[100];
  char screens[28];
  char battery[26];
  char fw[16];
  uint32_t uptime_s;
};
void uiDrawInfo(Arduino_GFX *g, const DeviceInfo &info);

#include "ui.h"
#include "config.h"
#include "logo.h"
#include "pin_config.h"
#include "world_map.h"
#include <time.h>
#include "fonts/roboto_s2.h"
#include "fonts/roboto_s3.h"
#include "fonts/roboto_s4.h"
#include "fonts/roboto_s5.h"

// Classic 6x8 font scaled: size 2 = 12x16, size 3 = 18x24, size 4 = 24x32 ...

// Safe area: the rounded bezel overhangs the panel edges, so keep content
// inside [UI_L, UI_R] horizontally and clear of the extreme top/bottom.
#define UI_L 28
#define UI_R 452

static const uint16_t COL_BG = RGB565_BLACK;
static const uint16_t COL_AMBER = RGB565(255, 176, 0);
static const uint16_t COL_WHITE = RGB565(230, 230, 230);
static const uint16_t COL_GREY = RGB565(130, 130, 130);
static const uint16_t COL_DIM = RGB565(60, 60, 60);
static const uint16_t COL_CYAN = RGB565(80, 210, 235);
static const uint16_t COL_GREEN = RGB565(80, 220, 100);
static const uint16_t COL_RED = RGB565(240, 80, 70);

static int g_jx = 0; // slow 0-2 px drift of static elements (AMOLED burn-in care)

// Smooth font: Roboto GFXfonts cap-height-matched to the classic sizes, so
// both fonts share the same layout coordinates (y = top of capitals).
static bool g_smooth = UI_FONT_DEFAULT != 0;

void uiSetFont(uint8_t font) { g_smooth = font != 0; }

static const GFXfont *smoothFont(uint8_t size) {
  switch (size) {
    case 2: return &RobotoS2;
    case 3: return &RobotoS3;
    case 4: return &RobotoS4;
    case 5: return &RobotoS5;
    default: return nullptr; // size 1 stays classic (tiny labels)
  }
}

static void text(Arduino_GFX *g, int x, int y, uint8_t size, uint16_t color, const char *s) {
  const GFXfont *f = g_smooth ? smoothFont(size) : nullptr;
  g->setTextColor(color);
  if (f) {
    g->setFont(f);
    g->setTextSize(1);
    // GFX fonts draw from the baseline; 'A' rises exactly the cap height
    g->setCursor(x, y - f->glyph['A' - f->first].yOffset);
    g->print(s);
    g->setFont(); // callers outside text() expect the classic font
  } else {
    g->setTextSize(size);
    g->setCursor(x, y);
    g->print(s);
  }
}

static int textW(uint8_t size, const char *s) {
  const GFXfont *f = g_smooth ? smoothFont(size) : nullptr;
  if (!f) return (int)strlen(s) * 6 * size;
  int w = 0;
  for (; *s; s++) {
    unsigned char c = (unsigned char)*s;
    if (c >= f->first && c <= f->last) w += f->glyph[c - f->first].xAdvance;
  }
  return w;
}

static void textRight(Arduino_GFX *g, int xRight, int y, uint8_t size, uint16_t color, const char *s) {
  text(g, xRight - textW(size, s), y, size, color, s);
}

static void textCentered(Arduino_GFX *g, int y, uint8_t size, uint16_t color, const char *s) {
  text(g, (LCD_WIDTH - textW(size, s)) / 2, y, size, color, s);
}

static void upper(char *s) {
  for (; *s; ++s) *s = toupper((unsigned char)*s);
}

static bool clockHM(char *out, size_t n) {
  struct tm tmNow;
  if (!getLocalTime(&tmNow, 20)) { snprintf(out, n, "--:--"); return false; }
  strftime(out, n, "%H:%M", &tmNow);
  return true;
}

static void drawHeader(Arduino_GFX *g, const char *title, bool wifiOk, bool stale,
                       uint16_t titleColor = COL_AMBER) {
  char clk[8];
  clockHM(clk, sizeof(clk));
  text(g, UI_L + g_jx, 14, 3, titleColor, title);
  textRight(g, UI_R - g_jx, 14, 3, COL_WHITE, clk);
  uint16_t dot = !wifiOk ? COL_RED : (stale ? COL_AMBER : COL_GREEN);
  g->fillCircle(UI_R - g_jx - textW(3, clk) - 20, 26, 6, dot);
  g->drawFastHLine(UI_L - 6, 50, UI_R - UI_L + 12, COL_DIM);
}

static void drawFooter(Arduino_GFX *g, int pageCount, int pageIdx, const char *status,
                       int flipInSec) {
  g->drawFastHLine(UI_L - 6, 434, UI_R - UI_L + 12, COL_DIM);
  text(g, UI_L + g_jx, 444, 2, COL_GREY, status);
  if (pageCount < 2) return; // no rotation, no dots
  int dotsLeft = UI_R - pageCount * 22 + 10 - 5;
  for (int i = 0; i < pageCount; i++) {
    int x = UI_R - (pageCount - i) * 22 + 10;
    if (i == pageIdx) g->fillCircle(x, 452, 5, COL_AMBER);
    else g->drawCircle(x, 452, 5, COL_GREY);
  }
  if (flipInSec >= 0) {
    char c[10];
    snprintf(c, sizeof(c), ">> %dS", flipInSec);
    textRight(g, dotsLeft - 12, 444, 2, COL_DIM, c);
  }
}

/* ---------- overhead view ---------- */

static void fmtInt(char *out, size_t n, float v, const char *dash = "-") {
  if (isnan(v)) snprintf(out, n, "%s", dash);
  else snprintf(out, n, "%d", (int)lroundf(v));
}

static void drawNearbyRow(Arduino_GFX *g, const Aircraft &n, int y) {
  char buf[12];
  snprintf(buf, sizeof(buf), "%s", n.callsign[0] ? n.callsign : (n.registration[0] ? n.registration : "?"));
  upper(buf);
  buf[8] = '\0';
  text(g, UI_L, y, 2, COL_WHITE, buf);
  text(g, 128, y, 2, COL_GREY, n.type);
  if (n.has_route && n.origin[0] && n.destination[0]) {
    char r[10];
    snprintf(r, sizeof(r), "%s>%s", n.origin, n.destination);
    text(g, 180, y, 2, COL_CYAN, r);
  }
  if (!isnan(n.distance_nm)) {
    snprintf(buf, sizeof(buf), "%.1fNM", n.distance_nm);
    text(g, 268, y, 2, COL_WHITE, buf);
  }
  if (!isnan(n.altitude_ft)) {
    snprintf(buf, sizeof(buf), "%dFT", (int)lroundf(n.altitude_ft));
    text(g, 348, y, 2, COL_GREY, buf);
  }
}

// Radar scope: home at centre, rings at 5/10/15 NM, spotlight aircraft as
// an arrow pointing along its track, other traffic as dots.
static void drawRadar(Arduino_GFX *g, const OverheadData &oh, int cx, int cy, int R,
                      int focusIdx) {
  const float maxNm = 15.0f;
  const int tip = R / 5, tail = (R * 7) / 50; // arrow scales with scope size

  for (int i = 1; i <= 3; i++) g->drawCircle(cx, cy, R * i / 3, COL_DIM);
  int rOvhd = (int)(R * oh.overhead_radius_nm / maxNm);
  if (rOvhd > 2 && rOvhd < R)
    g->drawCircle(cx, cy, rOvhd, oh.overhead_count > 0 ? COL_AMBER : RGB565(85, 85, 85));
  g->setTextSize(1);
  g->setTextColor(COL_GREY);
  g->setCursor(cx - 2, cy - R - 10);
  g->print("N");
  g->fillCircle(cx, cy, 2, COL_GREY); // home

  for (int i = oh.count - 1; i >= 0; i--) {
    const Aircraft &a = oh.aircraft[i];
    if (isnan(a.distance_nm) || isnan(a.bearing_from_home) || a.distance_nm > maxNm) continue;
    float br = a.bearing_from_home * (float)M_PI / 180.0f;
    float r = R * a.distance_nm / maxNm;
    int x = cx + (int)lroundf(r * sinf(br));
    int y = cy - (int)lroundf(r * cosf(br));
    if (i == focusIdx && !isnan(a.track)) {
      float t = a.track * (float)M_PI / 180.0f;
      int x1 = x + (int)lroundf(tip * sinf(t)), y1 = y - (int)lroundf(tip * cosf(t));
      int x2 = x + (int)lroundf(tail * sinf(t + 2.6f)), y2 = y - (int)lroundf(tail * cosf(t + 2.6f));
      int x3 = x + (int)lroundf(tail * sinf(t - 2.6f)), y3 = y - (int)lroundf(tail * cosf(t - 2.6f));
      g->fillTriangle(x1, y1, x2, y2, x3, y3, COL_AMBER);
    } else if (i == focusIdx) {
      g->fillCircle(x, y, 4, COL_AMBER);
    } else {
      g->fillCircle(x, y, 3, RGB565(150, 150, 150));
    }
  }
}

static void drawNearbyTraffic(Arduino_GFX *g, const OverheadData &oh);

static void drawOverhead(Arduino_GFX *g, const OverheadData &oh, const AppConfig &cfg,
                         int spotIdx, const PhotoState *photo) {
  if (!oh.valid) {
    textCentered(g, 220, 3, COL_GREY, "WAITING FOR DATA...");
    return;
  }
  if (oh.count == 0) {
    textCentered(g, 190, 5, COL_AMBER, "ALL QUIET");
    char sub[44];
    snprintf(sub, sizeof(sub), "NO AIRCRAFT WITHIN %d NM", (int)lroundf(oh.area_radius_nm));
    textCentered(g, 250, 2, COL_GREY, sub);
    return;
  }
  if (spotIdx < 0 || spotIdx >= oh.count) {
    // Nothing in the ring (or the overhead screen is unticked): show the
    // nearby-traffic layout instead of a misleading "overhead" spotlight
    drawNearbyTraffic(g, oh);
    return;
  }

  // Something in the ring: fullscreen spotlight on that one flight
  const Aircraft &a = oh.aircraft[spotIdx];

  // Photo box top-right (fetched for the spotlighted aircraft)
  const int pbX = UI_R - PHOTO_W, pbY = 58;
  g->drawRect(pbX, pbY, PHOTO_W, PHOTO_H, COL_DIM);
  if (photo && photo->valid && photo->buf && strcmp(photo->hex, a.hex) == 0) {
    g->draw16bitRGBBitmap(pbX + (PHOTO_W - photo->w) / 2, pbY + (PHOTO_H - photo->h) / 2,
                          photo->buf, photo->w, photo->h);
    // planespotters.net terms: attribution stays with the photo
    g->fillRect(pbX, pbY + PHOTO_H - 11, 112, 11, RGB565_BLACK);
    text(g, pbX + 2, pbY + PHOTO_H - 9, 1, COL_GREY, "PLANESPOTTERS.NET");
  } else {
    const char *t = a.type[0] ? a.type : "----";
    text(g, pbX + (PHOTO_W - textW(3, t)) / 2, pbY + (PHOTO_H - 24) / 2, 3, COL_DIM, t);
  }
  // More flights waiting in the ring behind this one: badge on the photo
  if (oh.overhead_count > 1) {
    char more[12];
    snprintf(more, sizeof(more), "+%d MORE", oh.overhead_count - 1);
    int bw = textW(2, more) + 12;
    g->fillRect(pbX + PHOTO_W - bw, pbY + PHOTO_H - 22, bw, 22, COL_AMBER);
    text(g, pbX + PHOTO_W - bw + 6, pbY + PHOTO_H - 19, 2, RGB565_BLACK, more);
  }

  // Left column beside the photo
  char line[42];
  if (a.airline[0]) {
    snprintf(line, sizeof(line), "%s", a.airline);
    upper(line);
    line[18] = '\0';
    text(g, UI_L, 62, 2, COL_GREY, line);
  }
  char cs[12];
  snprintf(cs, sizeof(cs), "%s", a.callsign[0] ? a.callsign : (a.registration[0] ? a.registration : "??????"));
  upper(cs);
  int csX = UI_L;
  const LogoImage *lg = logoGet(a.airline_iata, 32);
  if (lg) {
    g->draw16bitRGBBitmap(csX, 88, lg->buf, lg->w, lg->h);
    csX += lg->size + 10;
  }
  text(g, csX, 88, 4, COL_AMBER, cs);
  if (a.has_route && a.origin[0] && a.destination[0]) {
    char route[16];
    snprintf(route, sizeof(route), "%s > %s", a.origin, a.destination);
    text(g, UI_L, 126, 4, COL_CYAN, route);
    char names[42];
    snprintf(names, sizeof(names), "%s TO %s", a.origin_name, a.destination_name);
    upper(names);
    names[18] = '\0';
    text(g, UI_L, 162, 2, COL_GREY, names);
  } else {
    text(g, UI_L, 126, 4, COL_GREY, a.type[0] ? a.type : "----");
    text(g, UI_L, 162, 2, COL_GREY, "ROUTE UNKNOWN");
  }

  // Full width below the photo: airframe description + registration
  snprintf(line, sizeof(line), "%s", a.description[0] ? a.description : a.type);
  upper(line);
  line[22] = '\0';
  text(g, UI_L, 204, 2, COL_WHITE, line);
  if (a.registration[0]) textRight(g, UI_R, 204, 2, COL_GREY, a.registration);

  // Stat stack on the left, big radar on the right
  const char *labels[4] = {"ALT FT", "SPD KT", "DIST NM", "HDG"};
  char v[4][12];
  fmtInt(v[0], sizeof(v[0]), a.altitude_ft);
  fmtInt(v[1], sizeof(v[1]), a.ground_speed_kt);
  if (isnan(a.distance_nm)) snprintf(v[2], sizeof(v[2]), "-");
  else snprintf(v[2], sizeof(v[2]), "%.1f", a.distance_nm);
  const char *vr = (a.vertical_rate_fpm > 200) ? " ^" : (a.vertical_rate_fpm < -200 ? " v" : "");
  snprintf(v[3], sizeof(v[3]), "%s%s", a.heading_cardinal[0] ? a.heading_cardinal : "-", vr);
  int y = 240;
  for (int i = 0; i < 4; i++, y += 42) {
    text(g, UI_L + g_jx, y + 4, 2, COL_GREY, labels[i]);
    text(g, 140, y, 3, COL_WHITE, v[i]);
  }

  drawRadar(g, oh, 360, 322, 70, spotIdx);
}

// Nothing in the ring: nearest-aircraft summary + radar + traffic list
static void drawNearbyTraffic(Arduino_GFX *g, const OverheadData &oh) {
  const Aircraft &a = oh.aircraft[0];

  char cs[12];
  snprintf(cs, sizeof(cs), "%s", a.callsign[0] ? a.callsign : (a.registration[0] ? a.registration : "??????"));
  upper(cs);
  int csX = UI_L;
  const LogoImage *lg = logoGet(a.airline_iata, 32);
  if (lg) {
    g->draw16bitRGBBitmap(csX, 62, lg->buf, lg->w, lg->h);
    csX += lg->size + 10;
  }
  text(g, csX, 62, 4, COL_WHITE, cs);

  if (a.has_route && a.origin[0] && a.destination[0]) {
    char route[16];
    snprintf(route, sizeof(route), "%s > %s", a.origin, a.destination);
    text(g, UI_L, 100, 3, COL_CYAN, route);
  } else {
    text(g, UI_L, 100, 3, COL_GREY, a.type[0] ? a.type : "----");
  }

  char line[26];
  snprintf(line, sizeof(line), "%s", a.airline[0] ? a.airline : a.description);
  upper(line);
  line[23] = '\0';
  text(g, UI_L, 132, 2, COL_GREY, line);

  char stats[26];
  char d[8] = "-";
  if (!isnan(a.distance_nm)) snprintf(d, sizeof(d), "%.1f", a.distance_nm);
  snprintf(stats, sizeof(stats), "%sNM %s %dFT", d,
           a.heading_cardinal[0] ? a.heading_cardinal : "-",
           isnan(a.altitude_ft) ? 0 : (int)lroundf(a.altitude_ft));
  text(g, UI_L, 160, 2, COL_WHITE, stats);

  float eta = etaToOverhead(a, oh.overhead_radius_nm, oh.updated, time(nullptr));
  if (eta > 0) {
    char e[24];
    int m = (int)eta / 60, s = (int)eta % 60;
    snprintf(e, sizeof(e), "OVERHEAD IN %d:%02d", m, s);
    text(g, UI_L, 192, 2, COL_AMBER, e);
  }

  drawRadar(g, oh, 388, 176, 56, 0);

  g->drawFastHLine(UI_L - 6, 244, UI_R - UI_L + 12, COL_DIM);
  char nearHdr[28];
  snprintf(nearHdr, sizeof(nearHdr), "ALSO NEARBY (%d IN AREA)", oh.total_in_area);
  text(g, UI_L + g_jx, 254, 2, COL_GREY, nearHdr);
  int y = 280;
  for (int i = 1; i < oh.count && y <= 404; i++, y += 26) drawNearbyRow(g, oh.aircraft[i], y);
}

/* ---------- board views ---------- */

static uint16_t statusColor(const char *status, char *shortOut, size_t n) {
  String s = String(status);
  s.toLowerCase();
  if (s.indexOf("cancel") >= 0) { snprintf(shortOut, n, "CNCL"); return COL_RED; }
  if (s.indexOf("divert") >= 0) { snprintf(shortOut, n, "DVRT"); return COL_RED; }
  if (s.indexOf("delay") >= 0) { snprintf(shortOut, n, "DLAYD"); return COL_RED; }
  if (s.indexOf("board") >= 0) { snprintf(shortOut, n, "BOARD"); return COL_GREEN; }
  if (s.indexOf("depart") >= 0) { snprintf(shortOut, n, "DEP"); return COL_GREEN; }
  if (s.indexOf("arriv") >= 0 || s.indexOf("land") >= 0) { snprintf(shortOut, n, "LANDED"); return COL_GREEN; }
  if (s.indexOf("gate") >= 0) { snprintf(shortOut, n, "GATE"); return COL_AMBER; }
  if (s.indexOf("check") >= 0) { snprintf(shortOut, n, "CHKIN"); return COL_WHITE; }
  if (s.indexOf("route") >= 0 || s.indexOf("expect") >= 0 || s.indexOf("approach") >= 0) {
    snprintf(shortOut, n, "ENRTE");
    return COL_CYAN;
  }
  if (s.indexOf("sched") >= 0 || s.length() == 0) { snprintf(shortOut, n, "SCHED"); return COL_GREY; }
  snprintf(shortOut, n, "%.6s", status);
  upper(shortOut);
  return COL_WHITE;
}

static void drawBoard(Arduino_GFX *g, const BoardData &bd, bool departures) {
  if (!bd.valid) {
    textCentered(g, 220, 3, COL_GREY, "WAITING FOR DATA...");
    return;
  }
  if (bd.unavailable) {
    textCentered(g, 220, 3, COL_RED, "BOARD UNAVAILABLE");
    return;
  }
  const BoardRow *rows = departures ? bd.departures : bd.arrivals;
  int n = departures ? bd.n_departures : bd.n_arrivals;

  const int xTime = UI_L, xFlight = 100, xCity = 196, xGate = 320, xStatus = 380;
  text(g, xTime + g_jx, 58, 2, COL_GREY, "TIME");
  text(g, xFlight + g_jx, 58, 2, COL_GREY, "FLIGHT");
  text(g, xCity + g_jx, 58, 2, COL_GREY, departures ? "TO" : "FROM");
  text(g, xGate + g_jx, 58, 2, COL_GREY, "GATE");
  text(g, xStatus + g_jx, 58, 2, COL_GREY, "STATUS");

  if (n == 0) {
    textCentered(g, 220, 3, COL_GREY, departures ? "NO DEPARTURES" : "NO ARRIVALS");
    return;
  }
  int y = 86;
  for (int i = 0; i < n && y <= 396; i++, y += 36) {
    const BoardRow &r = rows[i];
    bool est = r.est_hm[0] != '\0';
    text(g, xTime, y, 2, est ? COL_AMBER : COL_WHITE, est ? r.est_hm : r.sched_hm);
    // Flight numbers are IATA-prefixed (QF939) - reuse the prefix for the
    // logo, and keep the text column fixed so rows stay aligned either way
    const LogoImage *lg = logoGet(r.flight, 16);
    if (lg) g->draw16bitRGBBitmap(xFlight, y, lg->buf, lg->w, lg->h);
    char f[10];
    snprintf(f, sizeof(f), "%.6s", r.flight);
    upper(f);
    text(g, xFlight + 22, y, 2, COL_WHITE, f);
    char c[12];
    snprintf(c, sizeof(c), "%.10s", r.city);
    upper(c);
    text(g, xCity, y, 2, COL_AMBER, c);
    text(g, xGate, y, 2, COL_WHITE, r.gate[0] ? r.gate : "-");
    char st[8];
    uint16_t sc = statusColor(r.status, st, sizeof(st));
    text(g, xStatus, y, 2, sc, st);
  }
}

/* ---------- sleep screensaver ---------- */

void uiDrawSleep(Arduino_GFX *g) {
  g->fillScreen(COL_BG);
  struct tm tmNow = {};
  char clk[8] = "--:--";
  int seed = 0;
  if (getLocalTime(&tmNow, 5)) {
    strftime(clk, sizeof(clk), "%H:%M", &tmNow);
    seed = tmNow.tm_hour * 60 + tmNow.tm_min;
  }
  // Drift around the safe area, new spot each minute
  int x = 50 + (seed * 37) % 270;
  int y = 70 + (seed * 53) % 320;
  text(g, x, y, 2, COL_DIM, "NO FLIGHTS");
  text(g, x + 15, y + 26, 2, RGB565(40, 40, 40), clk);
}

/* ---------- embedded vector world map ---------- */

// Country-scale window of the Natural Earth coastlines, centred on the
// aircraft, with the aircraft as a track-rotated arrow. No tiles, no network.
static void drawWorldMap(Arduino_GFX *g, const Aircraft &a, int bx, int by, int bw, int bh) {
  g->drawRect(bx, by, bw, bh, COL_DIM);
  if (isnan(a.lat) || isnan(a.lon)) {
    text(g, bx + (bw - textW(2, "NO POSITION")) / 2, by + bh / 2 - 8, 2, COL_DIM, "NO POSITION");
    return;
  }

  const float latSpan = 16.0f; // degrees shown vertically
  float cosLat = cosf(a.lat * (float)M_PI / 180.0f);
  if (cosLat < 0.2f) cosLat = 0.2f;
  const float lonSpan = latSpan * ((float)bw / bh) / cosLat;
  const float lat0 = a.lat, lon0 = a.lon;
  const float padLat = latSpan * 0.55f, padLon = lonSpan * 0.55f;

  for (int r = 0; r < WORLD_MAP_RINGS; r++) {
    int start = WORLD_MAP_RING_OFFSETS[r], end = WORLD_MAP_RING_OFFSETS[r + 1];
    for (int i = start; i < end - 1; i++) {
      float lon1 = WORLD_MAP_PTS[i][0] / 100.0f, lat1 = WORLD_MAP_PTS[i][1] / 100.0f;
      float lon2 = WORLD_MAP_PTS[i + 1][0] / 100.0f, lat2 = WORLD_MAP_PTS[i + 1][1] / 100.0f;
      if (fabsf(lat1 - lat0) > padLat || fabsf(lat2 - lat0) > padLat) continue;
      float d1 = lon1 - lon0, d2 = lon2 - lon0;
      if (fabsf(d1) > padLon || fabsf(d2) > padLon) continue; // also drops dateline wraps
      int x1 = bx + bw / 2 + (int)lroundf(d1 / lonSpan * bw);
      int y1 = by + bh / 2 - (int)lroundf((lat1 - lat0) / latSpan * bh);
      int x2 = bx + bw / 2 + (int)lroundf(d2 / lonSpan * bw);
      int y2 = by + bh / 2 - (int)lroundf((lat2 - lat0) / latSpan * bh);
      if (x1 < bx || x1 >= bx + bw || y1 < by || y1 >= by + bh) continue;
      if (x2 < bx || x2 >= bx + bw || y2 < by || y2 >= by + bh) continue;
      g->drawLine(x1, y1, x2, y2, COL_GREY);
    }
  }

  // The aircraft, centred, pointing along its track
  int cx = bx + bw / 2, cy = by + bh / 2;
  if (!isnan(a.track)) {
    float t = a.track * (float)M_PI / 180.0f;
    int x1 = cx + (int)lroundf(11 * sinf(t)), y1 = cy - (int)lroundf(11 * cosf(t));
    int x2 = cx + (int)lroundf(8 * sinf(t + 2.6f)), y2 = cy - (int)lroundf(8 * cosf(t + 2.6f));
    int x3 = cx + (int)lroundf(8 * sinf(t - 2.6f)), y3 = cy - (int)lroundf(8 * cosf(t - 2.6f));
    g->fillTriangle(x1, y1, x2, y2, x3, y3, COL_RED);
  } else {
    g->fillCircle(cx, cy, 5, COL_RED);
  }
}

/* ---------- squawk alert (7500/7600/7700) ---------- */

static const char *alertLabel(const Aircraft &a) {
  if (!strcmp(a.squawk, "7700")) return "GENERAL EMERGENCY";
  if (!strcmp(a.squawk, "7600")) return "RADIO FAILURE";
  if (!strcmp(a.squawk, "7500")) return "UNLAWFUL INTERFERENCE";
  return a.emergency[0] ? a.emergency : "EMERGENCY";
}

// radarIdx: index into oh.aircraft for the radar focus, or -1 to omit the
// radar (global alerts far outside its 15 NM range)
static void drawEmergency(Arduino_GFX *g, const OverheadData &oh, const Aircraft &a,
                          int radarIdx) {
  // Red banner with the alert meaning
  g->fillRect(UI_L - 6, 58, UI_R - UI_L + 12, 40, COL_RED);
  char btxt[26];
  snprintf(btxt, sizeof(btxt), "%s", alertLabel(a));
  upper(btxt);
  btxt[23] = '\0';
  text(g, (LCD_WIDTH - textW(3, btxt)) / 2, 66, 3, RGB565_BLACK, btxt);

  char cs[12];
  snprintf(cs, sizeof(cs), "%s", a.callsign[0] ? a.callsign : (a.registration[0] ? a.registration : "??????"));
  upper(cs);
  text(g, UI_L, 112, 5, COL_WHITE, cs);
  if (a.squawk[0]) textRight(g, UI_R, 118, 4, COL_RED, a.squawk);

  char line[42];
  if (a.has_route && a.origin[0] && a.destination[0]) {
    char route[16];
    snprintf(route, sizeof(route), "%s > %s", a.origin, a.destination);
    text(g, UI_L, 164, 3, COL_CYAN, route);
  } else {
    text(g, UI_L, 164, 3, COL_GREY, a.type[0] ? a.type : "----");
  }
  if (a.place[0]) {
    snprintf(line, sizeof(line), "OVER %s", a.place);
    upper(line);
    line[35] = '\0';
    text(g, UI_L, 194, 2, COL_WHITE, line);
  } else if (a.has_route && a.origin_name[0]) {
    snprintf(line, sizeof(line), "%s TO %s", a.origin_name, a.destination_name);
    upper(line);
    line[35] = '\0';
    text(g, UI_L, 194, 2, COL_GREY, line);
  } else {
    text(g, UI_L, 194, 2, COL_GREY, "ROUTE UNKNOWN");
  }
  snprintf(line, sizeof(line), "%s", a.description[0] ? a.description : a.type);
  upper(line);
  line[22] = '\0';
  text(g, UI_L, 216, 2, COL_WHITE, line);
  if (a.registration[0]) textRight(g, UI_R, 216, 2, COL_GREY, a.registration);

  const char *labels[4] = {"ALT FT", "SPD KT", "DIST NM", "HDG"};
  char v[4][12];
  fmtInt(v[0], sizeof(v[0]), a.altitude_ft);
  fmtInt(v[1], sizeof(v[1]), a.ground_speed_kt);
  if (isnan(a.distance_nm)) snprintf(v[2], sizeof(v[2]), "-");
  else if (a.distance_nm >= 100.0f) snprintf(v[2], sizeof(v[2]), "%d", (int)lroundf(a.distance_nm));
  else snprintf(v[2], sizeof(v[2]), "%.1f", a.distance_nm);
  const char *vr = (a.vertical_rate_fpm > 200) ? " ^" : (a.vertical_rate_fpm < -200 ? " v" : "");
  snprintf(v[3], sizeof(v[3]), "%s%s", a.heading_cardinal[0] ? a.heading_cardinal : "-", vr);
  int y = 240;
  for (int i = 0; i < 4; i++, y += 42) {
    text(g, UI_L + g_jx, y + 4, 2, COL_GREY, labels[i]);
    text(g, 140, y, 3, COL_WHITE, v[i]);
  }

  if (radarIdx >= 0) {
    drawRadar(g, oh, 360, 322, 70, radarIdx);
  } else {
    drawWorldMap(g, a, 252, 240, 200, 184);
  }
}

/* ---------- device info ---------- */

void uiDrawInfo(Arduino_GFX *g, const DeviceInfo &info) {
  g->fillScreen(COL_BG);
  char clk[8];
  clockHM(clk, sizeof(clk));
  text(g, UI_L, 14, 3, COL_AMBER, "DEVICE INFO");
  textRight(g, UI_R, 14, 3, COL_WHITE, clk);
  g->drawFastHLine(UI_L - 6, 50, UI_R - UI_L + 12, COL_DIM);

  char wifi[48], up[36];
  snprintf(wifi, sizeof(wifi), "%.24s (%d dBm)", info.ssid, info.rssi);
  uint32_t h = info.uptime_s / 3600, m = (info.uptime_s % 3600) / 60;
  snprintf(up, sizeof(up), "%luH %02luM / FW %.12s", (unsigned long)h, (unsigned long)m,
           info.fw);

  char settings[64];
  snprintf(settings, sizeof(settings), "http://%s/param", info.ip);

  struct { const char *label; const char *value; } rows[] = {
      {"WIFI", wifi},
      {"IP", info.ip},
      {"SETTINGS", settings},
      {"SERVER", info.server},
      {"SCREENS", info.screens},
      {"BATTERY", info.battery},
      {"UPTIME", up},
      // ODbL attribution - required by the adsb.lol Open Database License
      {"DATA", "ADS-B (C) ADSB.LOL (ODbL)"},
  };
  int y = 66;
  for (auto &r : rows) {
    text(g, UI_L, y, 2, COL_GREY, r.label);
    char v[36];
    snprintf(v, sizeof(v), "%.34s", r.value);
    text(g, UI_L, y + 20, 2, COL_WHITE, v);
    y += 45;
  }

  g->drawFastHLine(UI_L - 6, 434, UI_R - UI_L + 12, COL_DIM);
  text(g, UI_L, 444, 2, COL_DIM, "SWIPE OR TAP TO CLOSE");
}

/* ---------- top level ---------- */

void uiDraw(Arduino_GFX *g, int view, const OverheadData &oh, const BoardData &bd,
            const AppConfig &cfg, bool wifiOk, int spotIdx, int emIdx,
            const Aircraft *galert, int pageCount, int pageIdx, int flipInSec,
            const PhotoState *photo) {
  struct tm tmNow;
  g_jx = getLocalTime(&tmNow, 5) ? (tmNow.tm_min % 3) : 0;

  g->fillScreen(COL_BG);

  const char *iata = bd.airport_iata[0] ? bd.airport_iata : cfg.airport_iata;
  char title[24];
  bool stale;
  char status[40];

  switch (view) {
    case VIEW_EMERGENCY: {
      bool local = emIdx >= 0 && emIdx < oh.count;
      const Aircraft *a = local ? &oh.aircraft[emIdx] : galert;
      if (a && a->squawk[0]) snprintf(title, sizeof(title), "SQUAWK %s", a->squawk);
      else snprintf(title, sizeof(title), "EMERGENCY");
      stale = oh.valid && (millis() - oh.fetched_ms > STALE_AFTER_MS);
      drawHeader(g, title, wifiOk, stale, COL_RED);
      if (a) drawEmergency(g, oh, *a, local ? emIdx : -1);
      else textCentered(g, 220, 3, COL_GREY, "ALERT CLEARED");
      if (local) snprintf(status, sizeof(status), "%s | SQUAWK ALERT", oh.provider);
      else snprintf(status, sizeof(status), "GLOBAL 7700 WATCH");
      upper(status);
      break;
    }
    case VIEW_DEPARTURES:
    case VIEW_ARRIVALS: {
      bool dep = (view == VIEW_DEPARTURES);
      snprintf(title, sizeof(title), "%s %s", iata[0] ? iata : "", dep ? "DEPARTURES" : "ARRIVALS");
      stale = bd.valid && (millis() - bd.fetched_ms > 3 * POLL_BOARD_MS);
      drawHeader(g, title, wifiOk, stale);
      drawBoard(g, bd, dep);
      if (bd.valid && bd.updated) {
        struct tm tmUpd;
        time_t t = (time_t)bd.updated;
        localtime_r(&t, &tmUpd);
        snprintf(status, sizeof(status), "BOARD UPDATED %02d:%02d", tmUpd.tm_hour, tmUpd.tm_min);
      } else {
        snprintf(status, sizeof(status), "%s", wifiOk ? "FETCHING..." : "WIFI DOWN");
      }
      break;
    }
    default: {
      snprintf(title, sizeof(title), "%s", spotIdx >= 0 ? "OVERHEAD" : "NEARBY TRAFFIC");
      stale = oh.valid && (millis() - oh.fetched_ms > STALE_AFTER_MS);
      drawHeader(g, title, wifiOk, stale);
      drawOverhead(g, oh, cfg, spotIdx, photo);
      if (oh.valid) {
        snprintf(status, sizeof(status), "%s%s%s | %d OVHD", oh.provider,
                 stale ? " | STALE" : "", wifiOk ? "" : " | WIFI DOWN", oh.overhead_count);
        upper(status);
      } else {
        snprintf(status, sizeof(status), "%s", wifiOk ? "CONNECTING TO SERVER..." : "WIFI DOWN");
      }
      break;
    }
  }
  drawFooter(g, pageCount, pageIdx, status, flipInSec);
}

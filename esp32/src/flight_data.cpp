#include "flight_data.h"
#include "config.h"
#include "certs.h"
#include "device_id.h"

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <math.h>
#include <time.h>

// Copy a JSON string into a fixed buffer, dropping non-ASCII bytes so the
// built-in GFX font never renders garbage for accented city names.
static void copyAscii(char *dst, size_t dstLen, const char *src) {
  size_t o = 0;
  if (src) {
    for (const char *p = src; *p && o < dstLen - 1; ++p) {
      unsigned char c = (unsigned char)*p;
      if (c >= 32 && c < 127) dst[o++] = (char)c;
    }
  }
  dst[o] = '\0';
}

static String s_serverBase = SERVER_BASE_URL;

void setServerBaseUrl(const char *url) {
  String u = url ? url : "";
  u.trim();
  if (u.isEmpty()) return;
  if (!u.startsWith("http://") && !u.startsWith("https://")) u = "http://" + u;
  while (u.endsWith("/")) u.remove(u.length() - 1);
  s_serverBase = u;
}

const char *serverBaseUrl() { return s_serverBase.c_str(); }

static char s_query[128] = "";
static char s_airport[6] = "";

void setDeviceLocation(const char *latlon, const char *radius, const char *area,
                       const char *airport) {
  s_query[0] = '\0';
  s_airport[0] = '\0';
  size_t o = 0;

  /* snprintf returns the length it WOULD have written, so `o += snprintf(...)`
     can walk past the end of the buffer on truncation - after which both
     `s_query + o` and `sizeof(s_query) - o` (unsigned underflow) are wrong, and
     the next append writes out of bounds. Clamp after every call, the same way
     buildScreensHtml() in main.cpp does. Today's longest possible query is
     ~85 of 128 bytes, so this is a guard rather than a live fix. */
  auto append = [&](const char *fmt, auto... args) {
    if (o >= sizeof(s_query) - 1) return;
    int n = snprintf(s_query + o, sizeof(s_query) - o, fmt, args...);
    if (n < 0) return;
    o = (size_t)n >= sizeof(s_query) - o ? sizeof(s_query) - 1 : o + (size_t)n;
  };

  // "lat, lon" as pasted from Google Maps / the /locate helper page
  if (latlon && latlon[0]) {
    char *end;
    double lat = strtod(latlon, &end);
    while (*end == ' ' || *end == ',') end++;
    char *end2;
    double lon = strtod(end, &end2);
    if (end != latlon && end2 != end && lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180)
      append("?lat=%.6f&lon=%.6f", lat, lon);
  }
  auto addNum = [&](const char *name, const char *v) {
    double d = v && v[0] ? strtod(v, nullptr) : 0;
    if (d > 0)
      append("%s%s=%g", o ? "&" : "?", name, d);
  };
  addNum("radius", radius);
  addNum("area", area);
  if (airport) {
    size_t n = 0;
    for (const char *p = airport; *p && n < sizeof(s_airport) - 1; ++p)
      if (isalnum((unsigned char)*p)) s_airport[n++] = toupper((unsigned char)*p);
    s_airport[n] = '\0';
    if (n >= 3)
      append("%s%s=%s", o ? "&" : "?", "airport", s_airport);
    else
      s_airport[0] = '\0';
  }
}

const char *deviceQuery() { return s_query; }
const char *deviceAirport() { return s_airport; }

bool serverHostPort(char *host, size_t hostLen, uint16_t &port, bool &tls) {
  const char *u = s_serverBase.c_str();
  tls = false;
  const char *p = u;
  if (!strncmp(u, "https://", 8)) { tls = true; p = u + 8; }
  else if (!strncmp(u, "http://", 7)) p = u + 7;
  port = tls ? 443 : 80;
  const char *colon = strchr(p, ':');
  if (colon) {
    snprintf(host, hostLen, "%.*s", (int)(colon - p), p);
    port = (uint16_t)atoi(colon + 1);
  } else {
    snprintf(host, hostLen, "%s", p);
  }
  return host[0] != '\0';
}

static bool httpGetJson(const char *path, JsonDocument &doc, const JsonDocument *filter) {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  WiFiClient plain;
  WiFiClientSecure secure;
  WiFiClient *client = &plain;
  if (s_serverBase.startsWith("https://")) {
#ifdef PRODUCT_BUILD
    secure.setCACert(PINNED_ROOTS); // pinned root bundle - see certs.h
#else
    secure.setInsecure(); // dev/self-host: accept self-signed certs
#endif
    client = &secure;
  }
  String url = s_serverBase + path + s_query;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(*client, url)) return false;
  if (deviceToken()[0]) http.addHeader("X-Device-Token", deviceToken());
  http.addHeader("X-FW-Version", FW_VERSION);
  int code = http.GET();
  bool ok = false;
  if (code == HTTP_CODE_OK) {
    DeserializationError err = filter
        ? deserializeJson(doc, http.getStream(), DeserializationOption::Filter(*filter))
        : deserializeJson(doc, http.getStream());
    if (err) {
      Serial.printf("[net] %s JSON error: %s\n", path, err.c_str());
    } else {
      ok = true;
    }
  } else {
    Serial.printf("[net] GET %s -> %d\n", path, code);
  }
  http.end();
  return ok;
}

bool lookupPhotoUrl(const char *hex, char *out, size_t outLen) {
  out[0] = '\0';
  if (WiFi.status() != WL_CONNECTED) return false;
  WiFiClientSecure client;
  client.setInsecure(); // public metadata API, integrity is not critical
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  String url = String("https://api.adsbdb.com/v0/aircraft/") + hex;
  if (!http.begin(client, url)) return false;
  bool ok = false;
  if (http.GET() == HTTP_CODE_OK) {
    JsonDocument filter;
    filter["response"]["aircraft"]["url_photo_thumbnail"] = true;
    JsonDocument doc;
    if (!deserializeJson(doc, http.getStream(), DeserializationOption::Filter(filter))) {
      const char *u = doc["response"]["aircraft"]["url_photo_thumbnail"] | "";
      if (u[0]) {
        snprintf(out, outLen, "%s", u);
        ok = true;
      }
    }
  }
  http.end();
  return ok;
}

bool fetchConfig(AppConfig &out) {
  JsonDocument doc;
  if (!httpGetJson("/api/config", doc, nullptr)) return false;
  out.overhead_radius_nm = doc["overhead_radius_nm"] | 5.0f;
  out.area_radius_nm = doc["area_radius_nm"] | 60.0f;
  copyAscii(out.airport_iata, sizeof(out.airport_iata), doc["airport"]["iata"] | "");
  out.valid = true;
  return true;
}

static void overheadFilterInto(JsonVariant f) {
  f["updated"] = true;
  f["provider"] = true;
  f["overhead_count"] = true;
  f["overhead_radius_nm"] = true;
  f["area_radius_nm"] = true;
  JsonObject fa = f["aircraft"].add<JsonObject>();
  for (const char *k : {"hex", "callsign", "registration", "type", "description", "phase",
                        "heading_cardinal", "altitude_ft", "ground_speed_kt", "track",
                        "distance_nm", "bearing_from_home", "vertical_rate_fpm", "overhead",
                        "squawk", "emergency", "lat", "lon", "place"})
    fa[k] = true;
  for (const char *k : {"origin", "destination", "origin_name", "destination_name",
                        "airline", "airline_iata"})
    fa["route"][k] = true;
  fa["airline"]["airline"] = true;
  fa["airline"]["airline_iata"] = true;
  fa["info"]["photo_thumb"] = true;
}

static void parseAircraft(JsonObjectConst a, Aircraft &ac) {
  copyAscii(ac.hex, sizeof(ac.hex), a["hex"] | "");
  copyAscii(ac.callsign, sizeof(ac.callsign), a["callsign"] | "");
  copyAscii(ac.registration, sizeof(ac.registration), a["registration"] | "");
  copyAscii(ac.type, sizeof(ac.type), a["type"] | "");
  copyAscii(ac.description, sizeof(ac.description), a["description"] | "");
  copyAscii(ac.phase, sizeof(ac.phase), a["phase"] | "");
  copyAscii(ac.heading_cardinal, sizeof(ac.heading_cardinal), a["heading_cardinal"] | "");
  copyAscii(ac.place, sizeof(ac.place), a["place"] | "");
  ac.lat = a["lat"] | NAN;
  ac.lon = a["lon"] | NAN;
  ac.altitude_ft = a["altitude_ft"] | NAN;
  ac.ground_speed_kt = a["ground_speed_kt"] | NAN;
  ac.track = a["track"] | NAN;
  ac.distance_nm = a["distance_nm"] | NAN;
  ac.bearing_from_home = a["bearing_from_home"] | NAN;
  ac.vertical_rate_fpm = a["vertical_rate_fpm"] | 0;
  ac.overhead = a["overhead"] | false;
  copyAscii(ac.photo, sizeof(ac.photo), a["info"]["photo_thumb"] | "");
  copyAscii(ac.squawk, sizeof(ac.squawk), a["squawk"] | "");
  copyAscii(ac.emergency, sizeof(ac.emergency), a["emergency"] | "");
  JsonObjectConst route = a["route"];
  if (!route.isNull()) {
    ac.has_route = true;
    copyAscii(ac.origin, sizeof(ac.origin), route["origin"] | "");
    copyAscii(ac.destination, sizeof(ac.destination), route["destination"] | "");
    copyAscii(ac.origin_name, sizeof(ac.origin_name), route["origin_name"] | "");
    copyAscii(ac.destination_name, sizeof(ac.destination_name), route["destination_name"] | "");
    copyAscii(ac.airline, sizeof(ac.airline), route["airline"] | "");
    copyAscii(ac.airline_iata, sizeof(ac.airline_iata), route["airline_iata"] | "");
  }
  if (ac.airline[0] == '\0')
    copyAscii(ac.airline, sizeof(ac.airline), a["airline"]["airline"] | "");
  if (ac.airline_iata[0] == '\0')
    copyAscii(ac.airline_iata, sizeof(ac.airline_iata), a["airline"]["airline_iata"] | "");
}

static bool parseOverheadPayload(JsonVariantConst doc, OverheadData &out) {
  OverheadData d = {};
  JsonArrayConst arr = doc["aircraft"].as<JsonArrayConst>();
  d.total_in_area = arr.size();
  for (JsonObjectConst a : arr) {
    if (d.count >= MAX_AIRCRAFT) break;
    parseAircraft(a, d.aircraft[d.count]);
    d.count++;
  }
  d.overhead_count = doc["overhead_count"] | 0;
  d.overhead_radius_nm = doc["overhead_radius_nm"] | 5.0f;
  d.area_radius_nm = doc["area_radius_nm"] | 60.0f;
  copyAscii(d.provider, sizeof(d.provider), doc["provider"] | "");
  d.updated = doc["updated"] | 0;
  d.valid = true;
  d.fetched_ms = millis();
  out = d;
  return true;
}

bool fetchOverhead(OverheadData &out) {
  JsonDocument filter;
  overheadFilterInto(filter.to<JsonVariant>());
  JsonDocument doc;
  if (!httpGetJson("/api/overhead", doc, &filter)) return false;
  return parseOverheadPayload(doc, out);
}

// "2026-08-13 16:15+10:00" -> "16:15"
static void timeHM(char *dst, size_t dstLen, const char *iso) {
  dst[0] = '\0';
  if (iso && strlen(iso) >= 16) snprintf(dst, dstLen, "%.5s", iso + 11);
}

static void parseBoardRows(JsonArrayConst arr, BoardRow *rows, int &n) {
  n = 0;
  for (JsonObjectConst r : arr) {
    if (n >= MAX_BOARD_ROWS) break;
    BoardRow &row = rows[n];
    copyAscii(row.flight, sizeof(row.flight), r["flight"] | "");
    copyAscii(row.city, sizeof(row.city), r["city"] | "");
    copyAscii(row.code, sizeof(row.code), r["code"] | "");
    copyAscii(row.gate, sizeof(row.gate), r["gate"] | "");
    copyAscii(row.status, sizeof(row.status), r["status"] | "");
    timeHM(row.sched_hm, sizeof(row.sched_hm), r["scheduled"] | (const char *)nullptr);
    timeHM(row.est_hm, sizeof(row.est_hm), r["estimated"] | (const char *)nullptr);
    if (strcmp(row.est_hm, row.sched_hm) == 0) row.est_hm[0] = '\0';
    n++;
  }
}

static void boardFilterInto(JsonVariant f) {
  for (const char *dir : {"arrivals", "departures"}) {
    JsonObject fr = f[dir].add<JsonObject>();
    for (const char *k : {"flight", "city", "code", "scheduled", "estimated", "gate", "status"})
      fr[k] = true;
  }
  f["updated"] = true;
  f["unavailable"] = true;
  f["airport"]["iata"] = true;
}

static bool parseBoardPayload(JsonVariantConst doc, BoardData &out) {
  BoardData d = {};
  parseBoardRows(doc["arrivals"].as<JsonArrayConst>(), d.arrivals, d.n_arrivals);
  parseBoardRows(doc["departures"].as<JsonArrayConst>(), d.departures, d.n_departures);
  copyAscii(d.airport_iata, sizeof(d.airport_iata), doc["airport"]["iata"] | "");
  d.unavailable = doc["unavailable"] | false;
  d.updated = doc["updated"] | 0;
  d.valid = true;
  d.fetched_ms = millis();
  out = d;
  return true;
}

bool fetchBoard(BoardData &out) {
  JsonDocument filter;
  boardFilterInto(filter.to<JsonVariant>());
  JsonDocument doc;
  if (!httpGetJson("/api/board", doc, &filter)) return false;
  return parseBoardPayload(doc, out);
}

static bool parseAlertsPayload(JsonVariantConst doc, AlertsData &out) {
  AlertsData d = {};
  for (JsonObjectConst a : doc["aircraft"].as<JsonArrayConst>()) {
    if (d.count >= MAX_ALERTS) break;
    parseAircraft(a, d.alerts[d.count]);
    d.count++;
  }
  d.updated = doc["updated"] | 0;
  d.valid = true;
  d.fetched_ms = millis();
  out = d;
  return true;
}

bool fetchAlerts(AlertsData &out) {
  JsonDocument filter;
  overheadFilterInto(filter.to<JsonVariant>()); // same aircraft shape
  JsonDocument doc;
  if (!httpGetJson("/api/alerts", doc, &filter)) return false;
  return parseAlertsPayload(doc, out);
}

int handleWsMessage(const uint8_t *payload, size_t len, OverheadData &oh, BoardData &bd,
                    AlertsData &al) {
  JsonDocument filter;
  filter["type"] = true;
  JsonVariant fd = filter["data"].to<JsonObject>();
  overheadFilterInto(fd);
  boardFilterInto(fd);

  JsonDocument doc;
  DeserializationError err =
      deserializeJson(doc, payload, len, DeserializationOption::Filter(filter));
  if (err) {
    Serial.printf("[ws] JSON error: %s\n", err.c_str());
    return 0;
  }
  const char *type = doc["type"] | "";
  if (strcmp(type, "overhead") == 0) return parseOverheadPayload(doc["data"], oh) ? 1 : 0;
  if (strcmp(type, "board") == 0) return parseBoardPayload(doc["data"], bd) ? 2 : 0;
  if (strcmp(type, "alerts") == 0) return parseAlertsPayload(doc["data"], al) ? 3 : 0;
  return 0;
}

bool aircraftAlert(const Aircraft &a) {
  if (!strcmp(a.squawk, "7500") || !strcmp(a.squawk, "7600") || !strcmp(a.squawk, "7700"))
    return true;
  if (a.emergency[0] && strcmp(a.emergency, "none") != 0 && strcmp(a.emergency, "lifeguard") != 0)
    return true;
  return false;
}

float etaToOverhead(const Aircraft &a, float ringNm, uint32_t updatedEpoch, time_t nowEpoch) {
  if (a.overhead || isnan(a.distance_nm) || isnan(a.bearing_from_home) ||
      isnan(a.track) || !(a.ground_speed_kt > 50.0f))
    return -1.0f;
  float toHome = fmodf(a.bearing_from_home + 180.0f, 360.0f); // bearing aircraft -> home
  float delta = fmodf(a.track - toHome + 540.0f, 360.0f) - 180.0f;
  float rad = delta * (float)M_PI / 180.0f;
  float along = a.distance_nm * cosf(rad); // NM until closest approach
  float cross = fabsf(a.distance_nm * sinf(rad)); // miss distance NM
  if (along <= 0.0f || cross > ringNm) return -1.0f; // flying away, or will miss
  float toRing = along - sqrtf(ringNm * ringNm - cross * cross);
  if (toRing <= 0.0f) return -1.0f;
  float secs = toRing / (a.ground_speed_kt / 3600.0f);
  if (updatedEpoch && nowEpoch > (time_t)updatedEpoch)
    secs -= (float)(nowEpoch - (time_t)updatedEpoch);
  return (secs > 2.0f && secs < 900.0f) ? secs : -1.0f;
}

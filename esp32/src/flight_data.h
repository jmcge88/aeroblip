#pragma once
#include <Arduino.h>

#define MAX_AIRCRAFT 8
#define MAX_BOARD_ROWS 10

struct Aircraft {
  char hex[8]; // ICAO 24-bit address - stable identity across polls
  char callsign[12];
  char registration[12];
  char type[8];
  char description[40];
  char phase[14];
  char heading_cardinal[4];
  char place[28];      // reverse-geocoded location (global 7700 alerts)
  float lat;           // NAN when unknown
  float lon;
  float altitude_ft;   // NAN when unknown
  float ground_speed_kt;
  float track;
  float distance_nm;
  float bearing_from_home;
  int vertical_rate_fpm;
  bool overhead;
  bool has_route;
  char origin[6];
  char destination[6];
  char origin_name[22];
  char destination_name[22];
  char airline[26];
  char airline_iata[4]; // for /api/logo/{iata} lookups
  char photo[128]; // thumbnail URL, only present for in-ring (enriched) aircraft
  char squawk[6];
  char emergency[12]; // readsb emergency field ("none", "general", ...)
  bool circling;      // server-side orbit detection (helicopters, holding)
};

// True when the aircraft is squawking an emergency (7500/7600/7700 or a
// non-none emergency broadcast, medevac "lifeguard" excluded)
bool aircraftAlert(const Aircraft &a);

// Watch-rule matches ride along inside overhead snapshots (see the server's
// services/watches.py) - the device toasts and chimes on new ones.
#define MAX_WATCH_EVENTS 4
struct WatchEvent {
  char id[14];
  char kind[16];   // "type"|"airline"|"registration"|...|"circling"|"squawk"|"new_type"
  char title[44];
  char message[96];
};

struct OverheadData {
  Aircraft aircraft[MAX_AIRCRAFT];
  int count;            // entries copied into aircraft[]
  int total_in_area;    // all aircraft reported by the server
  int overhead_count;
  float overhead_radius_nm;
  float area_radius_nm;
  char provider[12];
  uint32_t updated;     // server epoch seconds
  bool sun_golden;      // photo-worthy light right now (server sun block)
  WatchEvent watch_events[MAX_WATCH_EVENTS];
  int n_watch_events;
  bool valid;
  uint32_t fetched_ms;  // millis() of last successful fetch
};

struct BoardRow {
  char flight[10];
  char city[18];
  char code[6];
  char sched_hm[6];  // "16:15"
  char est_hm[6];    // "" when absent or same as scheduled
  char gate[6];
  char status[14];
};

struct BoardData {
  BoardRow arrivals[MAX_BOARD_ROWS];
  int n_arrivals;
  BoardRow departures[MAX_BOARD_ROWS];
  int n_departures;
  char airport_iata[6];
  bool unavailable;
  uint32_t updated;
  bool valid;
  uint32_t fetched_ms;
};

#define MAX_ALERTS 4

// Global squawk-7700 watch (worldwide, from /api/alerts and ws "alerts" frames)
struct AlertsData {
  Aircraft alerts[MAX_ALERTS]; // nearest-first
  int count;
  uint32_t updated;
  bool valid;
  uint32_t fetched_ms;
};

// Follow-a-flight (server-side trackers; the device only displays them).
// Arrives on the ws "follow" frame, or from /api/follow when polling.
#define MAX_FOLLOWS_SHOWN 3
struct FollowFlight {
  Aircraft ac;          // position/route/identity, same parser as overhead
  char status[14];      // waiting | live | no_coverage | landed
  float progress_pct;   // NAN when the route geometry is unknown
  int eta_s;            // -1 when unknown
  uint32_t eta_utc;     // 0 when unknown
  float dist_to_dest_nm;
};
struct FollowData {
  FollowFlight flights[MAX_FOLLOWS_SHOWN];
  int count;
  bool valid;
  uint32_t fetched_ms;
};

// Airport weather strip for the board screens (/api/wx, cached server-side)
struct WxData {
  char icao[6];
  char line[44]; // pre-formatted summary: "140/07KT  22/13C  Q1024  FEW035"
  bool valid;
  uint32_t fetched_ms;
};

// Next naked-eye ISS pass (/api/sky), shown on the quiet/ALL QUIET screens
struct SkyData {
  bool has_pass;
  uint32_t pass_start; // epoch
  uint32_t pass_end;
  int max_el;
  char start_dir[4];
  char end_dir[4];
  bool valid;
  uint32_t fetched_ms;
};

struct AppConfig {
  float overhead_radius_nm = 5.0f;
  float area_radius_nm = 60.0f;
  char airport_iata[6] = "";
  bool valid = false;
};

// Override the server base URL (default SERVER_BASE_URL from config.h)
void setServerBaseUrl(const char *url);
const char *serverBaseUrl();

// Device-local location (set in the settings portal). Sent as query params on
// every API call so the server polls THIS device's sky; all-empty means the
// server's own configured location (self-host behaviour).
void setDeviceLocation(const char *latlon, const char *radius, const char *area,
                       const char *airport);
const char *deviceQuery();   // "?lat=..&lon=..&radius=..&area=..&airport=.." or ""
const char *deviceAirport(); // "" when unset
// Split the base URL for the websocket client. Returns false if unparseable.
bool serverHostPort(char *host, size_t hostLen, uint16_t &port, bool &tls);

bool fetchConfig(AppConfig &out);
bool fetchOverhead(OverheadData &out);
bool fetchBoard(BoardData &out);
bool fetchAlerts(AlertsData &out);
bool fetchFollow(FollowData &out);
bool fetchWx(WxData &out);
bool fetchSky(SkyData &out);

// Owner-opt-in photo lookup, straight from the device to adsbdb (which serves
// planespotters.net thumbnails). Product servers never handle photo data -
// the personal-use opt-in and the traffic are the owner's own.
bool lookupPhotoUrl(const char *hex, char *out, size_t outLen);

// Parse one /ws frame ({"type":"overhead"|"board"|"alerts"|"follow","data":{...}}).
// Returns 1 if oh was filled, 2 for bd, 3 for al, 4 for fl, 0 otherwise.
int handleWsMessage(const uint8_t *payload, size_t len, OverheadData &oh, BoardData &bd,
                    AlertsData &al, FollowData &fl);

// Seconds until the aircraft enters the overhead ring, or -1 if it won't.
// Port of etaToOverhead() in static/app.js.
float etaToOverhead(const Aircraft &a, float ringNm, uint32_t updatedEpoch, time_t nowEpoch);

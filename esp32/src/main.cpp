#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <time.h>
#include <esp_random.h>

#include <Arduino_GFX_Library.h>
#include <TouchDrvCSTXXX.hpp>
#include <SensorQMI8658.hpp>
#include <WiFiManager.h>
#include <ArduinoWebsockets.h>
#include <Preferences.h>
#define XPOWERS_CHIP_AXP2101
#include <XPowersLib.h>

#include "pin_config.h"
#include "config.h"
#include "certs.h"
#include "device_id.h"
#include "flight_data.h"
#include "ota.h"
#include "photo.h"
#include "logo.h"
#include "audio.h"
#include "ui.h"

#include "qrcode.h" // ricmoo/QRCode - setup-portal join QR

#define SETUP_AP_NAME "FlightInfo-Setup"
#define SETUP_AP_PASS_LEN 10 // WPA2 needs >= 8; 10 gives ~50 bits of entropy

// The UI loop keeps multi-KB data snapshots; the default 8KB loop stack
// overflows once overhead + board + alerts all arrive
SET_LOOP_TASK_STACK_SIZE(20 * 1024);

static Arduino_DataBus *bus = new Arduino_ESP32QSPI(
    LCD_CS, LCD_SCLK, LCD_SDIO0, LCD_SDIO1, LCD_SDIO2, LCD_SDIO3);
static Arduino_CO5300 *panel = new Arduino_CO5300(
    bus, LCD_RESET, 0 /* rotation */, LCD_WIDTH, LCD_HEIGHT, 0, 0, 0, 0);
static Arduino_Canvas *canvas = new Arduino_Canvas(LCD_WIDTH, LCD_HEIGHT, panel);

static TouchDrvCST92xx touch;
static bool touchOk = false;
static XPowersPMU power;
static bool powerOk = false;
static SensorQMI8658 imu;
static bool imuOk = false;
static uint8_t g_rot = 0; // current display rotation (0-3), driven by the IMU
static volatile bool touchPending = false;
static void IRAM_ATTR onTouchIrq() { touchPending = true; }

// Shared state: written by the network task (core 0), read by the UI loop (core 1)
static SemaphoreHandle_t dataLock;
static OverheadData g_overhead = {};
static BoardData g_board = {};
static AlertsData g_alerts = {};
static AppConfig g_config;
static volatile bool g_dirty = true;

// Which screens are enabled (settable in the setup portal / web settings page)
#define SCR_OVERHEAD 0x01  // fullscreen spotlight while something is in the ring
#define SCR_NEARBY 0x02    // nearby-traffic page
#define SCR_ARRIVALS 0x04
#define SCR_DEPARTURES 0x08
#define SCR_EMERGENCY 0x10 // squawk 7500/7600/7700 alert takeover
#define SCR_ALL 0x1F
static volatile uint8_t g_screens = SCR_ALL;

// Sounds (independently toggleable in the settings portal)
#define SND_CHIME 0x01 // airport chime when a flight enters the ring
#define SND_ALARM 0x02 // alarm when a squawk alert activates
#define SND_ALL 0x03
static volatile uint8_t g_sounds = SND_ALL;

// Aircraft photos come from planespotters.net (non-commercial terms), so
// every build ships with them OFF: enabling is the owner's personal-use
// opt-in, made on their own settings page, fetched by their own device.
#define PHOTOS_DEFAULT 0
static volatile bool g_photos = PHOTOS_DEFAULT;
// Display font: 0 = classic bitmap, 1 = smooth (Roboto)
static uint8_t g_font = UI_FONT_DEFAULT;
static volatile uint8_t g_volChime = 80; // 0-100
static volatile uint8_t g_volAlarm = 80; // 0-100
static volatile uint8_t g_nightStart = NIGHT_START_HOUR; // quiet hours, settable in web UI
static volatile uint8_t g_nightEnd = NIGHT_END_HOUR;
static char g_tz[64] = TZ_STRING; // POSIX TZ, settable in web UI
static volatile bool g_ringOccupied = false; // updated from each overhead snapshot
static volatile bool g_emActive = false;     // fresh squawk alert (local or global)
static volatile bool g_emLocal = false;      // alert aircraft is in the 60NM area
static char g_alertHex[8] = "";              // identity of the active alert

static int g_view = VIEW_OVERHEAD;
static volatile uint32_t g_lastInputMs = 0; // wake window after manual input
static uint32_t g_lastFlipMs = 0;           // rotation timer (reset by manual swipes)

// Spotlighted flight (written by the UI loop, read by the net task for photos)
static char g_spotHex[8] = "";
static PhotoState g_photo = {};
static uint16_t *g_photoScratch = nullptr;

// WebSocket push channel (HTTP polling remains as fallback)
static websockets::WebsocketsClient ws;
static volatile bool g_wsConnected = false;
static volatile uint32_t g_lastWsFrameMs = 0;
static volatile bool g_wsRestart = false;
static bool g_sleeping = false;
static bool g_showInfo = false; // device-info screen (vertical swipe)
static uint32_t g_infoSince = 0;

enum NetState : int { NET_CONNECTING, NET_PORTAL, NET_ONLINE };
static volatile NetState g_netState = NET_CONNECTING;
// Set by a 3-second hold of either side key; netTask reopens the config portal
static volatile bool g_portalRequest = false;

/* ---------- settings persistence ---------- */

static String loadServerUrl() {
  Preferences p;
  p.begin("flightinfo", true);
  String s = p.getString("server", SERVER_BASE_URL);
  p.end();
  return s;
}

static uint8_t loadScreens() {
  Preferences p;
  p.begin("flightinfo", true);
  uint8_t m = p.getUChar("screens", SCR_ALL);
  p.end();
  return (m & SCR_ALL) ? (m & SCR_ALL) : SCR_ALL;
}

static uint8_t loadSounds() {
  Preferences p;
  p.begin("flightinfo", true);
  uint8_t m = p.getUChar("sounds", SND_ALL);
  p.end();
  return m & SND_ALL;
}

static uint8_t loadVolume(const char *key) {
  Preferences p;
  p.begin("flightinfo", true);
  // pre-split installs stored a single "volume" - use it as the fallback
  uint8_t v = p.getUChar(key, p.getUChar("volume", 80));
  p.end();
  return v > 100 ? 100 : v;
}

static void loadTimeSettings() {
  Preferences p;
  p.begin("flightinfo", true);
  String tz = p.getString("tz", TZ_STRING);
  snprintf(g_tz, sizeof(g_tz), "%s", tz.c_str());
  g_nightStart = p.getUChar("nstart", NIGHT_START_HOUR) % 24;
  g_nightEnd = p.getUChar("nend", NIGHT_END_HOUR) % 24;
  p.end();
}

static void saveSettings(const String &serverUrl, uint8_t screens, uint8_t sounds) {
  Preferences p;
  p.begin("flightinfo", false);
  p.putString("server", serverUrl);
  p.putUChar("screens", screens);
  p.putUChar("sounds", sounds);
  p.putUChar("photos", g_photos ? 1 : 0);
  p.putUChar("font", g_font);
  p.putUChar("volchime", g_volChime);
  p.putUChar("volalarm", g_volAlarm);
  p.putString("tz", g_tz);
  p.putUChar("nstart", g_nightStart);
  p.putUChar("nend", g_nightEnd);
  p.end();
}

/* ---------- WiFi + config portal ---------- */

static WiFiManager wm;

/* Per-device password for the setup hotspot.
 *
 * The portal must not be open. WiFiManager serves an unauthenticated firmware
 * upload route (/u) and the full settings page, and the portal comes back up
 * on its own whenever the saved WiFi fails - so an open AP hands anyone in
 * radio range a way to reflash the unit or repoint it at another server while
 * the owner's router is simply down.
 *
 * The password is random per device and generated on first boot, NOT derived
 * from anything. Deriving it from the MAC would be worthless in an
 * open-source project: the AP broadcasts its BSSID, so anyone who had read
 * this file could compute it. It is stored in NVS so it stays the same for the
 * life of the unit (a factory reset preserves it), shown in plain text on the
 * setup screen, and embedded in the join QR so the owner never types it.
 */
static char s_apPass[SETUP_AP_PASS_LEN + 1] = "";

static const char *setupApPassword() {
  if (s_apPass[0]) return s_apPass;
  Preferences p;
  p.begin("flightinfo", false);
  String saved = p.getString("appass", "");
  if (saved.length() == SETUP_AP_PASS_LEN) {
    snprintf(s_apPass, sizeof(s_apPass), "%s", saved.c_str());
  } else {
    // Alphabet without 0/O, 1/I/L - this gets read off a screen and retyped
    static const char AB[] = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"; // 31 chars
    for (int i = 0; i < SETUP_AP_PASS_LEN; i++)
      s_apPass[i] = AB[esp_random() % (sizeof(AB) - 1)];
    s_apPass[SETUP_AP_PASS_LEN] = '\0';
    p.putString("appass", s_apPass);
    Serial.printf("[wifi] generated setup-AP password: %s\n", s_apPass);
  }
  p.end();
  return s_apPass;
}

static WiFiManagerParameter *serverParam;
static WiFiManagerParameter *locParam;     // "lat, lon" paste field
static WiFiManagerParameter *ovrParam;     // spotlight ring radius NM
static WiFiManagerParameter *areaParam;    // nearby-traffic radius NM
static WiFiManagerParameter *airportParam; // board airport code
static WiFiManagerParameter *screensParam;
static char screensHtml[5600];
static char s_locVal[40] = "", s_ovrVal[8] = "", s_areaVal[8] = "", s_airportVal[6] = "";
static bool g_forcePortal = false; // USER key held at power-on

static void loadLocation() {
  Preferences p;
  p.begin("flightinfo", true);
  snprintf(s_locVal, sizeof(s_locVal), "%s", p.getString("loc", "").c_str());
  snprintf(s_ovrVal, sizeof(s_ovrVal), "%s", p.getString("rovr", "").c_str());
  snprintf(s_areaVal, sizeof(s_areaVal), "%s", p.getString("rarea", "").c_str());
  snprintf(s_airportVal, sizeof(s_airportVal), "%s", p.getString("airport", "").c_str());
  p.end();
  setDeviceLocation(s_locVal, s_ovrVal, s_areaVal, s_airportVal);
}

static void saveLocation() {
  Preferences p;
  p.begin("flightinfo", false);
  p.putString("loc", s_locVal);
  p.putString("rovr", s_ovrVal);
  p.putString("rarea", s_areaVal);
  p.putString("airport", s_airportVal);
  p.end();
}

// Timezone presets - the select posts an index, we store the POSIX string
struct TzOption {
  const char *label;
  const char *tz;
};
static const TzOption TZ_OPTIONS[] = {
    {"Brisbane", "AEST-10"},
    {"Sydney / Melbourne", "AEST-10AEDT,M10.1.0,M4.1.0/3"},
    {"Adelaide", "ACST-9:30ACDT,M10.1.0,M4.1.0/3"},
    {"Darwin", "ACST-9:30"},
    {"Perth", "AWST-8"},
    {"Auckland", "NZST-12NZDT,M9.5.0,M4.1.0/3"},
    {"Tokyo", "JST-9"},
    {"Singapore / Hong Kong", "<+08>-8"},
    {"UTC", "UTC0"},
    {"UK / Ireland", "GMT0BST,M3.5.0/1,M10.5.0"},
    {"Central Europe", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"US Eastern", "EST5EDT,M3.2.0,M11.1.0"},
    {"US Central", "CST6CDT,M3.2.0,M11.1.0"},
    {"US Mountain", "MST7MDT,M3.2.0,M11.1.0"},
    {"US Pacific", "PST8PDT,M3.2.0,M11.1.0"},
};
#define TZ_COUNT ((int)(sizeof(TZ_OPTIONS) / sizeof(TZ_OPTIONS[0])))

static const char LOC_HINT_HTML[] =
    "<br/><small>Location: paste coordinates like <b>-27.4698, 153.0251</b> "
    "(long-press your home in Google Maps, or visit "
    "<b>api.aeroblip.com/locate</b> on your phone). Leave blank to use the "
    "server's default location.</small><br/>";

static void buildScreensHtml() {
  const char *chk[5];
  chk[0] = (g_screens & SCR_OVERHEAD) ? " checked" : "";
  chk[1] = (g_screens & SCR_NEARBY) ? " checked" : "";
  chk[2] = (g_screens & SCR_ARRIVALS) ? " checked" : "";
  chk[3] = (g_screens & SCR_DEPARTURES) ? " checked" : "";
  chk[4] = (g_screens & SCR_EMERGENCY) ? " checked" : "";
  int off = snprintf(screensHtml, sizeof(screensHtml),
           "<br/><label>Screens</label><br/>"
           "<input type='checkbox' name='sc_ovhd'%s> Traffic Overhead<br/>"
           "<input type='checkbox' name='sc_near'%s> Traffic Nearby<br/>"
           "<input type='checkbox' name='sc_arr'%s> Flight Board - Arrivals<br/>"
           "<input type='checkbox' name='sc_dep'%s> Flight Board - Departures<br/>"
           "<input type='checkbox' name='sc_em'%s> 7700 Alerts<br/>"
           "<input type='checkbox' name='sc_photo'%s> Aircraft photos "
           "(planespotters.net, personal use)<br/>"
           "<br/><label>Sounds</label><br/>"
           "<input type='checkbox' name='snd_chime'%s> Airport chime on overhead "
           "<button type='button' onclick=\"fetch('/chime')\">Test</button><br/>"
           "<input type='checkbox' name='snd_alarm'%s> Alarm on 7700 alert "
           "<button type='button' onclick=\"fetch('/alarm')\">Test</button><br/>"
           "<br/><label for='vol_chime'>Chime volume</label><br/>"
           "<input type='range' name='vol_chime' id='vol_chime' min='0' max='100' value='%u' "
           "style='width:70%%' oninput=\"document.getElementById('vcv').textContent=this.value;"
           "fetch('/volume?chime='+this.value)\"> <span id='vcv'>%u</span><br/>"
           "<label for='vol_alarm'>Alarm volume</label><br/>"
           "<input type='range' name='vol_alarm' id='vol_alarm' min='0' max='100' value='%u' "
           "style='width:70%%' oninput=\"document.getElementById('vav').textContent=this.value;"
           "fetch('/volume?alarm='+this.value)\"> <span id='vav'>%u</span><br/>",
           chk[0], chk[1], chk[2], chk[3], chk[4],
           g_photos ? " checked" : "",
           (g_sounds & SND_CHIME) ? " checked" : "",
           (g_sounds & SND_ALARM) ? " checked" : "",
           (unsigned)g_volChime, (unsigned)g_volChime,
           (unsigned)g_volAlarm, (unsigned)g_volAlarm);
  off = min(off, (int)sizeof(screensHtml) - 1);
  off += snprintf(screensHtml + off, sizeof(screensHtml) - off,
                  "<br/><label for='tz'>Timezone</label><br/><select name='tz' id='tz'>");
  off = min(off, (int)sizeof(screensHtml) - 1);
  for (int i = 0; i < TZ_COUNT; i++) {
    off += snprintf(screensHtml + off, sizeof(screensHtml) - off,
                    "<option value='%d'%s>%s</option>", i,
                    strcmp(g_tz, TZ_OPTIONS[i].tz) == 0 ? " selected" : "", TZ_OPTIONS[i].label);
    off = min(off, (int)sizeof(screensHtml) - 1);
  }
  snprintf(screensHtml + off, sizeof(screensHtml) - off,
           "</select><br/>"
           "<br/><label for='font'>Display font</label><br/>"
           "<select name='font' id='font'>"
           "<option value='1'%s>Smooth (Roboto)</option>"
           "<option value='0'%s>Classic (pixel)</option>"
           "</select><br/>"
           "<br/><label>Quiet hours (screen sleeps unless traffic overhead)</label><br/>"
           "<input type='number' name='q_start' min='0' max='23' value='%u' style='width:4em'>:00 to "
           "<input type='number' name='q_end' min='0' max='23' value='%u' style='width:4em'>:00<br/>"
           "<br/><label>Device</label><br/>"
           "<button type='button' onclick=\"if(confirm('Reboot the display?'))"
           "{fetch('/reboot');document.body.innerHTML='<h3>Rebooting...</h3>';}\">"
           "Reboot</button><br/><br/>"
           "<button type='button' style='background:#b00020' "
           "onclick=\"if(confirm('Erase WiFi and ALL settings? The display returns "
           "to the setup screen.'))"
           "{fetch('/factory');document.body.innerHTML='<h3>Factory resetting... "
           "reconnect via the setup hotspot.</h3>';}\">Factory reset</button><br/>"
           // Save in the background and return to this page instead of
           // landing on WiFiManager's bare "saved" screen
           "<script>addEventListener('DOMContentLoaded',function(){"
           "var f=document.querySelector('form');if(!f)return;"
           "f.addEventListener('submit',function(e){e.preventDefault();"
           "fetch(f.getAttribute('action')||'/paramsave',{method:'POST',"
           "body:new URLSearchParams(new FormData(f))})"
           ".then(function(){location='/param'});});});</script>",
           g_font ? " selected" : "", g_font ? "" : " selected",
           (unsigned)g_nightStart, (unsigned)g_nightEnd);
}

// Fires when settings are saved in the captive portal or the web settings page
static void onSaveParams() {
  if (wm.server) {
    uint8_t m = 0;
    if (wm.server->hasArg("sc_ovhd")) m |= SCR_OVERHEAD;
    if (wm.server->hasArg("sc_near")) m |= SCR_NEARBY;
    if (wm.server->hasArg("sc_arr")) m |= SCR_ARRIVALS;
    if (wm.server->hasArg("sc_dep")) m |= SCR_DEPARTURES;
    if (wm.server->hasArg("sc_em")) m |= SCR_EMERGENCY;
    g_screens = m ? m : SCR_ALL; // nothing ticked would leave nothing to show
    g_photos = wm.server->hasArg("sc_photo");
    uint8_t s = 0;
    if (wm.server->hasArg("snd_chime")) s |= SND_CHIME;
    if (wm.server->hasArg("snd_alarm")) s |= SND_ALARM;
    g_sounds = s; // both off is a legitimate choice
    if (wm.server->hasArg("vol_chime")) {
      int v = wm.server->arg("vol_chime").toInt();
      if (v >= 0 && v <= 100) g_volChime = (uint8_t)v;
    }
    if (wm.server->hasArg("vol_alarm")) {
      int v = wm.server->arg("vol_alarm").toInt();
      if (v >= 0 && v <= 100) g_volAlarm = (uint8_t)v;
    }
    audioSetVolumes(g_volChime, g_volAlarm);
    if (wm.server->hasArg("tz")) {
      int i = wm.server->arg("tz").toInt();
      if (i >= 0 && i < TZ_COUNT && strcmp(g_tz, TZ_OPTIONS[i].tz) != 0) {
        snprintf(g_tz, sizeof(g_tz), "%s", TZ_OPTIONS[i].tz);
        configTzTime(g_tz, NTP_SERVER_1, NTP_SERVER_2); // apply without reboot
      }
    }
    if (wm.server->hasArg("q_start"))
      g_nightStart = (uint8_t)constrain(wm.server->arg("q_start").toInt(), 0, 23);
    if (wm.server->hasArg("q_end"))
      g_nightEnd = (uint8_t)constrain(wm.server->arg("q_end").toInt(), 0, 23);
    if (wm.server->hasArg("font")) {
      g_font = wm.server->arg("font").toInt() ? 1 : 0;
      uiSetFont(g_font);
    }
  }
  setServerBaseUrl(serverParam->getValue());
  snprintf(s_locVal, sizeof(s_locVal), "%s", locParam->getValue());
  snprintf(s_ovrVal, sizeof(s_ovrVal), "%s", ovrParam->getValue());
  snprintf(s_areaVal, sizeof(s_areaVal), "%s", areaParam->getValue());
  snprintf(s_airportVal, sizeof(s_airportVal), "%s", airportParam->getValue());
  setDeviceLocation(s_locVal, s_ovrVal, s_areaVal, s_airportVal);
  saveLocation();
  saveSettings(serverBaseUrl(), g_screens, g_sounds);
  buildScreensHtml(); // keep the checkboxes' state current
  g_wsRestart = true; // reconnect the websocket in case the server/location moved
  g_dirty = true;
  Serial.printf("[cfg] saved: server=%s query=%s screens=0x%02x\n", serverBaseUrl(),
                deviceQuery(), (int)g_screens);
}

// Connect using credentials stored in flash; when there are none (first boot),
// they fail, or the USER key was held at power-on, open a captive-portal
// hotspot for on-device setup (WiFi, server URL, screen selection).
static void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  wm.setConfigPortalTimeout(300); // retry STA every 5 min while portal is idle
  // Sound test endpoints, registered whenever a portal webserver starts
  // (both the setup hotspot and the persistent settings page)
  wm.setWebServerCallback([]() {
    wm.server->on("/chime", []() {
      audioPlayChime();
      wm.server->send(200, "text/plain", "chime");
    });
    wm.server->on("/alarm", []() {
      audioPlayAlarm();
      wm.server->send(200, "text/plain", "alarm");
    });
    // Live volume preview while dragging the sliders (persisted on Save)
    wm.server->on("/volume", []() {
      if (wm.server->hasArg("chime")) {
        int v = wm.server->arg("chime").toInt();
        if (v >= 0 && v <= 100) g_volChime = (uint8_t)v;
      }
      if (wm.server->hasArg("alarm")) {
        int v = wm.server->arg("alarm").toInt();
        if (v >= 0 && v <= 100) g_volAlarm = (uint8_t)v;
      }
      audioSetVolumes(g_volChime, g_volAlarm);
      wm.server->send(200, "text/plain", "ok");
    });
    wm.server->on("/reboot", []() {
      wm.server->send(200, "text/plain", "rebooting");
      Serial.println("[cfg] reboot requested from settings page");
      delay(300); // let the response flush
      ESP.restart();
    });
    wm.server->on("/factory", []() {
      wm.server->send(200, "text/plain", "factory reset");
      Serial.println("[cfg] factory reset requested from settings page");
      String token = deviceToken(); // the unit's identity survives a reset
      Preferences p;
      p.begin("flightinfo", false);
      p.clear(); // server URL, location, screens, sounds, tz, volumes
      if (token.length()) p.putString("devtoken", token);
      // Keep the setup-AP password: it may be on a label, and a unit that
      // changed it on every reset would be needlessly confusing to recover.
      p.putString("appass", setupApPassword());
      p.end();
      wm.resetSettings(); // WiFi credentials
      delay(300);
      ESP.restart(); // boots into the setup-portal QR screen
    });
  });
  wm.setAPCallback([](WiFiManager *) {
    g_netState = NET_PORTAL;
    Serial.printf("[wifi] config portal up: join AP '%s' (password %s), "
                  "browse 192.168.4.1\n", SETUP_AP_NAME, setupApPassword());
  });

  String cur = loadServerUrl();
  setServerBaseUrl(cur.c_str());
  g_screens = loadScreens();
  g_sounds = loadSounds();
  {
    Preferences p;
    p.begin("flightinfo", true);
    g_photos = p.getUChar("photos", PHOTOS_DEFAULT) != 0;
    g_font = p.getUChar("font", UI_FONT_DEFAULT) ? 1 : 0;
    p.end();
  }
  uiSetFont(g_font);
  g_volChime = loadVolume("volchime");
  g_volAlarm = loadVolume("volalarm");
  loadTimeSettings();
  loadLocation();
  audioSetVolumes(g_volChime, g_volAlarm);
  buildScreensHtml();
  serverParam = new WiFiManagerParameter("server", "Flight-info server URL", cur.c_str(), 96);
  locParam = new WiFiManagerParameter("loc", "Location (lat, lon)", s_locVal, 38);
  ovrParam = new WiFiManagerParameter("rovr", "Overhead radius NM (blank = 5)", s_ovrVal, 6);
  areaParam = new WiFiManagerParameter("rarea", "Area radius NM (blank = 60)", s_areaVal, 6);
  airportParam = new WiFiManagerParameter("airport", "Airport code (e.g. BNE)",
                                          s_airportVal, 4);
  screensParam = new WiFiManagerParameter(screensHtml);
  static WiFiManagerParameter locHint(LOC_HINT_HTML);
  wm.addParameter(serverParam);
  wm.addParameter(&locHint);
  wm.addParameter(locParam);
  wm.addParameter(ovrParam);
  wm.addParameter(areaParam);
  wm.addParameter(airportParam);
  wm.addParameter(screensParam);
  wm.setSaveParamsCallback(onSaveParams);

  // Catch a slightly-late press too: sample the USER key while the
  // "connecting" splash is up instead of only in the first instant of boot
  for (uint32_t t0 = millis(); millis() - t0 < 2000 && !g_forcePortal;) {
    if (digitalRead(KEY_USER) == LOW) g_forcePortal = true;
    vTaskDelay(pdMS_TO_TICKS(20));
  }
  if (g_forcePortal) {
    Serial.println("[wifi] USER key held - opening config portal");
    // WPA2, not open: see setupApPassword(). The password is on the splash
    // screen and in the join QR.
    wm.startConfigPortal(SETUP_AP_NAME, setupApPassword()); // blocks until saved/timeout
  }
  while (!wm.autoConnect(SETUP_AP_NAME, setupApPassword())) {
    g_netState = NET_CONNECTING;
    Serial.println("[wifi] not configured/connected yet, retrying...");
    vTaskDelay(pdMS_TO_TICKS(1000));
  }
  g_netState = NET_ONLINE;
  Serial.printf("[wifi] connected to %s, ip=%s, server=%s, screens=0x%02x\n", WiFi.SSID().c_str(),
                WiFi.localIP().toString().c_str(), serverBaseUrl(), (int)g_screens);

  // Keep the settings page available at http://<device-ip> while running
  wm.setParamsPage(true);
  wm.startWebPortal();
}

/* ---------- network task ---------- */

static void onWsMessage(websockets::WebsocketsMessage msg) {
  if (!msg.isText()) return;
  const auto &raw = msg.rawData();
  // static: several KB each, and this callback only ever runs on the net task
  static OverheadData oh;
  static BoardData bd;
  static AlertsData al;
  int which = handleWsMessage((const uint8_t *)raw.data(), raw.size(), oh, bd, al);
  if (which) {
    if (!g_wsConnected) Serial.println("[ws] receiving frames");
    g_wsConnected = true;
    g_lastWsFrameMs = millis();
    xSemaphoreTake(dataLock, portMAX_DELAY);
    if (which == 1) g_overhead = oh;
    else if (which == 2) g_board = bd;
    else g_alerts = al;
    xSemaphoreGive(dataLock);
    g_dirty = true;
  }
}

static void onWsEvent(websockets::WebsocketsEvent event, String data) {
  using websockets::WebsocketsEvent;
  if (event == WebsocketsEvent::ConnectionOpened) {
    g_wsConnected = true;
    g_lastWsFrameMs = millis();
    Serial.println("[ws] connected");
  } else if (event == WebsocketsEvent::ConnectionClosed) {
    if (g_wsConnected) Serial.println("[ws] disconnected");
    g_wsConnected = false;
  }
}

static void wsBegin() {
  char host[64];
  uint16_t port;
  bool tls;
  if (!serverHostPort(host, sizeof(host), port, tls)) return;
  ws.close();
  if (tls) {
#ifdef PRODUCT_BUILD
    ws.setCACert(PINNED_ROOTS); // pinned root bundle - see certs.h
#else
    ws.setInsecure(); // dev/self-host: accept self-signed certs
#endif
  }
  char url[224];
  snprintf(url, sizeof(url), "%s://%s:%u/ws%s", tls ? "wss" : "ws", host, port, deviceQuery());
  Serial.printf("[ws] connecting to %s\n", url);
  if (!ws.connect(url)) g_wsConnected = false;
}

// Fetch the photo for the spotlighted aircraft once per hex
static void servicePhoto() {
  static char attempted[8] = "";
  static char waitingFor[8] = "";
  static uint32_t waitingSince = 0;
  if (!g_photo.buf || !g_photoScratch) return;
  char want[8];
  memcpy(want, (const void *)g_spotHex, sizeof(want));
  want[7] = '\0';
  if (!want[0] || !g_photos) {
    if (g_photo.valid) {
      xSemaphoreTake(dataLock, portMAX_DELAY);
      g_photo.valid = false;
      xSemaphoreGive(dataLock);
    }
    attempted[0] = '\0';
    waitingFor[0] = '\0';
    return;
  }
  if (strcmp(want, attempted) == 0) return;

  char url[128] = "";
  xSemaphoreTake(dataLock, portMAX_DELAY);
  for (int i = 0; i < g_overhead.count; i++)
    if (strcmp(g_overhead.aircraft[i].hex, want) == 0) {
      snprintf(url, sizeof(url), "%s", g_overhead.aircraft[i].photo);
      break;
    }
  xSemaphoreGive(dataLock);
  if (!url[0]) {
    // Give server-side enrichment a few seconds (self-hosted servers send the
    // URL); product servers never do, so the owner's opt-in falls back to a
    // direct device-side lookup
    if (strcmp(want, waitingFor) != 0) {
      snprintf(waitingFor, sizeof(waitingFor), "%s", want);
      waitingSince = millis();
      return;
    }
    if (millis() - waitingSince < 8000) return;
    snprintf(attempted, sizeof(attempted), "%s", want); // one lookup per hex
    if (!lookupPhotoUrl(want, url, sizeof(url))) return;
  }

  snprintf(attempted, sizeof(attempted), "%s", want);
  int w = 0, h = 0;
  bool ok = fetchAircraftPhoto(url, g_photoScratch, PHOTO_W, PHOTO_H, w, h);
  xSemaphoreTake(dataLock, portMAX_DELAY);
  if (ok && w > 0 && h > 0) {
    memcpy(g_photo.buf, g_photoScratch, (size_t)w * h * 2);
    g_photo.w = w;
    g_photo.h = h;
    snprintf(g_photo.hex, sizeof(g_photo.hex), "%s", want);
    g_photo.valid = true;
  } else {
    g_photo.valid = false;
  }
  xSemaphoreGive(dataLock);
  g_dirty = true;
  Serial.printf("[photo] %s for %s (%dx%d)\n", ok ? "ok" : "failed", want, w, h);
}

static void netTask(void *) {
  connectWiFi();
  configTzTime(g_tz, NTP_SERVER_1, NTP_SERVER_2);

  AppConfig cfg;
  while (!fetchConfig(cfg)) {
    Serial.println("[net] /api/config failed, retrying...");
    wm.process();
    // A device stuck here (bad token, broken server contract) must still be
    // able to take a firmware fix - OTA can't live only past this loop
    otaService();
    vTaskDelay(pdMS_TO_TICKS(3000));
  }
  // Device-local settings win over the server's defaults
  if (deviceAirport()[0])
    snprintf(cfg.airport_iata, sizeof(cfg.airport_iata), "%s", deviceAirport());
  xSemaphoreTake(dataLock, portMAX_DELAY);
  g_config = cfg;
  xSemaphoreGive(dataLock);
  Serial.printf("[net] config ok: airport=%s area=%.0fnm\n", cfg.airport_iata, cfg.area_radius_nm);

  ws.onMessage(onWsMessage);
  ws.onEvent(onWsEvent);
  // once, not per-connect: the library appends rather than replaces headers
  if (deviceToken()[0]) ws.addHeader("X-Device-Token", deviceToken());
  wsBegin();

  uint32_t lastOverhead = 0, lastBoard = 0, lastWsAttempt = millis();
  for (;;) {
    if (g_portalRequest) { // 3-second key hold: reopen the setup portal live
      g_portalRequest = false;
      Serial.println("[wifi] key held - reopening config portal");
      ws.close();
      g_wsConnected = false;
      wm.stopWebPortal();
      g_netState = NET_PORTAL;
      wm.startConfigPortal(SETUP_AP_NAME, setupApPassword()); // blocks until saved/timeout
      g_netState = WiFi.status() == WL_CONNECTED ? NET_ONLINE : NET_CONNECTING;
      wm.setParamsPage(true);
      wm.startWebPortal();
      g_wsRestart = true;
      g_dirty = true;
    }
    wm.process(); // serve the web settings page
    ws.poll();
    if (g_wsRestart) {
      g_wsRestart = false;
      lastWsAttempt = millis();
      wsBegin();
    }
    // Reconnect when closed or silent (no auto-reconnect in this library)
    if ((!ws.available() || millis() - g_lastWsFrameMs > 2 * WS_SILENCE_MS) &&
        millis() - lastWsAttempt > 5000 && WiFi.status() == WL_CONNECTED) {
      lastWsAttempt = millis();
      wsBegin();
    }
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("[wifi] lost, reconnecting...");
      WiFi.disconnect();
      WiFi.reconnect();
      vTaskDelay(pdMS_TO_TICKS(2000));
      continue;
    }
    // Server pushes overhead every poll (~5s); fall back to HTTP when silent
    bool wsAlive = g_wsConnected && (millis() - g_lastWsFrameMs) < WS_SILENCE_MS;
    uint32_t now = millis();
    if (!wsAlive && (now - lastOverhead >= POLL_OVERHEAD_MS || lastOverhead == 0)) {
      lastOverhead = now;
      OverheadData tmp;
      if (fetchOverhead(tmp)) {
        xSemaphoreTake(dataLock, portMAX_DELAY);
        g_overhead = tmp;
        xSemaphoreGive(dataLock);
        g_dirty = true;
      }
    }
    if (!wsAlive && (now - lastBoard >= POLL_BOARD_MS || lastBoard == 0)) {
      lastBoard = now;
      BoardData tmp;
      if (fetchBoard(tmp)) {
        xSemaphoreTake(dataLock, portMAX_DELAY);
        g_board = tmp;
        xSemaphoreGive(dataLock);
        g_dirty = true;
      }
    }
    static uint32_t lastAlerts = 0;
    if (!wsAlive && (now - lastAlerts >= POLL_ALERTS_MS || lastAlerts == 0)) {
      lastAlerts = now;
      AlertsData tmp;
      if (fetchAlerts(tmp)) {
        xSemaphoreTake(dataLock, portMAX_DELAY);
        g_alerts = tmp;
        xSemaphoreGive(dataLock);
        g_dirty = true;
      }
    }
    servicePhoto();
    serviceLogos();
    otaService();
    vTaskDelay(pdMS_TO_TICKS(50));
  }
}

/* ---------- orientation (QMI8658 accelerometer) ---------- */

// Map raw touch (rotation-0 panel coordinates) into the current logical
// orientation, inverse of Arduino_Canvas's rotation transform.
static void mapTouch(int16_t &tx, int16_t &ty) {
  int16_t X = tx, Y = ty;
  switch (g_rot) {
    case 1: tx = Y; ty = LCD_HEIGHT - 1 - X; break;
    case 2: tx = LCD_WIDTH - 1 - X; ty = LCD_HEIGHT - 1 - Y; break;
    case 3: tx = LCD_WIDTH - 1 - Y; ty = X; break;
  }
}

static void pollOrientation() {
  static uint32_t lastCheck = 0;
  static uint8_t candidate = 0, stableCount = 0;
  if (!imuOk || millis() - lastCheck < 300) return;
  lastCheck = millis();

  IMUdata acc;
  if (!imu.getDataReady() || !imu.getAccelerometer(acc.x, acc.y, acc.z)) return;

  int want = -1;
  if (acc.y > 0.8f) want = 0;
  else if (acc.x > 0.8f) want = 1;
  else if (acc.y < -0.8f) want = 2;
  else if (acc.x < -0.8f) want = 3;
  if (want < 0) { stableCount = 0; return; } // flat/ambiguous: keep current

  if ((uint8_t)want == g_rot) { stableCount = 0; return; }
  if ((uint8_t)want != candidate) {
    candidate = (uint8_t)want;
    stableCount = 1;
    return;
  }
  if (++stableCount >= 3) { // ~1s of agreement before rotating
    g_rot = candidate;
    canvas->setRotation(g_rot);
    stableCount = 0;
    g_dirty = true;
    Serial.printf("[imu] rotated display to %d\n", (int)g_rot);
  }
}

/* ---------- input ---------- */

// Pages currently in the rotation. The air page (overhead/nearby) is included
// when the nearby screen is ticked, or - with only overhead ticked - while
// something is actually in the ring.
static int buildPages(int *pages) {
  int n = 0;
  if ((g_screens & SCR_EMERGENCY) && g_emActive) pages[n++] = VIEW_EMERGENCY;
  if ((g_screens & SCR_OVERHEAD) && g_ringOccupied) pages[n++] = VIEW_OVERHEAD;
  if (g_screens & SCR_NEARBY) pages[n++] = VIEW_NEARBY;
  if (g_screens & SCR_DEPARTURES) pages[n++] = VIEW_DEPARTURES;
  if (g_screens & SCR_ARRIVALS) pages[n++] = VIEW_ARRIVALS;
  if (n == 0) pages[n++] = VIEW_OVERHEAD;
  return n;
}

static int pageIndex(const int *pages, int n) {
  for (int i = 0; i < n; i++)
    if (pages[i] == g_view) return i;
  return 0;
}

static void switchView(int delta) {
  g_lastInputMs = millis();
  g_dirty = true;
  if (g_showInfo) { g_showInfo = false; return; } // any input closes the info screen
  if (g_sleeping) return; // first press/swipe only wakes the screen

  int pages[VIEW_COUNT];
  int n = buildPages(pages);
  g_view = pages[(pageIndex(pages, n) + delta + n) % n];
  g_lastFlipMs = millis(); // fresh 30s slot on the chosen page, rotation continues
}

static void pollTouch() {
  static bool gestureActive = false;
  static int16_t startX, startY, lastX, lastY;

  if (!touchOk) return;
  bool pending = touchPending;
  touchPending = false;
  if (!pending && !gestureActive) return;

  int16_t x[2], y[2];
  uint8_t n = touch.getPoint(x, y, 2);
  if (n > 0) mapTouch(x[0], y[0]);
  if (n > 0) {
    if (!gestureActive) {
      gestureActive = true;
      startX = x[0];
      startY = y[0];
    }
    lastX = x[0];
    lastY = y[0];
  } else if (gestureActive) {
    gestureActive = false;
    int dx = lastX - startX, dy = lastY - startY;
    if (abs(dy) > 70 && abs(dy) > 2 * abs(dx)) {
      // Vertical swipe (either direction) toggles the device-info screen
      g_lastInputMs = millis();
      g_showInfo = !g_showInfo && !g_sleeping;
      if (g_showInfo) g_infoSince = millis();
      g_dirty = true;
    } else if (abs(dx) > 70 && abs(dx) > 2 * abs(dy)) {
      switchView(dx < 0 ? +1 : -1);
    } else if (g_sleeping || g_showInfo) {
      switchView(0); // tap wakes / closes info
    }
  }
}

static void pollButtons() {
  static uint32_t lastPress = 0, heldSince = 0;
  static bool userWas = true, bootWas = true;
  bool user = digitalRead(KEY_USER);
  bool boot = digitalRead(KEY_BOOT);
  uint32_t now = millis();
  // Hold either key 3s to reopen the setup portal (no power-cycle gymnastics;
  // safe for BOOT/GPIO0 too since its strapping only matters at reset)
  if (!user || !boot) {
    if (!heldSince) heldSince = now;
    if (now - heldSince > 3000 && g_netState != NET_PORTAL) {
      heldSince = 0;
      g_portalRequest = true;
      g_lastInputMs = now;
      g_dirty = true;
    }
  } else {
    heldSince = 0;
  }
  if (now - lastPress > 250) {
    if (!user && userWas) { switchView(+1); lastPress = now; }
    if (!boot && bootWas) { switchView(-1); lastPress = now; }
  }
  userWas = user;
  bootWas = boot;
}

/* ---------- display state machine ---------- */

static bool inQuietHours() {
  struct tm tmNow;
  if (!getLocalTime(&tmNow, 5)) return false;
  int s = g_nightStart, e = g_nightEnd, h = tmNow.tm_hour;
  if (s == e) return false; // start == end disables quiet hours
  return s < e ? (h >= s && h < e) : (h >= s || h < e);
}

static int g_lastBrightness = -1;

static void setBrightnessLevel(int level) {
  if (level != g_lastBrightness) {
    panel->setBrightness(level);
    g_lastBrightness = level;
  }
}

static void applyBrightness(bool sleeping) {
  setBrightnessLevel(sleeping ? BRIGHT_SLEEP : (inQuietHours() ? BRIGHT_NIGHT : BRIGHT_DAY));
}

// Decide sleep/wake and (when not manually overridden) which view to show.
// Returns seconds until the next automatic page change, or -1 when none is due.
static int chooseView(const OverheadData &oh, const BoardData &bd) {
  bool spotlight = (g_screens & SCR_OVERHEAD) && g_ringOccupied;
  bool alert = (g_screens & SCR_EMERGENCY) && g_emActive;

  // Global alerts can stay active for hours: take over for the first couple
  // of minutes, then join the normal rotation (the rot[] cycle below keeps
  // the page in it) so the AMOLED isn't pinned on a static red screen
  static char lastAlertHex[8] = "";
  static uint32_t alertSince = 0;
  int holdLeft = -1; // seconds until a global alert demotes into the rotation
  if (alert && !g_emLocal) {
    if (strcmp(g_alertHex, lastAlertHex) != 0) {
      snprintf(lastAlertHex, sizeof(lastAlertHex), "%s", g_alertHex);
      alertSince = millis();
    }
    uint32_t held = millis() - alertSince;
    if (held > GLOBAL_ALERT_TAKEOVER_MS)
      alert = false; // demoted
    else
      holdLeft = (int)((GLOBAL_ALERT_TAKEOVER_MS - held) / 1000) + 1;
  } else if (!g_emActive) {
    lastAlertHex[0] = '\0';
  }
  bool showAir = (g_screens & SCR_NEARBY) && oh.valid && oh.count > 0;
  bool showDeps = (g_screens & SCR_DEPARTURES) && bd.valid && !bd.unavailable && bd.n_departures > 0;
  bool showArrs = (g_screens & SCR_ARRIVALS) && bd.valid && !bd.unavailable && bd.n_arrivals > 0;
  bool manualHold = millis() - g_lastInputMs < MANUAL_HOLD_MS && g_lastInputMs != 0;
  bool quiet = inQuietHours();

  bool wasSleeping = g_sleeping;
  g_sleeping = !spotlight && !alert && !manualHold &&
               (quiet || !(showAir || showDeps || showArrs));
  if (wasSleeping != g_sleeping) g_dirty = true;

  // Takeover: snap to the alert/overhead page the moment one becomes active.
  // With both active, alternate between them. Manual swipes may visit other
  // screens - the slot timer (reset by each swipe) brings the takeover back.
  int takeover = -1;
  if (alert && spotlight)
    takeover = ((millis() / ALERT_ALTERNATE_MS) & 1) ? VIEW_OVERHEAD : VIEW_EMERGENCY;
  else if (alert)
    takeover = VIEW_EMERGENCY;
  else if (spotlight)
    takeover = VIEW_OVERHEAD;

  static bool wasTakeover = false;
  if (takeover >= 0) {
    bool onTakeoverPage = (g_view == VIEW_OVERHEAD) || (g_view == VIEW_EMERGENCY);
    // A fresh manual choice owns its slot even on takeover pages - without
    // this, swiping off (or onto) the alert page is snapped away instantly
    bool manualFresh = g_lastInputMs != 0 && millis() - g_lastInputMs < BOARD_FLIP_MS;
    if (!wasTakeover) {
      wasTakeover = true;
      g_lastFlipMs = millis();
      if (g_view != takeover) {
        g_view = takeover;
        g_dirty = true;
      }
      return holdLeft;
    }
    if (g_view == takeover) return holdLeft;
    if (onTakeoverPage && !manualFresh) {
      g_view = takeover;
      g_dirty = true;
      return holdLeft;
    }
    if (millis() - g_lastFlipMs >= BOARD_FLIP_MS) {
      g_view = takeover;
      g_dirty = true;
      g_lastFlipMs = millis();
      return holdLeft;
    }
    return (int)((BOARD_FLIP_MS - (millis() - g_lastFlipMs)) / 1000) + 1;
  }
  wasTakeover = false;
  if (g_sleeping) return -1;

  // Nothing overhead: rotate through every active screen with content. A
  // manual swipe resets the timer (in switchView) but never pauses rotation.
  int rot[VIEW_COUNT];
  int rn = 0;
  // Reaching here with an active alert means it was demoted above: keep it cycling
  if ((g_screens & SCR_EMERGENCY) && g_emActive) rot[rn++] = VIEW_EMERGENCY;
  if (showAir) rot[rn++] = VIEW_NEARBY; // nearby traffic
  if (showDeps) rot[rn++] = VIEW_DEPARTURES;
  if (showArrs) rot[rn++] = VIEW_ARRIVALS;
  if (rn == 0) return -1; // sleep logic above already covers this

  int cur = -1;
  for (int i = 0; i < rn; i++)
    if (rot[i] == g_view) cur = i;
  int want = g_view; // off-rotation pages (manually chosen) hold their slot too
  if (millis() - g_lastFlipMs >= BOARD_FLIP_MS) {
    want = (cur < 0) ? rot[0] : rot[(cur + 1) % rn];
    g_lastFlipMs = millis();
  }
  if (want != g_view) {
    g_view = want;
    g_dirty = true;
  }
  // Show the countdown whenever a page change is actually scheduled
  return (rn >= 2 || cur < 0) ? (int)((BOARD_FLIP_MS - (millis() - g_lastFlipMs)) / 1000) + 1 : -1;
}

/* ---------- splash / provisioning screen ---------- */

static void drawSplash(const char *line1, const char *line2, const char *line3) {
  canvas->fillScreen(RGB565_BLACK);
  canvas->setTextSize(4);
  canvas->setTextColor(RGB565(255, 176, 0));
  canvas->setCursor((LCD_WIDTH - 11 * 24) / 2, 150);
  canvas->print("FLIGHT INFO");
  canvas->setTextSize(2);
  canvas->setTextColor(RGB565(200, 200, 200));
  const char *lines[3] = {line1, line2, line3};
  int y = 230;
  for (int i = 0; i < 3; i++, y += 30) {
    if (!lines[i]) continue;
    canvas->setCursor((LCD_WIDTH - (int)strlen(lines[i]) * 12) / 2, y);
    canvas->print(lines[i]);
  }
  canvas->flush();
}

// Firmware-update screen: progress bar + the one instruction that matters.
// Drawn by the UI task while the net task downloads and flashes.
static void drawOtaScreen(int pct, const char *toVersion) {
  canvas->fillScreen(RGB565_BLACK);
  canvas->setTextSize(3);
  canvas->setTextColor(RGB565(255, 176, 0));
  const char *title = "FIRMWARE UPDATE";
  canvas->setCursor((LCD_WIDTH - (int)strlen(title) * 18) / 2, 140);
  canvas->print(title);

  char ver[40];
  snprintf(ver, sizeof(ver), "v%s  >  v%s", FW_VERSION, toVersion[0] ? toVersion : "?");
  canvas->setTextSize(2);
  canvas->setTextColor(RGB565(200, 200, 200));
  canvas->setCursor((LCD_WIDTH - (int)strlen(ver) * 12) / 2, 190);
  canvas->print(ver);

  const int bx = 90, bw = LCD_WIDTH - 180, by = 240, bh = 28;
  canvas->drawRect(bx, by, bw, bh, RGB565(200, 200, 200));
  if (pct >= 0)
    canvas->fillRect(bx + 2, by + 2, (bw - 4) * pct / 100, bh - 4, RGB565(255, 176, 0));

  char p[24];
  if (pct >= 0) snprintf(p, sizeof(p), "%d%%", pct);
  else snprintf(p, sizeof(p), "CONNECTING...");
  canvas->setCursor((LCD_WIDTH - (int)strlen(p) * 12) / 2, 288);
  canvas->print(p);

  canvas->setTextColor(RGB565(255, 90, 90));
  const char *warn = "DO NOT UNPLUG";
  canvas->setCursor((LCD_WIDTH - (int)strlen(warn) * 12) / 2, 330);
  canvas->print(warn);
  canvas->setTextColor(RGB565(120, 120, 120));
  const char *sub = "RESTARTS AUTOMATICALLY WHEN DONE";
  canvas->setCursor((LCD_WIDTH - (int)strlen(sub) * 12) / 2, 360);
  canvas->print(sub);
  canvas->flush();
}

// Setup-portal screen: a WIFI: join QR so one phone scan connects to the
// hotspot (the captive portal then opens itself), with manual steps below.
static void drawPortalSplash() {
  canvas->fillScreen(RGB565_BLACK);
  canvas->setTextSize(3);
  canvas->setTextColor(RGB565(255, 176, 0));
  const char *title = "WIFI SETUP";
  canvas->setCursor((LCD_WIDTH - (int)strlen(title) * 18) / 2, 26);
  canvas->print(title);

  canvas->setTextSize(2);
  canvas->setTextColor(RGB565(200, 200, 200));
  const char *hint = "SCAN TO CONNECT";
  canvas->setCursor((LCD_WIDTH - (int)strlen(hint) * 12) / 2, 72);
  canvas->print(hint);

  /* The AP is WPA2-protected, so the QR carries the password too:
     "WIFI:T:WPA;S:<ssid>;P:<pass>;;" is ~44 bytes, which overflows a version-4
     symbol in byte mode (42 B at ECC_MEDIUM) - hence version 5 (60 B), 37
     modules square. Scanning it joins the hotspot without anyone typing the
     password; it is also printed below for manual entry. */
  char joinSpec[64];
  snprintf(joinSpec, sizeof(joinSpec), "WIFI:T:WPA;S:%s;P:%s;;",
           SETUP_AP_NAME, setupApPassword());
  QRCode qr;
  static uint8_t qrData[192]; // >= qrcode_getBufferSize(5) == 172
  int qrBottom = 104;
  if (qrcode_initText(&qr, qrData, 5, ECC_MEDIUM, joinSpec) == 0) {
    const int scale = 5;
    const int quiet = 2 * scale; // quiet zone so scanners lock on
    const int side = qr.size * scale + 2 * quiet;
    const int x0 = (LCD_WIDTH - side) / 2, y0 = 100;
    canvas->fillRect(x0, y0, side, side, RGB565_WHITE);
    for (int yy = 0; yy < qr.size; yy++)
      for (int xx = 0; xx < qr.size; xx++)
        if (qrcode_getModule(&qr, xx, yy))
          canvas->fillRect(x0 + quiet + xx * scale, y0 + quiet + yy * scale,
                           scale, scale, RGB565_BLACK);
    qrBottom = y0 + side;
  }

  int y = qrBottom + 16;
  canvas->setTextColor(RGB565(200, 200, 200));
  const char *hotspot = "OR JOIN: " SETUP_AP_NAME;
  canvas->setCursor((LCD_WIDTH - (int)strlen(hotspot) * 12) / 2, y);
  canvas->print(hotspot);
  y += 32;

  // The password in plain text, amber so it reads as the thing to type
  char pw[40];
  snprintf(pw, sizeof(pw), "PASSWORD: %s", setupApPassword());
  canvas->setTextColor(RGB565(255, 176, 0));
  canvas->setCursor((LCD_WIDTH - (int)strlen(pw) * 12) / 2, y);
  canvas->print(pw);
  y += 32;

  canvas->setTextColor(RGB565(200, 200, 200));
  const char *browse = "THEN BROWSE TO 192.168.4.1";
  canvas->setCursor((LCD_WIDTH - (int)strlen(browse) * 12) / 2, y);
  canvas->print(browse);
  canvas->flush();
}

/* ---------- arduino ---------- */

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[boot] flight-info AMOLED display");
  otaBootGuard(); // roll back a crash-looping OTA before touching anything else

  pinMode(KEY_USER, INPUT_PULLUP);
  pinMode(KEY_BOOT, INPUT_PULLUP);
  g_forcePortal = (digitalRead(KEY_USER) == LOW); // hold USER at power-on to reconfigure

  Wire.begin(IIC_SDA, IIC_SCL);

  if (!canvas->begin()) {
    Serial.println("[boot] canvas/panel begin FAILED");
  }
  bus->writeC8D8(0x36, 0xA0); // panel orientation, per vendor sample
  panel->setBrightness(BRIGHT_DAY);

  powerOk = power.begin(Wire, AXP2101_SLAVE_ADDRESS, IIC_SDA, IIC_SCL);
  if (!powerOk) Serial.println("[boot] AXP2101 not found - battery status unavailable");

  imuOk = imu.begin(Wire, QMI8658_L_SLAVE_ADDRESS, IIC_SDA, IIC_SCL);
  if (imuOk) {
    imu.configAccelerometer(SensorQMI8658::ACC_RANGE_4G, SensorQMI8658::ACC_ODR_125Hz,
                            SensorQMI8658::LPF_MODE_0);
    imu.enableAccelerometer();
  } else {
    Serial.println("[boot] QMI8658 not found - auto-rotation disabled");
  }

  audioInit(); // after Wire.begin; harmless no-op if the codec is missing

  touch.setPins(TP_RST, TP_INT);
  touchOk = touch.begin(Wire, CST92XX_SLAVE_ADDRESS, IIC_SDA, IIC_SCL);
  if (touchOk) {
    touch.setMaxCoordinates(LCD_WIDTH, LCD_HEIGHT);
    touch.setSwapXY(true);
    touch.setMirrorXY(true, false);
    pinMode(TP_INT, INPUT_PULLUP);
    attachInterrupt(digitalPinToInterrupt(TP_INT), onTouchIrq, FALLING);
    Serial.printf("[boot] touch ok: %s\n", touch.getModelName());
  } else {
    Serial.println("[boot] touch not found - swipe disabled, buttons still work");
  }

  drawSplash("CONNECTING TO WIFI...", nullptr, nullptr);

  g_photo.buf = (uint16_t *)heap_caps_malloc(PHOTO_W * PHOTO_H * 2, MALLOC_CAP_SPIRAM);
  g_photoScratch = (uint16_t *)heap_caps_malloc(PHOTO_W * PHOTO_H * 2, MALLOC_CAP_SPIRAM);
  if (!g_photo.buf || !g_photoScratch)
    Serial.println("[boot] photo buffers unavailable - photos disabled");

  dataLock = xSemaphoreCreateMutex();
  xTaskCreatePinnedToCore(netTask, "net", 20480, nullptr, 1, nullptr, 0);
}

void loop() {
  // A running update owns the screen: redraw on progress (or 1s heartbeat),
  // ignore inputs, and never let stale flight data paint over the warning
  if (otaInProgress()) {
    // Full brightness regardless of quiet hours - an update screen drawn at
    // sleep brightness (8/255) is an invisible update screen
    setBrightnessLevel(BRIGHT_DAY);
    static int lastPct = -2;
    static uint32_t lastOtaDraw = 0;
    if (otaProgressPct() != lastPct || millis() - lastOtaDraw > 1000) {
      lastPct = otaProgressPct();
      lastOtaDraw = millis();
      drawOtaScreen(lastPct, otaTargetVersion());
    }
    g_dirty = true; // repaint the normal UI if the update fails and we resume
    delay(20);
    return;
  }

  pollTouch();
  pollButtons();
  pollOrientation();
  devicePollSerial(); // PROVISION/DEVINFO commands from tools/flash_product.py

  static uint32_t lastDraw = 0;
  uint32_t now = millis();
  if (g_dirty || now - lastDraw >= 1000) {
    g_dirty = false;
    lastDraw = now;

    // static: keeps ~6KB of snapshots off the loop task stack
    static OverheadData oh;
    static BoardData bd;
    static AlertsData al;
    static AppConfig cfg;
    xSemaphoreTake(dataLock, portMAX_DELAY);
    oh = g_overhead;
    bd = g_board;
    al = g_alerts;
    cfg = g_config;
    xSemaphoreGive(dataLock);

    if (g_showInfo && millis() - g_infoSince > 60000) g_showInfo = false; // auto-hide

    // Stale data must release the takeovers - otherwise a fetch outage would
    // freeze a long-gone flight "overhead" indefinitely
    bool fresh = oh.valid && (millis() - oh.fetched_ms) < 3 * STALE_AFTER_MS;
    g_ringOccupied = fresh && oh.overhead_count > 0;
    int emIdx = -1;
    const Aircraft *galert = nullptr;
    if (g_screens & SCR_EMERGENCY) {
      if (fresh) {
        for (int i = 0; i < oh.count; i++)
          if (aircraftAlert(oh.aircraft[i])) {
            emIdx = i;
            break;
          }
      }
      // No local alert: fall back to the global 7700 watch (nearest first)
      if (emIdx < 0 && al.valid && al.count > 0 &&
          (millis() - al.fetched_ms) < ALERTS_FRESH_MS) {
        galert = &al.alerts[0];
      }
    }
    g_emActive = emIdx >= 0 || galert != nullptr;
    g_emLocal = emIdx >= 0;
    const Aircraft *alertAc = emIdx >= 0 ? &oh.aircraft[emIdx] : galert;
    snprintf(g_alertHex, sizeof(g_alertHex), "%s", alertAc ? alertAc->hex : "");

    // Sound triggers on state transitions (chime respects quiet hours;
    // an emergency alarm does not). A new alert aircraft re-alarms.
    static bool prevRing = false;
    static char prevAlertHex[8] = "";
    if (g_ringOccupied && !prevRing && (g_sounds & SND_CHIME) && !inQuietHours())
      audioPlayChime();
    if (g_emActive && strcmp(g_alertHex, prevAlertHex) != 0 && (g_sounds & SND_ALARM))
      audioPlayAlarm();
    prevRing = g_ringOccupied;
    snprintf(prevAlertHex, sizeof(prevAlertHex), "%s", g_alertHex);

    if (g_netState == NET_PORTAL) {
      // Portal takes the screen even when old data is still around (e.g. the
      // 3s-hold gesture reopened it while the device was happily online)
      drawPortalSplash();
    } else if (WiFi.status() == WL_CONNECTED || oh.valid) {
      int flipIn = chooseView(oh, bd);
      if (g_sleeping) {
        g_showInfo = false;
        uiDrawSleep(canvas);
      } else if (g_showInfo) {
        DeviceInfo di = {};
        snprintf(di.ssid, sizeof(di.ssid), "%s", WiFi.SSID().c_str());
        di.rssi = WiFi.RSSI();
        snprintf(di.ip, sizeof(di.ip), "%s", WiFi.localIP().toString().c_str());
        snprintf(di.server, sizeof(di.server), "%s", serverBaseUrl());
        snprintf(di.screens, sizeof(di.screens), "%s%s%s%s%s",
                 (g_screens & SCR_OVERHEAD) ? "OVHD " : "",
                 (g_screens & SCR_NEARBY) ? "NEAR " : "",
                 (g_screens & SCR_ARRIVALS) ? "ARR " : "",
                 (g_screens & SCR_DEPARTURES) ? "DEP " : "",
                 (g_screens & SCR_EMERGENCY) ? "7700" : "");
        if (!powerOk) {
          snprintf(di.battery, sizeof(di.battery), "UNKNOWN");
        } else if (!power.isBatteryConnect()) {
          snprintf(di.battery, sizeof(di.battery), "USB POWER (NO BATTERY)");
        } else {
          snprintf(di.battery, sizeof(di.battery), "%d%% %s", power.getBatteryPercent(),
                   power.isCharging() ? "CHARGING" : (power.isVbusIn() ? "ON USB" : "DISCHARGING"));
        }
        snprintf(di.fw, sizeof(di.fw), "%s", FW_VERSION);
        di.uptime_s = millis() / 1000;
        uiDrawInfo(canvas, di);
      } else {
        int pages[VIEW_COUNT];
        int n = buildPages(pages);
        // Sticky spotlight: stay on one in-ring flight until it leaves the
        // ring, then move to the next (tablet app behaviour)
        int spotIdx = -1;
        if ((g_screens & SCR_OVERHEAD) && g_ringOccupied) {
          for (int i = 0; i < oh.count; i++)
            if (oh.aircraft[i].overhead && g_spotHex[0] && strcmp(oh.aircraft[i].hex, g_spotHex) == 0) {
              spotIdx = i;
              break;
            }
          if (spotIdx < 0)
            for (int i = 0; i < oh.count; i++)
              if (oh.aircraft[i].overhead) {
                spotIdx = i;
                snprintf(g_spotHex, sizeof(g_spotHex), "%s", oh.aircraft[i].hex);
                break;
              }
        } else {
          g_spotHex[0] = '\0';
        }
        PhotoState ph;
        xSemaphoreTake(dataLock, portMAX_DELAY);
        ph = g_photo;
        xSemaphoreGive(dataLock);
        uiDraw(canvas, g_view, oh, bd, cfg, WiFi.status() == WL_CONNECTED,
               g_view == VIEW_OVERHEAD ? spotIdx : -1, emIdx, galert, n,
               pageIndex(pages, n), flipIn, &ph);
      }
      canvas->flush();
      applyBrightness(g_sleeping);
    } else if (g_netState == NET_PORTAL) {
      drawPortalSplash();
    } else {
      drawSplash("CONNECTING TO WIFI...", nullptr, nullptr);
    }
  }
  delay(10);
}

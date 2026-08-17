#include "ota.h"
#include "config.h"
#include "certs.h"
#include "device_id.h"
#include "flight_data.h"

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <esp_ota_ops.h>

#define CRASH_LOOP_LIMIT 3
#define STABLE_AFTER_BOOT_MS 60000UL

static volatile bool s_otaActive = false;
static volatile int s_otaPct = -1;
static char s_otaVersion[16] = "";

bool otaInProgress() { return s_otaActive; }
int otaProgressPct() { return s_otaPct; }
const char *otaTargetVersion() { return s_otaVersion; }

void otaBootGuard() {
  esp_reset_reason_t why = esp_reset_reason();
  bool crashed = why == ESP_RST_PANIC || why == ESP_RST_INT_WDT ||
                 why == ESP_RST_TASK_WDT || why == ESP_RST_WDT;
  if (!crashed) return; // power-on/normal resets never count toward the limit

  Preferences p;
  p.begin("flightinfo", false);
  uint8_t fails = p.getUChar("bootfail", 0) + 1;
  p.putUChar("bootfail", fails);
  p.end();
  Serial.printf("[ota] crash reboot %u/%u\n", fails, CRASH_LOOP_LIMIT);
  if (fails < CRASH_LOOP_LIMIT) return;

  // Crash-looping: try to boot the previous firmware from the other OTA slot
  const esp_partition_t *other = esp_ota_get_next_update_partition(nullptr);
  esp_app_desc_t desc;
  if (other && esp_ota_get_partition_description(other, &desc) == ESP_OK) {
    Preferences q;
    q.begin("flightinfo", false);
    q.putUChar("bootfail", 0);
    q.end();
    Serial.printf("[ota] crash loop - rolling back to %s in %s\n", desc.version,
                  other->label);
    if (esp_ota_set_boot_partition(other) == ESP_OK) {
      delay(100);
      ESP.restart();
    }
  }
  Serial.println("[ota] crash loop but no other firmware to roll back to");
}

static void markStableOnce() {
  static bool marked = false;
  if (marked || millis() < STABLE_AFTER_BOOT_MS) return;
  marked = true;
  Preferences p;
  p.begin("flightinfo", false);
  if (p.getUChar("bootfail", 0)) p.putUChar("bootfail", 0);
  p.end();
}

// Build the client matching the URL scheme. Product builds verify against the
// pinned Let's Encrypt root; dev builds accept any cert (self-hosters often
// run self-signed or plain HTTP anyway).
static WiFiClient *clientFor(const String &url, WiFiClient &plain, WiFiClientSecure &secure) {
  if (url.startsWith("https://")) {
#ifdef PRODUCT_BUILD
    secure.setCACert(PINNED_ROOTS);
#else
    secure.setInsecure();
#endif
    return &secure;
  }
  return &plain;
}

static bool fetchManifest(String &version, String &url) {
  String murl = String(serverBaseUrl()) + "/api/fw/latest";
  WiFiClient plain;
  WiFiClientSecure secure;
  WiFiClient *client = clientFor(murl, plain, secure);
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(*client, murl)) return false;
  if (deviceToken()[0]) http.addHeader("X-Device-Token", deviceToken());
  http.addHeader("X-FW-Version", FW_VERSION);
  int code = http.GET();
  bool ok = false;
  if (code == HTTP_CODE_OK) {
    JsonDocument doc;
    if (!deserializeJson(doc, http.getStream())) {
      version = (const char *)(doc["version"] | "");
      url = (const char *)(doc["url"] | "");
      ok = version.length() && url.length();
    }
  } else if (code != HTTP_CODE_NOT_FOUND) { // 404 = server has no firmware dir
    Serial.printf("[ota] manifest fetch -> %d\n", code);
  }
  http.end();
  return ok;
}

void otaService() {
  markStableOnce(); // a minute of running clears the crash-loop counter
  static uint32_t lastCheck = 0;
  uint32_t now = millis();
  bool due = lastCheck == 0 ? now > OTA_FIRST_CHECK_MS : now - lastCheck > OTA_CHECK_MS;
  if (!due || WiFi.status() != WL_CONNECTED) return;
  lastCheck = now;

  String version, url;
  if (!fetchManifest(version, url)) return;
  if (version == FW_VERSION) return;
  if (url.startsWith("/")) url = String(serverBaseUrl()) + url;
  Serial.printf("[ota] updating %s -> %s from %s\n", FW_VERSION, version.c_str(), url.c_str());

  snprintf(s_otaVersion, sizeof(s_otaVersion), "%s", version.c_str());
  s_otaPct = -1;
  s_otaActive = true; // the UI task switches to the progress screen
  httpUpdate.onProgress([](int done, int total) {
    if (total > 0) s_otaPct = (int)((int64_t)done * 100 / total);
  });

  WiFiClient plain;
  WiFiClientSecure secure;
  WiFiClient *client = clientFor(url, plain, secure);
  httpUpdate.rebootOnUpdate(true);
  t_httpUpdate_return ret = httpUpdate.update(*client, url);
  // Reached only on failure/no-update (success reboots)
  s_otaActive = false;
  s_otaPct = -1;
  if (ret == HTTP_UPDATE_FAILED)
    Serial.printf("[ota] failed: %d %s\n", httpUpdate.getLastError(),
                  httpUpdate.getLastErrorString().c_str());
}

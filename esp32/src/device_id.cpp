#include "device_id.h"
#include "pin_config.h"
#include "config.h"
#include "flight_data.h"

#include <Preferences.h>
#include <WiFi.h>
#include <Wire.h>
#include "audio.h"
#include <esp_mac.h>

static char s_token[49] = "";
static bool s_loaded = false;

const char *deviceToken() {
  if (!s_loaded) {
    Preferences p;
    p.begin("flightinfo", true);
    String t = p.getString("devtoken", "");
    p.end();
    snprintf(s_token, sizeof(s_token), "%s", t.c_str());
    s_loaded = true;
  }
  return s_token;
}

static void saveToken(const char *token) {
  Preferences p;
  p.begin("flightinfo", false);
  p.putString("devtoken", token);
  p.end();
  snprintf(s_token, sizeof(s_token), "%s", token);
  s_loaded = true;
}

void devicePollSerial() {
  static char line[96];
  static size_t len = 0;
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c != '\n') {
      if (len < sizeof(line) - 1) line[len++] = c;
      continue;
    }
    line[len] = '\0';
    len = 0;
    if (!strncmp(line, "PROVISION ", 10)) {
      const char *tok = line + 10;
      // Tokens are URL-safe base64 from the flash script; accept a sane subset
      bool ok = tok[0] != '\0' && strlen(tok) < sizeof(s_token);
      for (const char *p = tok; ok && *p; ++p)
        if (!isalnum((unsigned char)*p) && *p != '-' && *p != '_') ok = false;
      if (ok) {
        saveToken(tok);
        Serial.printf("PROVISIONED %s\n", tok);
      } else {
        Serial.println("PROVISION_ERROR bad token");
      }
    } else if (!strcmp(line, "DEVINFO")) {
      // On the P4 the WiFi MAC lives on the esp_hosted C6 and reads as zeros
      // until the station is up - fall back to the chip's own factory MAC
      String mac = WiFi.macAddress();
      if (mac == "00:00:00:00:00:00") {
        uint8_t m[6];
        if (esp_efuse_mac_get_default(m) == ESP_OK) {
          char b[18];
          snprintf(b, sizeof(b), "%02X:%02X:%02X:%02X:%02X:%02X", m[0], m[1], m[2], m[3], m[4], m[5]);
          mac = b;
        }
      }
      Serial.printf("DEVINFO fw=%s mac=%s token=%s server=%s board=%s variant=%s heap=%u\n",
                    FW_VERSION, mac.c_str(), deviceToken()[0] ? "set" : "unset",
                    serverBaseUrl(), BOARD_NAME, FW_VARIANT, ESP.getFreeHeap());
    } else if (!strcmp(line, "GPIOS")) {
      // Bench helper: levels of the GPIOs not claimed by the pin map, with
      // pull-ups on - press an unmapped key and the pin that reads 0 is it
#if CONFIG_IDF_TARGET_ESP32C6
      static const int spare[] = {10, 14, 18};
#else
      static const int spare[] = {};
#endif
      Serial.print("GPIOS");
      for (size_t i = 0; i < sizeof(spare) / sizeof(spare[0]); i++) {
        pinMode(spare[i], INPUT_PULLUP);
        Serial.printf(" %d=%d", spare[i], digitalRead(spare[i]));
      }
      Serial.println();
    } else if (!strcmp(line, "I2CSCAN")) {
      // Bench helper: which I2C addresses answer on the shared bus
      Serial.print("I2CSCAN");
      for (uint8_t a = 1; a < 0x7F; a++) {
        Wire.beginTransmission(a);
        if (Wire.endTransmission() == 0) Serial.printf(" 0x%02X", a);
      }
      Serial.println();
    } else if (!strcmp(line, "CHIME")) {
      audioPlayChime(); // bench helper: same path as the settings-page test
      Serial.println("CHIME queued");
    } else if (!strcmp(line, "REBOOT")) {
      Serial.println("REBOOTING");
      delay(100);
      ESP.restart();
    }
  }
}

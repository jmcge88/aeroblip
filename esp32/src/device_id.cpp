#include "device_id.h"
#include "config.h"
#include "flight_data.h"

#include <Preferences.h>
#include <WiFi.h>

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
      Serial.printf("DEVINFO fw=%s mac=%s token=%s server=%s\n", FW_VERSION,
                    WiFi.macAddress().c_str(), deviceToken()[0] ? "set" : "unset",
                    serverBaseUrl());
    } else if (!strcmp(line, "REBOOT")) {
      Serial.println("REBOOTING");
      delay(100);
      ESP.restart();
    }
  }
}

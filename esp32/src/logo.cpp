#include "logo.h"
#include "photo.h"
#include "flight_data.h"
#include <WiFi.h>

// Write-once cache: a home installation sees a couple of dozen airlines at
// most, so slots are never evicted - that keeps UI reads safe without locks.
#define LOGO_SLOTS 24
#define LOGO_RETRY_MS (10UL * 60UL * 1000UL)

static LogoImage s_slots[LOGO_SLOTS];
static volatile int s_used = 0;

const LogoImage *logoGet(const char *iata, int size) {
  if (!iata || !isalnum((unsigned char)iata[0]) || !isalnum((unsigned char)iata[1]))
    return nullptr;
  char key[4] = {(char)toupper((unsigned char)iata[0]),
                 (char)toupper((unsigned char)iata[1]), '\0', '\0'};
  for (int i = 0; i < s_used; i++) {
    LogoImage &e = s_slots[i];
    if (e.size == size && strcmp(e.iata, key) == 0) return e.valid ? &e : nullptr;
  }
  if (s_used >= LOGO_SLOTS) return nullptr;
  // Register for the net task to pick up; fields before the s_used bump
  LogoImage &e = s_slots[s_used];
  memcpy(e.iata, key, sizeof(e.iata));
  e.size = (uint8_t)size;
  e.buf = nullptr;
  e.valid = false;
  e.failed = false;
  e.tried_ms = 0;
  s_used = s_used + 1;
  return nullptr;
}

void serviceLogos() {
  if (WiFi.status() != WL_CONNECTED) return;
  for (int i = 0; i < s_used; i++) {
    LogoImage &e = s_slots[i];
    if (e.valid) continue;
    if (e.failed && millis() - e.tried_ms < LOGO_RETRY_MS) continue;
    if (!e.buf)
      e.buf = (uint16_t *)heap_caps_malloc((size_t)e.size * e.size * 2,
                                           MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!e.buf) return;
    char url[160];
    snprintf(url, sizeof(url), "%s/api/logo/%s?size=%d", serverBaseUrl(), e.iata, e.size);
    int w = 0, h = 0;
    bool ok = fetchAircraftPhoto(url, e.buf, e.size, e.size, w, h);
    e.tried_ms = millis();
    if (ok) {
      e.w = w;
      e.h = h;
      e.valid = true;
    } else {
      e.failed = true;
    }
    Serial.printf("[logo] %s %s (%dpx)\n", ok ? "ok" : "failed", e.iata, e.size);
    return; // one fetch per tick keeps the net task responsive
  }
}

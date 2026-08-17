#include "audio.h"

#include <Arduino.h>
#include <ESP_I2S.h>
#include <math.h>
#include "pin_config.h"
#include "es8311.h"

#define SAMPLE_RATE 16000
#define CODEC_VOLUME 82 // 0-100

static I2SClass s_i2s;
static bool s_ok = false;
static volatile uint8_t s_pending = 0; // 1 = chime, 2 = alarm
static volatile uint8_t s_volChime = 80;
static volatile uint8_t s_volAlarm = 80;
static TaskHandle_t s_task = nullptr;
static es8311_handle_t s_codec = nullptr;

struct Note {
  float freq;   // Hz, 0 = rest
  uint16_t ms;
  float amp;    // 0..1
};

// Terminal PA chime: three slow descending tones, long final ring
static const Note CHIME[] = {
    {783.99f, 620, 0.50f}, // G5
    {0, 60, 0},
    {659.26f, 620, 0.50f}, // E5
    {0, 60, 0},
    {523.25f, 1300, 0.50f}, // C5, fades out
};

// Urgent but not obnoxious: two rising triplets
static const Note ALARM[] = {
    {740.0f, 110, 0.7f}, {0, 40, 0}, {880.0f, 110, 0.7f}, {0, 40, 0}, {1108.7f, 160, 0.7f},
    {0, 220, 0},
    {740.0f, 110, 0.7f}, {0, 40, 0}, {880.0f, 110, 0.7f}, {0, 40, 0}, {1108.7f, 260, 0.7f},
};

static void playNotes(const Note *notes, int count) {
  static int16_t buf[512 * 2]; // stereo frames
  digitalWrite(PIN_PA, HIGH);
  delay(20); // let the amp settle
  for (int n = 0; n < count; n++) {
    const Note &note = notes[n];
    const int total = (int)((int32_t)SAMPLE_RATE * note.ms / 1000);
    float phase = 0, step = 2.0f * (float)M_PI * note.freq / SAMPLE_RATE;
    int done = 0;
    while (done < total) {
      int chunk = min(512, total - done);
      for (int i = 0; i < chunk; i++) {
        float t = (float)(done + i) / total;
        // Fast attack, exponential-ish decay - reads as a struck chime bar
        float env = (t < 0.03f) ? t / 0.03f : (1.0f - t) * (1.0f - t);
        int16_t s = note.freq > 0 ? (int16_t)(32767.0f * note.amp * env * sinf(phase)) : 0;
        phase += step;
        buf[i * 2] = s;
        buf[i * 2 + 1] = s;
      }
      s_i2s.write((uint8_t *)buf, chunk * 4);
      done += chunk;
    }
  }
  delay(30);
  digitalWrite(PIN_PA, LOW); // amp off between sounds - no idle hiss
}

static void applyVolume(uint8_t vol) {
  if (s_codec) es8311_voice_volume_set(s_codec, vol > 100 ? 100 : vol, NULL);
}

static void audioTask(void *) {
  for (;;) {
    if (s_pending == 1) {
      s_pending = 0;
      applyVolume(s_volChime);
      playNotes(CHIME, sizeof(CHIME) / sizeof(CHIME[0]));
    } else if (s_pending == 2) {
      s_pending = 0;
      applyVolume(s_volAlarm);
      playNotes(ALARM, sizeof(ALARM) / sizeof(ALARM[0]));
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }
}

bool audioInit() {
  pinMode(PIN_PA, OUTPUT);
  digitalWrite(PIN_PA, LOW);

  s_i2s.setPins(I2S_BCLK, I2S_WS, I2S_DOUT, I2S_DIN, I2S_MCLK);
  if (!s_i2s.begin(I2S_MODE_STD, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT,
                   I2S_SLOT_MODE_STEREO, I2S_STD_SLOT_BOTH)) {
    Serial.println("[audio] I2S init failed");
    return false;
  }

  s_codec = es8311_create(0, ES8311_ADDRRES_0);
  if (!s_codec) {
    Serial.println("[audio] ES8311 not found");
    return false;
  }
  const es8311_clock_config_t clk = {
      .mclk_inverted = false,
      .sclk_inverted = false,
      .mclk_from_mclk_pin = true,
      .mclk_frequency = SAMPLE_RATE * 256,
      .sample_frequency = SAMPLE_RATE,
  };
  if (es8311_init(s_codec, &clk, ES8311_RESOLUTION_16, ES8311_RESOLUTION_16) != ESP_OK ||
      es8311_sample_frequency_config(s_codec, SAMPLE_RATE * 256, SAMPLE_RATE) != ESP_OK ||
      es8311_voice_volume_set(s_codec, CODEC_VOLUME, NULL) != ESP_OK) {
    Serial.println("[audio] ES8311 init failed");
    return false;
  }

  xTaskCreatePinnedToCore(audioTask, "audio", 4096, nullptr, 1, &s_task, 0);
  s_ok = true;
  Serial.println("[audio] codec ready");
  return true;
}

void audioPlayChime() {
  if (s_ok && s_pending == 0) s_pending = 1;
}

void audioPlayAlarm() {
  if (s_ok) s_pending = 2; // alarm may override a queued chime
}

void audioSetVolumes(uint8_t chimeVol, uint8_t alarmVol) {
  s_volChime = chimeVol > 100 ? 100 : chimeVol;
  s_volAlarm = alarmVol > 100 ? 100 : alarmVol;
}

#pragma once
#include <stdint.h>

// ES8311 codec + speaker amp. Sounds are synthesized (no sample files) and
// played from a low-priority task so callers never block.
bool audioInit();      // call after Wire.begin(); false if the codec is absent
void audioPlayChime(); // drawn-out airport PA chime (flight entered the ring)
void audioPlayAlarm(); // urgent alarm (squawk 7700 active)
void audioSetVolumes(uint8_t chimeVol, uint8_t alarmVol); // 0-100 each, applied per sound

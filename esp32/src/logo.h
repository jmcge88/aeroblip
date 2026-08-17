#pragma once
#include <Arduino.h>

// Decoded airline logo (packed RGB565, square) from /api/logo/{iata}?size=N.
// The server flattens PNGs onto white and re-encodes as JPEG for us.
struct LogoImage {
  char iata[4];
  uint8_t size;
  uint16_t *buf;
  int w, h;
  volatile bool valid; // set last by the net task - UI must check it
  bool failed;
  uint32_t tried_ms;
};

// Cached logo, or nullptr (unknown entries are queued for the net task).
// Safe to call from the UI task.
const LogoImage *logoGet(const char *iata, int size);

// Fetch at most one queued logo (network + JPEG decode) - net task only.
void serviceLogos();

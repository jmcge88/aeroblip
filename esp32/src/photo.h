#pragma once
#include <Arduino.h>

// Download a JPEG (http/https) and decode it into dst (RGB565, packed rows of
// outW pixels), scaled to fit dstW x dstH. Returns false on any failure.
bool fetchAircraftPhoto(const char *url, uint16_t *dst, int dstW, int dstH,
                        int &outW, int &outH);

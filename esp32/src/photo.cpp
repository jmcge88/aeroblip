#include "photo.h"

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <JPEGDEC.h>

static JPEGDEC s_jpeg; // large object - keep out of task stacks

static uint16_t *s_dst;
static int s_outW, s_outH;

static int jpegDrawCb(JPEGDRAW *p) {
  for (int r = 0; r < p->iHeight; r++) {
    int dy = p->y + r;
    if (dy >= s_outH) break;
    int w = p->iWidth;
    if (p->x + w > s_outW) w = s_outW - p->x;
    if (w <= 0) continue;
    memcpy(s_dst + (size_t)dy * s_outW + p->x, ((uint16_t *)p->pPixels) + (size_t)r * p->iWidth,
           (size_t)w * 2);
  }
  return 1;
}

bool fetchAircraftPhoto(const char *url, uint16_t *dst, int dstW, int dstH,
                        int &outW, int &outH) {
  if (!url || !url[0] || WiFi.status() != WL_CONNECTED) return false;

  WiFiClient *client;
  WiFiClientSecure secure;
  WiFiClient plain;
  if (!strncmp(url, "https://", 8)) {
    secure.setInsecure(); // public photo CDN, integrity is not critical
    client = &secure;
  } else {
    client = &plain;
  }

  HTTPClient http;
  http.setTimeout(8000);
  http.setConnectTimeout(8000);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  if (!http.begin(*client, url)) return false;

  bool ok = false;
  uint8_t *jbuf = nullptr;
  int len = 0;
  if (http.GET() == HTTP_CODE_OK) {
    len = http.getSize();
    if (len > 0 && len <= 300 * 1024) {
      jbuf = (uint8_t *)heap_caps_malloc(len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
      if (jbuf) {
        WiFiClient *stream = http.getStreamPtr();
        int got = 0;
        uint32_t t0 = millis();
        while (got < len && millis() - t0 < 10000) {
          int n = stream->readBytes(jbuf + got, len - got);
          if (n <= 0) break;
          got += n;
        }
        ok = (got == len);
      }
    }
  }
  http.end();
  if (!ok) {
    if (jbuf) free(jbuf);
    return false;
  }

  ok = false;
  if (s_jpeg.openRAM(jbuf, len, jpegDrawCb)) {
    int w = s_jpeg.getWidth(), h = s_jpeg.getHeight();
    int f = 1;
    while ((w / f > dstW || h / f > dstH) && f < 8) f *= 2;
    int opt = (f == 2) ? JPEG_SCALE_HALF : (f == 4) ? JPEG_SCALE_QUARTER
              : (f == 8) ? JPEG_SCALE_EIGHTH : 0;
    s_outW = min(w / f, dstW);
    s_outH = min(h / f, dstH);
    s_dst = dst;
    s_jpeg.setPixelType(RGB565_LITTLE_ENDIAN);
    ok = s_jpeg.decode(0, 0, opt) == 1;
    s_jpeg.close();
    outW = s_outW;
    outH = s_outH;
  }
  free(jbuf);
  return ok;
}

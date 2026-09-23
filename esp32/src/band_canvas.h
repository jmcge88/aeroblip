#pragma once
#include <Arduino_GFX_Library.h>

// A full-resolution logical canvas backed by a strip of BAND_H rows.
//
// Why this exists: the ESP32-C6 board has no PSRAM, so the 460 KB full-screen
// canvas the S3 build uses cannot be allocated - and drawing straight to the
// CO5300 does not work either: the controller only honours QSPI writes whose
// column window is 2-pixel aligned, so the thousands of 1-pixel and odd-width
// windows that text and line drawing produce are silently dropped (the panel
// stays black bar a stray pixel). A full-width strip blit is always aligned.
//
// Usage: for each frame, for b in 0..bands()-1: beginBand(b); <draw the whole
// screen as usual>; flushBand(). Every primitive is clipped to the current
// strip, so the UI code is unchanged - it just runs once per strip.
//
// Rotation is done in software with exactly Arduino_Canvas's coordinate
// mapping (main.cpp's mapTouch() is its inverse), so the IMU auto-rotation
// behaves the same on both boards. The panel is square, so all four
// rotations keep the strip geometry.
class BandCanvas : public Arduino_GFX {
public:
  BandCanvas(int16_t w, int16_t h, int16_t bandH, Arduino_GFX *output)
      : Arduino_GFX(w, h), _output(output), _bandH(bandH) {}

  bool begin(int32_t speed = GFX_NOT_DEFINED) override {
    if (_output && !_output->begin(speed)) return false;
    if (!_buf) _buf = (uint16_t *)malloc((size_t)WIDTH * _bandH * 2);
    return _buf != nullptr;
  }

  int bands() const { return (HEIGHT + _bandH - 1) / _bandH; }
  size_t bufferBytes() const { return (size_t)WIDTH * _bandH * 2; }

  void beginBand(int i) {
    _y0 = i * _bandH;
    _y1 = _y0 + _bandH;
    if (_y1 > HEIGHT) _y1 = HEIGHT;
    if (_buf) memset(_buf, 0, bufferBytes());
  }

  void flushBand() {
    if (_output && _buf) _output->draw16bitRGBBitmap(0, _y0, _buf, WIDTH, _y1 - _y0);
  }

  // Frames are pushed strip by strip; nothing to do at the end of a frame
  void flush(bool = false) override {}

  void writePixelPreclipped(int16_t x, int16_t y, uint16_t color) override {
    int16_t px, py;
    toPhysical(x, y, px, py);
    if (py >= _y0 && py < _y1) _buf[(py - _y0) * WIDTH + px] = color;
  }

  void writeFillRectPreclipped(int16_t x, int16_t y, int16_t w, int16_t h,
                               uint16_t color) override {
    // Map the logical rect's two corners, then fill the physical rect
    int16_t ax, ay, bx, by;
    toPhysical(x, y, ax, ay);
    toPhysical(x + w - 1, y + h - 1, bx, by);
    if (ax > bx) { int16_t t = ax; ax = bx; bx = t; }
    if (ay > by) { int16_t t = ay; ay = by; by = t; }
    int16_t ya = ay > _y0 ? ay : _y0;
    int16_t yb = (by + 1) < _y1 ? (by + 1) : _y1;
    int16_t pw = bx - ax + 1;
    for (int16_t yy = ya; yy < yb; yy++) {
      uint16_t *p = _buf + (yy - _y0) * WIDTH + ax;
      for (int16_t i = 0; i < pw; i++) p[i] = color;
    }
  }

private:
  // Same mapping as Arduino_Canvas::writePixelPreclipped
  inline void toPhysical(int16_t x, int16_t y, int16_t &px, int16_t &py) const {
    switch (_rotation) {
      case 1:  px = _max_y - y; py = x; break;
      case 2:  px = _max_x - x; py = _max_y - y; break;
      case 3:  px = y; py = _max_x - x; break;
      default: px = x; py = y; break;
    }
  }

  Arduino_GFX *_output;
  int16_t _bandH;
  int16_t _y0 = 0, _y1 = 0;
  uint16_t *_buf = nullptr;
};

#pragma once
#include <sdkconfig.h> // CONFIG_IDF_TARGET_*

// The two AMOLED boards carry the same 2.16" 480x480 CO5300 AMOLED, CST9220
// touch, AXP2101 PMU, QMI8658 IMU and ES8311 codec - only the MCU and the
// GPIO map differ. The P4 board is a different display (720x720 DSI LCD) that
// renders the same 480x480 UI scaled up. The build picks the map from the
// chip it targets.
#define LCD_WIDTH 480
#define LCD_HEIGHT 480

// Panel orientation written to MADCTL (0x36) after init. Same value on both
// boards per the vendor samples; override with -DLCD_MADCTL=0x30 if a unit
// comes up mirrored.
#ifndef LCD_MADCTL
#define LCD_MADCTL 0xA0
#endif

#if CONFIG_IDF_TARGET_ESP32P4
// ---- Waveshare ESP32-P4-WIFI6-Touch-LCD-4B ------------------------------
// Pin map from the vendor BSP (components.espressif.com
// waveshare/esp32_p4_wifi6_touch_lcd_4b 3.0.1). ESP32-P4 + 32MB PSRAM, 32MB
// flash. No radio of its own: WiFi comes from the on-board ESP32-C6 over SDIO
// (esp_hosted - the Arduino core's default P4 SDIO pins match this board).
// No PMU, no IMU, no side key.
#define BOARD_NAME "ESP32-P4-WIFI6-Touch-LCD-4B"
#define FW_VARIANT "esp32p4"

// 4" 720x720 IPS, ST7703 over 2-lane MIPI-DSI. The UI is laid out for 480x480
// (LCD_WIDTH/HEIGHT above stay the logical size); dsi_display.h scales each
// frame 1.5x into the panel's framebuffer.
#define PANEL_DSI 1
#define PANEL_W 720
#define PANEL_H 720
#define LCD_RESET 27
#define LCD_BL 26 // backlight PWM, active low (BSP drives it with output_invert)

// GT911 touch + codecs on I2C. Touch INT/RST are not wired to GPIOs: the
// touch is polled.
#define IIC_SDA 7
#define IIC_SCL 8
#define TP_INT -1
#define TP_RST -1

// BOOT is GPIO35 (the P4's boot-mode strapping pin, input only after boot)
#define KEY_BOOT 35
#define KEY_USER -1

#define IMU_X_SIGN 1.0f // no IMU on this board - unused

// ES8311 codec (shared I2S bus with the ES7210 mic ADC) + speaker amp enable
#define I2S_MCLK 13
#define I2S_BCLK 12
#define I2S_WS 10
#define I2S_DOUT 9
#define I2S_DIN 11
#define PIN_PA 53

#elif CONFIG_IDF_TARGET_ESP32C6
// ---- Waveshare ESP32-C6-Touch-AMOLED-2.16 -------------------------------
// Pin map from the vendor repo (02_Example/*/user_config.h and the XiaoZhi
// board config). Single core, 16MB flash, NO PSRAM (~328 KB heap).
#define BOARD_NAME "ESP32-C6-Touch-AMOLED-2.16"
#define FW_VARIANT "esp32c6"

// CO5300 AMOLED, QSPI (shares the bus with the TF slot: SD CS is GPIO6)
#define LCD_SDIO0 1
#define LCD_SDIO1 2
#define LCD_SDIO2 3
#define LCD_SDIO3 4
#define LCD_SCLK 0
#define LCD_RESET -1 // panel reset is not on a GPIO - driver falls back to SWRESET
#define LCD_CS 15

// CST9220 touch + shared I2C bus (RTC, IMU, codec, AXP2101 PMU)
#define IIC_SDA 8
#define IIC_SCL 7
#define TP_INT 5
#define TP_RST 11

// Physical keys. BOOT is GPIO9 (strapping pin - input only after boot; holding
// it at power-on enters ROM download mode, so the "portal at boot" gesture is
// to hold it once the CONNECTING splash is up). The side KEY button's GPIO is
// not in any vendor example: build with -DKEY_USER=<n> once known (the serial
// command GPIOS prints the spare pins' levels to find it).
#define KEY_BOOT 9
#ifndef KEY_USER
#define KEY_USER -1
#endif

// QMI8658 is mounted with its X axis mirrored relative to the S3 board: a
// left tilt reads as a right tilt. Flip it so the same auto-rotation logic
// picks the same orientation on both boards.
#define IMU_X_SIGN -1.0f

// ES8311 codec (shared I2S bus with the ES7210 mic ADC); no amp-enable GPIO
#define I2S_MCLK 19
#define I2S_BCLK 20
#define I2S_WS 22
#define I2S_DOUT 23
#define I2S_DIN 21
#define PIN_PA -1

#else
// ---- Waveshare ESP32-S3-Touch-AMOLED-2.16 (from the vendor sample repo) ----
#define BOARD_NAME "ESP32-S3-Touch-AMOLED-2.16"
#define FW_VARIANT "esp32s3"

// CO5300 AMOLED, QSPI
#define LCD_SDIO0 4
#define LCD_SDIO1 5
#define LCD_SDIO2 6
#define LCD_SDIO3 7
#define LCD_SCLK 38
#define LCD_RESET 39
#define LCD_CS 12

// CST9220 touch + shared I2C bus (RTC, IMU, codec, AXP2101 PMU)
#define IIC_SDA 15
#define IIC_SCL 14
#define TP_INT 11
#define TP_RST 40

// Physical keys (a third key is the AXP2101 power button)
#define KEY_BOOT 0  // active low, also strapping pin - input only after boot
#define KEY_USER 18 // active low, external 10K pull-up

#define IMU_X_SIGN 1.0f // accelerometer X as mounted (reference orientation)

// ES8311 codec (shared I2S bus with the ES7210 mic ADC) + speaker amp enable
#define I2S_MCLK 42
#define I2S_BCLK 9
#define I2S_WS 45
#define I2S_DOUT 8
#define I2S_DIN 10
#define PIN_PA 46
#endif

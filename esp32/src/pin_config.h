#pragma once

// Waveshare ESP32-S3-Touch-AMOLED-2.16 pin map (from the vendor sample repo)

// CO5300 AMOLED, QSPI
#define LCD_SDIO0 4
#define LCD_SDIO1 5
#define LCD_SDIO2 6
#define LCD_SDIO3 7
#define LCD_SCLK 38
#define LCD_RESET 39
#define LCD_CS 12
#define LCD_WIDTH 480
#define LCD_HEIGHT 480

// CST9220 touch + shared I2C bus (RTC, IMU, codec, AXP2101 PMU)
#define IIC_SDA 15
#define IIC_SCL 14
#define TP_INT 11
#define TP_RST 40

// Physical keys (a third key is the AXP2101 power button)
#define KEY_BOOT 0  // active low, also strapping pin - input only after boot
#define KEY_USER 18 // active low, external 10K pull-up

// ES8311 codec (shared I2S bus with the ES7210 mic ADC) + speaker amp enable
#define I2S_MCLK 42
#define I2S_BCLK 9
#define I2S_WS 45
#define I2S_DOUT 8
#define I2S_DIN 10
#define PIN_PA 46

#pragma once
#include <Arduino.h>

// Per-device token, baked in at flash time by tools/flash_product.py and kept
// in NVS. Empty string when the device has never been provisioned (dev builds
// work fine without one - the server only enforces tokens in product mode).
const char *deviceToken();

// Handle provisioning commands arriving on the USB serial console:
//   PROVISION <token>  store the device token (replies "PROVISIONED <token>")
//   DEVINFO            reply with fw version, MAC, token status, server URL
//   REBOOT             restart the device
// Call from loop(); cheap no-op while no serial input is pending.
void devicePollSerial();

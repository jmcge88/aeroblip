#pragma once

// Crash-loop protection for OTA updates. Call FIRST in setup(): counts
// consecutive panic/watchdog reboots (power cycles don't count) and after 3
// flips the boot partition back to the previous firmware in the other OTA
// slot - so a bad OTA release un-ships itself instead of bricking the fleet.
// The counter clears once the firmware has run stably for a minute.
void otaBootGuard();

// Live progress for the UI task: while an update is downloading/flashing the
// display shows a progress screen instead of stale flight data (and warns
// against unplugging - interrupting a flash is the one thing users must not
// do, and a frozen screen invites exactly that).
bool otaInProgress();
int otaProgressPct();          // 0-100, or -1 before the size is known
const char *otaTargetVersion();

// Periodic HTTPS OTA against the flight-info server. The server exposes
// GET /api/fw/latest -> {"version": "1.0.1", "url": "/fw/product-1.0.1.bin"};
// when the version differs from FW_VERSION the image is downloaded and
// flashed (device reboots on success). Call from the network task loop;
// throttles itself to OTA_CHECK_MS (first check OTA_FIRST_CHECK_MS after boot).
void otaService();

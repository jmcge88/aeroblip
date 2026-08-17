#!/usr/bin/env python3
"""Flash and provision one retail unit, or publish a firmware release for OTA.

Per-unit flow (device on USB):
    python tools/flash_product.py --port COM5 --name batch1-003 \
        --server https://api.aeroblip.com --admin-token $env:ADMIN_TOKEN

    1. builds the PlatformIO `product` environment (skip with --skip-build)
    2. flashes it over the given COM port
    3. mirrors the same firmware into the second OTA slot (and clears the
       otadata selector) so crash-loop rollback always has a valid image,
       even on a unit that has never taken an OTA update
    4. generates a unique device token and provisions it over serial
    5. waits for the firmware boot banner as a smoke test
    6. registers the token with the server (skipped without --server)
    7. appends the unit to tools/devices_manifest.csv

Release flow (publish the current product build to the server's OTA dir):
    python tools/flash_product.py --release
    -> copies esp32/.pio/build/product/firmware.bin to fw/product-<ver>.bin
       and rewrites fw/manifest.json; deploy the fw/ dir with the server.

Requires: pyserial (pip install pyserial) for per-unit provisioning.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ESP32_DIR = REPO / "esp32"
MANIFEST_CSV = REPO / "tools" / "devices_manifest.csv"
FW_DIR = REPO / "fw"
BAUD = 115200


def fw_version() -> str:
    ini = (ESP32_DIR / "platformio.ini").read_text()
    m = re.search(r'-DFW_VERSION=\\"([^"\\]+)\\"', ini)
    if not m:
        sys.exit("FW_VERSION not found in esp32/platformio.ini")
    return m.group(1)


def run_pio(*args: str) -> None:
    cmd = [sys.executable, "-m", "platformio", *args]
    print(f"+ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=ESP32_DIR, check=True)


def app1_offset() -> int:
    """Second OTA slot offset from the partition table (default_16MB.csv)."""
    csv_path = (Path.home() / ".platformio" / "packages" / "framework-arduinoespressif32"
                / "tools" / "partitions" / "default_16MB.csv")
    try:
        for line in csv_path.read_text().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if parts and parts[0] == "app1":
                return int(parts[3], 0)
    except (OSError, IndexError, ValueError):
        pass
    return 0x650000  # default_16MB layout: app0 @ 0x10000, app1 @ 0x650000


def mirror_second_slot(port: str, env: str) -> None:
    """Write the just-flashed firmware into the other OTA slot too.

    Also erases otadata so the bootloader always boots app0 after a USB
    flash - otherwise a previously-OTA'd unit would keep booting the old
    firmware from app1 no matter what USB wrote to app0.
    """
    esptool = Path.home() / ".platformio" / "packages" / "tool-esptoolpy" / "esptool.py"
    fw = ESP32_DIR / ".pio" / "build" / env / "firmware.bin"
    if not esptool.exists():
        print(f"WARNING: {esptool} not found - second slot NOT mirrored")
        return
    base = [sys.executable, str(esptool), "--chip", "esp32s3", "--port", port,
            "--baud", "460800"]
    print("+ esptool erase_region otadata")
    subprocess.run(base + ["erase_region", "0xe000", "0x2000"], check=True)
    off = app1_offset()
    print(f"+ esptool write_flash {off:#x} (second OTA slot)")
    subprocess.run(base + ["write_flash", f"{off:#x}", str(fw)], check=True)


def release() -> None:
    version = fw_version()
    src = ESP32_DIR / ".pio" / "build" / "product" / "firmware.bin"
    if not src.exists():
        sys.exit(f"{src} missing - build first: python -m platformio run -e product")
    FW_DIR.mkdir(exist_ok=True)
    dest = FW_DIR / f"product-{version}.bin"
    shutil.copyfile(src, dest)
    (FW_DIR / "manifest.json").write_text(
        json.dumps({"version": version, "file": dest.name}, indent=2))
    print(f"released {dest.name} ({dest.stat().st_size} bytes) -> fw/manifest.json")
    print("deploy the fw/ directory alongside the server to serve this OTA update")


def wait_serial_line(ser, pattern: str, timeout_s: float) -> str | None:
    """Read lines until one contains `pattern` (returns it) or timeout."""
    deadline = time.monotonic() + timeout_s
    buf = b""
    while time.monotonic() < deadline:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode(errors="replace").strip()
                if text:
                    print(f"  [serial] {text}")
                if pattern in text:
                    return text
    return None


def provision(port: str, token: str, expect_boot: bool = True) -> dict:
    try:
        import serial  # pyserial
    except ImportError:
        sys.exit("pyserial missing: pip install pyserial")

    # The board re-enumerates its USB CDC port after flashing; retry the open
    ser = None
    for _ in range(20):
        try:
            ser = serial.Serial(port, BAUD, timeout=0.5)
            break
        except serial.SerialException:
            time.sleep(1)
    if ser is None:
        sys.exit(f"could not open {port} after flashing")

    with ser:
        if expect_boot:
            print("waiting for boot banner...")
            if not wait_serial_line(ser, "[boot] flight-info", 30):
                sys.exit("no boot banner on serial - flash may have failed")
        time.sleep(1)

        ser.write(f"PROVISION {token}\n".encode())
        if not wait_serial_line(ser, f"PROVISIONED {token}", 10):
            sys.exit("device did not acknowledge PROVISION")
        print("token provisioned")

        ser.write(b"DEVINFO\n")
        info_line = wait_serial_line(ser, "DEVINFO ", 10) or ""
        info = dict(kv.split("=", 1) for kv in info_line.split()[1:] if "=" in kv)
        if info.get("token") != "set":
            sys.exit(f"DEVINFO does not confirm the token: {info_line!r}")
        return info


def register(server: str, admin_token: str, token: str, name: str) -> bool:
    req = urllib.request.Request(
        server.rstrip("/") + "/api/devices/register",
        data=json.dumps({"token": token, "name": name}).encode(),
        headers={"Content-Type": "application/json", "X-Admin-Token": admin_token},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:
        print(f"WARNING: server registration failed: {exc}")
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="COM5", help="serial port (default COM5)")
    ap.add_argument("--name", default="", help="unit label for the manifest, e.g. batch1-003")
    ap.add_argument("--server", default="", help="server base URL to register the token with")
    ap.add_argument("--admin-token", default="", help="server ADMIN_TOKEN for registration")
    ap.add_argument("--skip-build", action="store_true", help="flash the existing build")
    ap.add_argument("--no-flash", action="store_true",
                    help="skip build+flash: provision/register the firmware already on the device")
    ap.add_argument("--env", default="product",
                    help="PlatformIO env to build/flash (default product; e.g. product-dev)")
    ap.add_argument("--release", action="store_true",
                    help="publish the current product build to fw/ for OTA and exit")
    args = ap.parse_args()

    if args.release:
        release()
        return

    version = fw_version()
    if not args.no_flash:
        if not args.skip_build:
            run_pio("run", "-e", args.env)
        run_pio("run", "-e", args.env, "-t", "upload", "--upload-port", args.port)
        mirror_second_slot(args.port, args.env)

    token = secrets.token_urlsafe(24)  # 32 url-safe chars
    info = provision(args.port, token, expect_boot=not args.no_flash)

    registered = False
    if args.server:
        if not args.admin_token:
            print("WARNING: --server given without --admin-token; skipping registration")
        else:
            registered = register(args.server, args.admin_token, token, args.name)
            if registered:
                print("registered with server")
    else:
        print("no --server given; register the token later via POST /api/devices/register")

    new_file = not MANIFEST_CSV.exists()
    with MANIFEST_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["date", "name", "mac", "token", "fw", "registered"])
        w.writerow([dt.date.today().isoformat(), args.name, info.get("mac", ""),
                    token, version, "yes" if registered else "no"])
    print(f"\nunit complete: fw {version}, mac {info.get('mac', '?')}, "
          f"token {token}\nmanifest: {MANIFEST_CSV}")


if __name__ == "__main__":
    main()

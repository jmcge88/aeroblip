#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ $# -ne 1 ]; then
  echo "Usage: $0 /path/to/homeassistant/config" >&2
  exit 1
fi

CONFIG_DIR="$1"

if [ ! -d "$CONFIG_DIR" ]; then
  echo "Error: '$CONFIG_DIR' does not exist or is not a directory" >&2
  exit 1
fi

CUSTOM_COMPONENTS_DIR="$CONFIG_DIR/custom_components"
if [ ! -d "$CUSTOM_COMPONENTS_DIR" ]; then
  mkdir -p "$CUSTOM_COMPONENTS_DIR"
fi

TARGET_LINK="$CUSTOM_COMPONENTS_DIR/aeroblip"
SOURCE="$SCRIPT_DIR/custom_components/aeroblip"

if [ -e "$TARGET_LINK" ]; then
  if [ -L "$TARGET_LINK" ]; then
    rm "$TARGET_LINK"
  else
    echo "Warning: $TARGET_LINK exists and is not a symlink. Skipping." >&2
    exit 0
  fi
fi

ln -s "$SOURCE" "$TARGET_LINK"

echo "Aeroblip integration linked to $TARGET_LINK"
echo "Please restart Home Assistant to load the integration."

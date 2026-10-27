#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
SOURCE_DIR=${1:-"$PROJECT_ROOT/lake/gold"}
DESTINATION_DIR=${2:-/volume1/Temp/Quotes/macro}
VOLUME_MOUNT=${3:-/volume1}
LOG_DIR="$PROJECT_ROOT/.logs"
LOCK_DIR="$PROJECT_ROOT/.locks"
LOG_PATH="$LOG_DIR/macro-loader-gold-copy.log"
LOCK_PATH="$LOCK_DIR/macro-loader-gold-copy.lock"

mkdir -p "$LOG_DIR" "$LOCK_DIR"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
	printf 'Gold copy is already running\n' >&2
	exit 3
fi
exec >>"$LOG_PATH" 2>&1

printf '\n[%s] Starting weekly Gold copy\n' "$(date --iso-8601=seconds)"

if [[ ! -d "$SOURCE_DIR" ]]; then
	printf 'Gold source directory is missing: %s\n' "$SOURCE_DIR" >&2
	exit 2
fi

if ! command -v mountpoint >/dev/null 2>&1 || ! mountpoint -q "$VOLUME_MOUNT"; then
	printf 'Destination volume is not mounted: %s\n' "$VOLUME_MOUNT" >&2
	exit 4
fi

if ! command -v rsync >/dev/null 2>&1; then
	printf 'Required executable is missing: rsync\n' >&2
	exit 5
fi

mkdir -p "$DESTINATION_DIR"
rsync --archive --human-readable --stats "$SOURCE_DIR/" "$DESTINATION_DIR/"

printf '[%s] Weekly Gold copy completed\n' "$(date --iso-8601=seconds)"

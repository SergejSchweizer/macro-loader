#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_ROOT"
CONFIG_FILE="$PROJECT_ROOT/config.yaml"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
CLI="$PROJECT_ROOT/.venv/bin/macro-loader"
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
for required_executable in "$PYTHON" "$CLI"; do
	if [[ ! -x "$required_executable" ]]; then
		printf 'Required cron executable is missing or not executable: %s\n' "$required_executable" >&2
		exit 2
	fi
done
LOG_DIR="$PROJECT_ROOT/.logs"
LOG_PATH="$LOG_DIR/fed-policy-eod.log"
LOCK_DIR="$PROJECT_ROOT/.locks"
LOCK_PATH="$LOCK_DIR/fed-policy-eod.lock"

if ! MACRO_LOADER_GIT_SHA=$(git -C "$PROJECT_ROOT" rev-parse --verify HEAD); then
	printf 'Unable to resolve repository Git identity\n' >&2
	exit 2
fi
export MACRO_LOADER_GIT_SHA

mkdir -p "$LOG_DIR" "$LOCK_DIR"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
	printf 'Fed-policy EOD job is already running\n' >&2
	exit 3
fi
exec >>"$LOG_PATH" 2>&1

printf '\n[%s] Starting Fed-policy EOD job\n' "$(date --iso-8601=seconds)"
eval "$("$PYTHON" "$PROJECT_ROOT/scripts/export_cron_config.py" "$CONFIG_FILE")"

printf '[%s] Running Fed-policy bounded update\n' "$(date --iso-8601=seconds)"
"$CLI" --lake-root "$LAKE_ROOT" run-fed-policy-eod

printf '[%s] Synchronizing Fed-policy PostgreSQL state\n' "$(date --iso-8601=seconds)"
"$CLI" --lake-root "$LAKE_ROOT" fed-policy-sync-postgres

printf '[%s] Verifying PostgreSQL serving state\n' "$(date --iso-8601=seconds)"
"$CLI" --lake-root "$LAKE_ROOT" postgres-verify

printf '[%s] Fed-policy EOD job completed\n' "$(date --iso-8601=seconds)"

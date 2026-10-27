"""Run the installed daily wrapper and prove its PostgreSQL replay is a no-op."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--report", type=Path, default=Path("artifacts/acceptance/macro-feature-cron-v2.json")
    )
    parser.add_argument("--execute", action="store_true", help="execute the installed wrapper")
    return parser


def _generated_at_utc() -> str:
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_date_epoch is not None:
        return datetime.fromtimestamp(int(source_date_epoch), UTC).isoformat()
    return datetime.now(UTC).isoformat()


def _database_command(project_root: Path, command: str) -> list[str]:
    root = project_root.resolve()
    shell = (
        "set -euo pipefail; "
        'export MACRO_LOADER_PROJECT_ROOT="$1"; '
        'eval "$("$1/.venv/bin/python" '
        '"$1/scripts/export_cron_config.py" "$1/config.yaml")"; '
        'export PGUSER="$PGSYNCUSER" PGPASSWORD="$PGSYNCPASSWORD"; '
        'exec "$1/.venv/bin/macro-loader" --lake-root "$LAKE_ROOT" '
        f"{command}"
    )
    return ["bash", "-c", shell, "macro-feature-cron-acceptance", str(root)]


def _event(output: str, command: str, stage: str) -> dict[str, object] | None:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and value.get("command") == command
            and value.get("stage") == stage
        ):
            return value
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runner = args.project_root / "ops" / "run-macro-loader-daily.sh"
    if not args.execute:
        print(f"Refusing to execute without --execute: {runner}")
        return 2
    results: list[dict[str, object]] = []
    for name in ("daily-wrapper-run-1", "daily-wrapper-run-2"):
        completed = subprocess.run(
            [str(runner)], cwd=args.project_root, check=False, capture_output=True, text=True
        )
        results.append({"name": name, "exit_code": completed.returncode})
        if completed.returncode != 0:
            break
    if len(results) == 2 and all(result["exit_code"] == 0 for result in results):
        replay = subprocess.run(
            _database_command(args.project_root, "gold-sync-postgres"),
            cwd=args.project_root,
            check=False,
            capture_output=True,
            text=True,
        )
        event = _event(replay.stdout + "\n" + replay.stderr, "gold-sync-postgres", "postgres_sync")
        no_op = (
            replay.returncode == 0
            and event is not None
            and event.get("status") == "success"
            and all(event.get(key) == 0 for key in ("inserted", "updated", "deleted"))
        )
        results.append(
            {
                "name": "zero-change-replay",
                "exit_code": replay.returncode,
                "inserted": event.get("inserted") if event else None,
                "updated": event.get("updated") if event else None,
                "deleted": event.get("deleted") if event else None,
                "status": "PASS" if no_op else "FAIL",
            }
        )
    if len(results) == 3 and results[-1].get("status") == "PASS":
        verify = subprocess.run(
            _database_command(args.project_root, "postgres-verify"),
            cwd=args.project_root,
            check=False,
            capture_output=True,
            text=True,
        )
        try:
            verification = json.loads(verify.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            verification = {}
        verified = verify.returncode == 0 and verification.get("status") == "PASS"
        results.append(
            {
                "name": "independent-postgres-verify",
                "exit_code": verify.returncode,
                "status": "PASS" if verified else "FAIL",
                "summaries": verification.get("summaries", {}),
            }
        )
    passed = (
        len(results) == 4
        and all(result["exit_code"] == 0 for result in results)
        and all(result.get("status", "PASS") == "PASS" for result in results)
    )
    payload = {
        "schema": "macro-feature-cron-v2",
        "generated_at_utc": _generated_at_utc(),
        "result": "PASS" if passed else "FAIL",
        "wrapper": "ops/run-macro-loader-daily.sh",
        "stages": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

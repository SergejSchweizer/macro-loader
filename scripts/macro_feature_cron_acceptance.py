"""Run the installed daily wrapper and prove its PostgreSQL replay is a no-op."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from api.logging_config import configure_debug_logging

logger = logging.getLogger(__name__)


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
    credentials = (
        'export PGUSER="$PGREADUSER" PGPASSWORD="$PGREADPASSWORD"; '
        if command == "postgres-verify"
        else 'export PGUSER="$PGSYNCUSER" PGPASSWORD="$PGSYNCPASSWORD"; '
    )
    shell = (
        "set -euo pipefail; "
        'export MACRO_LOADER_PROJECT_ROOT="$1"; '
        'eval "$("$1/.venv/bin/python" '
        '"$1/scripts/export_cron_config.py" "$1/config.yaml")"; '
        + credentials
        + 'exec "$1/.venv/bin/macro-loader" --lake-root "$LAKE_ROOT" '
        + command
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
    configure_debug_logging(Path.cwd())
    runner = args.project_root / "ops" / "run-macro-loader-daily.sh"
    logger.debug("daily-cron acceptance started execute=%s", args.execute)
    if not args.execute:
        logger.debug("daily-cron acceptance stopped reason=execute_flag_required")
        print(f"Refusing to execute without --execute: {runner}")
        return 2
    results: list[dict[str, object]] = []
    for name in ("daily-wrapper-run-1", "daily-wrapper-run-2"):
        logger.debug("daily-cron acceptance stage started stage=%s", name)
        try:
            completed = subprocess.run(
                [str(runner)], cwd=args.project_root, check=False, capture_output=True, text=True
            )
        except Exception as exc:
            logger.debug(
                "daily-cron acceptance stage failed stage=%s error_type=%s",
                name,
                type(exc).__name__,
            )
            raise
        results.append({"name": name, "exit_code": completed.returncode})
        logger.debug(
            "daily-cron acceptance stage completed stage=%s exit_code=%d",
            name,
            completed.returncode,
        )
        if completed.returncode != 0:
            break
    if len(results) == 2 and all(result["exit_code"] == 0 for result in results):
        logger.debug("daily-cron acceptance stage started stage=zero-change-replay")
        try:
            replay = subprocess.run(
                _database_command(args.project_root, "gold-sync-postgres"),
                cwd=args.project_root,
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            logger.debug(
                "daily-cron acceptance stage failed stage=zero-change-replay error_type=%s",
                type(exc).__name__,
            )
            raise
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
        logger.debug(
            "daily-cron acceptance replay completed status=%s exit_code=%d",
            "PASS" if no_op else "FAIL",
            replay.returncode,
        )
    if len(results) == 3 and results[-1].get("status") == "PASS":
        logger.debug("daily-cron acceptance stage started stage=independent-postgres-verify")
        try:
            verify = subprocess.run(
                _database_command(args.project_root, "postgres-verify"),
                cwd=args.project_root,
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            logger.debug(
                "daily-cron acceptance stage failed stage=independent-postgres-verify "
                "error_type=%s",
                type(exc).__name__,
            )
            raise
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
        logger.debug(
            "daily-cron acceptance verification completed status=%s exit_code=%d",
            "PASS" if verified else "FAIL",
            verify.returncode,
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
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except Exception as exc:
        logger.debug("daily-cron acceptance report failed error_type=%s", type(exc).__name__)
        raise
    logger.debug(
        "daily-cron acceptance completed result=%s stage_count=%d",
        payload["result"],
        len(results),
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

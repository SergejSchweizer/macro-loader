"""Authorized, deterministic full-history acceptance runner for the feature library."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from api.logging_config import configure_debug_logging
from application.paths import LakePaths
from application.registry import SERIES_REGISTRY
from ingestion.bronze_uow import FilesystemBronzeUnitOfWork

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StageResult:
    name: str
    command: tuple[str, ...]
    status: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake-root", type=Path, required=True)
    parser.add_argument(
        "--report", type=Path, default=Path("artifacts/acceptance/macro-feature-full-run-v1.json")
    )
    parser.add_argument("--execute", action="store_true", help="perform the authorized run")
    return parser


def _commands(lake_root: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    prefix = ("macro-loader", "--lake-root", str(lake_root))
    commands: list[tuple[str, tuple[str, ...]]] = []
    bronze = FilesystemBronzeUnitOfWork(LakePaths(lake_root))
    for series, contract in SERIES_REGISTRY.items():
        operation = "bootstrap" if bronze.bounds(contract).maximum is None else "reconcile"
        commands.append((f"{operation}:{series}", (*prefix, operation, "--series", series)))
    commands.extend(
        (
            ("silver-build", (*prefix, "silver-build")),
            ("gold-build", (*prefix, "gold-build")),
            ("postgres-migrate", (*prefix, "postgres-migrate")),
            ("gold-sync-postgres", (*prefix, "gold-sync-postgres")),
            ("postgres-verify", (*prefix, "postgres-verify")),
            ("gold-sync-postgres-replay", (*prefix, "gold-sync-postgres")),
            ("postgres-verify-replay", (*prefix, "postgres-verify")),
        )
    )
    return tuple(commands)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_debug_logging(Path.cwd())
    logger.debug("full-run acceptance planning started")
    try:
        commands = _commands(args.lake_root)
    except Exception as exc:
        logger.debug("full-run acceptance planning failed error_type=%s", type(exc).__name__)
        raise
    logger.debug(
        "full-run acceptance plan completed command_count=%d execute=%s",
        len(commands),
        args.execute,
    )
    if not args.execute:
        logger.debug("full-run acceptance stopped reason=execute_flag_required")
        print("Refusing to execute without --execute; planned stages:")
        for name, command in commands:
            print(name, " ".join(command))
        return 2
    results: list[StageResult] = []
    passed = True
    for name, command in commands:
        logger.debug("full-run acceptance stage started stage=%s", name)
        try:
            completed = subprocess.run(
                command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception as exc:
            logger.debug(
                "full-run acceptance stage failed stage=%s error_type=%s",
                name,
                type(exc).__name__,
            )
            raise
        status = "PASS" if completed.returncode == 0 else "FAIL"
        results.append(StageResult(name, command, status))
        logger.debug(
            "full-run acceptance stage completed stage=%s status=%s exit_code=%d",
            name,
            status,
            completed.returncode,
        )
        if status == "FAIL":
            passed = False
            break
    payload = {
        "schema": "macro-feature-full-run-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "result": "PASS" if passed and len(results) == len(commands) else "FAIL",
        "stages": [asdict(result) for result in results],
    }
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except Exception as exc:
        logger.debug("full-run acceptance report failed error_type=%s", type(exc).__name__)
        raise
    logger.debug(
        "full-run acceptance completed result=%s stage_count=%d",
        payload["result"],
        len(results),
    )
    return 0 if payload["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

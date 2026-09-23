"""Run the authorized Fed ``macro_raw`` quality acceptance and emit a report."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StageResult:
    name: str
    command: tuple[str, ...]
    exit_code: int


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake-root", type=Path, required=True)
    parser.add_argument("--dsn", help="PostgreSQL DSN used by the guarded QA stages")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("artifacts/acceptance/fed-policy-macro-raw-quality-v1.json"),
    )
    parser.add_argument("--today", help="completed source date in ISO format")
    parser.add_argument("--execute", action="store_true", help="perform the authorized run")
    return parser


def _commands(
    lake_root: Path, dsn: str | None = None, today: str | None = None
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    prefix: tuple[str, ...] = ("macro-loader", "--lake-root", str(lake_root))
    if today is not None:
        prefix = (*prefix, "--today", today)
    database = ("--dsn", dsn) if dsn else ()
    return (
        ("fed-policy-macro-raw-reconcile", (*prefix, "fed-policy-reconcile", *database)),
        ("fed-policy-macro-raw-sync", (*prefix, "fed-policy-sync-postgres", *database)),
        ("fed-policy-macro-raw-verify", (*prefix, "postgres-verify", *database)),
    )


def _write_report(
    report: Path,
    results: list[StageResult],
    expected_stage_count: int,
    *,
    planned: bool = False,
) -> None:
    passed = (
        not planned
        and len(results) == expected_stage_count
        and all(result.exit_code == 0 for result in results)
    )
    payload = {
        "authorization": "explicit-execute" if not planned else "not-authorized",
        "historical_start": "2010-01-01",
        "result": "PASS" if passed else "FAIL",
        "schema": "fed-policy-macro-raw-quality-v1",
        "stages": [asdict(result) for result in results],
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    commands = _commands(args.lake_root, args.dsn, args.today)
    if not args.execute:
        print("Refusing to execute without --execute; planned stages:")
        for name, command in commands:
            print(name, " ".join(command))
        _write_report(args.report, [], len(commands), planned=True)
        return 2

    results: list[StageResult] = []
    for name, command in commands:
        completed = subprocess.run(command, check=False)
        result = StageResult(name, command, completed.returncode)
        results.append(result)
        if completed.returncode != 0:
            break
    _write_report(args.report, results, len(commands))
    return 0 if len(results) == len(commands) and all(r.exit_code == 0 for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

"""Run the installed Sunday wrapper twice and emit a sanitized operational report."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--report", type=Path, default=Path("artifacts/acceptance/macro-feature-cron-v1.json")
    )
    parser.add_argument("--execute", action="store_true", help="execute the installed wrapper")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runner = args.project_root / "ops" / "run-macro-loader-sunday.sh"
    if not args.execute:
        print(f"Refusing to execute without --execute: {runner}")
        return 2
    results: list[dict[str, object]] = []
    for name in ("mutation-run", "unchanged-replay"):
        completed = subprocess.run([str(runner)], cwd=args.project_root, check=False)
        results.append({"name": name, "exit_code": completed.returncode})
        if completed.returncode != 0:
            break
    passed = len(results) == 2 and all(result["exit_code"] == 0 for result in results)
    payload = {
        "schema": "macro-feature-cron-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "result": "PASS" if passed else "FAIL",
        "wrapper": "ops/run-macro-loader-sunday.sh",
        "stages": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the installed Fed-policy EOD wrapper twice and emit sanitized evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--report", type=Path, default=Path("artifacts/acceptance/fed-policy-cron-v1.json")
    )
    parser.add_argument("--execute", action="store_true", help="execute the installed wrapper")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runner = args.project_root / "ops" / "run-macro-loader-sunday.sh"
    if not args.execute:
        print(f"Refusing to execute without --execute: {runner}")
        return 2

    stages: list[dict[str, object]] = []
    for name in ("mutation-run", "unchanged-replay"):
        completed = subprocess.run([str(runner)], cwd=args.project_root, check=False)
        stages.append({"name": name, "exit_code": completed.returncode})
        if completed.returncode != 0:
            break
    passed = len(stages) == 2 and all(stage["exit_code"] == 0 for stage in stages)
    payload = {
        "schema": "fed-policy-cron-v1",
        "result": "PASS" if passed else "FAIL",
        "authorization": "explicit-execute",
        "wrapper": "ops/run-macro-loader-sunday.sh",
        "stages": stages,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

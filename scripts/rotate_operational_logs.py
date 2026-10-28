"""Rotate and retain the two stable cron-wrapper logs for 90 calendar days."""

from __future__ import annotations

import argparse
import re
from datetime import date, datetime, timedelta
from pathlib import Path

_LOG_NAMES = ("macro-loader.log", "macro-loader-gold-copy.log")
_ARCHIVE_PATTERN = re.compile(r"^(macro-loader(?:-gold-copy)?\.log)\.(\d{4}-\d{2}-\d{2})$")
_RETENTION_DAYS = 90


def rotate_operational_logs(log_directory: Path, *, today: date | None = None) -> tuple[Path, ...]:
    """Rotate prior-day wrapper logs and remove only dated archives older than 90 days."""
    current_day = today or date.today()
    log_directory.mkdir(parents=True, exist_ok=True)
    rotated: list[Path] = []

    for name in _LOG_NAMES:
        source = log_directory / name
        if not source.is_file() or source.is_symlink():
            continue
        modified_day = datetime.fromtimestamp(source.stat().st_mtime).date()
        if modified_day >= current_day:
            continue
        destination = log_directory / f"{name}.{modified_day.isoformat()}"
        if destination.exists():
            raise FileExistsError(f"refusing to overwrite an existing log archive: {destination}")
        source.replace(destination)
        rotated.append(destination)

    cutoff = current_day - timedelta(days=_RETENTION_DAYS)
    for candidate in log_directory.iterdir():
        if not candidate.is_file() or candidate.is_symlink():
            continue
        match = _ARCHIVE_PATTERN.fullmatch(candidate.name)
        if match is None:
            continue
        archive_day = date.fromisoformat(match.group(2))
        if archive_day < cutoff:
            candidate.unlink()
    return tuple(rotated)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-dir", type=Path, required=True)
    args = parser.parse_args()
    rotate_operational_logs(args.log_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

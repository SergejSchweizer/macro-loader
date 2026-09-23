"""Pure quality checks for Fed origins materialized in ``macro_raw``."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any

from application.fed_policy_postgres import FED_POLICY_FEATURE_COLUMNS


@dataclass(frozen=True, slots=True)
class FeatureCoverage:
    first_non_null: date | None
    last_non_null: date | None
    non_null_count: int
    null_count: int
    longest_gap_days: int
    yearly_non_null: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class QualityReport:
    result: str
    checks: tuple[str, ...]
    coverage: dict[str, FeatureCoverage]
    mismatches: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.result not in {"PASS", "FAIL"}:
            raise ValueError("quality result must be PASS or FAIL")
        if tuple(sorted(set(self.checks))) != self.checks:
            raise ValueError("quality checks must be unique and sorted")
        if tuple(sorted(set(self.mismatches))) != self.mismatches:
            raise ValueError("quality mismatches must be unique and sorted")

    def as_json(self) -> str:
        return json.dumps(
            {
                "checks": list(self.checks),
                "coverage": {
                    name: {
                        "first_non_null": value.first_non_null.isoformat()
                        if value.first_non_null
                        else None,
                        "last_non_null": value.last_non_null.isoformat()
                        if value.last_non_null
                        else None,
                        "longest_gap_days": value.longest_gap_days,
                        "non_null_count": value.non_null_count,
                        "null_count": value.null_count,
                        "yearly_non_null": dict(value.yearly_non_null),
                    }
                    for name, value in sorted(self.coverage.items())
                },
                "mismatches": list(self.mismatches),
                "result": self.result,
                "schema": "fed-policy-macro-raw-quality-v1",
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )


def _day(row: Mapping[str, Any]) -> date:
    timestamp = row["timestamp_m1"]
    return timestamp.date() if isinstance(timestamp, datetime) else timestamp


def compute_feature_coverage(
    rows: Iterable[Mapping[str, Any]],
    start: date,
    end: date,
) -> dict[str, FeatureCoverage]:
    bounded = sorted((_day(row), row) for row in rows if start <= _day(row) <= end)
    result: dict[str, FeatureCoverage] = {}
    for feature in FED_POLICY_FEATURE_COLUMNS:
        non_null_days = sorted(day for day, row in bounded if row.get(feature) is not None)
        years: dict[int, int] = {}
        for day in non_null_days:
            years[day.year] = years.get(day.year, 0) + 1
        occupied = set(non_null_days)
        longest_gap = 0
        cursor = start
        while cursor <= end:
            if cursor not in occupied:
                gap_start = cursor
                while cursor <= end and cursor not in occupied:
                    cursor += timedelta(days=1)
                longest_gap = max(longest_gap, (cursor - gap_start).days)
            else:
                cursor += timedelta(days=1)
        result[feature] = FeatureCoverage(
            non_null_days[0] if non_null_days else None,
            non_null_days[-1] if non_null_days else None,
            len(non_null_days),
            (end - start).days + 1 - len(non_null_days),
            longest_gap,
            tuple(sorted(years.items())),
        )
    return result


def diff_canonical_parity(
    macro_raw_rows: Iterable[Mapping[str, Any]],
    canonical_rows: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    canonical = {_day(row): row for row in canonical_rows}
    mismatches: list[str] = []
    for row in macro_raw_rows:
        day = _day(row)
        expected = canonical.get(day)
        if expected is None:
            continue
        for feature in FED_POLICY_FEATURE_COLUMNS:
            if row.get(feature) != expected.get(feature):
                mismatches.append(f"{day.isoformat()}:{feature}")
    return tuple(sorted(set(mismatches)))


def semantic_quality_checks(rows: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    failures: set[str] = set()
    for row in rows:
        for feature in FED_POLICY_FEATURE_COLUMNS:
            value = row.get(feature)
            if value is not None and (not isinstance(value, (int, float)) or not isfinite(value)):
                failures.add(f"finite:{feature}")
        uncertainty = row.get("fed_next_uncertainty_bp")
        if uncertainty is not None and uncertainty < 0:
            failures.add("uncertainty_non_negative")
        available = row.get("available_at_utc")
        timestamp = row.get("timestamp_m1")
        if available is not None and timestamp is not None and available < timestamp:
            failures.add("point_in_time_availability")
    return tuple(sorted(failures))

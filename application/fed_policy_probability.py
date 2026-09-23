"""Pure reconstruction of CME-style quarter-point meeting probabilities."""

from __future__ import annotations

import calendar
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date

import polars as pl

METHODOLOGY_VERSION = "cme-zq-month-weighted-v1"
METHODOLOGY_FINGERPRINT = hashlib.sha256(METHODOLOGY_VERSION.encode()).hexdigest()
PROBABILITY_COLUMNS = (
    "observation_date",
    "meeting_date",
    "move_bp",
    "probability",
    "available_at_utc",
    "methodology_version",
)


@dataclass(frozen=True, slots=True)
class ProbabilityOutcome:
    move_bp: float
    probability: float


def reconstruct_meeting_distribution(
    settlements: dict[str, float], meeting: date, baseline_percent: float
) -> tuple[ProbabilityOutcome, ...]:
    """Infer a normalized quarter-point distribution for one future meeting."""
    month_key = f"{calendar.month_abbr[meeting.month].upper()} {meeting.year % 100:02d}"
    if month_key not in settlements or not math.isfinite(baseline_percent):
        return ()
    if any(not math.isfinite(value) or value <= 0 for value in settlements.values()):
        raise ValueError("settlement curve must contain finite positive prices")
    implied_percent = 100.0 - settlements[month_key]
    previous = _previous_month_key(meeting)
    pre_percent = 100.0 - settlements[previous] if previous in settlements else baseline_percent
    days = calendar.monthrange(meeting.year, meeting.month)[1]
    after = days - meeting.day + 1
    if after <= 0:
        raise ValueError("meeting date is outside its calendar month")
    next_key = _next_month_key(meeting)
    if meeting.day <= 3 and next_key in settlements:
        post_percent = 100.0 - settlements[next_key]
    else:
        post_percent = (implied_percent * days - pre_percent * (meeting.day - 1)) / after
    expected_move_bp = (post_percent - pre_percent) * 100.0
    lower = math.floor(expected_move_bp / 25.0) * 25.0
    upper = lower + 25.0
    upper_probability = max(0.0, min(1.0, (expected_move_bp - lower) / 25.0))
    return (
        ProbabilityOutcome(lower, 1.0 - upper_probability),
        ProbabilityOutcome(upper, upper_probability),
    )


def reconstruct_probability_tree(
    settlements: dict[str, float],
    observation_date: date,
    meetings: tuple[date, ...],
    baseline_percent: float,
    available_at_utc: object,
) -> pl.DataFrame:
    """Build deterministic distributions for known future meetings."""
    if not meetings:
        return pl.DataFrame(
            schema={
                "observation_date": pl.Date,
                "meeting_date": pl.Date,
                "move_bp": pl.Float64,
                "probability": pl.Float64,
                "available_at_utc": pl.Datetime("us", "UTC"),
                "methodology_version": pl.String,
            }
        )
    rows: list[dict[str, object]] = []
    for meeting in sorted(set(meetings)):
        if meeting <= observation_date:
            continue
        for outcome in reconstruct_meeting_distribution(settlements, meeting, baseline_percent):
            if outcome.probability <= 0:
                continue
            rows.append(
                {
                    "observation_date": observation_date,
                    "meeting_date": meeting,
                    "move_bp": outcome.move_bp,
                    "probability": outcome.probability,
                    "available_at_utc": available_at_utc,
                    "methodology_version": METHODOLOGY_VERSION,
                }
            )
    return pl.DataFrame(
        rows,
        schema={
            "observation_date": pl.Date,
            "meeting_date": pl.Date,
            "move_bp": pl.Float64,
            "probability": pl.Float64,
            "available_at_utc": pl.Datetime("us", "UTC"),
            "methodology_version": pl.String,
        },
    ).select(PROBABILITY_COLUMNS)


def methodology_payload() -> str:
    return json.dumps(
        {"version": METHODOLOGY_VERSION, "fingerprint": METHODOLOGY_FINGERPRINT}, sort_keys=True
    )


def _previous_month_key(value: date) -> str:
    year, month = (value.year - 1, 12) if value.month == 1 else (value.year, value.month - 1)
    return f"{calendar.month_abbr[month].upper()} {year % 100:02d}"


def _next_month_key(value: date) -> str:
    year, month = (value.year + 1, 1) if value.month == 12 else (value.year, value.month + 1)
    return f"{calendar.month_abbr[month].upper()} {year % 100:02d}"

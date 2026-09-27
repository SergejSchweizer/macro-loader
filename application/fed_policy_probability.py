"""Pure reconstruction of CME-style quarter-point meeting probabilities."""

from __future__ import annotations

import calendar
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date

import polars as pl

METHODOLOGY_VERSION = "cme-zq-month-weighted-v2"
_METHODOLOGY_SPEC = {
    "version": METHODOLOGY_VERSION,
    "anchor": "latest-full-non-meeting-month-or-current-effr",
    "day_weighting": "before=meeting.day-1, after=days-in-month-before",
    "outcomes": "quarter-point-floor-and-upper-interpolation",
    "missing_anchor": "empty-distribution",
}
METHODOLOGY_FINGERPRINT = hashlib.sha256(
    json.dumps(_METHODOLOGY_SPEC, separators=(",", ":"), sort_keys=True).encode()
).hexdigest()
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
    """Infer a normalized quarter-point distribution for one future meeting.

    A meeting-month futures price is a weighted average.  The pre-meeting rate
    is anchored by the immediately preceding complete month when available;
    otherwise the point-in-time EFFR/policy baseline is used.  The meeting day
    is included in the post-meeting period, matching CME's calendar weighting.
    """
    return _reconstruct_with_pre_rate(
        settlements,
        meeting,
        _initial_anchor(settlements, meeting, baseline_percent),
    )


def _reconstruct_with_pre_rate(
    settlements: dict[str, float], meeting: date, pre_percent: float
) -> tuple[ProbabilityOutcome, ...]:
    month_key = f"{calendar.month_abbr[meeting.month].upper()} {meeting.year % 100:02d}"
    if month_key not in settlements or not math.isfinite(pre_percent):
        return ()
    if any(not math.isfinite(value) or value <= 0 for value in settlements.values()):
        raise ValueError("settlement curve must contain finite positive prices")
    implied_percent = 100.0 - settlements[month_key]
    days = calendar.monthrange(meeting.year, meeting.month)[1]
    before = meeting.day - 1
    after = days - before
    if after <= 0:
        raise ValueError("meeting date is outside its calendar month")
    post_percent = (implied_percent * days - pre_percent * before) / after
    expected_move_bp = (post_percent - pre_percent) * 100.0
    lower = math.floor(expected_move_bp / 25.0) * 25.0
    upper = lower + 25.0
    upper_probability = max(0.0, min(1.0, (expected_move_bp - lower) / 25.0))
    return (
        ProbabilityOutcome(lower, 1.0 - upper_probability),
        ProbabilityOutcome(upper, upper_probability),
    )


def _initial_anchor(
    settlements: dict[str, float], meeting: date, baseline_percent: float
) -> float:
    previous = _previous_month_key(meeting)
    if previous in settlements:
        return 100.0 - settlements[previous]
    return baseline_percent


def _post_rate(
    settlements: dict[str, float], meeting: date, pre_percent: float
) -> float | None:
    month_key = f"{calendar.month_abbr[meeting.month].upper()} {meeting.year % 100:02d}"
    if month_key not in settlements or not math.isfinite(pre_percent):
        return None
    days = calendar.monthrange(meeting.year, meeting.month)[1]
    before = meeting.day - 1
    after = days - before
    return ((100.0 - settlements[month_key]) * days - pre_percent * before) / after


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
    ordered_meetings = sorted(set(meeting for meeting in meetings if meeting > observation_date))
    last_post: float | None = None
    last_meeting: date | None = None
    for meeting in ordered_meetings:
        if last_post is None:
            pre_percent = _initial_anchor(settlements, meeting, baseline_percent)
        elif last_meeting is not None and (
            last_meeting.year == meeting.year and last_meeting.month == meeting.month
        ):
            pre_percent = last_post
        else:
            previous = _previous_month_key(meeting)
            previous_has_meeting = any(
                prior.year == meeting.year
                and prior.month == meeting.month - 1
                if meeting.month > 1
                else prior.year == meeting.year - 1
                and prior.month == 12
                for prior in ordered_meetings
                if prior < meeting
            )
            pre_percent = (
                100.0 - settlements[previous]
                if previous in settlements and not previous_has_meeting
                else last_post
            )
        for outcome in _reconstruct_with_pre_rate(settlements, meeting, pre_percent):
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
        last_post = _post_rate(settlements, meeting, pre_percent)
        last_meeting = meeting
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
        {**_METHODOLOGY_SPEC, "fingerprint": METHODOLOGY_FINGERPRINT}, sort_keys=True
    )


def _previous_month_key(value: date) -> str:
    year, month = (value.year - 1, 12) if value.month == 1 else (value.year, value.month - 1)
    return f"{calendar.month_abbr[month].upper()} {year % 100:02d}"


def _next_month_key(value: date) -> str:
    year, month = (value.year + 1, 1) if value.month == 12 else (value.year, value.month + 1)
    return f"{calendar.month_abbr[month].upper()} {year % 100:02d}"

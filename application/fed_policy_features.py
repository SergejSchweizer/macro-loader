"""Pure Fed policy-expectations feature calculations.

The input is a normalized, end-of-day snapshot of CME FedWatch meeting
outcomes.  This module deliberately emits only the five requested features;
source availability metadata remains in the policy snapshot layer.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import sqrt

import polars as pl

FED_POLICY_FEATURE_COLUMNS = (
    "fed_next_expected_move_bp",
    "fed_next_uncertainty_bp",
    "fed_3m_expected_move_bp",
    "fed_next_expected_move_bp_delta_5obs",
    "fomc_business_days_to_next",
)
FED_POLICY_SNAPSHOT_COLUMNS = (
    "observation_date",
    "meeting_date",
    "move_bp",
    "probability",
    "available_at_utc",
)
EOD_UTC = (23, 59, 59, 999999)


@dataclass(frozen=True, slots=True)
class FedMeetingOutcome:
    """One CME probability-tree outcome for one meeting."""

    meeting_date: date
    move_bp: float
    probability: float


def _observed_holiday(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _us_federal_holidays(year: int) -> frozenset[date]:
    """Return observed US federal holidays for a calendar year."""
    def nth_weekday(month: int, weekday: int, occurrence: int) -> date:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (occurrence - 1))

    holidays = {
        _observed_holiday(date(year, 1, 1)),
        _observed_holiday(date(year, 6, 19)),
        _observed_holiday(date(year, 7, 4)),
        _observed_holiday(date(year, 11, 11)),
        _observed_holiday(date(year, 12, 25)),
    }
    # Federal holidays defined by weekday occurrence.
    holidays.add(nth_weekday(1, 0, 3))
    holidays.add(nth_weekday(2, 0, 3))
    holidays.add(
        max(
            date(year, 5, day)
            for day in range(25, 32)
            if date(year, 5, day).weekday() == 0
        )
    )
    holidays.add(nth_weekday(9, 0, 1))
    holidays.add(nth_weekday(10, 0, 2))
    holidays.add(nth_weekday(11, 3, 4))
    return frozenset(holidays)


def us_business_days_between(start: date, end: date) -> int:
    """Count US business days strictly after ``start`` through ``end``."""
    if end <= start:
        return 0
    holidays = _us_federal_holidays(start.year) | _us_federal_holidays(end.year)
    cursor = start + timedelta(days=1)
    count = 0
    while cursor <= end:
        if cursor.weekday() < 5 and cursor not in holidays:
            count += 1
        cursor += timedelta(days=1)
    return count


def _add_months(day: date, months: int) -> date:
    ordinal = day.year * 12 + day.month - 1 + months
    year, month_index = divmod(ordinal, 12)
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _validate_snapshot(frame: pl.DataFrame) -> None:
    missing = [column for column in FED_POLICY_SNAPSHOT_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"Fed policy snapshot is missing columns: {missing}")
    if frame.is_empty():
        return
    for column in ("observation_date", "meeting_date"):
        if frame.schema[column] != pl.Date:
            raise TypeError(f"Fed policy {column} must be Date")
    if frame.schema["available_at_utc"] != pl.Datetime("us", "UTC"):
        raise TypeError("Fed policy available_at_utc must be UTC microsecond datetime")
    expected_time = time(*EOD_UTC)
    if bool(
        frame.select(pl.col("available_at_utc").dt.time() != expected_time).to_series().any()
    ):
        raise ValueError("FedWatch snapshots are only available at the explicit UTC EOD boundary")
    if bool(
        frame.select((pl.col("probability") < 0) | (pl.col("probability") > 1)).to_series().any()
    ):
        raise ValueError("Fed policy probabilities must lie in [0, 1]")
    if bool(frame.select(~pl.col("move_bp").is_finite()).to_series().any()):
        raise ValueError("Fed policy moves must be finite")
    if bool(frame.select(~pl.col("probability").is_finite()).to_series().any()):
        raise ValueError("Fed policy probabilities must be finite")
    duplicate = frame.group_by(["observation_date", "meeting_date", "move_bp"]).len()
    if duplicate.filter(pl.col("len") > 1).height:
        raise ValueError("Fed policy snapshot contains duplicate outcomes")


def _meeting_mean(outcomes: Iterable[FedMeetingOutcome]) -> float:
    values = tuple(outcomes)
    total = sum(item.probability for item in values)
    if not values or total <= 0:
        raise ValueError("meeting outcome probabilities must have positive mass")
    if abs(total - 1.0) > 1e-6:
        raise ValueError("meeting outcome probabilities must sum to one")
    return sum(item.probability * item.move_bp for item in values)


def _feature_row(
    observation_date: date, outcomes: Sequence[FedMeetingOutcome]
) -> dict[str, object]:
    by_meeting: dict[date, list[FedMeetingOutcome]] = {}
    for outcome in outcomes:
        if outcome.meeting_date > observation_date:
            by_meeting.setdefault(outcome.meeting_date, []).append(outcome)
    meetings = sorted(by_meeting)
    if not meetings:
        return {
            "timestamp_m1": datetime.combine(observation_date, datetime.min.time()),
            **{column: None for column in FED_POLICY_FEATURE_COLUMNS},
        }
    next_meeting = meetings[0]
    next_outcomes = by_meeting[next_meeting]
    next_mean = _meeting_mean(next_outcomes)
    next_uncertainty = sqrt(
        sum(item.probability * (item.move_bp - next_mean) ** 2 for item in next_outcomes)
    )
    horizon = _add_months(observation_date, 3)
    horizon_mean = sum(
        _meeting_mean(by_meeting[meeting]) for meeting in meetings if meeting <= horizon
    )
    return {
        "timestamp_m1": datetime.combine(observation_date, datetime.min.time()),
        "fed_next_expected_move_bp": next_mean,
        "fed_next_uncertainty_bp": next_uncertainty,
        "fed_3m_expected_move_bp": horizon_mean,
        "fed_next_expected_move_bp_delta_5obs": None,
        "fomc_business_days_to_next": float(
            us_business_days_between(observation_date, next_meeting)
        ),
    }


def build_fed_policy_features(snapshot_frame: pl.DataFrame) -> pl.DataFrame:
    """Build the exact five causal Fed policy features from EOD snapshots."""
    if snapshot_frame.is_empty() and not snapshot_frame.columns:
        return pl.DataFrame(
            schema={
                "timestamp_m1": pl.Datetime("us", "UTC"),
                **{c: pl.Float64 for c in FED_POLICY_FEATURE_COLUMNS},
            }
        )
    _validate_snapshot(snapshot_frame)
    if snapshot_frame.is_empty():
        return pl.DataFrame(
            schema={
                "timestamp_m1": pl.Datetime("us", "UTC"),
                **{c: pl.Float64 for c in FED_POLICY_FEATURE_COLUMNS},
            }
        )
    rows: list[dict[str, object]] = []
    for key, group in (
        snapshot_frame.sort("observation_date")
        .partition_by("observation_date", as_dict=True)
        .items()
    ):
        observation_date = key[0] if isinstance(key, tuple) else key
        outcomes = tuple(
            FedMeetingOutcome(row["meeting_date"], row["move_bp"], row["probability"])
            for row in group.iter_rows(named=True)
        )
        rows.append(_feature_row(observation_date, outcomes))
    result = pl.DataFrame(rows).with_columns(
        pl.col("timestamp_m1").cast(pl.Datetime("us")).dt.replace_time_zone("UTC")
    )
    result = result.with_columns(
        (pl.col("fed_next_expected_move_bp") - pl.col("fed_next_expected_move_bp").shift(5)).alias(
            "fed_next_expected_move_bp_delta_5obs"
        )
    )
    return result.select(["timestamp_m1", *FED_POLICY_FEATURE_COLUMNS]).sort("timestamp_m1")

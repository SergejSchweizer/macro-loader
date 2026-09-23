"""Pure Fed policy-expectations feature calculations.

The input is a normalized, end-of-day snapshot of CME FedWatch meeting
outcomes.  This module deliberately emits only the four requested features;
source availability metadata remains in the policy snapshot layer.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from math import sqrt

import polars as pl

FED_POLICY_FEATURE_COLUMNS = (
    "fed_next_expected_move_bp",
    "fed_next_uncertainty_bp",
    "fed_path_slope_m3_bp",
    "fed_repricing_5obs_bp",
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
    if bool(frame.select(pl.col("available_at_utc").dt.time() != expected_time).to_series().any()):
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
    observation_date: date, outcomes: Sequence[FedMeetingOutcome], available_at_utc: datetime
) -> dict[str, object]:
    by_meeting: dict[date, list[FedMeetingOutcome]] = {}
    for outcome in outcomes:
        if outcome.meeting_date > observation_date:
            by_meeting.setdefault(outcome.meeting_date, []).append(outcome)
    meetings = sorted(by_meeting)
    if not meetings:
        return {
            "timestamp_m1": datetime.combine(observation_date, datetime.min.time(), tzinfo=UTC),
            **{column: None for column in FED_POLICY_FEATURE_COLUMNS},
            "available_at_utc": available_at_utc,
        }
    next_meeting = meetings[0]
    next_outcomes = by_meeting[next_meeting]
    next_mean = _meeting_mean(next_outcomes)
    next_uncertainty = sqrt(
        sum(item.probability * (item.move_bp - next_mean) ** 2 for item in next_outcomes)
    )
    third_mean = _meeting_mean(by_meeting[meetings[2]]) - next_mean if len(meetings) >= 3 else None
    return {
        "timestamp_m1": datetime.combine(observation_date, datetime.min.time(), tzinfo=UTC),
        "fed_next_expected_move_bp": next_mean,
        "fed_next_uncertainty_bp": next_uncertainty,
        "fed_path_slope_m3_bp": third_mean,
        "fed_repricing_5obs_bp": None,
        "available_at_utc": available_at_utc,
    }


def build_fed_policy_features(snapshot_frame: pl.DataFrame) -> pl.DataFrame:
    """Build the exact four causal Fed policy features from EOD snapshots."""
    if snapshot_frame.is_empty() and not snapshot_frame.columns:
        return pl.DataFrame(
            schema={
                "timestamp_m1": pl.Datetime("us", "UTC"),
                **{c: pl.Float64 for c in FED_POLICY_FEATURE_COLUMNS},
                "available_at_utc": pl.Datetime("us", "UTC"),
            }
        )
    _validate_snapshot(snapshot_frame)
    if snapshot_frame.is_empty():
        return pl.DataFrame(
            schema={
                "timestamp_m1": pl.Datetime("us", "UTC"),
                **{c: pl.Float64 for c in FED_POLICY_FEATURE_COLUMNS},
                "available_at_utc": pl.Datetime("us", "UTC"),
            }
        )
    rows: list[dict[str, object]] = []
    for key, group in (
        snapshot_frame.sort("observation_date")
        .partition_by("observation_date", as_dict=True)
        .items()
    ):
        observation_date = key[0] if isinstance(key, tuple) else key
        available_value = group.get_column("available_at_utc").max()
        if not isinstance(available_value, datetime):
            raise ValueError("Fed policy availability must not be null")
        available_at_utc = available_value
        outcomes = tuple(
            FedMeetingOutcome(row["meeting_date"], row["move_bp"], row["probability"])
            for row in group.iter_rows(named=True)
        )
        rows.append(_feature_row(observation_date, outcomes, available_at_utc))
    result = pl.DataFrame(rows).with_columns(
        pl.col("timestamp_m1").cast(pl.Datetime("us")).dt.replace_time_zone("UTC"),
        pl.col("available_at_utc").cast(pl.Datetime("us", "UTC")),
    )
    valid_next_values: list[float] = []
    repricing: list[float | None] = []
    for value in result.get_column("fed_next_expected_move_bp").to_list():
        if value is None:
            repricing.append(None)
            continue
        repricing.append(value - valid_next_values[-5] if len(valid_next_values) >= 5 else None)
        valid_next_values.append(value)
    result = result.with_columns(pl.Series("fed_repricing_5obs_bp", repricing, dtype=pl.Float64))
    return result.select(["timestamp_m1", *FED_POLICY_FEATURE_COLUMNS, "available_at_utc"]).sort(
        "timestamp_m1"
    )

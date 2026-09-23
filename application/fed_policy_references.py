"""Point-in-time contracts for Federal Reserve reference data."""

from __future__ import annotations

import polars as pl

EFFR_COLUMNS = ("activity_date", "effr_percent", "source_id", "source_url", "available_at_utc")
MEETING_COLUMNS = (
    "decision_date",
    "meeting_type",
    "source_id",
    "source_url",
    "first_known_at_utc",
)
TARGET_COLUMNS = (
    "effective_date",
    "lower_bound_percent",
    "upper_bound_percent",
    "source_id",
    "source_url",
    "known_at_utc",
)
UTC = pl.Datetime("us", "UTC")


def validate_effr(frame: pl.DataFrame) -> pl.DataFrame:
    _validate(
        frame,
        EFFR_COLUMNS,
        {
            "activity_date": pl.Date,
            "effr_percent": pl.Float64,
            "source_id": pl.String,
            "source_url": pl.String,
            "available_at_utc": UTC,
        },
    )
    return frame.sort(["activity_date", "available_at_utc"])


def validate_meetings(frame: pl.DataFrame) -> pl.DataFrame:
    _validate(
        frame,
        MEETING_COLUMNS,
        {
            "decision_date": pl.Date,
            "meeting_type": pl.String,
            "source_id": pl.String,
            "source_url": pl.String,
            "first_known_at_utc": UTC,
        },
    )
    return frame.sort(["decision_date", "first_known_at_utc"])


def validate_targets(frame: pl.DataFrame) -> pl.DataFrame:
    _validate(
        frame,
        TARGET_COLUMNS,
        {
            "effective_date": pl.Date,
            "lower_bound_percent": pl.Float64,
            "upper_bound_percent": pl.Float64,
            "source_id": pl.String,
            "source_url": pl.String,
            "known_at_utc": UTC,
        },
    )
    if frame.height and bool(
        frame.select((pl.col("lower_bound_percent") > pl.col("upper_bound_percent")).any()).item()
    ):
        raise ValueError("target lower bound must not exceed upper bound")
    return frame.sort(["effective_date", "known_at_utc"])


def _validate(frame: pl.DataFrame, columns: tuple[str, ...], schema: dict[str, object]) -> None:
    if frame.columns != list(columns):
        raise ValueError("Fed reference schema/order drift")
    for name, expected in schema.items():
        if frame.schema[name] != expected:
            raise TypeError(f"{name} has an invalid type")
    if frame.is_empty():
        return
    for name in columns:
        if name.endswith("_at_utc") and frame.get_column(name).is_null().any():
            raise ValueError(f"{name} must not be null")
    for name in ("source_id", "source_url"):
        if name in frame.columns and frame.get_column(name).str.len_chars().eq(0).any():
            raise ValueError(f"{name} must be non-empty")

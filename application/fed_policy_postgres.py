"""Storage-neutral contracts for synchronizing canonical Fed policy features."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite

import polars as pl

from application.fed_policy_features import FED_POLICY_FEATURE_COLUMNS as FRAME_FEATURE_COLUMNS

__all__ = ["FED_POLICY_FEATURE_COLUMNS"]

FED_POLICY_DATASET_ID = "fed_policy_features_daily"
FED_POLICY_FEATURE_COLUMNS = (
    "fed_next_expected_move_bp",
    "fed_path_slope_m3_bp",
    "fed_next_uncertainty_bp",
    "fed_repricing_5obs_bp",
)
FED_POLICY_LINEAGE_COLUMN = "available_at_utc"
FED_POLICY_COLUMNS = ("timestamp_m1", *FED_POLICY_FEATURE_COLUMNS, FED_POLICY_LINEAGE_COLUMN)


@dataclass(frozen=True, slots=True)
class FedPolicyFeatureRow:
    timestamp_m1: datetime
    values: tuple[float | None, float | None, float | None, float | None]
    available_at_utc: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.timestamp_m1, "timestamp_m1"),
            (self.available_at_utc, "available_at_utc"),
        ):
            if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
                raise ValueError(f"{name} must be zero-offset UTC")
        if len(self.values) != len(FED_POLICY_FEATURE_COLUMNS):
            raise ValueError("Fed policy feature row has an unexpected value count")
        if any(value is not None and not isinstance(value, float) for value in self.values):
            raise TypeError("Fed policy feature values must be float or null")
        if any(value is not None and not isfinite(value) for value in self.values):
            raise ValueError("Fed policy feature values must be finite or null")

    @property
    def row_sha256(self) -> str:
        payload = {
            "timestamp_m1": self.timestamp_m1.isoformat(),
            "values": self.values,
            "available_at_utc": self.available_at_utc.isoformat(),
        }
        return sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()


def fed_policy_rows(frame: pl.DataFrame) -> tuple[FedPolicyFeatureRow, ...]:
    """Validate and convert the canonical local feature frame to immutable rows."""
    expected_frame_columns = ("timestamp_m1", *FRAME_FEATURE_COLUMNS, FED_POLICY_LINEAGE_COLUMN)
    if frame.columns != list(expected_frame_columns):
        raise ValueError("Fed policy feature schema/order drift")
    if frame.schema["timestamp_m1"] != pl.Datetime("us", "UTC"):
        raise TypeError("Fed policy timestamp_m1 must be UTC microsecond datetime")
    if frame.schema["available_at_utc"] != pl.Datetime("us", "UTC"):
        raise TypeError("Fed policy available_at_utc must be UTC microsecond datetime")
    for column in FRAME_FEATURE_COLUMNS:
        if frame.schema[column] != pl.Float64:
            raise TypeError(f"{column} must be Float64")
    if frame.height and frame.get_column("timestamp_m1").n_unique() != frame.height:
        raise ValueError("Fed policy feature timestamps must be unique")
    ordered = frame.sort("timestamp_m1")
    rows = tuple(
        FedPolicyFeatureRow(
            row["timestamp_m1"],
            tuple(row[column] for column in FED_POLICY_FEATURE_COLUMNS),
            row["available_at_utc"],
        )
        for row in ordered.iter_rows(named=True)
    )
    if any(
        row.timestamp_m1 >= following.timestamp_m1
        for row, following in zip(rows, rows[1:], strict=False)
    ):
        raise ValueError("Fed policy feature timestamps must be increasing")
    return rows


@dataclass(frozen=True, slots=True)
class FedPolicyDeltaPlan:
    inserts: tuple[FedPolicyFeatureRow, ...]
    updates: tuple[FedPolicyFeatureRow, ...]
    deletes: tuple[datetime, ...]
    unchanged: tuple[datetime, ...]

    @property
    def mutated(self) -> bool:
        return bool(self.inserts or self.updates or self.deletes)


def plan_fed_policy_delta(
    desired: tuple[FedPolicyFeatureRow, ...],
    existing: tuple[FedPolicyFeatureRow, ...],
) -> FedPolicyDeltaPlan:
    existing_by_timestamp = {row.timestamp_m1: row for row in existing}
    desired_by_timestamp = {row.timestamp_m1: row for row in desired}
    inserts: list[FedPolicyFeatureRow] = []
    updates: list[FedPolicyFeatureRow] = []
    unchanged: list[datetime] = []
    for timestamp in sorted(desired_by_timestamp):
        row = desired_by_timestamp[timestamp]
        previous = existing_by_timestamp.get(timestamp)
        if previous is None:
            inserts.append(row)
        elif previous.row_sha256 == row.row_sha256:
            unchanged.append(timestamp)
        else:
            updates.append(row)
    deletes = sorted(set(existing_by_timestamp) - set(desired_by_timestamp))
    return FedPolicyDeltaPlan(tuple(inserts), tuple(updates), tuple(deletes), tuple(unchanged))

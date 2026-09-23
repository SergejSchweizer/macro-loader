"""Durable point-in-time Federal Reserve reference repositories."""

from __future__ import annotations

from datetime import datetime

import polars as pl

from application.fed_policy_references import validate_effr, validate_meetings, validate_targets
from application.paths import LakePaths
from ingestion.parquet_repository import atomic_write_parquet, merge_frames


class FedPolicyReferenceStore:
    def __init__(self, paths: LakePaths) -> None:
        self._root = paths.root / "fed_policy"

    def _upsert(
        self, name: str, incoming: pl.DataFrame, key: list[str], validator: object
    ) -> pl.DataFrame:
        path = self._root / f"{name}.parquet"
        existing = pl.read_parquet(path) if path.is_file() else incoming.head(0)
        merged, diff = merge_frames(existing, incoming, key=key)
        if diff.has_changes:
            atomic_write_parquet(merged, path)
        return merged

    def read_effr(self) -> pl.DataFrame:
        path = self._root / "effr.parquet"
        return validate_effr(
            pl.read_parquet(path)
            if path.is_file()
            else pl.DataFrame(
                schema={
                    "activity_date": pl.Date,
                    "effr_percent": pl.Float64,
                    "source_id": pl.String,
                    "source_url": pl.String,
                    "available_at_utc": pl.Datetime("us", "UTC"),
                }
            )
        )

    def upsert_effr(self, frame: pl.DataFrame) -> pl.DataFrame:
        return validate_effr(
            self._upsert(
                "effr", validate_effr(frame), ["activity_date", "available_at_utc"], validate_effr
            )
        )

    def read_meetings(self) -> pl.DataFrame:
        path = self._root / "meetings.parquet"
        return validate_meetings(
            pl.read_parquet(path)
            if path.is_file()
            else pl.DataFrame(
                schema={
                    "decision_date": pl.Date,
                    "meeting_type": pl.String,
                    "source_id": pl.String,
                    "source_url": pl.String,
                    "first_known_at_utc": pl.Datetime("us", "UTC"),
                }
            )
        )

    def upsert_meetings(self, frame: pl.DataFrame) -> pl.DataFrame:
        return validate_meetings(
            self._upsert(
                "meetings",
                validate_meetings(frame),
                ["decision_date", "meeting_type", "first_known_at_utc"],
                validate_meetings,
            )
        )

    def read_targets(self) -> pl.DataFrame:
        path = self._root / "targets.parquet"
        return validate_targets(
            pl.read_parquet(path)
            if path.is_file()
            else pl.DataFrame(
                schema={
                    "effective_date": pl.Date,
                    "lower_bound_percent": pl.Float64,
                    "upper_bound_percent": pl.Float64,
                    "source_id": pl.String,
                    "source_url": pl.String,
                    "known_at_utc": pl.Datetime("us", "UTC"),
                }
            )
        )

    def upsert_targets(self, frame: pl.DataFrame) -> pl.DataFrame:
        return validate_targets(
            self._upsert(
                "targets",
                validate_targets(frame),
                ["effective_date", "known_at_utc"],
                validate_targets,
            )
        )

    def effr_as_of(self, cutoff: datetime) -> pl.DataFrame:
        return (
            self.read_effr()
            .filter(pl.col("available_at_utc") <= cutoff)
            .sort(["activity_date", "available_at_utc"])
            .unique(subset=["activity_date"], keep="last")
            .sort("activity_date")
        )

    def meetings_as_of(self, cutoff: datetime) -> pl.DataFrame:
        return (
            self.read_meetings()
            .filter(pl.col("first_known_at_utc") <= cutoff)
            .sort("decision_date")
        )

    def targets_as_of(self, cutoff: datetime) -> pl.DataFrame:
        return (
            self.read_targets()
            .filter(pl.col("known_at_utc") <= cutoff)
            .sort(["effective_date", "known_at_utc"])
            .unique(subset=["effective_date"], keep="last")
            .sort("effective_date")
        )

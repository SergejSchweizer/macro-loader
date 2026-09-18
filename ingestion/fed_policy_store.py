"""Persistence adapter for normalized FedWatch EOD snapshots."""

from __future__ import annotations

from datetime import date

import polars as pl

from application.fed_policy_features import FED_POLICY_SNAPSHOT_COLUMNS
from application.paths import LakePaths
from ingestion.fed_policy_provider import FedPolicyProvider
from ingestion.parquet_repository import atomic_write_parquet

_KEY = ("observation_date", "meeting_date", "move_bp")


class FedPolicySnapshotStore:
    """Small immutable-schema source store with idempotent snapshot upserts."""

    def __init__(self, paths: LakePaths, provider: FedPolicyProvider) -> None:
        self._paths = paths
        self._provider = provider

    def read(self) -> pl.DataFrame:
        path = self._paths.fed_policy_snapshots()
        if not path.is_file():
            return pl.DataFrame(
                schema={
                    "observation_date": pl.Date,
                    "meeting_date": pl.Date,
                    "move_bp": pl.Float64,
                    "probability": pl.Float64,
                    "available_at_utc": pl.Datetime("us", "UTC"),
                }
            )
        frame = pl.read_parquet(path)
        if frame.columns != list(FED_POLICY_SNAPSHOT_COLUMNS):
            raise ValueError("Fed policy snapshot schema/order drift")
        return frame.sort(list(_KEY))

    def refresh(self, start: date, end: date) -> pl.DataFrame:
        incoming = self._provider.fetch(start, end)
        existing = self.read()
        merged = (
            pl.concat([existing, incoming], how="vertical_relaxed") if existing.height else incoming
        )
        if merged.height:
            merged = merged.unique(subset=list(_KEY), keep="last", maintain_order=False).sort(
                list(_KEY)
            )
            atomic_write_parquet(merged, self._paths.fed_policy_snapshots())
        return merged

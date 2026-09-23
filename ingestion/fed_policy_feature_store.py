"""Atomic local publication adapter for canonical Fed-policy features."""

from __future__ import annotations

import polars as pl

from application.fed_policy_features import FED_POLICY_FEATURE_COLUMNS
from application.paths import LakePaths
from ingestion.parquet_repository import atomic_write_parquet


class FedPolicyFeatureStore:
    def __init__(self, paths: LakePaths) -> None:
        self._path = paths.fed_policy_features()

    def read(self) -> pl.DataFrame:
        if not self._path.is_file():
            return pl.DataFrame()
        return pl.read_parquet(self._path)

    def publish(self, frame: pl.DataFrame) -> None:
        required = ("timestamp_m1", *FED_POLICY_FEATURE_COLUMNS, "available_at_utc")
        if frame.columns != list(required):
            raise ValueError("Fed policy feature schema/order drift")
        atomic_write_parquet(frame, self._path)

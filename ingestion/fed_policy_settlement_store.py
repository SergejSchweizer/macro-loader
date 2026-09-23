"""Parquet repository for canonical CME ZQ final settlement curves."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl

from application.fed_policy_settlements import (
    ZQ_SETTLEMENT_KEY,
    empty_zq_settlements,
    validate_zq_settlements,
)
from application.paths import LakePaths
from ingestion.parquet_repository import read_monthly, upsert_monthly


@dataclass(frozen=True, slots=True)
class SettlementUpsertResult:
    frame: pl.DataFrame
    inserted_count: int
    revision_count: int
    unchanged_count: int
    rewritten_paths: tuple[Path, ...]


class FedPolicySettlementStore:
    """Persist ZQ rows with natural-key revisions and monthly partitions."""

    def __init__(self, paths: LakePaths) -> None:
        self._paths = paths

    def _paths_for_existing(self) -> tuple[Path, ...]:
        return tuple(
            sorted(self._paths.fed_policy_settlement_root().glob("year=*/month=*/data.parquet"))
        )

    def _path_for_date(self, observation_date: date) -> Path:
        return self._paths.fed_policy_settlement_month(observation_date)

    def read(self) -> pl.DataFrame:
        paths = self._paths_for_existing()
        if not paths:
            return empty_zq_settlements()
        frame = read_monthly(paths, sort_by=ZQ_SETTLEMENT_KEY)
        return validate_zq_settlements(frame)

    def upsert(self, incoming: pl.DataFrame) -> SettlementUpsertResult:
        incoming = validate_zq_settlements(incoming)
        existing = self.read()
        diff, rewritten = upsert_monthly(
            existing,
            incoming,
            key=ZQ_SETTLEMENT_KEY,
            date_column="observation_date",
            path_for_date=self._path_for_date,
        )
        merged = existing if not diff.has_changes else self.read()
        return SettlementUpsertResult(
            frame=merged,
            inserted_count=diff.inserts.height,
            revision_count=diff.revisions.height,
            unchanged_count=diff.unchanged.height,
            rewritten_paths=rewritten,
        )

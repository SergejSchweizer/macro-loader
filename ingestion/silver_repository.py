"""Filesystem repository for canonical monthly Silver series."""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from application.contracts import SeriesContract
from application.paths import LakePaths
from application.ports.lake import FrameDiff
from application.silver import SILVER_SCHEMA, canonicalize_silver
from ingestion.parquet_repository import read_monthly, upsert_monthly

_SILVER_KEY = ("series_id", "observation_date")
logger = logging.getLogger(__name__)


class SilverSeriesRepository:
    """Repository/Adapter that builds one selected Silver series from retained Bronze."""

    def __init__(self, paths: LakePaths) -> None:
        self._paths = paths

    def _bronze_paths(self, contract: SeriesContract) -> tuple[Path, ...]:
        root = (
            self._paths.root
            / "bronze"
            / f"provider={contract.provider.value}"
            / f"series={contract.series_id}"
        )
        return tuple(sorted(root.glob("year=*/month=*/data.parquet"))) if root.exists() else ()

    def _silver_paths(self, contract: SeriesContract) -> tuple[Path, ...]:
        root = self._paths.root / "silver" / f"series={contract.series_id}"
        return tuple(sorted(root.glob("year=*/month=*/data.parquet"))) if root.exists() else ()

    def read(self, contract: SeriesContract) -> pl.DataFrame:
        logger.debug(
            "Silver series read started provider=%s series_id=%s",
            contract.provider.value,
            contract.series_id,
        )
        try:
            frame = read_monthly(self._silver_paths(contract), sort_by=())
            if not frame.columns:
                frame = pl.DataFrame(schema=SILVER_SCHEMA)
            elif frame.schema != SILVER_SCHEMA:
                raise ValueError("invalid persisted Silver schema")
            else:
                frame = frame.sort(list(_SILVER_KEY))
        except Exception as exc:
            logger.debug(
                "Silver series read failed provider=%s series_id=%s error_type=%s",
                contract.provider.value,
                contract.series_id,
                type(exc).__name__,
            )
            raise
        logger.debug(
            "Silver series read completed provider=%s series_id=%s row_count=%d",
            contract.provider.value,
            contract.series_id,
            frame.height,
        )
        return frame

    def build(self, contract: SeriesContract) -> FrameDiff:
        logger.debug(
            "Silver series build started provider=%s series_id=%s",
            contract.provider.value,
            contract.series_id,
        )
        try:
            bronze = read_monthly(
                self._bronze_paths(contract),
                sort_by=("provider", "series_id", "observation_date"),
            )
            candidate = canonicalize_silver(contract, bronze)
            existing = self.read(contract)
            diff, written = upsert_monthly(
                existing,
                candidate,
                key=_SILVER_KEY,
                date_column="observation_date",
                path_for_date=lambda day: self._paths.silver_month(contract.series_id, day),
            )
        except Exception as exc:
            logger.debug(
                "Silver series build failed provider=%s series_id=%s error_type=%s",
                contract.provider.value,
                contract.series_id,
                type(exc).__name__,
            )
            raise
        logger.debug(
            "Silver series build completed provider=%s series_id=%s "
            "inserted=%d revised=%d written_partitions=%d",
            contract.provider.value,
            contract.series_id,
            diff.inserts.height,
            diff.revisions.height,
            len(written),
        )
        return diff

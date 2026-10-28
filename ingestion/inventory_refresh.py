"""Bronze-derived authoritative inventory refresh Adapter."""

from __future__ import annotations

import logging
from datetime import date

import polars as pl

from application.operational_records import InventoryRecord
from application.paths import LakePaths
from application.registry import SERIES_REGISTRY
from ingestion.operational_repository import write_inventory

logger = logging.getLogger(__name__)


class InventoryRefreshService:
    """Rebuild the deterministic Bronze inventory snapshot for all canonical series."""

    def __init__(self, paths: LakePaths) -> None:
        self._paths = paths

    def refresh(self) -> tuple[InventoryRecord, ...]:
        logger.debug("Bronze inventory refresh started series_count=%d", len(SERIES_REGISTRY))
        try:
            records = tuple(self._record(series_id) for series_id in SERIES_REGISTRY)
            write_inventory(self._paths.inventory(), list(records))
        except Exception as exc:
            logger.debug("Bronze inventory refresh failed error_type=%s", type(exc).__name__)
            raise
        logger.debug(
            "Bronze inventory refresh completed series_count=%d nonempty_count=%d",
            len(records),
            sum(record.row_count > 0 for record in records),
        )
        return records

    def _record(self, series_id: str) -> InventoryRecord:
        contract = SERIES_REGISTRY[series_id]
        root = (
            self._paths.root
            / "bronze"
            / f"provider={contract.provider.value}"
            / f"series={series_id}"
        )
        paths = tuple(sorted(root.glob("year=*/month=*/data.parquet"))) if root.exists() else ()
        if not paths:
            return InventoryRecord(
                series_id=series_id,
                provider=contract.provider,
                min_observation_date=None,
                max_observation_date=None,
                row_count=0,
                duplicate_key_count=0,
                file_count=0,
            )
        frames = [pl.read_parquet(path) for path in paths]
        frame = pl.concat(frames, how="vertical")
        required = {"provider", "series_id", "observation_date"}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"Bronze inventory frame missing columns: {sorted(missing)}")
        if frame.schema["observation_date"] != pl.Date:
            raise TypeError("Bronze inventory observation_date must be Date")
        provider_values = frame.get_column("provider").unique().to_list()
        series_values = frame.get_column("series_id").unique().to_list()
        if provider_values != [contract.provider.value] or series_values != [series_id]:
            raise ValueError("Bronze inventory identity mismatch")
        minimum = frame.get_column("observation_date").min()
        maximum = frame.get_column("observation_date").max()
        if minimum is not None and not isinstance(minimum, date):
            raise TypeError("Bronze inventory minimum must be Date")
        if maximum is not None and not isinstance(maximum, date):
            raise TypeError("Bronze inventory maximum must be Date")
        duplicate_count = frame.select(["series_id", "observation_date"]).is_duplicated().sum()
        return InventoryRecord(
            series_id=series_id,
            provider=contract.provider,
            min_observation_date=minimum,
            max_observation_date=maximum,
            row_count=frame.height,
            duplicate_key_count=int(duplicate_count),
            file_count=len(paths),
        )

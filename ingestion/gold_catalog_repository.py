"""Authoritative Parquet Repository for Gold publication catalog state."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import polars as pl

from application.gold_catalog import GoldBuildStatus, GoldCatalogRecord
from ingestion.parquet_repository import atomic_write_parquet

GOLD_CATALOG_SCHEMA = pl.Schema(
    {
        "dataset_id": pl.String(),
        "build_id": pl.String(),
        "status": pl.String(),
        "current": pl.Boolean(),
        "started_at_utc": pl.Datetime("us", "UTC"),
        "completed_at_utc": pl.Datetime("us", "UTC"),
        "schema_version": pl.Int64(),
        "feature_version": pl.Int64(),
        "min_timestamp": pl.Datetime("us", "UTC"),
        "max_timestamp": pl.Datetime("us", "UTC"),
        "row_count": pl.Int64(),
        "data_path": pl.String(),
        "build_manifest_path": pl.String(),
        "plot_path": pl.String(),
        "pruned_at_utc": pl.Datetime("us", "UTC"),
    }
)
logger = logging.getLogger(__name__)


def _validate_record(record: GoldCatalogRecord) -> None:
    for name in (
        "started_at_utc",
        "completed_at_utc",
        "min_timestamp",
        "max_timestamp",
        "pruned_at_utc",
    ):
        value = getattr(record, name)
        if value is not None and value.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware")
    if record.row_count is not None and record.row_count < 0:
        raise ValueError("row_count cannot be negative")
    if record.completed_at_utc is not None and record.completed_at_utc < record.started_at_utc:
        raise ValueError("completed_at_utc cannot precede started_at_utc")
    if (
        record.min_timestamp is not None
        and record.max_timestamp is not None
        and record.max_timestamp < record.min_timestamp
    ):
        raise ValueError("max_timestamp cannot precede min_timestamp")
    paths = (record.data_path, record.build_manifest_path, record.plot_path)
    present = [path is not None for path in paths]
    if any(present) and not all(present):
        raise ValueError("Gold artifact paths must be all present or all null")
    if record.current and record.status is not GoldBuildStatus.COMPLETE:
        raise ValueError("only a complete Gold build may be current")
    if record.current and record.pruned_at_utc is not None:
        raise ValueError("a pruned Gold build cannot be current")
    if (
        record.status is GoldBuildStatus.COMPLETE
        and record.pruned_at_utc is None
        and (
            record.completed_at_utc is None
            or record.row_count is None
            or not record.artifact_paths_complete
        )
    ):
        raise ValueError(
            "unpruned complete Gold build requires completion metadata and all artifact paths"
        )


def _frame(records: list[GoldCatalogRecord]) -> pl.DataFrame:
    for record in records:
        _validate_record(record)
    if len({record.build_id for record in records}) != len(records):
        raise ValueError("duplicate Gold build_id in catalog")
    if sum(record.current for record in records) > 1:
        raise ValueError("Gold catalog contains multiple current rows")
    rows = [
        {
            "dataset_id": record.dataset_id,
            "build_id": record.build_id,
            "status": record.status.value,
            "current": record.current,
            "started_at_utc": record.started_at_utc,
            "completed_at_utc": record.completed_at_utc,
            "schema_version": record.schema_version,
            "feature_version": record.feature_version,
            "min_timestamp": record.min_timestamp,
            "max_timestamp": record.max_timestamp,
            "row_count": record.row_count,
            "data_path": record.data_path,
            "build_manifest_path": record.build_manifest_path,
            "plot_path": record.plot_path,
            "pruned_at_utc": record.pruned_at_utc,
        }
        for record in records
    ]
    return pl.DataFrame(rows, schema=GOLD_CATALOG_SCHEMA).sort(["started_at_utc", "build_id"])


def _record(row: dict[str, object]) -> GoldCatalogRecord:
    def _dt(name: str) -> datetime | None:
        value = row[name]
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"catalog {name} must be datetime")
        return value

    def _int(name: str) -> int:
        value = row[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"catalog {name} must be int")
        return value

    row_count_value = row["row_count"]
    row_count = None if row_count_value is None else _int("row_count")
    return GoldCatalogRecord(
        dataset_id=str(row["dataset_id"]),
        build_id=str(row["build_id"]),
        status=GoldBuildStatus(str(row["status"])),
        current=bool(row["current"]),
        started_at_utc=_dt("started_at_utc") or _raise_missing_started(),
        completed_at_utc=_dt("completed_at_utc"),
        schema_version=_int("schema_version"),
        feature_version=_int("feature_version"),
        min_timestamp=_dt("min_timestamp"),
        max_timestamp=_dt("max_timestamp"),
        row_count=row_count,
        data_path=None if row["data_path"] is None else str(row["data_path"]),
        build_manifest_path=None
        if row["build_manifest_path"] is None
        else str(row["build_manifest_path"]),
        plot_path=None if row["plot_path"] is None else str(row["plot_path"]),
        pruned_at_utc=_dt("pruned_at_utc"),
    )


def _raise_missing_started() -> datetime:
    raise ValueError("catalog started_at_utc cannot be null")


class GoldCatalogRepository:
    """Repository owning the sole authoritative manifest.parquet state."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> list[GoldCatalogRecord]:
        logger.debug("gold catalog read started")
        try:
            if not self._path.is_file():
                records: list[GoldCatalogRecord] = []
            else:
                frame = pl.read_parquet(self._path)
                if frame.schema != GOLD_CATALOG_SCHEMA:
                    raise ValueError("invalid Gold catalog schema")
                records = [_record(row) for row in frame.iter_rows(named=True)]
                _frame(records)
        except Exception as exc:
            logger.debug("gold catalog read failed error_type=%s", type(exc).__name__)
            raise
        logger.debug("gold catalog read completed record_count=%d", len(records))
        return records

    def replace(self, records: list[GoldCatalogRecord]) -> None:
        logger.debug("gold catalog replace started record_count=%d", len(records))
        try:
            atomic_write_parquet(_frame(records), self._path)
        except Exception as exc:
            logger.debug("gold catalog replace failed error_type=%s", type(exc).__name__)
            raise
        logger.debug("gold catalog replace completed record_count=%d", len(records))

    def append(self, record: GoldCatalogRecord) -> None:
        self.replace([*self.read(), record])

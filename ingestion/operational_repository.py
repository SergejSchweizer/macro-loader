"""Parquet repositories for inventory snapshots and ingestion-run audit records."""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from application.contracts import Provider
from application.operational_records import IngestionRunRecord, InventoryRecord, RunMode, RunStatus
from ingestion.parquet_repository import atomic_write_parquet

INVENTORY_SCHEMA = pl.Schema(
    {
        "series_id": pl.String(),
        "provider": pl.String(),
        "min_observation_date": pl.Date(),
        "max_observation_date": pl.Date(),
        "row_count": pl.Int64(),
        "duplicate_key_count": pl.Int64(),
        "file_count": pl.Int64(),
    }
)

RUN_SCHEMA = pl.Schema(
    {
        "run_id": pl.String(),
        "provider": pl.String(),
        "series_id": pl.String(),
        "mode": pl.String(),
        "requested_start": pl.Date(),
        "requested_end": pl.Date(),
        "fetched_rows": pl.Int64(),
        "accepted_rows": pl.Int64(),
        "inserted_rows": pl.Int64(),
        "revised_rows": pl.Int64(),
        "written_partitions": pl.Int64(),
        "status": pl.String(),
        "started_at_utc": pl.Datetime("us", "UTC"),
        "completed_at_utc": pl.Datetime("us", "UTC"),
        "error_category": pl.String(),
        "error_message": pl.String(),
    }
)
logger = logging.getLogger(__name__)


def _sanitize(text: str | None, secrets: tuple[str, ...]) -> str | None:
    if text is None:
        return None
    sanitized = text
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "***")
    return sanitized


def _inventory_frame(records: list[InventoryRecord]) -> pl.DataFrame:
    if not records:
        return pl.DataFrame(schema=INVENTORY_SCHEMA)
    rows = [
        {
            "series_id": item.series_id,
            "provider": item.provider.value,
            "min_observation_date": item.min_observation_date,
            "max_observation_date": item.max_observation_date,
            "row_count": item.row_count,
            "duplicate_key_count": item.duplicate_key_count,
            "file_count": item.file_count,
        }
        for item in records
    ]
    frame = pl.DataFrame(rows, schema=INVENTORY_SCHEMA)
    duplicates = frame.group_by("series_id").len().filter(pl.col("len") > 1)
    if duplicates.height:
        raise ValueError("duplicate inventory series_id")
    return frame.sort("series_id")


def write_inventory(path: Path, records: list[InventoryRecord]) -> None:
    """Atomically replace the authoritative inventory snapshot."""
    logger.debug("inventory snapshot write started record_count=%d", len(records))
    try:
        atomic_write_parquet(_inventory_frame(records), path)
    except Exception as exc:
        logger.debug("inventory snapshot write failed error_type=%s", type(exc).__name__)
        raise
    logger.debug("inventory snapshot write completed record_count=%d", len(records))


def read_inventory(path: Path) -> list[InventoryRecord]:
    """Read exact authoritative inventory records."""
    logger.debug("inventory snapshot read started")
    if not path.is_file():
        logger.debug("inventory snapshot read completed record_count=0")
        return []
    try:
        frame = pl.read_parquet(path)
    except Exception as exc:
        logger.debug("inventory snapshot read failed error_type=%s", type(exc).__name__)
        raise
    if frame.schema != INVENTORY_SCHEMA:
        logger.debug("inventory snapshot read failed error_type=ValueError")
        raise ValueError("invalid inventory schema")
    records = [
        InventoryRecord(
            series_id=row["series_id"],
            provider=Provider(row["provider"]),
            min_observation_date=row["min_observation_date"],
            max_observation_date=row["max_observation_date"],
            row_count=row["row_count"],
            duplicate_key_count=row["duplicate_key_count"],
            file_count=row["file_count"],
        )
        for row in frame.iter_rows(named=True)
    ]
    logger.debug("inventory snapshot read completed record_count=%d", len(records))
    return records


def _run_frame(records: list[IngestionRunRecord], *, secrets: tuple[str, ...]) -> pl.DataFrame:
    if not records:
        return pl.DataFrame(schema=RUN_SCHEMA)
    rows = [
        {
            "run_id": item.run_id,
            "provider": item.provider.value,
            "series_id": item.series_id,
            "mode": item.mode.value,
            "requested_start": item.requested_start,
            "requested_end": item.requested_end,
            "fetched_rows": item.fetched_rows,
            "accepted_rows": item.accepted_rows,
            "inserted_rows": item.inserted_rows,
            "revised_rows": item.revised_rows,
            "written_partitions": item.written_partitions,
            "status": item.status.value,
            "started_at_utc": item.started_at_utc,
            "completed_at_utc": item.completed_at_utc,
            "error_category": _sanitize(item.error_category, secrets),
            "error_message": _sanitize(item.error_message, secrets),
        }
        for item in records
    ]
    frame = pl.DataFrame(rows, schema=RUN_SCHEMA)
    duplicates = frame.group_by("run_id").len().filter(pl.col("len") > 1)
    if duplicates.height:
        raise ValueError("duplicate ingestion run_id")
    return frame.sort(["started_at_utc", "run_id"])


def write_runs(
    path: Path,
    records: list[IngestionRunRecord],
    *,
    secrets: tuple[str, ...] = (),
) -> None:
    """Atomically replace a validated run-record snapshot."""
    logger.debug("ingestion run snapshot write started record_count=%d", len(records))
    try:
        atomic_write_parquet(_run_frame(records, secrets=secrets), path)
    except Exception as exc:
        logger.debug("ingestion run snapshot write failed error_type=%s", type(exc).__name__)
        raise
    logger.debug("ingestion run snapshot write completed record_count=%d", len(records))


def read_runs(path: Path) -> list[IngestionRunRecord]:
    """Read exact typed run records."""
    logger.debug("ingestion run snapshot read started")
    if not path.is_file():
        logger.debug("ingestion run snapshot read completed record_count=0")
        return []
    try:
        frame = pl.read_parquet(path)
    except Exception as exc:
        logger.debug("ingestion run snapshot read failed error_type=%s", type(exc).__name__)
        raise
    if frame.schema != RUN_SCHEMA:
        logger.debug("ingestion run snapshot read failed error_type=ValueError")
        raise ValueError("invalid ingestion-run schema")
    records = [
        IngestionRunRecord(
            run_id=row["run_id"],
            provider=Provider(row["provider"]),
            series_id=row["series_id"],
            mode=RunMode(row["mode"]),
            requested_start=row["requested_start"],
            requested_end=row["requested_end"],
            fetched_rows=row["fetched_rows"],
            accepted_rows=row["accepted_rows"],
            inserted_rows=row["inserted_rows"],
            revised_rows=row["revised_rows"],
            written_partitions=row["written_partitions"],
            status=RunStatus(row["status"]),
            started_at_utc=row["started_at_utc"],
            completed_at_utc=row["completed_at_utc"],
            error_category=row["error_category"],
            error_message=row["error_message"],
        )
        for row in frame.iter_rows(named=True)
    ]
    logger.debug("ingestion run snapshot read completed record_count=%d", len(records))
    return records


def upsert_run(
    path: Path,
    record: IngestionRunRecord,
    *,
    secrets: tuple[str, ...] = (),
) -> None:
    """Insert or replace one unique run ID deterministically."""
    logger.debug(
        "ingestion run upsert started run_id=%s provider=%s series_id=%s",
        record.run_id,
        record.provider.value,
        record.series_id,
    )
    existing = read_runs(path)
    retained = [item for item in existing if item.run_id != record.run_id]
    write_runs(path, [*retained, record], secrets=secrets)
    logger.debug("ingestion run upsert completed run_id=%s", record.run_id)

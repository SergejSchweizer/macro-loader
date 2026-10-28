"""Parquet adapter for typed ingestion state."""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from application.contracts import Provider
from application.planner import OperationMode
from application.state import IngestionState
from ingestion.parquet_repository import atomic_write_parquet

STATE_SCHEMA = pl.Schema(
    {
        "provider": pl.String(),
        "series_id": pl.String(),
        "last_success_utc": pl.Datetime("us", "UTC"),
        "last_observed_date": pl.Date(),
        "last_requested_start": pl.Date(),
        "last_requested_end": pl.Date(),
        "mode": pl.String(),
        "fetched_row_count": pl.Int64(),
        "accepted_row_count": pl.Int64(),
        "changed_row_count": pl.Int64(),
        "last_reconcile_utc": pl.Datetime("us", "UTC"),
    }
)
logger = logging.getLogger(__name__)


def _row(state: IngestionState) -> dict[str, object]:
    return {
        "provider": state.provider.value,
        "series_id": state.series_id,
        "last_success_utc": state.last_success_utc,
        "last_observed_date": state.last_observed_date,
        "last_requested_start": state.last_requested_start,
        "last_requested_end": state.last_requested_end,
        "mode": None if state.mode is None else state.mode.value,
        "fetched_row_count": state.fetched_row_count,
        "accepted_row_count": state.accepted_row_count,
        "changed_row_count": state.changed_row_count,
        "last_reconcile_utc": state.last_reconcile_utc,
    }


def _frame(states: list[IngestionState]) -> pl.DataFrame:
    if not states:
        return pl.DataFrame(schema=STATE_SCHEMA)
    frame = pl.DataFrame([_row(state) for state in states], schema=STATE_SCHEMA)
    duplicates = frame.group_by(["provider", "series_id"]).len().filter(pl.col("len") > 1)
    if duplicates.height:
        raise ValueError("duplicate ingestion-state key")
    return frame.sort(["provider", "series_id"])


def write_states(path: Path, states: list[IngestionState]) -> None:
    """Atomically replace the authoritative state snapshot."""
    logger.debug("ingestion state write started record_count=%d", len(states))
    try:
        atomic_write_parquet(_frame(states), path)
    except Exception as exc:
        logger.debug("ingestion state write failed error_type=%s", type(exc).__name__)
        raise
    logger.debug("ingestion state write completed record_count=%d", len(states))


def read_states(path: Path) -> list[IngestionState]:
    """Read and validate the typed state snapshot."""
    logger.debug("ingestion state read started")
    if not path.is_file():
        logger.debug("ingestion state read completed record_count=0")
        return []
    try:
        frame = pl.read_parquet(path)
        if frame.schema != STATE_SCHEMA:
            raise ValueError("invalid ingestion-state schema")
        result: list[IngestionState] = []
        for row in frame.iter_rows(named=True):
            mode_raw = row["mode"]
            result.append(
                IngestionState(
                    provider=Provider(row["provider"]),
                    series_id=row["series_id"],
                    last_success_utc=row["last_success_utc"],
                    last_observed_date=row["last_observed_date"],
                    last_requested_start=row["last_requested_start"],
                    last_requested_end=row["last_requested_end"],
                    mode=None if mode_raw is None else OperationMode(mode_raw),
                    fetched_row_count=row["fetched_row_count"],
                    accepted_row_count=row["accepted_row_count"],
                    changed_row_count=row["changed_row_count"],
                    last_reconcile_utc=row["last_reconcile_utc"],
                )
            )
    except Exception as exc:
        logger.debug("ingestion state read failed error_type=%s", type(exc).__name__)
        raise
    logger.debug("ingestion state read completed record_count=%d", len(result))
    return result


def upsert_state(path: Path, state: IngestionState) -> None:
    """Replace exactly one provider/series state key."""
    logger.debug(
        "ingestion state upsert started provider=%s series_id=%s",
        state.provider.value,
        state.series_id,
    )
    existing = read_states(path)
    retained = [
        item
        for item in existing
        if (item.provider, item.series_id) != (state.provider, state.series_id)
    ]
    write_states(path, [*retained, state])
    logger.debug(
        "ingestion state upsert completed provider=%s series_id=%s",
        state.provider.value,
        state.series_id,
    )

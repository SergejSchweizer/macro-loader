"""Deterministic complete-state Gold row hashing and PostgreSQL delta planning."""

from __future__ import annotations

import hashlib
import logging
import math
import struct
from datetime import UTC, datetime

import polars as pl

from application.postgres_sync import (
    POSTGRES_RAW_COLUMNS,
    GoldDeltaPlan,
    GoldRowDigest,
    GoldRowPayload,
    GoldSyncState,
)

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_DIGEST_PREFIX = b"macro-loader:gold-row:v1\x00"


def _epoch_microseconds(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("Gold timestamp_m1 must be timezone-aware")
    delta = value.astimezone(UTC) - _EPOCH
    return delta.days * 86_400 * 1_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _canonical_float(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("Gold row digest rejects NaN and infinity")
    return 0.0 if value == 0.0 else value


def gold_row_sha256(row: GoldRowPayload) -> str:
    """Hash one row with stable type/null framing and exact canonical column order."""
    digest = hashlib.sha256()
    digest.update(_DIGEST_PREFIX)
    digest.update(POSTGRES_RAW_COLUMNS[0].encode("utf-8"))
    digest.update(b"\x00T")
    digest.update(struct.pack(">q", _epoch_microseconds(row.timestamp_m1)))
    for column, value in zip(
        POSTGRES_RAW_COLUMNS[1:],
        row.values,
        strict=True,
    ):
        digest.update(column.encode("utf-8"))
        digest.update(b"\x00")
        if value is None:
            digest.update(b"N")
        else:
            digest.update(b"F")
            digest.update(struct.pack(">d", _canonical_float(value)))
    return digest.hexdigest()


def _validate_source_frame(frame: pl.DataFrame) -> None:
    if frame.columns != list(POSTGRES_RAW_COLUMNS):
        raise ValueError("PostgreSQL delta source must use exact raw source column order")
    if frame.schema["timestamp_m1"] != pl.Datetime("us", "UTC"):
        raise TypeError("PostgreSQL delta source timestamp_m1 must be Datetime(us, UTC)")
    timestamp = frame.get_column("timestamp_m1")
    if timestamp.null_count():
        raise ValueError("PostgreSQL delta source timestamp_m1 cannot be null")
    if bool(timestamp.is_duplicated().any()):
        raise ValueError("PostgreSQL delta source timestamp_m1 must be unique")
    for column in POSTGRES_RAW_COLUMNS[1:]:
        if frame.schema[column] != pl.Float64:
            raise TypeError(f"PostgreSQL delta source feature {column} must be Float64")


def _source_rows_and_digests(
    frame: pl.DataFrame,
) -> tuple[tuple[GoldRowPayload, ...], tuple[GoldRowDigest, ...]]:
    """Convert the complete current Gold frame to deterministic rows and digests."""
    _validate_source_frame(frame)
    rows: list[GoldRowPayload] = []
    digests: list[GoldRowDigest] = []
    for raw in frame.sort("timestamp_m1").iter_rows(named=False):
        timestamp = raw[0]
        if not isinstance(timestamp, datetime):
            raise TypeError("Gold timestamp_m1 row value must be datetime")
        values: list[float | None] = []
        for value in raw[1:]:
            if value is None:
                values.append(None)
                continue
            if not isinstance(value, float):
                raise TypeError("Gold feature row values must be float or null")
            values.append(_canonical_float(value))
        payload = GoldRowPayload(timestamp, tuple(values))
        rows.append(payload)
        digests.append(GoldRowDigest(payload.timestamp_m1, gold_row_sha256(payload)))
    return tuple(rows), tuple(digests)


def source_rows_and_digests(
    frame: pl.DataFrame,
) -> tuple[tuple[GoldRowPayload, ...], tuple[GoldRowDigest, ...]]:
    """Convert a Gold frame to complete-state rows and log only safe counts."""
    logger.debug(
        "postgres delta source digest preparation started row_count=%d column_count=%d",
        frame.height,
        frame.width,
    )
    try:
        rows, digests = _source_rows_and_digests(frame)
    except Exception as exc:
        logger.debug(
            "postgres delta source digest preparation failed error_type=%s", type(exc).__name__
        )
        raise
    logger.debug(
        "postgres delta source digest preparation completed row_count=%d digest_count=%d",
        len(rows),
        len(digests),
    )
    return rows, digests


def _target_digest_map(target_digests: tuple[GoldRowDigest, ...]) -> dict[datetime, str]:
    result: dict[datetime, str] = {}
    for digest in target_digests:
        if digest.timestamp_m1 in result:
            raise ValueError("PostgreSQL digest state contains duplicate timestamp_m1")
        result[digest.timestamp_m1] = digest.row_sha256
    return result


def _plan_gold_delta(
    frame: pl.DataFrame,
    target_digests: tuple[GoldRowDigest, ...],
    sync_state: GoldSyncState | None,
) -> GoldDeltaPlan:
    """Compare complete source and target digest state; never use a time watermark."""
    rows, source_digests = source_rows_and_digests(frame)
    target_by_timestamp = _target_digest_map(target_digests)
    if sync_state is None and target_by_timestamp:
        raise ValueError("PostgreSQL target has row digests but no authoritative sync state")

    source_hashes = {digest.timestamp_m1: digest.row_sha256 for digest in source_digests}
    row_by_timestamp = {row.timestamp_m1: row for row in rows}
    inserts: list[GoldRowPayload] = []
    updates: list[GoldRowPayload] = []
    unchanged: list[datetime] = []

    for timestamp in sorted(source_hashes):
        target_hash = target_by_timestamp.get(timestamp)
        if target_hash is None:
            inserts.append(row_by_timestamp[timestamp])
        elif target_hash != source_hashes[timestamp]:
            updates.append(row_by_timestamp[timestamp])
        else:
            unchanged.append(timestamp)

    deletes = tuple(sorted(set(target_by_timestamp) - set(source_hashes)))
    return GoldDeltaPlan(
        inserts=tuple(inserts),
        updates=tuple(updates),
        deletes=deletes,
        unchanged=tuple(unchanged),
        source_digests=source_digests,
    )


def plan_gold_delta(
    frame: pl.DataFrame,
    target_digests: tuple[GoldRowDigest, ...],
    sync_state: GoldSyncState | None,
) -> GoldDeltaPlan:
    """Build the complete-state delta with count-only lifecycle logging."""
    logger.debug(
        "postgres delta planning started source_row_count=%d target_digest_count=%d "
        "sync_state_present=%s",
        frame.height,
        len(target_digests),
        sync_state is not None,
    )
    try:
        plan = _plan_gold_delta(frame, target_digests, sync_state)
    except Exception as exc:
        logger.debug("postgres delta planning failed error_type=%s", type(exc).__name__)
        raise
    logger.debug(
        "postgres delta planning completed inserted=%d updated=%d deleted=%d unchanged=%d",
        len(plan.inserts),
        len(plan.updates),
        len(plan.deletes),
        len(plan.unchanged),
    )
    return plan

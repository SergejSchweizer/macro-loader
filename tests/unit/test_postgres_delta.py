from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from application.postgres_delta import (
    gold_row_sha256,
    plan_gold_delta,
    source_rows_and_digests,
)
from application.postgres_sync import (
    POSTGRES_DATASET_ID,
    POSTGRES_RAW_COLUMNS,
    GoldRowDigest,
    GoldRowPayload,
    GoldSyncState,
)


def _ts(day: int, *, micros: int = 0) -> datetime:
    return datetime(2026, 8, day, tzinfo=UTC) + timedelta(microseconds=micros)


def _frame(days: tuple[int, ...], *, offset: float = 0.0) -> pl.DataFrame:
    data: dict[str, list[object]] = {"timestamp_m1": [_ts(day) for day in days]}
    for index, column in enumerate(POSTGRES_RAW_COLUMNS[1:], start=1):
        data[column] = [float(day + index) + offset for day in days]
    return pl.DataFrame(data).with_columns(pl.col("timestamp_m1").cast(pl.Datetime("us", "UTC")))


def _state(frame: pl.DataFrame) -> GoldSyncState:
    timestamps = frame.get_column("timestamp_m1")
    return GoldSyncState(
        dataset_id=POSTGRES_DATASET_ID,
        source_build_id="20260822T100000Z",
        data_sha256="a" * 64,
        schema_version=1,
        feature_version=1,
        row_count=frame.height,
        min_timestamp=timestamps.min(),
        max_timestamp=timestamps.max(),
        synced_at_utc=_ts(22),
    )


def test_row_digest_is_deterministic_and_value_sensitive() -> None:
    values = tuple(float(index) for index in range(len(POSTGRES_RAW_COLUMNS) - 1))
    row = GoldRowPayload(_ts(1), values)
    assert gold_row_sha256(row) == gold_row_sha256(row)
    changed = GoldRowPayload(_ts(1), (*values[:-1], values[-1] + 1.0))
    assert gold_row_sha256(row) != gold_row_sha256(changed)
    later = GoldRowPayload(_ts(1, micros=1), values)
    assert gold_row_sha256(row) != gold_row_sha256(later)


def test_row_digest_has_explicit_null_and_normalizes_negative_zero() -> None:
    values = tuple(0.0 for _ in POSTGRES_RAW_COLUMNS[1:])
    negative = GoldRowPayload(_ts(1), (-0.0, *values[1:]))
    positive = GoldRowPayload(_ts(1), values)
    null = GoldRowPayload(_ts(1), (None, *values[1:]))
    assert gold_row_sha256(negative) == gold_row_sha256(positive)
    assert gold_row_sha256(null) != gold_row_sha256(positive)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_row_digest_rejects_non_finite_features(invalid: float) -> None:
    values = [0.0 for _ in POSTGRES_RAW_COLUMNS[1:]]
    values[0] = invalid
    with pytest.raises(ValueError, match="NaN and infinity"):
        gold_row_sha256(GoldRowPayload(_ts(1), tuple(values)))


def test_complete_empty_target_is_full_bootstrap() -> None:
    frame = _frame((1, 2, 3))
    plan = plan_gold_delta(frame, (), None)
    assert [row.timestamp_m1 for row in plan.inserts] == [_ts(1), _ts(2), _ts(3)]
    assert (plan.updates, plan.deletes, plan.unchanged) == ((), (), ())
    assert len(plan.source_digests) == 3


def test_ambiguous_bootstrap_is_rejected() -> None:
    frame = _frame((1,))
    _, digests = source_rows_and_digests(frame)
    with pytest.raises(ValueError, match="no authoritative sync state"):
        plan_gold_delta(frame, digests, None)


def test_complete_state_plan_classifies_insert_update_delete_and_unchanged() -> None:
    frame = _frame((1, 2, 3))
    _, source = source_rows_and_digests(frame)
    target = (
        source[0],
        GoldRowDigest(_ts(2), "f" * 64),
        GoldRowDigest(_ts(4), "e" * 64),
    )
    plan = plan_gold_delta(frame, target, _state(frame))
    assert [row.timestamp_m1 for row in plan.inserts] == [_ts(3)]
    assert [row.timestamp_m1 for row in plan.updates] == [_ts(2)]
    assert plan.deletes == (_ts(4),)
    assert plan.unchanged == (_ts(1),)


def test_noop_plan_has_no_mutations() -> None:
    frame = _frame((1, 2, 3))
    _, digests = source_rows_and_digests(frame)
    plan = plan_gold_delta(frame, digests, _state(frame))
    assert (plan.inserts, plan.updates, plan.deletes) == ((), (), ())
    assert plan.unchanged == (_ts(1), _ts(2), _ts(3))


def test_missed_runs_and_historical_revision_are_not_hidden_by_time() -> None:
    old = _frame((1, 2))
    _, old_digests = source_rows_and_digests(old)
    current = _frame((1, 2, 8, 15, 22))
    current = current.with_columns(
        pl.when(pl.col("timestamp_m1") == _ts(1))
        .then(pl.col(POSTGRES_RAW_COLUMNS[1]) + 100.0)
        .otherwise(pl.col(POSTGRES_RAW_COLUMNS[1]))
        .alias(POSTGRES_RAW_COLUMNS[1])
    )
    plan = plan_gold_delta(current, old_digests, _state(old))
    assert [row.timestamp_m1 for row in plan.updates] == [_ts(1)]
    assert [row.timestamp_m1 for row in plan.inserts] == [_ts(8), _ts(15), _ts(22)]
    assert plan.unchanged == (_ts(2),)


def test_duplicate_target_digest_and_invalid_source_schema_fail() -> None:
    frame = _frame((1,))
    _, digests = source_rows_and_digests(frame)
    with pytest.raises(ValueError, match="duplicate"):
        plan_gold_delta(frame, (digests[0], digests[0]), _state(frame))
    with pytest.raises(ValueError, match="column order"):
        source_rows_and_digests(frame.select(list(reversed(POSTGRES_RAW_COLUMNS))))


def test_non_finite_source_frame_fails_before_plan() -> None:
    frame = _frame((1,)).with_columns(pl.lit(float("nan")).alias(POSTGRES_RAW_COLUMNS[1]))
    with pytest.raises(ValueError, match="NaN and infinity"):
        source_rows_and_digests(frame)

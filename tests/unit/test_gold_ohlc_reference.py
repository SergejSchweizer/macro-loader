"""Independent reference checks for native OHLC propagation into canonical Gold."""

from __future__ import annotations

import hashlib
import struct
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from application.contracts import NativeShape
from application.gold_frame import GOLD_SCHEMA_VERSION, assemble_gold_frame
from application.postgres_delta import gold_row_sha256, plan_gold_delta
from application.postgres_sync import GoldRowDigest, GoldRowPayload, GoldSyncState
from application.registry import SERIES_REGISTRY
from application.silver import SILVER_SCHEMA

_OHLC_FIELDS = ("open", "high", "low", "close")
_OHLC_SERIES = tuple(
    series_id
    for series_id, contract in SERIES_REGISTRY.items()
    if contract.native_shape is NativeShape.OHLC
)
_SCALAR_SERIES = tuple(
    series_id
    for series_id, contract in SERIES_REGISTRY.items()
    if contract.native_shape is NativeShape.SCALAR
)
_START = date(2026, 1, 5)


def _silver(series_id: str, observations: tuple[tuple[int, float], ...]) -> pl.DataFrame:
    contract = SERIES_REGISTRY[series_id]
    dates = [_START + timedelta(days=offset) for offset, _ in observations]
    levels = [value for _, value in observations]
    if contract.native_shape is NativeShape.OHLC:
        opens = [level - 0.25 for level in levels]
        highs = [level + 1.5 for level in levels]
        lows = [level - 2.0 for level in levels]
        closes = levels
    else:
        opens = highs = lows = closes = [None] * len(levels)
    return pl.DataFrame(
        {
            "observation_date": dates,
            "series_id": [series_id] * len(levels),
            "value": levels,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "unit": [contract.unit] * len(levels),
            "provider": [contract.provider.value] * len(levels),
            "source_id": [contract.source_id] * len(levels),
            "fetched_at_utc": [datetime(2026, 10, 26, tzinfo=UTC)] * len(levels),
        },
        schema=SILVER_SCHEMA,
    )


def _fixture() -> dict[str, pl.DataFrame]:
    return {
        series_id: _silver(series_id, ((0, 10.0 + index), (2, 20.0 + index)))
        for index, series_id in enumerate(SERIES_REGISTRY)
    }


def test_registry_ohlc_values_reach_gold_without_new_dates_or_scalar_fields() -> None:
    silver = _fixture()
    build = assemble_gold_frame(silver)
    assert build.schema_version == GOLD_SCHEMA_VERSION == 9
    expected_dates = sorted(
        {
            observation_date
            for frame in silver.values()
            for observation_date in frame.get_column("observation_date").to_list()
        }
    )

    assert len(_OHLC_SERIES) == 6
    assert set(_OHLC_SERIES) == {"vix", "vix9d", "vix3m", "vix6m", "vix1y", "move"}
    assert build.frame.columns == _expected_raw_columns()
    assert build.frame.get_column("timestamp_m1").to_list() == [
        datetime.combine(day, datetime.min.time(), tzinfo=UTC) for day in expected_dates
    ]
    assert build.frame.height == 2

    for series_id in _OHLC_SERIES:
        series_index = list(SERIES_REGISTRY).index(series_id)
        expected_levels = (10.0 + series_index, 20.0 + series_index)
        assert (
            silver[series_id].get_column("value").to_list()
            == silver[series_id].get_column("close").to_list()
        )
        assert build.frame.get_column(f"{series_id}_level").to_list() == list(expected_levels)
        for field, expected_delta in (
            ("open", -0.25),
            ("high", 1.5),
            ("low", -2.0),
            ("close", 0.0),
        ):
            column = f"{series_id}_{field}"
            assert column in build.frame.columns
            actual = build.frame.get_column(column).to_list()
            assert actual == pytest.approx([value + expected_delta for value in expected_levels])
        assert (
            build.frame.get_column(f"{series_id}_close").to_list()
            == build.frame.get_column(f"{series_id}_level").to_list()
        )

    for series_id in _SCALAR_SERIES:
        assert not any(f"{series_id}_{field}" in build.frame.columns for field in _OHLC_FIELDS)
        assert not any(f"{series_id}_{field}" in _expected_raw_columns() for field in _OHLC_FIELDS)


def test_ohlc_only_revision_changes_the_canonical_sync_digest_and_delta() -> None:
    timestamp = datetime(2026, 1, 5, tzinfo=UTC)
    raw_columns = _expected_raw_columns()
    before_values = {column: 1.0 for column in raw_columns[1:]}
    before_values["vix_level"] = 20.0
    before_values["vix_open"] = 19.5
    after_values = {**before_values, "vix_open": 19.75}
    revised = pl.DataFrame(
        {"timestamp_m1": [timestamp], **{key: [value] for key, value in after_values.items()}},
        schema={
            "timestamp_m1": pl.Datetime("us", "UTC"),
            **dict.fromkeys(raw_columns[1:], pl.Float64),
        },
    ).select(raw_columns)
    prior_payload = GoldRowPayload(
        timestamp, tuple(before_values[column] for column in raw_columns[1:])
    )
    prior_digest = GoldRowDigest(timestamp, gold_row_sha256(prior_payload))
    next_payload = GoldRowPayload(
        timestamp, tuple(after_values[column] for column in raw_columns[1:])
    )
    next_digest = GoldRowDigest(timestamp, gold_row_sha256(next_payload))

    assert prior_digest.row_sha256 == _reference_digest(timestamp, before_values, raw_columns)
    assert next_digest.row_sha256 == _reference_digest(timestamp, after_values, raw_columns)
    assert prior_digest.row_sha256 != next_digest.row_sha256
    state = GoldSyncState(
        dataset_id="macro_features_daily",
        source_build_id="prior-build",
        data_sha256="a" * 64,
        schema_version=8,
        feature_version=6,
        row_count=1,
        min_timestamp=timestamp,
        max_timestamp=timestamp,
        synced_at_utc=datetime(2026, 1, 6, tzinfo=UTC),
    )
    plan = plan_gold_delta(revised, (prior_digest,), state)
    assert len(plan.updates) == 1
    assert plan.updates[0].timestamp_m1 == timestamp


def _expected_raw_columns() -> list[str]:
    columns = ["timestamp_m1", *(f"{series_id}_level" for series_id in SERIES_REGISTRY)]
    columns.extend(f"{series_id}_{field}" for series_id in _OHLC_SERIES for field in _OHLC_FIELDS)
    return columns


def _reference_digest(
    timestamp: datetime,
    values: dict[str, float | None],
    columns: list[str],
) -> str:
    """Independent implementation of the documented typed, column-framed Gold hash."""
    digest = hashlib.sha256(b"macro-loader:gold-row:v1\x00")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = timestamp.astimezone(UTC) - epoch
    micros = elapsed.days * 86_400_000_000 + elapsed.seconds * 1_000_000 + elapsed.microseconds
    digest.update(b"timestamp_m1\x00T")
    digest.update(struct.pack(">q", micros))
    for column in columns[1:]:
        value = values[column]
        digest.update(column.encode("utf-8"))
        digest.update(b"\x00")
        if value is None:
            digest.update(b"N")
        else:
            digest.update(b"F")
            digest.update(struct.pack(">d", 0.0 if value == 0.0 else value))
    return digest.hexdigest()

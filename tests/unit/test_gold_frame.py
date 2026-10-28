from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from application.gold_frame import (
    GOLD_COLUMNS,
    GOLD_FEATURE_VERSION,
    GOLD_SCHEMA_VERSION,
    GOLD_SOURCE_SERIES,
    GoldSemanticVersions,
    assemble_gold_frame,
)
from application.registry import OHLC_FIELDS, OHLC_SERIES, SERIES_REGISTRY
from application.silver import SILVER_SCHEMA

START = date(2026, 1, 1)


def _silver(series_id: str, values: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "observation_date": [START + timedelta(days=index) for index in range(len(values))],
            "series_id": [series_id for _ in values],
            "value": values,
            "open": [None for _ in values],
            "high": [None for _ in values],
            "low": [None for _ in values],
            "close": [None for _ in values],
            "unit": ["fixture" for _ in values],
            "provider": ["fixture" for _ in values],
            "source_id": [series_id for _ in values],
            "fetched_at_utc": [datetime(2026, 8, 19, 2, tzinfo=UTC) for _ in values],
        },
        schema=SILVER_SCHEMA,
    )


def _all_silver(length: int = 3) -> dict[str, pl.DataFrame]:
    return {
        series_id: _silver(series_id, [float(index + 1) for index in range(length)])
        for series_id in GOLD_SOURCE_SERIES
    }


def test_raw_gold_has_only_source_levels_and_provenance() -> None:
    build = assemble_gold_frame(_all_silver())

    assert build.frame.columns == list(GOLD_COLUMNS)
    assert build.frame.columns == [
        "timestamp_m1",
        *(f"{series_id}_level" for series_id in GOLD_SOURCE_SERIES),
        *(f"{series_id}_{field}" for series_id in OHLC_SERIES for field in OHLC_FIELDS),
    ]
    assert build.frame.item(1, "vix_level") == pytest.approx(2.0)
    assert build.schema_version == GOLD_SCHEMA_VERSION == 11
    assert build.feature_version == GOLD_FEATURE_VERSION == 6
    assert [item.series_id for item in build.inputs] == list(GOLD_SOURCE_SERIES)
    assert all(len(item.sha256) == 64 for item in build.inputs)


def test_gold_frame_debug_logs_safe_lifecycle_counts(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="application.gold_frame"):
        build = assemble_gold_frame(_all_silver(2))

    assert "gold frame assembly started input_series_count=25" in caplog.text
    assert (
        f"gold frame assembly completed output_row_count={build.frame.height} "
        f"output_column_count={build.frame.width} schema_version=11 feature_version=6"
    ) in caplog.text
    assert all(signature.sha256 not in caplog.text for signature in build.inputs)
    assert "2026-01-01" not in caplog.text


def test_gold_frame_failure_logs_exception_type_only(caplog: pytest.LogCaptureFixture) -> None:
    silver = _all_silver(1)
    silver["vix"] = _silver("vix", [float("nan")])

    with (
        caplog.at_level(logging.DEBUG, logger="application.gold_frame"),
        pytest.raises(ValueError, match="value must be finite"),
    ):
        assemble_gold_frame(silver)

    assert "gold frame assembly failed error_type=ValueError" in caplog.text
    assert "value must be finite" not in caplog.text
    assert "nan" not in caplog.text.lower()


def test_gold_outer_joins_original_levels_without_calendar_fill() -> None:
    silver = _all_silver(3)
    silver["vix"] = _silver("vix", [1.0, 3.0]).filter(
        pl.col("observation_date") != START + timedelta(days=1)
    )

    build = assemble_gold_frame(silver)

    assert build.frame.height == 3
    assert build.frame.item(1, "vix_level") is None
    assert build.frame.item(1, "ciss_level") == pytest.approx(2.0)


def test_gold_preserves_registry_ohlc_and_excludes_scalar_columns() -> None:
    silver = _all_silver(2)
    expected_dates = [START, START + timedelta(days=1)]
    for index, series_id in enumerate(OHLC_SERIES, start=1):
        silver[series_id] = silver[series_id].with_columns(
            pl.Series("open", [index * 10.0 + 1, index * 10.0 + 2]),
            pl.Series("high", [index * 10.0 + 3, index * 10.0 + 4]),
            pl.Series("low", [index * 10.0 - 1, index * 10.0]),
            pl.Series("close", [index * 10.0 + 5, index * 10.0 + 6]),
            pl.Series("value", [index * 10.0 + 5, index * 10.0 + 6]),
        )

    build = assemble_gold_frame(silver)

    assert build.frame.height == len(expected_dates)
    assert build.frame.get_column("timestamp_m1").to_list() == [
        datetime.combine(day, datetime.min.time(), tzinfo=UTC) for day in expected_dates
    ]
    for series_id in OHLC_SERIES:
        registry_index = OHLC_SERIES.index(series_id) + 1
        expected_values = {
            "open": [registry_index * 10.0 + 1, registry_index * 10.0 + 2],
            "high": [registry_index * 10.0 + 3, registry_index * 10.0 + 4],
            "low": [registry_index * 10.0 - 1, registry_index * 10.0],
            "close": [registry_index * 10.0 + 5, registry_index * 10.0 + 6],
        }
        for field in OHLC_FIELDS:
            assert build.frame.schema[f"{series_id}_{field}"] == pl.Float64
            assert (
                build.frame.get_column(f"{series_id}_{field}").to_list() == expected_values[field]
            )
        close = build.frame.get_column(f"{series_id}_close")
        level = build.frame.get_column(f"{series_id}_level")
        assert close.to_list() == level.to_list()
        assert close.null_count() == 0
    scalar_series = tuple(
        series_id for series_id, contract in SERIES_REGISTRY.items() if series_id not in OHLC_SERIES
    )
    assert all(
        f"{series_id}_{field}" not in build.frame.columns
        for series_id in scalar_series
        for field in OHLC_FIELDS
    )


def test_gold_rejects_non_finite_source_levels() -> None:
    silver = _all_silver(1)
    silver["vix"] = _silver("vix", [float("nan")])
    with pytest.raises(ValueError, match="value must be finite"):
        assemble_gold_frame(silver)

    silver["vix"] = _silver("vix", [float("inf")])
    with pytest.raises(ValueError, match="value must be finite"):
        assemble_gold_frame(silver)


def test_gold_rejects_missing_or_invalid_silver_inputs() -> None:
    silver = _all_silver(1)
    del silver["move"]
    with pytest.raises(KeyError, match="move"):
        assemble_gold_frame(silver)

    silver = _all_silver(1)
    silver["vix"] = silver["vix"].with_columns(pl.lit("wrong").alias("series_id"))
    with pytest.raises(ValueError, match="identity mismatch"):
        assemble_gold_frame(silver)


def test_gold_rejects_invalid_silver_schema_values_and_dates() -> None:
    silver = _all_silver(1)
    silver["vix"] = silver["vix"].with_columns(pl.col("value").cast(pl.Int64))
    with pytest.raises(ValueError, match="Silver schema mismatch"):
        assemble_gold_frame(silver)

    silver = _all_silver(1)
    silver["vix"] = silver["vix"].with_columns(pl.lit(None).cast(pl.Float64).alias("value"))
    with pytest.raises(ValueError, match="value cannot be null"):
        assemble_gold_frame(silver)

    silver = _all_silver(2)
    duplicate_date = silver["vix"].get_column("observation_date")[0]
    silver["vix"] = silver["vix"].with_columns(
        pl.when(pl.col("observation_date") == START + timedelta(days=1))
        .then(pl.lit(duplicate_date))
        .otherwise(pl.col("observation_date"))
        .alias("observation_date")
    )
    with pytest.raises(ValueError, match="observation dates must be unique"):
        assemble_gold_frame(silver)


def test_gold_versions_are_fail_closed_and_provenance_is_deterministic() -> None:
    silver = _all_silver(2)
    first = assemble_gold_frame(silver)
    second = assemble_gold_frame(silver)

    assert first.inputs == second.inputs
    assert first.frame.equals(second.frame)
    with pytest.raises(ValueError, match="schema_version"):
        GoldSemanticVersions(schema_version=1)
    with pytest.raises(ValueError, match="feature_version"):
        GoldSemanticVersions(feature_version=1)

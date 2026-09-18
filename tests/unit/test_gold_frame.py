from __future__ import annotations

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
    ]
    assert build.frame.item(1, "vix_level") == pytest.approx(2.0)
    assert build.schema_version == GOLD_SCHEMA_VERSION == 7
    assert build.feature_version == GOLD_FEATURE_VERSION == 6
    assert [item.series_id for item in build.inputs] == list(GOLD_SOURCE_SERIES)
    assert all(len(item.sha256) == 64 for item in build.inputs)


def test_gold_outer_joins_original_levels_without_calendar_fill() -> None:
    silver = _all_silver(3)
    silver["vix"] = _silver("vix", [1.0, 3.0]).filter(
        pl.col("observation_date") != START + timedelta(days=1)
    )

    build = assemble_gold_frame(silver)

    assert build.frame.height == 3
    assert build.frame.item(1, "vix_level") is None
    assert build.frame.item(1, "ciss_level") == pytest.approx(2.0)


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

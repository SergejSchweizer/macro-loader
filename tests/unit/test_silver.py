from __future__ import annotations

import logging
from datetime import UTC, date, datetime

import polars as pl
import pytest

from application.registry import series_contract
from application.silver import SILVER_COLUMNS, SILVER_SCHEMA, canonicalize_silver

NOW = datetime(2026, 8, 19, 2, tzinfo=UTC)


def _ohlc() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "series_id": ["vix", "vix"],
            "provider": ["cboe", "cboe"],
            "observation_date": [date(2026, 8, 17), date(2026, 8, 19)],
            "fetched_at_utc": [NOW, NOW],
            "source_id": ["VIX_History.csv", "VIX_History.csv"],
            "source_url": ["https://source", "https://source"],
            "open": [10.0, 11.0],
            "high": [12.0, 13.0],
            "low": [9.0, 10.0],
            "close": [11.5, 12.5],
        },
        schema={
            "series_id": pl.String,
            "provider": pl.String,
            "observation_date": pl.Date,
            "fetched_at_utc": pl.Datetime("us", "UTC"),
            "source_id": pl.String,
            "source_url": pl.String,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
        },
    )


def _scalar() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "series_id": ["us_10y", "us_10y"],
            "provider": ["fred", "fred"],
            "observation_date": [date(2026, 8, 17), date(2026, 8, 19)],
            "fetched_at_utc": [NOW, NOW],
            "source_id": ["DGS10", "DGS10"],
            "source_url": ["https://source", "https://source"],
            "value": [4.1, 4.2],
        },
        schema={
            "series_id": pl.String,
            "provider": pl.String,
            "observation_date": pl.Date,
            "fetched_at_utc": pl.Datetime("us", "UTC"),
            "source_id": pl.String,
            "source_url": pl.String,
            "value": pl.Float64,
        },
    )


def test_ohlc_maps_close_to_value_and_preserves_ohlc_exact_schema() -> None:
    contract = series_contract("vix")
    result = canonicalize_silver(contract, _ohlc())
    assert tuple(result.columns) == SILVER_COLUMNS
    assert result.schema == SILVER_SCHEMA
    assert result.get_column("value").to_list() == [11.5, 12.5]
    assert result.get_column("close").to_list() == [11.5, 12.5]
    assert result.get_column("unit").unique().item() == contract.unit
    assert contract.unit == "index_points"
    assert result.get_column("observation_date").to_list() == [
        date(2026, 8, 17),
        date(2026, 8, 19),
    ]


def test_silver_debug_logs_counts_without_values_or_urls(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="application.silver"):
        result = canonicalize_silver(series_contract("us_10y"), _scalar())

    assert result.height == 2
    assert "silver canonicalization started series=us_10y" in caplog.text
    assert "row_count=2" in caplog.text
    assert "4.1" not in caplog.text
    assert "https://source" not in caplog.text


def test_scalar_maps_value_and_nullable_float_ohlc() -> None:
    contract = series_contract("us_10y")
    result = canonicalize_silver(contract, _scalar())
    assert result.get_column("value").to_list() == [4.1, 4.2]
    assert result.get_column("unit").unique().item() == contract.unit
    assert contract.unit == "percent"
    for column in ("open", "high", "low", "close"):
        assert result.schema[column] == pl.Float64
        assert result.get_column(column).null_count() == result.height


def test_gaps_are_not_synthesized() -> None:
    result = canonicalize_silver(series_contract("vix"), _ohlc())
    assert result.get_column("observation_date").to_list() == [
        date(2026, 8, 17),
        date(2026, 8, 19),
    ]


def test_identity_duplicate_and_dtype_drift_are_rejected() -> None:
    wrong = _scalar().with_columns(pl.lit("DGS2").alias("source_id"))
    with pytest.raises(ValueError, match="source_id"):
        canonicalize_silver(series_contract("us_10y"), wrong)
    duplicate = pl.concat([_scalar().head(1), _scalar().head(1)])
    with pytest.raises(ValueError, match="duplicate"):
        canonicalize_silver(series_contract("us_10y"), duplicate)
    bad_date = _scalar().with_columns(pl.col("observation_date").cast(pl.String))
    with pytest.raises(TypeError, match="observation_date"):
        canonicalize_silver(series_contract("us_10y"), bad_date)
    naive = _scalar().with_columns(pl.col("fetched_at_utc").dt.replace_time_zone(None))
    with pytest.raises(TypeError, match="fetched_at_utc"):
        canonicalize_silver(series_contract("us_10y"), naive)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_value_is_rejected(value: float) -> None:
    frame = _scalar().with_columns(pl.lit(value).alias("value"))
    with pytest.raises(ValueError, match="non-finite"):
        canonicalize_silver(series_contract("us_10y"), frame)


def test_empty_schema_less_bronze_returns_typed_empty_silver() -> None:
    result = canonicalize_silver(series_contract("us_10y"), pl.DataFrame())
    assert result.height == 0
    assert result.schema == SILVER_SCHEMA

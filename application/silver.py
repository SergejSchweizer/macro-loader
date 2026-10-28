"""Pure Bronze-to-Silver canonicalization policy."""

from __future__ import annotations

import logging

import polars as pl

from application.contracts import NativeShape, SeriesContract

logger = logging.getLogger(__name__)

SILVER_COLUMNS = (
    "observation_date",
    "series_id",
    "value",
    "open",
    "high",
    "low",
    "close",
    "unit",
    "provider",
    "source_id",
    "fetched_at_utc",
)
SILVER_SCHEMA = pl.Schema(
    {
        "observation_date": pl.Date(),
        "series_id": pl.String(),
        "value": pl.Float64(),
        "open": pl.Float64(),
        "high": pl.Float64(),
        "low": pl.Float64(),
        "close": pl.Float64(),
        "unit": pl.String(),
        "provider": pl.String(),
        "source_id": pl.String(),
        "fetched_at_utc": pl.Datetime("us", "UTC"),
    }
)


def _assert_identity(frame: pl.DataFrame, contract: SeriesContract) -> None:
    expected = {
        "series_id": contract.series_id,
        "provider": contract.provider.value,
        "source_id": contract.source_id,
    }
    for column, value in expected.items():
        if column not in frame.columns:
            raise ValueError(f"Bronze frame is missing {column}")
        values = frame.get_column(column).drop_nulls().unique().to_list()
        if values != [value]:
            raise ValueError(f"Bronze {column} does not match registry contract")


def _assert_common(frame: pl.DataFrame, contract: SeriesContract) -> None:
    required = {
        "series_id",
        "provider",
        "observation_date",
        "fetched_at_utc",
        "source_id",
        "source_url",
    }
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Bronze frame is missing common columns: {sorted(missing)}")
    if frame.schema["observation_date"] != pl.Date:
        raise TypeError("Bronze observation_date must be Date")
    if frame.schema["fetched_at_utc"] != pl.Datetime("us", "UTC"):
        raise TypeError("Bronze fetched_at_utc must be Datetime(us, UTC)")
    _assert_identity(frame, contract)
    if bool(frame.select(pl.col("observation_date").is_null().any()).item()):
        raise ValueError("Bronze observation_date cannot be null")
    if bool(frame.select(["series_id", "observation_date"]).is_duplicated().any()):
        raise ValueError("Bronze contains duplicate Silver natural keys")


def _finite_non_null(frame: pl.DataFrame, columns: tuple[str, ...]) -> None:
    for column in columns:
        if column not in frame.columns:
            raise ValueError(f"Bronze frame is missing {column}")
        values = pl.col(column).cast(pl.Float64, strict=False)
        if bool(frame.select(values.is_null().any()).item()):
            raise ValueError(f"Bronze {column} contains missing/non-numeric values")
        if not bool(frame.select(values.is_finite().all()).item()):
            raise ValueError(f"Bronze {column} contains non-finite values")


def canonicalize_silver(contract: SeriesContract, bronze: pl.DataFrame) -> pl.DataFrame:
    """Map one retained Bronze series to exact deterministic Silver schema."""
    logger.debug(
        "silver canonicalization started series=%s provider=%s row_count=%d native_shape=%s",
        contract.series_id,
        contract.provider.value,
        bronze.height,
        contract.native_shape.value,
    )
    if bronze.is_empty() and not bronze.columns:
        logger.debug("silver canonicalization completed series=%s row_count=0", contract.series_id)
        return pl.DataFrame(schema=SILVER_SCHEMA)
    _assert_common(bronze, contract)
    if contract.native_shape is NativeShape.OHLC:
        _finite_non_null(bronze, ("open", "high", "low", "close"))
        payload = bronze.select(
            "observation_date",
            "series_id",
            pl.col("close").cast(pl.Float64).alias("value"),
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.lit(contract.unit).alias("unit"),
            "provider",
            "source_id",
            "fetched_at_utc",
        )
    elif contract.native_shape is NativeShape.SCALAR:
        _finite_non_null(bronze, ("value",))
        payload = bronze.select(
            "observation_date",
            "series_id",
            pl.col("value").cast(pl.Float64),
            pl.lit(None, dtype=pl.Float64).alias("open"),
            pl.lit(None, dtype=pl.Float64).alias("high"),
            pl.lit(None, dtype=pl.Float64).alias("low"),
            pl.lit(None, dtype=pl.Float64).alias("close"),
            pl.lit(contract.unit).alias("unit"),
            "provider",
            "source_id",
            "fetched_at_utc",
        )
    else:
        raise ValueError(f"unsupported native shape: {contract.native_shape}")
    result = payload.sort("observation_date")
    if result.schema != SILVER_SCHEMA:
        raise TypeError(f"canonical Silver schema drift: {result.schema}")
    if not bool(result.get_column("value").is_finite().all()):
        raise ValueError("Silver value must be finite")
    logger.debug(
        "silver canonicalization completed series=%s row_count=%d",
        contract.series_id,
        result.height,
    )
    return result

"""Canonical contract for individual CME ZQ final settlements."""

from __future__ import annotations

import re

import polars as pl

ZQ_SETTLEMENT_COLUMNS: tuple[str, ...] = (
    "observation_date",
    "contract_month",
    "settlement_price",
    "contract_symbol",
    "settlement_type",
    "source_id",
    "source_url",
    "fetched_at_utc",
    "available_at_utc",
)
ZQ_SETTLEMENT_SCHEMA = {
    "observation_date": pl.Date,
    "contract_month": pl.Date,
    "settlement_price": pl.Float64,
    "contract_symbol": pl.String,
    "settlement_type": pl.String,
    "source_id": pl.String,
    "source_url": pl.String,
    "fetched_at_utc": pl.Datetime("us", "UTC"),
    "available_at_utc": pl.Datetime("us", "UTC"),
}
ZQ_SETTLEMENT_KEY: tuple[str, ...] = ("observation_date", "contract_month")
_CONTRACT_SYMBOL = re.compile(r"^ZQ[A-Z]\d{2}$")


def empty_zq_settlements() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "observation_date": pl.Date,
            "contract_month": pl.Date,
            "settlement_price": pl.Float64,
            "contract_symbol": pl.String,
            "settlement_type": pl.String,
            "source_id": pl.String,
            "source_url": pl.String,
            "fetched_at_utc": pl.Datetime("us", "UTC"),
            "available_at_utc": pl.Datetime("us", "UTC"),
        }
    )


def validate_zq_settlements(frame: pl.DataFrame) -> pl.DataFrame:
    """Validate and deterministically order canonical final-settlement rows."""
    if frame.columns != list(ZQ_SETTLEMENT_COLUMNS):
        raise ValueError("ZQ settlement schema/order drift")
    if frame.schema != ZQ_SETTLEMENT_SCHEMA:
        for column, expected in ZQ_SETTLEMENT_SCHEMA.items():
            if frame.schema.get(column) != expected:
                raise TypeError(f"{column} has an invalid type")
        raise TypeError("ZQ settlement schema/types drift")
    if frame.is_empty():
        return frame
    if frame.get_column("contract_month").dt.day().ne(1).any():
        raise ValueError("contract_month must be the first day of its month")
    if (
        frame.get_column("settlement_price").is_null().any()
        or not frame.select(
            pl.col("settlement_price").is_finite().all() & (pl.col("settlement_price") > 0).all()
        ).item()
    ):
        raise ValueError("ZQ settlement price must be finite and positive")
    if (
        frame.get_column("contract_symbol")
        .map_elements(
            lambda value: _CONTRACT_SYMBOL.fullmatch(value) is None, return_dtype=pl.Boolean
        )
        .any()
    ):
        raise ValueError("only individual ZQ contract symbols are accepted")
    if frame.get_column("settlement_type").ne("final_settlement").any():
        raise ValueError("only final ZQ settlements are accepted")
    for column in ("source_id", "source_url"):
        if (
            frame.get_column(column).is_null().any()
            or frame.get_column(column).str.len_chars().eq(0).any()
        ):
            raise ValueError(f"{column} must be non-empty")
    return frame.sort(list(ZQ_SETTLEMENT_KEY))

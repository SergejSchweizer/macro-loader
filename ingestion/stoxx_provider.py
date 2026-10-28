"""STOXX full-file adapter for the registered VSTOXX series."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from types import MappingProxyType

import polars as pl

from application.contracts import FetchCapability, NativeShape, Provider, SeriesContract
from application.errors import ProviderHttpError
from application.ports.http import HttpRequest, HttpTransport, RequestContext
from application.ports.market_data import ProviderRequest

Clock = Callable[[], datetime]
_DEFAULT_SOURCE_URL = "https://www.stoxx.com/document/Indices/Current/HistoricalData/h_v2tx.txt"
_HISTORICAL_DATA_URL = "https://www.stoxx.com/document/Indices/Current/HistoricalData"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StoxxSource:
    """Provider-specific identity and full-file URL for one STOXX series."""

    series_id: str
    source_id: str
    source_url: str


STOXX_SOURCES = MappingProxyType(
    {
        source.source_id: source
        for source in (
            StoxxSource("vstoxx", "V2TX", _DEFAULT_SOURCE_URL),
            StoxxSource("vstoxx1m", "V6I1", f"{_HISTORICAL_DATA_URL}/h_v6i1.txt"),
            StoxxSource("vstoxx2m", "V6I2", f"{_HISTORICAL_DATA_URL}/h_v6i2.txt"),
            StoxxSource("vstoxx3m", "V6I3", f"{_HISTORICAL_DATA_URL}/h_v6i3.txt"),
        )
    }
)


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


def _delimiter(content: bytes) -> str:
    first = content.splitlines()[0] if content.splitlines() else b""
    if first.count(b";") >= 1:
        return ";"
    if first.count(b"\t") >= 1:
        return "\t"
    return ","


def _date_expr(column: str) -> pl.Expr:
    text = pl.col(column).cast(pl.String)
    return (
        text.str.strptime(pl.Date, "%Y-%m-%d", strict=False)
        .fill_null(text.str.strptime(pl.Date, "%d.%m.%Y", strict=False))
        .fill_null(text.str.strptime(pl.Date, "%d/%m/%Y", strict=False))
    )


class StoxxProvider:
    """Adapter translating registered STOXX full-history sources into scalar Bronze."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._transport = transport
        self._clock = clock if clock is not None else _system_utc_now

    @property
    def provider(self) -> Provider:
        return Provider.STOXX

    def fetch(self, series: SeriesContract, request: ProviderRequest) -> pl.DataFrame:
        source = self._validate_contract(series)
        _LOGGER.debug(
            "provider fetch started provider=stoxx series=%s operation=%s start=%s end=%s",
            series.series_id,
            request.operation,
            request.logical_start,
            request.logical_end,
        )
        context = RequestContext(self.provider, series.series_id, series.source_id)
        response = self._transport.send(HttpRequest("GET", source.source_url), context=context)
        if response.status_code != 200:
            raise ProviderHttpError(
                context=context,
                category="source_unavailable",
                request_path=source.source_url,
                status_code=response.status_code,
            )
        frame = self._parse(series, source, response.content)
        if request.operation == "update":
            if request.maximum_history or request.logical_start is None:
                raise ValueError("STOXX update requires bounded logical delta semantics")
            frame = frame.filter(
                pl.col("observation_date").is_between(
                    request.logical_start,
                    request.logical_end,
                    closed="both",
                )
            )
        result = frame.sort("observation_date")
        _LOGGER.debug(
            "provider fetch completed provider=stoxx series=%s rows=%d",
            series.series_id,
            result.height,
        )
        return result

    def _validate_contract(self, series: SeriesContract) -> StoxxSource:
        if series.provider is not self.provider:
            raise ValueError("unsupported STOXX series contract")
        try:
            source = STOXX_SOURCES[series.source_id]
        except KeyError as exc:
            raise ValueError("unsupported STOXX source identity") from exc
        if source.series_id != series.series_id:
            raise ValueError("STOXX series and source identity do not match")
        if series.native_shape is not NativeShape.SCALAR:
            raise ValueError("STOXX volatility indices must use scalar Bronze shape")
        if series.fetch_capability is not FetchCapability.FULL_FILE:
            raise ValueError("STOXX volatility indices must use full_file capability")
        return source

    def _parse(self, series: SeriesContract, source: StoxxSource, content: bytes) -> pl.DataFrame:
        try:
            raw = pl.read_csv(
                BytesIO(content), separator=_delimiter(content), infer_schema_length=1000
            )
        except Exception as exc:
            raise ValueError(f"invalid STOXX {source.source_id} payload") from exc
        normalized = {name: name.strip().upper() for name in raw.columns}
        raw = raw.rename(normalized)
        date_column = next(
            (candidate for candidate in ("DATE", "TIME_PERIOD") if candidate in raw.columns),
            None,
        )
        value_column = next(
            (
                candidate
                for candidate in (source.source_id, "OBS_VALUE", "VALUE", "INDEXVALUE", "CLOSE")
                if candidate in raw.columns
            ),
            None,
        )
        if date_column is None or value_column is None:
            raise ValueError(f"STOXX payload is missing date or {source.source_id} value")
        symbol_column = "SYMBOL" if "SYMBOL" in raw.columns else None
        if symbol_column is not None:
            matching_symbol = (
                pl.col(symbol_column).cast(pl.String).str.strip_chars().str.to_uppercase()
                == source.source_id
            )
            raw = raw.filter(matching_symbol)
            if raw.is_empty():
                raise ValueError(f"STOXX payload contains no {source.source_id} observations")
        frame = raw.select(
            _date_expr(date_column).alias("observation_date"),
            pl.col(value_column).cast(pl.Float64, strict=False).alias("value"),
            pl.col(value_column).is_null().alias("source_value_missing"),
        )
        if bool(frame.select(pl.col("observation_date").is_null().any()).item()):
            raise ValueError("STOXX payload contains invalid observation dates")
        # Official history files can contain a valid observation date with a blank
        # Indexvalue. Treat that explicit source-null as an absent observation, while
        # still rejecting non-empty malformed text below.
        frame = frame.filter(~pl.col("source_value_missing")).drop("source_value_missing")
        if bool(frame.select(pl.col("value").is_null().any()).item()):
            raise ValueError("STOXX payload contains missing/non-numeric values")
        if frame.is_empty():
            raise ValueError("STOXX payload contains no numeric observations")
        if not bool(frame.select(pl.col("value").is_finite().all()).item()):
            raise ValueError("STOXX payload contains non-finite values")
        if bool(frame.select(pl.col("observation_date").is_duplicated().any()).item()):
            raise ValueError("STOXX payload contains duplicate observation dates")
        fetched_at = self._clock()
        if fetched_at.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        fetched_at = fetched_at.astimezone(UTC)
        return frame.with_columns(
            pl.lit(series.series_id).alias("series_id"),
            pl.lit(self.provider.value).alias("provider"),
            pl.lit(fetched_at).alias("fetched_at_utc"),
            pl.lit(series.source_id).alias("source_id"),
            pl.lit(source.source_url).alias("source_url"),
        ).select(
            "series_id",
            "provider",
            "observation_date",
            "fetched_at_utc",
            "source_id",
            "source_url",
            "value",
        )

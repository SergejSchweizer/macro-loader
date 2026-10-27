from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest

from application.errors import ProviderHttpError
from application.ports.http import HttpRequest, HttpResponse, RequestContext
from application.ports.market_data import ProviderRequest
from application.registry import series_contract
from ingestion.stoxx_provider import STOXX_SOURCES, StoxxProvider

NOW = datetime(2026, 8, 19, 2, tzinfo=UTC)
PAYLOAD = b"DATE;V2TX\n2026-08-10;18.0\n2026-08-11;19.0\n2026-08-19;20.0\n"
LIVE_PAYLOAD = b"Date;Symbol;Indexvalue\n2026-08-10;V2TX;18.0\n2026-08-19;V2TX;20.0\n"
MISSING_VALUE_PAYLOAD = b"Date;Symbol;Indexvalue\n21.06.2006;V6I1;\n22.06.2006;V6I1;14.25\n"


class FakeTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[tuple[HttpRequest, RequestContext]] = []

    def send(self, request: HttpRequest, *, context: RequestContext) -> HttpResponse:
        self.calls.append((request, context))
        return self.response


def request(operation: str = "update") -> ProviderRequest:
    if operation == "update":
        return ProviderRequest("update", date(2026, 8, 11), date(2026, 8, 19), False)
    return ProviderRequest(operation, None, date(2026, 8, 19), True)  # type: ignore[arg-type]


def test_vstoxx_scalar_schema_and_strict_delta_filter() -> None:
    transport = FakeTransport(HttpResponse(200, PAYLOAD, {}))
    provider = StoxxProvider(transport, clock=lambda: NOW)
    frame = provider.fetch(series_contract("vstoxx"), request())
    assert frame.columns == [
        "series_id",
        "provider",
        "observation_date",
        "fetched_at_utc",
        "source_id",
        "source_url",
        "value",
    ]
    assert frame.get_column("observation_date").to_list() == [
        date(2026, 8, 11),
        date(2026, 8, 19),
    ]
    assert frame.get_column("value").dtype == pl.Float64
    assert frame.get_column("source_id").unique().item() == "V2TX"
    assert transport.calls[0][0].params == {}


def test_bootstrap_and_reconcile_accept_full_history() -> None:
    provider = StoxxProvider(FakeTransport(HttpResponse(200, PAYLOAD, {})), clock=lambda: NOW)
    assert provider.fetch(series_contract("vstoxx"), request("bootstrap")).height == 3
    assert provider.fetch(series_contract("vstoxx"), request("reconcile")).height == 3


def test_live_stoxx_indexvalue_column_is_accepted() -> None:
    provider = StoxxProvider(FakeTransport(HttpResponse(200, LIVE_PAYLOAD, {})), clock=lambda: NOW)

    frame = provider.fetch(series_contract("vstoxx"), request("bootstrap"))

    assert frame.get_column("observation_date").to_list() == [date(2026, 8, 10), date(2026, 8, 19)]
    assert frame.get_column("value").to_list() == [18.0, 20.0]


def test_blank_official_indexvalue_is_omitted_without_fabricating_an_observation() -> None:
    provider = StoxxProvider(
        FakeTransport(HttpResponse(200, MISSING_VALUE_PAYLOAD, {})), clock=lambda: NOW
    )

    frame = provider.fetch(series_contract("vstoxx1m"), request("bootstrap"))

    assert frame.get_column("observation_date").to_list() == [date(2006, 6, 22)]
    assert frame.get_column("value").to_list() == [14.25]


@pytest.mark.parametrize(
    ("series_id", "symbol", "url"),
    (
        ("vstoxx1m", "V6I1", "h_v6i1.txt"),
        ("vstoxx2m", "V6I2", "h_v6i2.txt"),
        ("vstoxx3m", "V6I3", "h_v6i3.txt"),
    ),
)
def test_term_structure_series_parse_matching_symbol_and_use_source_url(
    series_id: str, symbol: str, url: str
) -> None:
    payload = (
        f"Date;Symbol;Indexvalue\n"
        f"04.01.1999;{symbol};21.25\n"
        f"05.01.1999;{symbol};22.5\n"
        "06.01.1999;OTHER;99.0\n"
    ).encode()
    transport = FakeTransport(HttpResponse(200, payload, {}))
    frame = StoxxProvider(transport, clock=lambda: NOW).fetch(
        series_contract(series_id), request("bootstrap")
    )

    assert frame.get_column("series_id").unique().item() == series_id
    assert frame.get_column("source_id").unique().item() == symbol
    assert frame.get_column("source_url").unique().item().endswith(url)
    assert frame.get_column("observation_date").to_list() == [date(1999, 1, 4), date(1999, 1, 5)]
    assert frame.get_column("value").to_list() == [21.25, 22.5]
    assert transport.calls[0][0].url.endswith(url)


def test_term_structure_update_filters_full_file_to_exact_logical_delta() -> None:
    payload = (
        b"Date;Symbol;Indexvalue\n"
        b"10.08.2026;V6I1;18.0\n11.08.2026;V6I1;19.0\n"
        b"19.08.2026;V6I1;20.0\n20.08.2026;V6I1;21.0\n"
    )
    provider = StoxxProvider(FakeTransport(HttpResponse(200, payload, {})), clock=lambda: NOW)

    frame = provider.fetch(series_contract("vstoxx1m"), request())

    assert frame.get_column("observation_date").to_list() == [date(2026, 8, 11), date(2026, 8, 19)]


def test_stoxx_source_catalog_is_closed_and_vstoxx_remains_registered() -> None:
    assert tuple(STOXX_SOURCES) == ("V2TX", "V6I1", "V6I2", "V6I3")
    assert STOXX_SOURCES["V2TX"].series_id == "vstoxx"


def test_only_registered_vstoxx_contract_is_accepted() -> None:
    provider = StoxxProvider(FakeTransport(HttpResponse(200, PAYLOAD, {})), clock=lambda: NOW)
    with pytest.raises(ValueError, match="unsupported STOXX"):
        provider.fetch(series_contract("vix"), request())
    with pytest.raises(ValueError, match="bounded logical"):
        provider.fetch(
            series_contract("vstoxx"),
            ProviderRequest("update", date(2026, 8, 11), date(2026, 8, 19), True),
        )


@pytest.mark.parametrize(
    "payload, error",
    [
        (b"DATE;WRONG\n2026-08-19;1\n", "missing date or V2TX"),
        (b"DATE;V2TX\nbad;1\n", "invalid observation"),
        (b"DATE;V2TX\n2026-08-19;bad\n", "missing/non-numeric"),
        (b"DATE;V2TX\n2026-08-19;nan\n", "non-finite"),
        (
            b"DATE;V2TX\n2026-08-19;1\n2026-08-19;2\n",
            "duplicate observation",
        ),
    ],
)
def test_invalid_payloads_are_rejected(payload: bytes, error: str) -> None:
    provider = StoxxProvider(FakeTransport(HttpResponse(200, payload, {})), clock=lambda: NOW)
    with pytest.raises(ValueError, match=error):
        provider.fetch(series_contract("vstoxx"), request())


def test_shortened_source_stays_short_and_revision_value_is_preserved() -> None:
    short = b"DATE;V2TX\n2026-08-19;99.5\n"
    provider = StoxxProvider(FakeTransport(HttpResponse(200, short, {})), clock=lambda: NOW)
    frame = provider.fetch(series_contract("vstoxx"), request())
    assert frame.height == 1
    assert frame.get_column("value").item() == 99.5


def test_http_failure_and_naive_clock_fail_safely() -> None:
    provider = StoxxProvider(FakeTransport(HttpResponse(503, b"", {})), clock=lambda: NOW)
    with pytest.raises(ProviderHttpError) as captured:
        provider.fetch(series_contract("vstoxx"), request())
    assert captured.value.status_code == 503
    naive = StoxxProvider(
        FakeTransport(HttpResponse(200, PAYLOAD, {})),
        clock=lambda: datetime(2026, 8, 19, 2),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        naive.fetch(series_contract("vstoxx"), request())

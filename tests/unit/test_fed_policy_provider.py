from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import polars as pl
import pytest

from application.errors import ProviderHttpError
from application.ports.http import HttpRequest, HttpResponse, RequestContext
from ingestion.fed_policy_provider import (
    CmeZqSettlementProvider,
    FedPolicyProvider,
    _fomc_decision_dates,
    _reconstructed_outcomes,
)


class FakeTransport:
    def __init__(
        self,
        *,
        calendar_status: int = 200,
        effr_status: int = 200,
        settlement_status: int = 200,
        raise_settlement: bool = False,
    ) -> None:
        self.calendar_status = calendar_status
        self.effr_status = effr_status
        self.settlement_status = settlement_status
        self.raise_settlement = raise_settlement
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest, *, context: RequestContext) -> HttpResponse:
        self.requests.append(request)
        if "fomccalendars" in request.url:
            return HttpResponse(
                self.calendar_status,
                b"#### 2026 FOMC Meetings\nJanuary\n27-28\nMarch\n17-18*\n",
                {},
            )
        if "fredgraph" in request.url:
            return HttpResponse(self.effr_status, b"DATE,EFFR\n2026-01-02,3.64\n", {})
        if self.raise_settlement:
            raise ProviderHttpError(context, "blocked", "/settlements", 403)
        return HttpResponse(
            self.settlement_status,
            json.dumps(
                {
                    "settlements": [
                        {"month": "JAN 26", "settle": "96.36"},
                        {"month": "FEB 26", "settle": "96.36"},
                        {"month": "Total", "settle": "96.36"},
                    ]
                }
            ).encode(),
            {},
        )


def test_official_calendar_parser_uses_decision_day() -> None:
    assert _fomc_decision_dates("#### 2026 FOMC Meetings\nJanuary\n27-28\n") == (date(2026, 1, 28),)


def test_provider_normalizes_public_inputs_to_eod_outcomes() -> None:
    transport = FakeTransport()
    result = FedPolicyProvider(transport).fetch(date(2026, 1, 2), date(2026, 1, 2))
    assert result.schema["observation_date"] == pl.Date
    assert result.schema["available_at_utc"] == pl.Datetime("us", "UTC")
    assert result.height == 2
    assert result[0, "available_at_utc"].hour == 23
    assert result[0, "available_at_utc"].microsecond == 999999
    assert any("Settlements" in request.url for request in transport.requests)


def test_provider_fails_closed_when_calendar_is_unavailable() -> None:
    with pytest.raises(ValueError, match="calendar unavailable"):
        FedPolicyProvider(FakeTransport(calendar_status=503)).fetch(
            date(2026, 1, 2), date(2026, 1, 2)
        )


def test_provider_rejects_reverse_date_range_and_unavailable_effr() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        FedPolicyProvider(FakeTransport()).fetch(date(2026, 1, 3), date(2026, 1, 2))
    with pytest.raises(ValueError, match="EFFR history unavailable"):
        FedPolicyProvider(FakeTransport(effr_status=503)).fetch(date(2026, 1, 2), date(2026, 1, 2))


def test_provider_skips_unavailable_settlement_response() -> None:
    result = FedPolicyProvider(FakeTransport(settlement_status=503)).fetch(
        date(2026, 1, 2), date(2026, 1, 2)
    )
    assert result.is_empty()


def test_zq_provider_requests_business_days_and_preserves_contract_curve() -> None:
    transport = FakeTransport()
    provider = CmeZqSettlementProvider(
        transport, clock=lambda: datetime(2026, 1, 3, 18, tzinfo=UTC)
    )

    result = provider.fetch(date(2026, 1, 2), date(2026, 1, 5))

    assert [request.params["tradeDate"] for request in transport.requests] == [
        "01/02/2026",
        "01/05/2026",
    ]
    assert result.height == 4
    assert result.get_column("contract_month").unique().sort().to_list() == [
        date(2026, 1, 1),
        date(2026, 2, 1),
    ]
    assert result.get_column("settlement_type").unique().to_list() == ["final_settlement"]


def test_zq_provider_rejects_malformed_payload_without_rows() -> None:
    class MalformedTransport(FakeTransport):
        def send(self, request: HttpRequest, *, context: RequestContext) -> HttpResponse:
            self.requests.append(request)
            return HttpResponse(200, b"<html>blocked</html>", {})

    with pytest.raises(ValueError, match="not JSON"):
        CmeZqSettlementProvider(
            MalformedTransport(), clock=lambda: datetime(2026, 1, 3, tzinfo=UTC)
        ).fetch(date(2026, 1, 2), date(2026, 1, 2))


def test_zq_provider_rejects_non_final_settlements() -> None:
    class NonFinalTransport(FakeTransport):
        def send(self, request: HttpRequest, *, context: RequestContext) -> HttpResponse:
            self.requests.append(request)
            return HttpResponse(
                200,
                json.dumps(
                    {
                        "settlements": [
                            {"month": "JAN 26", "settle": "96.36", "settlementType": "close"}
                        ]
                    }
                ).encode(),
                {},
            )

    with pytest.raises(ValueError, match="non-final"):
        CmeZqSettlementProvider(
            NonFinalTransport(), clock=lambda: datetime(2026, 1, 3, tzinfo=UTC)
        ).fetch(date(2026, 1, 2), date(2026, 1, 2))


def test_zq_provider_rejects_reverse_range_and_http_failure() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        CmeZqSettlementProvider(
            FakeTransport(), clock=lambda: datetime(2026, 1, 3, tzinfo=UTC)
        ).fetch(date(2026, 1, 3), date(2026, 1, 2))
    with pytest.raises(ValueError, match="request failed: 503"):
        CmeZqSettlementProvider(
            FakeTransport(settlement_status=503),
            clock=lambda: datetime(2026, 1, 3, tzinfo=UTC),
        ).fetch(date(2026, 1, 2), date(2026, 1, 2))


def test_provider_falls_back_to_browser_after_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FedPolicyProvider(FakeTransport(raise_settlement=True), browser_only=True)
    expected = pl.DataFrame({"observation_date": [date(2026, 1, 2)]})
    monkeypatch.setattr(provider, "_browser_fetch", lambda *args: expected)

    assert provider.fetch(date(2026, 1, 2), date(2026, 1, 2)).equals(expected)


def test_production_provider_does_not_fall_back_to_browser() -> None:
    with pytest.raises(ProviderHttpError, match="blocked"):
        FedPolicyProvider(FakeTransport(raise_settlement=True)).fetch(
            date(2026, 1, 2), date(2026, 1, 2)
        )


class _FakeDownload:
    def __init__(self, path: Path) -> None:
        self._path = path

    def path(self) -> Path:
        return self._path


class _DownloadContext:
    def __init__(self, download: _FakeDownload) -> None:
        self.value = download

    def __enter__(self) -> _DownloadContext:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeLink:
    def __init__(self, href: str) -> None:
        self.href = href

    def get_attribute(self, name: str) -> str | None:
        return self.href if name == "href" else None

    def click(self) -> None:
        return None


class _FakeLinks:
    def __init__(self, links: list[_FakeLink]) -> None:
        self.links = links

    def count(self) -> int:
        return len(self.links)

    def nth(self, index: int) -> _FakeLink:
        return self.links[index]


class _FakeFrame:
    url = "https://example/QuikStrikeView"

    def get_by_role(self, role: str, name: object) -> SimpleNamespace:
        return SimpleNamespace(first=SimpleNamespace(click=lambda: None))

    def wait_for_timeout(self, milliseconds: int) -> None:
        return None

    def locator(self, selector: str) -> _FakeLinks:
        return _FakeLinks(
            [
                _FakeLink("https://example/MeetingExport.aspx?MeetingDate=invalid"),
                _FakeLink("https://example/MeetingExport.aspx?MeetingDate=20260128"),
            ]
        )


class _FakePage:
    frames = [_FakeFrame()]

    def __init__(self, download: _FakeDownload) -> None:
        self.download = download

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        return None

    def wait_for_timeout(self, milliseconds: int) -> None:
        return None

    def expect_download(self, *, timeout: int) -> _DownloadContext:
        return _DownloadContext(self.download)


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self.pages = [page]

    def new_page(self) -> _FakePage:
        return self.pages[0]

    def close(self) -> None:
        return None


def test_browser_fallback_parses_official_export_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    export = tmp_path / "fedwatch.csv"
    export.write_text("Date,(0-25),(25-50)\n01/02/2026,0,1\n", encoding="utf-8")
    browser = _FakeBrowser(_FakePage(_FakeDownload(export)))

    class _Playwright:
        chromium = SimpleNamespace(launch_persistent_context=lambda *args, **kwargs: browser)

        def __enter__(self) -> _Playwright:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: _Playwright()
    playwright = ModuleType("playwright")
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    result = FedPolicyProvider(FakeTransport(), browser_only=True).fetch(
        date(2026, 1, 1), date(2026, 1, 3)
    )

    assert result.height == 1
    assert result[0, "meeting_date"] == date(2026, 1, 28)
    assert result[0, "probability"] == 1.0


def test_settlement_parser_ignores_malformed_and_total_rows() -> None:
    assert FedPolicyProvider._settlement_map(b"[]") == {}
    assert FedPolicyProvider._settlement_map(
        json.dumps(
            {
                "settlements": [
                    {"month": "JAN 26", "settle": "96.36"},
                    {"month": "Total", "settle": "96.36"},
                    {"month": "BAD", "settle": "not-a-number"},
                    "invalid",
                ]
            }
        ).encode()
    ) == {"JAN 26": 96.36}


def test_reconstructed_outcomes_handles_missing_months_and_early_meeting() -> None:
    meeting = date(2026, 1, 2)
    assert _reconstructed_outcomes({}, meeting, 3.64) == ()
    settlements = {"JAN 26": 96.36, "FEB 26": 96.11}
    outcomes = _reconstructed_outcomes(settlements, meeting, 3.64)
    assert outcomes[0][0] == pytest.approx(25.0)
    assert sum(probability for _, probability in outcomes) == pytest.approx(1.0)

from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest

from application.ports.http import HttpRequest, HttpResponse, RequestContext
from ingestion.fed_policy_provider import FedPolicyProvider, _fomc_decision_dates


class FakeTransport:
    def __init__(self, *, calendar_status: int = 200) -> None:
        self.calendar_status = calendar_status
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest, *, context: RequestContext) -> HttpResponse:
        del context
        self.requests.append(request)
        if "fomccalendars" in request.url:
            return HttpResponse(
                self.calendar_status,
                b"#### 2026 FOMC Meetings\nJanuary\n27-28\nMarch\n17-18*\n",
                {},
            )
        if "fredgraph" in request.url:
            return HttpResponse(200, b"DATE,EFFR\n2026-01-02,3.64\n", {})
        return HttpResponse(
            200,
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
    assert _fomc_decision_dates("#### 2026 FOMC Meetings\nJanuary\n27-28\n") == (
        date(2026, 1, 28),
    )


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

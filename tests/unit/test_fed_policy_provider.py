from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest

from application.ports.http import HttpRequest, HttpResponse, RequestContext
from ingestion.fed_policy_provider import (
    FedPolicyProvider,
    _fomc_decision_dates,
    _outcomes,
)


class FakeTransport:
    def __init__(
        self, *, calendar_status: int = 200, effr_status: int = 200, settlement_status: int = 200
    ) -> None:
        self.calendar_status = calendar_status
        self.effr_status = effr_status
        self.settlement_status = settlement_status
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
            return HttpResponse(self.effr_status, b"DATE,EFFR\n2026-01-02,3.64\n", {})
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


def test_outcomes_handles_missing_months_and_early_meeting() -> None:
    meeting = date(2026, 1, 2)
    assert _outcomes({}, meeting, 3.64) == ()
    settlements = {"JAN 26": 96.36, "FEB 26": 96.11}
    outcomes = _outcomes(settlements, meeting, 3.64)
    assert outcomes[0][0] == pytest.approx(0.0)
    assert sum(probability for _, probability in outcomes) == pytest.approx(1.0)

"""Official public-source FedWatch snapshot adapter.

This adapter intentionally uses only the public CME 30-Day Fed Funds
settlement endpoint, the public Federal Reserve FOMC calendar, and the public
Federal Reserve EFFR CSV.  It does not call CME's paid FedWatch API/DataMine.
"""

from __future__ import annotations

import calendar
import json
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from io import BytesIO

import polars as pl

from application.contracts import Provider
from application.fed_policy_features import EOD_UTC, FED_POLICY_SNAPSHOT_COLUMNS
from application.ports.http import HttpRequest, HttpTransport, RequestContext

_CME_SETTLEMENTS_URL = "https://www.cmegroup.com/CmeWS/mvc/Settlements/Futures/Settlements/305/FUT"
_EFFR_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_MONTHS = {name: index for index, name in enumerate(calendar.month_abbr) if name}
_MONTH_CODES = {
    1: "F",
    2: "G",
    3: "H",
    4: "J",
    5: "K",
    6: "M",
    7: "N",
    8: "Q",
    9: "U",
    10: "V",
    11: "X",
    12: "Z",
}


def _business_days(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            yield cursor
        cursor += timedelta(days=1)


def _fomc_decision_dates(html: str) -> tuple[date, ...]:
    """Parse meeting decision dates from the official calendar HTML."""
    dates: list[date] = []
    text = re.sub(r"<[^>]+>", "\n", html)
    text = re.sub(r"\s+", " ", text)
    sections = re.finditer(
        r"\b(20\d{2})\s+FOMC Meetings(.*?)(?=\b20\d{2}\s+FOMC Meetings|$)",
        text,
        flags=re.IGNORECASE,
    )
    for section in sections:
        year = int(section.group(1))
        body = section.group(2)
        for match in re.finditer(
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?",
            body,
        ):
            month = list(calendar.month_name).index(match.group(1))
            day = int(match.group(3) or match.group(2))
            dates.append(date(year, month, day))
    return tuple(sorted(set(dates)))


def _month_key(year: int, month: int) -> str:
    return f"{calendar.month_abbr[month].upper()} {year % 100:02d}"


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _outcomes(
    settlements: dict[str, float], meeting: date, current_effr: float
) -> tuple[tuple[float, float], ...]:
    month_key = _month_key(meeting.year, meeting.month)
    if month_key not in settlements:
        return ()
    implied = 100.0 - settlements[month_key]
    py, pm = _previous_month(meeting.year, meeting.month)
    pre = (
        100.0 - settlements[_month_key(py, pm)]
        if _month_key(py, pm) in settlements
        else current_effr
    )
    days = calendar.monthrange(meeting.year, meeting.month)[1]
    after = days - meeting.day + 1
    ny, nm = _next_month(meeting.year, meeting.month)
    next_key = _month_key(ny, nm)
    if after <= 3 and next_key in settlements:
        post = 100.0 - settlements[next_key]
    else:
        post = (implied * days - pre * (meeting.day - 1)) / after
    expected_quarters = (post - pre) * 100.0 / 25.0
    lower = int(expected_quarters // 1)
    remainder = max(0.0, min(1.0, expected_quarters - lower))
    return ((lower * 25.0, 1.0 - remainder), ((lower + 1) * 25.0, remainder))


class FedPolicyProvider:
    """Fetch end-of-day public FedWatch inputs and normalize outcome rows."""

    def __init__(self, transport: HttpTransport, *, fomc_url: str = _FOMC_URL) -> None:
        self._transport = transport
        self._fomc_url = fomc_url

    def fetch(self, start: date, end: date) -> pl.DataFrame:
        if start > end:
            raise ValueError("Fed policy start must not exceed end")
        context = RequestContext(Provider.FEDWATCH, "fed_policy", "cme-fedwatch-eod")
        calendar_response = self._transport.send(
            HttpRequest("GET", self._fomc_url), context=context
        )
        if calendar_response.status_code != 200:
            raise ValueError("official Federal Reserve FOMC calendar unavailable")
        meetings = _fomc_decision_dates(calendar_response.content.decode("utf-8", errors="replace"))
        effr = self._effr(start, end, context)
        rows: list[dict[str, object]] = []
        for trade_date in _business_days(start, end):
            response = self._transport.send(
                HttpRequest(
                    "GET",
                    _CME_SETTLEMENTS_URL,
                    params={"tradeDate": trade_date.strftime("%m/%d/%Y")},
                ),
                context=context,
            )
            if response.status_code != 200:
                continue
            settlements = self._settlement_map(response.content)
            if not settlements or trade_date not in effr:
                continue
            for meeting in meetings:
                if meeting <= trade_date:
                    continue
                for move, probability in _outcomes(settlements, meeting, effr[trade_date]):
                    if probability <= 0:
                        continue
                    rows.append(
                        {
                            "observation_date": trade_date,
                            "meeting_date": meeting,
                            "move_bp": move,
                            "probability": probability,
                            "available_at_utc": datetime(
                                trade_date.year,
                                trade_date.month,
                                trade_date.day,
                                *EOD_UTC,
                                tzinfo=UTC,
                            ),
                        }
                    )
        return pl.DataFrame(
            rows,
            schema={
                "observation_date": pl.Date,
                "meeting_date": pl.Date,
                "move_bp": pl.Float64,
                "probability": pl.Float64,
                "available_at_utc": pl.Datetime("us", "UTC"),
            },
        ).select(FED_POLICY_SNAPSHOT_COLUMNS)

    def _effr(self, start: date, end: date, context: RequestContext) -> dict[date, float]:
        response = self._transport.send(
            HttpRequest(
                "GET",
                _EFFR_URL,
                params={"id": "EFFR", "cosd": start.isoformat(), "coed": end.isoformat()},
            ),
            context=context,
        )
        if response.status_code != 200:
            raise ValueError("official EFFR history unavailable")
        frame = pl.read_csv(BytesIO(response.content), infer_schema_length=1000)
        value_column = frame.columns[-1]
        frame = (
            frame.rename({frame.columns[0]: "observation_date"})
            .with_columns(
                pl.col("observation_date").str.strptime(pl.Date, "%Y-%m-%d"),
                pl.col(value_column).cast(pl.String).str.strip_chars().alias("_raw_effr"),
            )
            .with_columns(pl.col("_raw_effr").cast(pl.Float64, strict=False).alias("_effr"))
        )
        return {
            row["observation_date"]: row["_effr"]
            for row in frame.filter(pl.col("_effr").is_not_null()).iter_rows(named=True)
        }

    @staticmethod
    def _settlement_map(content: bytes) -> dict[str, float]:
        raw = json.loads(content)
        if not isinstance(raw, dict):
            return {}
        settlements = raw.get("settlements", [])
        result: dict[str, float] = {}
        for item in settlements:
            if not isinstance(item, dict) or item.get("month") == "Total":
                continue
            try:
                result[str(item["month"])] = float(item["settle"])
            except (KeyError, TypeError, ValueError):
                continue
        return result

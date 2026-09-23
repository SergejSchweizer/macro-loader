from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from application.fed_policy_settlements import ZQ_SETTLEMENT_COLUMNS
from application.paths import LakePaths
from ingestion.fed_policy_settlement_store import FedPolicySettlementStore

NOW = datetime(2026, 1, 3, 18, tzinfo=UTC)


def _rows(*, price: float = 96.25, observation_date: date = date(2026, 1, 2)) -> pl.DataFrame:
    return pl.DataFrame(
        [
            (
                observation_date,
                date(2026, 3, 1),
                price,
                "ZQH26",
                "final_settlement",
                "cme-zq",
                "https://source.example/zq",
                NOW,
                datetime(2026, 1, 2, 23, 0, tzinfo=UTC),
            )
        ],
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
        },
        orient="row",
    )


def test_round_trip_preserves_schema_and_multiple_contract_months(tmp_path: Path) -> None:
    frame = pl.concat(
        [
            _rows(),
            _rows(observation_date=date(2026, 1, 5)).with_columns(
                pl.lit(date(2026, 4, 1)).alias("contract_month")
            ),
        ],
        how="vertical",
    )
    store = FedPolicySettlementStore(LakePaths(tmp_path))

    result = store.upsert(frame)

    assert result.inserted_count == 2
    assert result.revision_count == 0
    assert result.frame.columns == list(ZQ_SETTLEMENT_COLUMNS)
    assert store.read().sort("observation_date").get_column("contract_month").to_list() == [
        date(2026, 3, 1),
        date(2026, 4, 1),
    ]


def test_revision_rewrites_only_changed_partition_and_replay_is_noop(tmp_path: Path) -> None:
    store = FedPolicySettlementStore(LakePaths(tmp_path))
    first = store.upsert(_rows())
    untouched = store.upsert(_rows(observation_date=date(2026, 2, 2)))

    revised = store.upsert(_rows(price=96.5))
    replay = store.upsert(_rows(price=96.5))

    assert first.rewritten_paths == (
        LakePaths(tmp_path).fed_policy_settlement_month(date(2026, 1, 2)),
    )
    assert untouched.inserted_count == 1
    assert revised.revision_count == 1
    assert revised.rewritten_paths == first.rewritten_paths
    assert replay.inserted_count == 0
    assert replay.revision_count == 0
    assert replay.rewritten_paths == ()
    assert store.read().filter(pl.col("observation_date") == date(2026, 2, 2)).height == 1


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("contract_symbol", "ZQ=F", "individual ZQ"),
        ("settlement_type", "close", "final ZQ"),
        ("settlement_price", 0.0, "finite and positive"),
        ("contract_month", date(2026, 3, 2), "first day"),
    ],
)
def test_invalid_settlement_rows_fail_closed(
    tmp_path: Path, column: str, value: object, message: str
) -> None:
    frame = _rows().with_columns(pl.lit(value).alias(column))

    with pytest.raises((TypeError, ValueError), match=message):
        FedPolicySettlementStore(LakePaths(tmp_path)).upsert(frame)


def test_naive_timestamps_are_rejected(tmp_path: Path) -> None:
    frame = _rows().with_columns(pl.col("available_at_utc").dt.replace_time_zone(None))

    with pytest.raises(TypeError, match="available_at_utc"):
        FedPolicySettlementStore(LakePaths(tmp_path)).upsert(frame)

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from application.fed_policy_features import FED_POLICY_SNAPSHOT_COLUMNS
from application.paths import LakePaths
from ingestion.fed_policy_store import FedPolicySnapshotStore


class FakeProvider:
    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame
        self.calls: list[tuple[date, date]] = []

    def fetch(self, start: date, end: date) -> pl.DataFrame:
        self.calls.append((start, end))
        return self.frame


def _frame(probability: float = 0.5) -> pl.DataFrame:
    return pl.DataFrame(
        [
            (
                date(2026, 1, 2),
                date(2026, 1, 28),
                0.0,
                probability,
                datetime(2026, 1, 2, 23, 59, 59, 999999, tzinfo=UTC),
            )
        ],
        schema={
            "observation_date": pl.Date,
            "meeting_date": pl.Date,
            "move_bp": pl.Float64,
            "probability": pl.Float64,
            "available_at_utc": pl.Datetime("us", "UTC"),
        },
        orient="row",
    )


def test_read_returns_empty_schema_when_snapshot_file_is_absent(tmp_path: Path) -> None:
    store = FedPolicySnapshotStore(LakePaths(tmp_path), FakeProvider(pl.DataFrame()))
    result = store.read()
    assert result.columns == list(FED_POLICY_SNAPSHOT_COLUMNS)
    assert result.is_empty()


def test_refresh_writes_and_deduplicates_snapshots(tmp_path: Path) -> None:
    incoming = pl.concat([_frame(), _frame(0.75)], how="vertical")
    provider = FakeProvider(incoming)
    store = FedPolicySnapshotStore(LakePaths(tmp_path), provider)

    result = store.refresh(date(2026, 1, 1), date(2026, 1, 3))

    assert provider.calls == [(date(2026, 1, 1), date(2026, 1, 3))]
    assert result.height == 1
    assert result[0, "probability"] == 0.75
    assert LakePaths(tmp_path).fed_policy_snapshots().is_file()


def test_read_rejects_schema_order_drift(tmp_path: Path) -> None:
    path = LakePaths(tmp_path).fed_policy_snapshots()
    path.parent.mkdir(parents=True)
    pl.DataFrame({"unexpected": [1]}).write_parquet(path)
    store = FedPolicySnapshotStore(LakePaths(tmp_path), FakeProvider(pl.DataFrame()))

    try:
        store.read()
    except ValueError as error:
        assert "schema/order drift" in str(error)
    else:
        raise AssertionError("schema drift must be rejected")

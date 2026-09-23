from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from application.fed_policy_postgres import (
    FED_POLICY_COLUMNS,
    FED_POLICY_FEATURE_COLUMNS,
    fed_policy_rows,
    plan_fed_policy_delta,
)
from ingestion.fed_policy_postgres_repository import FedPolicyPostgresRepository
from ingestion.postgres_gold_repository import POSTGRES_HOST, POSTGRES_PORT, PostgresSyncConfig


def _frame(days: list[int], value: float = 1.0) -> pl.DataFrame:
    rows = []
    for day in days:
        timestamp = datetime(2026, 8, day, tzinfo=UTC)
        rows.append(
            (timestamp, value, None, 2.0, value - 1.0, timestamp + timedelta(hours=23, minutes=59))
        )
    return pl.DataFrame(
        rows,
        schema={
            "timestamp_m1": pl.Datetime("us", "UTC"),
            **{column: pl.Float64 for column in FED_POLICY_FEATURE_COLUMNS},
            "available_at_utc": pl.Datetime("us", "UTC"),
        },
        orient="row",
    )


def test_rows_are_sorted_and_delta_is_exact() -> None:
    desired = fed_policy_rows(_frame([3, 1]))
    existing = fed_policy_rows(_frame([1, 2], value=0.0))
    plan = plan_fed_policy_delta(desired, existing)
    assert [row.timestamp_m1.day for row in plan.inserts] == [3]
    assert [row.timestamp_m1.day for row in plan.updates] == [1]
    assert [timestamp.day for timestamp in plan.deletes] == [2]


def test_replay_is_a_true_noop() -> None:
    rows = fed_policy_rows(_frame([1, 2]))
    plan = plan_fed_policy_delta(rows, rows)
    assert not plan.mutated
    assert len(plan.unchanged) == 2


def test_schema_drift_and_duplicate_timestamp_fail_closed() -> None:
    with pytest.raises(ValueError, match="schema/order"):
        fed_policy_rows(_frame([1]).select("timestamp_m1"))
    with pytest.raises(ValueError, match="unique"):
        fed_policy_rows(_frame([1, 1]))


def test_public_contract_has_exact_four_features() -> None:
    assert FED_POLICY_COLUMNS == (
        "timestamp_m1",
        "fed_next_expected_move_bp",
        "fed_path_slope_m3_bp",
        "fed_next_uncertainty_bp",
        "fed_repricing_5obs_bp",
        "available_at_utc",
    )


class _Cursor:
    def __init__(
        self,
        rows: list[tuple[object, ...]],
        digest_rows: list[tuple[object, ...]] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.rows = rows
        self.digest_rows = digest_rows if digest_rows is not None else []
        self.result_rows = rows
        self.fail_on = fail_on
        self.queries: list[str] = []

    @property
    def rowcount(self) -> int:
        return 1

    def execute(self, query: str, params: object = None) -> object:
        self.queries.append(query)
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("refresh failed")
        self.result_rows = self.digest_rows if "row_sha256" in query else self.rows
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.result_rows

    def close(self) -> None:
        pass


class _Connection:
    def __init__(
        self,
        rows: list[tuple[object, ...]],
        digest_rows: list[tuple[object, ...]] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.cursor_value = _Cursor(rows, digest_rows, fail_on)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return self.cursor_value

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass


def _config() -> PostgresSyncConfig:
    return PostgresSyncConfig(
        POSTGRES_HOST, POSTGRES_PORT, "macro-loader", "macro_loader", "secret"
    )


def test_sync_refreshes_once_for_mutation() -> None:
    connection = _Connection([])
    repository = FedPolicyPostgresRepository(_config(), connection_factory=lambda _: connection)
    plan = repository.sync(
        _frame([1]),
        source_build_id="build-1",
        schema_version=1,
        feature_version=1,
        synced_at_utc=datetime(2026, 8, 2, tzinfo=UTC),
    )
    assert plan.mutated
    assert (
        sum("refresh_macro_features_explicit" in query for query in connection.cursor_value.queries)
        == 1
    )
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_sync_noop_does_not_refresh() -> None:
    rows = fed_policy_rows(_frame([1]))
    database_rows = [(row.timestamp_m1, *row.values, row.available_at_utc) for row in rows]
    digest_rows = [(row.timestamp_m1, row.row_sha256) for row in rows]
    connection = _Connection(database_rows, digest_rows)
    repository = FedPolicyPostgresRepository(_config(), connection_factory=lambda _: connection)
    plan = repository.sync(
        _frame([1]),
        source_build_id="build-1",
        schema_version=1,
        feature_version=1,
        synced_at_utc=datetime(2026, 8, 2, tzinfo=UTC),
    )
    assert not plan.mutated
    assert not any(
        "refresh_macro_features_explicit" in query for query in connection.cursor_value.queries
    )


def test_sync_rolls_back_when_refresh_fails() -> None:
    connection = _Connection([], fail_on="refresh_macro_features_explicit")
    repository = FedPolicyPostgresRepository(_config(), connection_factory=lambda _: connection)
    with pytest.raises(Exception, match="failed"):
        repository.sync(
            _frame([1]),
            source_build_id="build-1",
            schema_version=1,
            feature_version=1,
            synced_at_utc=datetime(2026, 8, 2, tzinfo=UTC),
        )
    assert connection.commits == 0
    assert connection.rollbacks == 1

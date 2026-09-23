from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl

from application.fed_policy_features import FED_POLICY_SNAPSHOT_COLUMNS, build_fed_policy_features
from application.fed_policy_orchestration import FedPolicyEodOrchestrator, FedPolicyMode
from application.paths import LakePaths
from ingestion.fed_policy_feature_store import FedPolicyFeatureStore


def _snapshots(observation_date: date) -> pl.DataFrame:
    return pl.DataFrame(
        [
            (
                observation_date,
                date(2026, 12, 10),
                25.0,
                1.0,
                datetime.combine(observation_date, datetime.max.time(), tzinfo=UTC),
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
    ).select(FED_POLICY_SNAPSHOT_COLUMNS)


class Snapshots:
    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame
        self.calls: list[tuple[date, date]] = []

    def read(self) -> pl.DataFrame:
        return self.frame

    def refresh(self, start: date, end: date) -> pl.DataFrame:
        self.calls.append((start, end))
        return self.frame


class Settlements:
    def __init__(self, latest: date) -> None:
        self.frame = pl.DataFrame({"observation_date": [latest]})

    def read(self) -> pl.DataFrame:
        return self.frame


class Features:
    def __init__(self) -> None:
        self.published: list[pl.DataFrame] = []

    def read(self) -> pl.DataFrame:
        return pl.DataFrame()

    def publish(self, frame: pl.DataFrame) -> None:
        self.published.append(frame)


class Postgres:
    def __init__(self, features: Features) -> None:
        self.features = features
        self.synced: list[pl.DataFrame] = []

    def sync(self, frame: pl.DataFrame) -> None:
        assert self.features.published
        self.synced.append(frame)


def test_update_uses_latest_durable_settlement_with_calendar_overlap() -> None:
    snapshots = Snapshots(_snapshots(date(2026, 8, 19)))
    orchestrator = FedPolicyEodOrchestrator(
        snapshots=snapshots,
        settlements=Settlements(date(2026, 8, 18)),
        features=Features(),
    )

    orchestrator.update(today=date(2026, 8, 19))

    assert snapshots.calls == [(date(2026, 8, 11), date(2026, 8, 19))]


def test_eod_mode_is_update_and_publishes_before_postgres_sync() -> None:
    snapshots = Snapshots(_snapshots(date(2026, 8, 19)))
    features = Features()
    postgres = Postgres(features)
    orchestrator = FedPolicyEodOrchestrator(
        snapshots=snapshots,
        settlements=Settlements(date(2026, 8, 18)),
        features=features,
        postgres=postgres,
    )

    orchestrator.run(FedPolicyMode.UPDATE, today=date(2026, 8, 19))

    assert len(features.published) == 1
    assert len(postgres.synced) == 1


def test_bootstrap_and_reconcile_use_configured_history_start() -> None:
    snapshots = Snapshots(_snapshots(date(2026, 8, 19)))
    orchestrator = FedPolicyEodOrchestrator(
        snapshots=snapshots,
        settlements=Settlements(date(2026, 8, 18)),
        features=Features(),
        start_date=lambda: date(2010, 1, 1),
    )

    orchestrator.bootstrap(today=date(2026, 8, 19))
    orchestrator.reconcile(today=date(2026, 8, 19))

    assert snapshots.calls == [
        (date(2010, 1, 1), date(2026, 8, 19)),
        (date(2010, 1, 1), date(2026, 8, 19)),
    ]


def test_feature_store_round_trips_and_rejects_wrong_schema(tmp_path) -> None:
    store = FedPolicyFeatureStore(LakePaths(tmp_path))
    frame = _snapshots(date(2026, 8, 19))
    features = build_fed_policy_features(frame)

    store.publish(features)

    assert store.read().equals(features)
    try:
        store.publish(pl.DataFrame({"wrong": [1]}))
    except ValueError as error:
        assert "schema/order drift" in str(error)
    else:
        raise AssertionError("feature schema drift must be rejected")

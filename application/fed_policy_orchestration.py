"""Explicit Fed-policy bootstrap, update, and reconciliation commands."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from enum import StrEnum
from typing import Protocol

import polars as pl

from application.fed_policy_features import build_fed_policy_features


class FedPolicyMode(StrEnum):
    BOOTSTRAP = "bootstrap"
    UPDATE = "update"
    RECONCILE = "reconcile"


class FedPolicySnapshotPort(Protocol):
    def read(self) -> pl.DataFrame: ...

    def refresh(self, start: date, end: date) -> pl.DataFrame: ...


class FedPolicySettlementPort(Protocol):
    def read(self) -> pl.DataFrame: ...


class FedPolicyFeaturePublicationPort(Protocol):
    def read(self) -> pl.DataFrame: ...

    def publish(self, frame: pl.DataFrame) -> None: ...


class FedPolicyPostgresSyncPort(Protocol):
    def sync(self, frame: pl.DataFrame) -> object: ...


def _default_start() -> date:
    return date(2010, 1, 1)


class FedPolicyEodOrchestrator:
    """Coordinate bounded Fed-policy refresh and atomic feature publication."""

    def __init__(
        self,
        *,
        snapshots: FedPolicySnapshotPort,
        settlements: FedPolicySettlementPort,
        features: FedPolicyFeaturePublicationPort,
        postgres: FedPolicyPostgresSyncPort | None = None,
        start_date: Callable[[], date] = _default_start,
    ) -> None:
        self._snapshots = snapshots
        self._settlements = settlements
        self._features = features
        self._postgres = postgres
        self._start_date = start_date

    def run(self, mode: FedPolicyMode, *, today: date) -> pl.DataFrame:
        durable_settlements = self._settlements.read()
        if mode is FedPolicyMode.UPDATE and durable_settlements.height:
            latest = durable_settlements.get_column("observation_date").max()
            if not isinstance(latest, date):
                raise ValueError("Fed policy snapshots have no durable observation date")
            start = latest - timedelta(days=7)
        else:
            start = self._start_date()
        snapshots = self._snapshots.refresh(start, today)
        candidate = build_fed_policy_features(snapshots)
        self._features.publish(candidate)
        if self._postgres is not None:
            self._postgres.sync(candidate)
        return candidate

    def bootstrap(self, *, today: date) -> pl.DataFrame:
        return self.run(FedPolicyMode.BOOTSTRAP, today=today)

    def update(self, *, today: date) -> pl.DataFrame:
        return self.run(FedPolicyMode.UPDATE, today=today)

    def reconcile(self, *, today: date) -> pl.DataFrame:
        return self.run(FedPolicyMode.RECONCILE, today=today)

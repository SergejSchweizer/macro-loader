from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from threading import Barrier, Event, Lock

import polars as pl
import pytest

from application.bronze_orchestration import BronzeOrchestrator
from application.contracts import Provider, SeriesContract
from application.operational_records import IngestionRunRecord
from application.parallelism import PolarsExecutionPolicy
from application.ports.bronze import PreparedBronze
from application.ports.lake import FrameDiff, ObservationBounds
from application.ports.market_data import ProviderRequest
from application.registry import SERIES_REGISTRY
from application.state import IngestionState

TODAY = date(2026, 8, 19)
NOW = datetime(2026, 8, 19, 2, tzinfo=UTC)
SERIES = ("us_2y", "us_10y", "euro_hy_oas", "usd_broad")


class ConcurrentProvider:
    provider = Provider.FRED

    def __init__(self, *, failures: frozenset[str] = frozenset()) -> None:
        self.failures = failures
        self.active = 0
        self.peak = 0
        self._lock = Lock()
        self._first_two = Barrier(2)
        self.all_fetched = Event()
        self.fetch_count = 0

    def fetch(self, series: SeriesContract, request: ProviderRequest) -> pl.DataFrame:
        del request
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.fetch_count += 1
            if self.fetch_count == len(SERIES):
                self.all_fetched.set()
        try:
            if series.series_id in SERIES[:2]:
                self._first_two.wait(timeout=2)
            if series.series_id in self.failures:
                raise RuntimeError("injected provider error")
            return pl.DataFrame()
        finally:
            with self._lock:
                self.active -= 1


class RecordingUnitOfWork:
    def __init__(self, *, pause_first_mutation: bool = False) -> None:
        self.pause_first_mutation = pause_first_mutation
        self.first_mutation_entered = Event()
        self.release_first_mutation = Event()
        self.mutation_entries = 0
        self.active_mutations = 0
        self.peak_mutations = 0
        self._lock = Lock()

    def bounds(self, contract: SeriesContract) -> ObservationBounds:
        del contract
        return ObservationBounds(None, None)

    def state(self, contract: SeriesContract) -> IngestionState:
        return IngestionState(contract.provider, contract.series_id)

    def prepare(self, contract: SeriesContract, incoming: pl.DataFrame) -> PreparedBronze:
        empty = incoming.head(0)
        return PreparedBronze(
            contract=contract,
            merged=incoming,
            diff=FrameDiff(empty, empty, empty),
            post_bounds=ObservationBounds(None, None),
            written_partitions=0,
        )

    def commit_success(
        self,
        prepared: PreparedBronze,
        run: IngestionRunRecord,
        state: IngestionState,
    ) -> None:
        del prepared, run, state
        self._mutate()

    def record_failure(self, run: IngestionRunRecord) -> None:
        del run
        self._mutate()

    def _mutate(self) -> None:
        with self._lock:
            self.active_mutations += 1
            self.mutation_entries += 1
            self.peak_mutations = max(self.peak_mutations, self.active_mutations)
            first = self.mutation_entries == 1
            if first:
                self.first_mutation_entered.set()
        if first and self.pause_first_mutation:
            assert self.release_first_mutation.wait(timeout=2)
        with self._lock:
            self.active_mutations -= 1


def _orchestrator(
    provider: ConcurrentProvider,
    uow: RecordingUnitOfWork,
    *,
    workers: int = 2,
) -> BronzeOrchestrator:
    return BronzeOrchestrator(
        series_registry=SERIES_REGISTRY,
        providers={Provider.FRED: provider},
        unit_of_work=uow,  # type: ignore[arg-type]
        clock=lambda: NOW,
        run_id_factory=lambda series_id: f"run-{series_id}",
        execution_policy=PolarsExecutionPolicy(workers),
    )


def test_run_many_is_bounded_and_isolates_failures_in_input_order() -> None:
    provider = ConcurrentProvider(failures=frozenset({"us_10y", "usd_broad"}))
    uow = RecordingUnitOfWork()
    service = _orchestrator(provider, uow, workers=2)

    result = service.run_many(SERIES, today=TODAY)

    assert provider.peak == 2
    assert [item.series_id for item in result.successes] == ["us_2y", "euro_hy_oas"]
    assert result.failures == ("us_10y", "usd_broad")
    assert uow.peak_mutations == 1


def test_debug_log_records_bounded_series_run_counts_without_failure_text(caplog) -> None:
    class DebugProvider:
        provider = Provider.FRED

        def fetch(self, series: SeriesContract, request: ProviderRequest) -> pl.DataFrame:
            del request
            if series.series_id == "us_10y":
                raise RuntimeError("injected provider error")
            return pl.DataFrame()

    uow = RecordingUnitOfWork()
    service = BronzeOrchestrator(
        series_registry=SERIES_REGISTRY,
        providers={Provider.FRED: DebugProvider()},
        unit_of_work=uow,  # type: ignore[arg-type]
        clock=lambda: NOW,
        run_id_factory=lambda series_id: f"run-{series_id}",
        execution_policy=PolarsExecutionPolicy(1),
    )
    caplog.set_level(logging.DEBUG, logger="application.bronze_orchestration")

    result = service.run_series("us_2y", today=TODAY)

    assert result.inserted_rows == 0
    assert "series ingestion started provider=fred series=us_2y mode=bootstrap" in caplog.text
    assert (
        "series ingestion committed provider=fred series=us_2y fetched=0 accepted=0" in caplog.text
    )
    assert "injected provider error" not in caplog.text

    with pytest.raises(RuntimeError, match="injected provider error"):
        service.run_series("us_10y", today=TODAY)
    assert "series ingestion failed provider=fred series=us_10y category=RuntimeError" in (
        caplog.text
    )
    assert "injected provider error" not in caplog.text


def test_run_many_serializes_success_and_failure_manifest_mutations() -> None:
    provider = ConcurrentProvider(failures=frozenset({"usd_broad"}))
    uow = RecordingUnitOfWork(pause_first_mutation=True)
    service = _orchestrator(provider, uow, workers=4)

    with ThreadPoolExecutor(max_workers=1) as caller:
        pending = caller.submit(service.run_many, SERIES, today=TODAY)
        assert uow.first_mutation_entered.wait(timeout=2)
        assert provider.all_fetched.wait(timeout=2)
        assert uow.mutation_entries == 1
        uow.release_first_mutation.set()
        result = pending.result(timeout=2)

    assert result.failures == ("usd_broad",)
    assert len(result.successes) == 3
    assert uow.mutation_entries == 4
    assert uow.peak_mutations == 1


def test_run_many_rejects_duplicate_ids_before_provider_or_uow_side_effects() -> None:
    provider = ConcurrentProvider()
    uow = RecordingUnitOfWork()
    service = _orchestrator(provider, uow)

    with pytest.raises(ValueError, match="distinct"):
        service.run_many(("us_2y", "us_2y"), today=TODAY)

    assert provider.fetch_count == 0
    assert uow.mutation_entries == 0

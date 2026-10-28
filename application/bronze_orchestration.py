"""Registry-driven Bronze ingestion application service."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from threading import Lock

import polars as pl

from application.contracts import Provider, SeriesContract
from application.operational_records import IngestionRunRecord, RunMode, RunStatus
from application.parallelism import PolarsExecutionPolicy
from application.planner import FetchInstruction, OperationMode, PlannerConfig, build_plan
from application.ports.bronze import BronzeUnitOfWork, PreparedBronze
from application.ports.market_data import MarketDataProvider, ProviderRequest
from application.state import advance_state

Clock = Callable[[], datetime]
RunIdFactory = Callable[[str], str]
_LOGGER = logging.getLogger(__name__)


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


def _default_run_id(series_id: str) -> str:
    return f"{series_id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"


@dataclass(frozen=True, slots=True)
class SeriesRunResult:
    """Stable result returned by one isolated series execution."""

    series_id: str
    provider: Provider
    run_id: str
    mode: OperationMode
    request_start: date | None
    request_end: date
    maximum_history: bool
    inserted_rows: int
    revised_rows: int
    written_partitions: int


@dataclass(frozen=True, slots=True)
class BatchRunResult:
    """Per-series isolation result for a multi-series request."""

    successes: tuple[SeriesRunResult, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SeriesOutcome:
    result: SeriesRunResult | None = None
    failed: bool = False


class BronzeOrchestrator:
    """Application service coordinating providers through injected registries and a UoW."""

    def __init__(
        self,
        *,
        series_registry: Mapping[str, SeriesContract],
        providers: Mapping[Provider, MarketDataProvider],
        unit_of_work: BronzeUnitOfWork,
        planner_config: PlannerConfig | None = None,
        clock: Clock | None = None,
        run_id_factory: RunIdFactory | None = None,
        execution_policy: PolarsExecutionPolicy | None = None,
    ) -> None:
        self._series_registry = series_registry
        self._providers = providers
        self._uow = unit_of_work
        self._planner_config = planner_config if planner_config is not None else PlannerConfig()
        self._clock = clock if clock is not None else _system_utc_now
        self._run_id_factory = run_id_factory if run_id_factory is not None else _default_run_id
        self._execution_policy = (
            execution_policy
            if execution_policy is not None
            else PolarsExecutionPolicy.all_available_cores()
        )
        self._uow_write_lock = Lock()

    def run_series(
        self,
        series_id: str,
        *,
        operation: OperationMode = OperationMode.UPDATE,
        today: date,
    ) -> SeriesRunResult:
        contract = self._resolve_series(series_id)
        provider = self._resolve_provider(contract.provider)
        bounds = self._uow.bounds(contract)
        if operation is OperationMode.BOOTSTRAP and bounds.maximum is not None:
            raise ValueError("explicit bootstrap requires empty authoritative Bronze")
        plan = build_plan(
            contract,
            bounds,
            today=today,
            explicit_reconcile=operation is OperationMode.RECONCILE,
            config=self._planner_config,
        )
        if operation is OperationMode.BOOTSTRAP and plan.mode is not OperationMode.BOOTSTRAP:
            raise ValueError("explicit bootstrap did not resolve to bootstrap plan")
        if (
            operation is OperationMode.UPDATE
            and bounds.maximum is not None
            and (plan.mode is not OperationMode.UPDATE or plan.maximum_history)
        ):
            raise AssertionError("normal existing-history call must remain a bounded update")

        run_id = self._run_id_factory(series_id)
        started = self._utc_now()
        fetched_rows = 0
        accepted_rows = 0
        requested_start = plan.filter_start or plan.request_start
        _LOGGER.debug(
            "series ingestion started provider=%s series=%s mode=%s start=%s end=%s "
            "maximum_history=%s",
            contract.provider.value,
            contract.series_id,
            plan.mode.value,
            requested_start,
            plan.request_end,
            plan.maximum_history,
        )
        try:
            incoming = provider.fetch(contract, self._provider_request(plan))
            fetched_rows = incoming.height
            accepted = self._enforce_window(incoming, plan)
            accepted_rows = accepted.height
            prepared = self._uow.prepare(contract, accepted)
            completed = self._utc_now()
            success_run = self._success_run(
                run_id=run_id,
                contract=contract,
                plan=plan,
                started=started,
                completed=completed,
                fetched_rows=fetched_rows,
                accepted_rows=accepted_rows,
                prepared=prepared,
            )
            prior = self._uow.state(contract)
            next_state = advance_state(
                prior,
                plan,
                committed_at_utc=completed,
                authoritative_bounds=prepared.post_bounds,
                fetched_rows=fetched_rows,
                accepted_rows=accepted_rows,
                changed_rows=prepared.diff.inserts.height + prepared.diff.revisions.height,
                durable_bronze=True,
                durable_success_manifest=True,
            )
            with self._uow_write_lock:
                self._uow.commit_success(prepared, success_run, next_state)
            _LOGGER.debug(
                "series ingestion committed provider=%s series=%s fetched=%d accepted=%d "
                "inserted=%d revised=%d partitions=%d",
                contract.provider.value,
                contract.series_id,
                fetched_rows,
                accepted_rows,
                prepared.diff.inserts.height,
                prepared.diff.revisions.height,
                prepared.written_partitions,
            )
            return SeriesRunResult(
                series_id=series_id,
                provider=contract.provider,
                run_id=run_id,
                mode=plan.mode,
                request_start=requested_start,
                request_end=plan.request_end,
                maximum_history=plan.maximum_history,
                inserted_rows=prepared.diff.inserts.height,
                revised_rows=prepared.diff.revisions.height,
                written_partitions=prepared.written_partitions,
            )
        except Exception as exc:
            _LOGGER.debug(
                "series ingestion failed provider=%s series=%s category=%s",
                contract.provider.value,
                contract.series_id,
                type(exc).__name__,
            )
            failed = IngestionRunRecord(
                run_id=run_id,
                provider=contract.provider,
                series_id=contract.series_id,
                mode=RunMode(plan.mode.value),
                requested_start=requested_start,
                requested_end=plan.request_end,
                fetched_rows=fetched_rows,
                accepted_rows=accepted_rows,
                inserted_rows=0,
                revised_rows=0,
                written_partitions=0,
                status=RunStatus.FAILED,
                started_at_utc=started,
                completed_at_utc=self._utc_now(),
                error_category=type(exc).__name__,
                error_message="series ingestion failed",
            )
            with self._uow_write_lock:
                self._uow.record_failure(failed)
            raise

    def run_many(
        self,
        series_ids: Sequence[str],
        *,
        operation: OperationMode = OperationMode.UPDATE,
        today: date,
    ) -> BatchRunResult:
        """Run distinct series independently and preserve input order in each result group.

        Duplicate IDs are rejected before work starts because concurrent runs for the same
        series would invalidate the per-series read/prepare/commit isolation boundary.
        """
        selected = tuple(series_ids)
        if len(set(selected)) != len(selected):
            raise ValueError("run_many requires distinct series IDs")
        _LOGGER.debug("series batch started operation=%s count=%d", operation.value, len(selected))

        def run_one(series_id: str) -> _SeriesOutcome:
            try:
                return _SeriesOutcome(
                    result=self.run_series(series_id, operation=operation, today=today)
                )
            except Exception:
                return _SeriesOutcome(failed=True)

        outcomes = self._execution_policy.map(run_one, selected)
        successes: list[SeriesRunResult] = []
        for outcome in outcomes:
            if outcome.result is not None:
                successes.append(outcome.result)
        failures = tuple(
            series_id
            for series_id, outcome in zip(selected, outcomes, strict=True)
            if outcome.failed
        )
        _LOGGER.debug(
            "series batch completed operation=%s successes=%d failures=%d",
            operation.value,
            len(successes),
            len(failures),
        )
        return BatchRunResult(tuple(successes), failures)

    def _resolve_series(self, series_id: str) -> SeriesContract:
        try:
            return self._series_registry[series_id]
        except KeyError as exc:
            raise KeyError(f"unknown canonical series_id: {series_id}") from exc

    def _resolve_provider(self, provider: Provider) -> MarketDataProvider:
        try:
            return self._providers[provider]
        except KeyError as exc:
            raise KeyError(f"provider adapter not registered: {provider.value}") from exc

    @staticmethod
    def _provider_request(plan: FetchInstruction) -> ProviderRequest:
        return ProviderRequest(
            plan.mode.value,
            plan.filter_start or plan.request_start,
            plan.request_end,
            plan.maximum_history,
        )

    @staticmethod
    def _enforce_window(frame: pl.DataFrame, plan: FetchInstruction) -> pl.DataFrame:
        if plan.mode is not OperationMode.UPDATE or frame.is_empty():
            return frame
        start = plan.filter_start or plan.request_start
        if start is None:
            raise AssertionError("bounded update has no logical start")
        if "observation_date" not in frame.columns:
            raise ValueError("provider frame is missing observation_date")
        outside = frame.filter(
            ~pl.col("observation_date").is_between(start, plan.request_end, closed="both")
        )
        if outside.height:
            raise ValueError("provider returned observations outside logical request window")
        return frame

    @staticmethod
    def _success_run(
        *,
        run_id: str,
        contract: SeriesContract,
        plan: FetchInstruction,
        started: datetime,
        completed: datetime,
        fetched_rows: int,
        accepted_rows: int,
        prepared: PreparedBronze,
    ) -> IngestionRunRecord:
        return IngestionRunRecord(
            run_id=run_id,
            provider=contract.provider,
            series_id=contract.series_id,
            mode=RunMode(plan.mode.value),
            requested_start=plan.filter_start or plan.request_start,
            requested_end=plan.request_end,
            fetched_rows=fetched_rows,
            accepted_rows=accepted_rows,
            inserted_rows=prepared.diff.inserts.height,
            revised_rows=prepared.diff.revisions.height,
            written_partitions=prepared.written_partitions,
            status=RunStatus.SUCCESS,
            started_at_utc=started,
            completed_at_utc=completed,
        )

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetime")
        return value.astimezone(UTC)

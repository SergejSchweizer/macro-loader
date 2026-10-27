from __future__ import annotations

import ast
import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from application.bronze_orchestration import BronzeOrchestrator
from application.contracts import Provider, SeriesContract
from application.paths import LakePaths
from application.planner import OperationMode
from application.ports.market_data import ProviderRequest
from application.registry import SERIES_REGISTRY
from ingestion.bronze_uow import FilesystemBronzeUnitOfWork
from ingestion.operational_repository import read_runs

pytestmark = pytest.mark.integration
NOW = datetime(2026, 8, 19, 2, tzinfo=UTC)
TODAY = date(2026, 8, 19)


def _scalar_frame(
    series: SeriesContract,
    rows: list[tuple[date, float]],
    *,
    fetched_at: datetime,
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "series_id": [series.series_id for _ in rows],
            "provider": [series.provider.value for _ in rows],
            "observation_date": [day for day, _ in rows],
            "fetched_at_utc": [fetched_at for _ in rows],
            "source_id": [series.source_id for _ in rows],
            "source_url": ["https://fixture.test/source" for _ in rows],
            "value": [value for _, value in rows],
        },
        schema={
            "series_id": pl.String,
            "provider": pl.String,
            "observation_date": pl.Date,
            "fetched_at_utc": pl.Datetime("us", "UTC"),
            "source_id": pl.String,
            "source_url": pl.String,
            "value": pl.Float64,
        },
    )


class FakeFredProvider:
    provider = Provider.FRED

    def __init__(self) -> None:
        self.requests: list[tuple[str, ProviderRequest]] = []
        self.responses: dict[str, list[list[tuple[date, float]]]] = {}
        self.fail_series: set[str] = set()
        self._fetch_count = 0

    def queue(self, series_id: str, *responses: list[tuple[date, float]]) -> None:
        self.responses.setdefault(series_id, []).extend(responses)

    def fetch(self, series: SeriesContract, request: ProviderRequest) -> pl.DataFrame:
        self.requests.append((series.series_id, request))
        if series.series_id in self.fail_series:
            raise RuntimeError("fixture provider failure SECRET")
        queued = self.responses.get(series.series_id, [])
        rows = queued.pop(0) if queued else []
        self._fetch_count += 1
        return _scalar_frame(
            series,
            rows,
            fetched_at=NOW + timedelta(seconds=self._fetch_count),
        )


def _run_ids() -> Callable[[str], str]:
    counter = 0

    def next_id(series_id: str) -> str:
        nonlocal counter
        counter += 1
        return f"run-{counter:03d}-{series_id}"

    return next_id


def _service(
    tmp_path: Path,
    provider: FakeFredProvider,
    *,
    fault: Callable[[str], None] | None = None,
) -> tuple[BronzeOrchestrator, FilesystemBronzeUnitOfWork, LakePaths]:
    paths = LakePaths(tmp_path / "lake")
    uow = FilesystemBronzeUnitOfWork(paths, secrets=("SECRET",), fault_injector=fault)
    service = BronzeOrchestrator(
        series_registry=SERIES_REGISTRY,
        providers={Provider.FRED: provider},
        unit_of_work=uow,
        clock=lambda: NOW,
        run_id_factory=_run_ids(),
    )
    return service, uow, paths


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_canonical_delta_bootstrap_update_noop_and_revision(tmp_path: Path) -> None:
    provider = FakeFredProvider()
    provider.queue(
        "us_10y",
        [(date(2000, 1, 3), 6.5), (date(2026, 8, 18), 4.1)],
        [(date(2026, 8, 11), 4.0), (date(2026, 8, 19), 4.2)],
        [(date(2026, 8, 19), 4.2)],
        [(date(2026, 8, 19), 9.9)],
    )
    service, uow, paths = _service(tmp_path, provider)

    bootstrap = service.run_series("us_10y", today=TODAY)
    assert bootstrap.mode is OperationMode.BOOTSTRAP
    assert provider.requests[0][1].operation == "bootstrap"
    assert provider.requests[0][1].maximum_history
    assert uow.bounds(SERIES_REGISTRY["us_10y"]).minimum == date(2000, 1, 3)

    update = service.run_series("us_10y", today=TODAY)
    assert update.mode is OperationMode.UPDATE
    request = provider.requests[1][1]
    assert request.logical_start == date(2026, 8, 11)
    assert request.logical_end == TODAY
    assert not request.maximum_history
    assert request.logical_start != date(2000, 1, 3)

    august = paths.bronze_month(Provider.FRED, "us_10y", TODAY)
    before_hash = _sha(august)
    before_mtime = august.stat().st_mtime_ns
    noop = service.run_series("us_10y", today=TODAY)
    assert noop.inserted_rows == 0
    assert noop.revised_rows == 0
    assert noop.written_partitions == 0
    assert _sha(august) == before_hash
    assert august.stat().st_mtime_ns == before_mtime

    revision = service.run_series("us_10y", today=TODAY)
    assert revision.revised_rows == 1
    assert revision.written_partitions == 1
    assert (
        pl.read_parquet(august).filter(pl.col("observation_date") == TODAY).item(0, "value") == 9.9
    )
    runs = read_runs(paths.ingestion_runs())
    assert [run.requested_start for run in runs[-3:]] == [
        date(2026, 8, 11),
        date(2026, 8, 12),
        date(2026, 8, 12),
    ]


def test_explicit_reconcile_only_and_bootstrap_guard(tmp_path: Path) -> None:
    provider = FakeFredProvider()
    provider.queue(
        "us_10y",
        [(date(2020, 1, 1), 1.0)],
        [(date(2010, 1, 1), 2.0), (date(2020, 1, 1), 1.1)],
    )
    service, _, _ = _service(tmp_path, provider)
    service.run_series("us_10y", today=TODAY)
    with pytest.raises(ValueError, match="explicit bootstrap"):
        service.run_series("us_10y", operation=OperationMode.BOOTSTRAP, today=TODAY)
    reconciled = service.run_series("us_10y", operation=OperationMode.RECONCILE, today=TODAY)
    assert reconciled.mode is OperationMode.RECONCILE
    assert provider.requests[-1][1].operation == "reconcile"
    assert provider.requests[-1][1].maximum_history
    assert provider.requests[-1][1].logical_start is None


def test_new_euro_hy_series_keeps_history_outside_short_source_update(tmp_path: Path) -> None:
    provider = FakeFredProvider()
    provider.queue(
        "euro_hy_yield",
        [(date(2023, 10, 27), 7.83), (date(2026, 8, 18), 5.46)],
        [(date(2026, 8, 11), 5.50), (TODAY, 5.53)],
    )
    service, _, paths = _service(tmp_path, provider)

    first = service.run_series("euro_hy_yield", today=TODAY)
    assert first.mode is OperationMode.BOOTSTRAP
    update = service.run_series("euro_hy_yield", today=TODAY)
    assert update.mode is OperationMode.UPDATE
    assert provider.requests[-1][1].logical_start == date(2026, 8, 11)
    assert provider.requests[-1][1].logical_end == TODAY

    bronze_files = sorted(
        (paths.root / "bronze" / "provider=fred" / "series=euro_hy_yield").glob(
            "year=*/month=*/data.parquet"
        )
    )
    bronze = pl.concat([pl.read_parquet(path) for path in bronze_files]).sort("observation_date")
    assert bronze.get_column("observation_date").min() == date(2023, 10, 27)
    assert bronze.get_column("observation_date").to_list() == [
        date(2023, 10, 27),
        date(2026, 8, 11),
        date(2026, 8, 18),
        TODAY,
    ]


@pytest.mark.parametrize("fault_stage", ["after_bronze", "after_run", "after_state"])
def test_barrier_failure_rolls_back_authoritative_data_and_state(
    tmp_path: Path, fault_stage: str
) -> None:
    provider = FakeFredProvider()
    provider.queue("us_10y", [(date(2026, 8, 18), 4.1)])
    base_service, _, paths = _service(tmp_path, provider)
    base_service.run_series("us_10y", today=TODAY)
    august = paths.bronze_month(Provider.FRED, "us_10y", TODAY)
    bronze_before = _sha(august)
    state_before = paths.ingestion_state().read_bytes()

    provider.queue("us_10y", [(date(2026, 8, 19), 8.8)])

    def fail(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError("barrier failure")

    failing_service, _, _ = _service(tmp_path, provider, fault=fail)
    with pytest.raises(RuntimeError, match="barrier failure"):
        failing_service.run_series("us_10y", today=TODAY)
    assert _sha(august) == bronze_before
    assert paths.ingestion_state().read_bytes() == state_before
    failed = read_runs(paths.ingestion_runs())[-1]
    assert failed.status.value == "failed"
    assert failed.inserted_rows == 0
    assert failed.revised_rows == 0
    assert failed.written_partitions == 0
    assert "SECRET" not in (failed.error_message or "")


def test_multi_series_failure_isolation_and_safe_failure_record(tmp_path: Path) -> None:
    provider = FakeFredProvider()
    provider.queue("us_2y", [(date(2026, 8, 19), 3.3)])
    provider.fail_series.add("us_10y")
    service, uow, paths = _service(tmp_path, provider)
    result = service.run_many(["us_2y", "us_10y"], today=TODAY)
    assert [item.series_id for item in result.successes] == ["us_2y"]
    assert result.failures == ("us_10y",)
    assert uow.bounds(SERIES_REGISTRY["us_2y"]).maximum == TODAY
    assert uow.bounds(SERIES_REGISTRY["us_10y"]).maximum is None
    runs = read_runs(paths.ingestion_runs())
    assert {run.series_id: run.status.value for run in runs} == {
        "us_2y": "success",
        "us_10y": "failed",
    }
    assert "SECRET" not in (runs[-1].error_message or "")


def test_application_orchestrator_has_no_provider_implementation_or_http_imports() -> None:
    tree = ast.parse(Path("application/bronze_orchestration.py").read_text())
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("ingestion")
        for node in imports
    )
    source = Path("application/bronze_orchestration.py").read_text()
    for concrete in (
        "CboeProvider",
        "StoxxProvider",
        "YahooMoveProvider",
        "EcbProvider",
        "FredProvider",
    ):
        assert concrete not in source


def test_unknown_registry_entries_and_naive_clock_fail(tmp_path: Path) -> None:
    provider = FakeFredProvider()
    service, _, _ = _service(tmp_path, provider)
    with pytest.raises(KeyError, match="unknown canonical"):
        service.run_series("unknown", today=TODAY)
    missing_provider = BronzeOrchestrator(
        series_registry=SERIES_REGISTRY,
        providers={},
        unit_of_work=FilesystemBronzeUnitOfWork(LakePaths(tmp_path / "missing")),
        clock=lambda: NOW,
        run_id_factory=_run_ids(),
    )
    with pytest.raises(KeyError, match="not registered"):
        missing_provider.run_series("us_10y", today=TODAY)
    provider.queue("us_10y", [(TODAY, 1.0)])
    naive = BronzeOrchestrator(
        series_registry=SERIES_REGISTRY,
        providers={Provider.FRED: provider},
        unit_of_work=FilesystemBronzeUnitOfWork(LakePaths(tmp_path / "naive")),
        clock=lambda: datetime(2026, 8, 19, 2),
        run_id_factory=_run_ids(),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        naive.run_series("us_10y", today=TODAY)

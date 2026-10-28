"""Filesystem implementation of the one-series Bronze Unit of Work."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl

from application.contracts import SeriesContract
from application.operational_records import IngestionRunRecord
from application.paths import LakePaths
from application.ports.bronze import PreparedBronze
from application.ports.lake import ObservationBounds
from application.state import IngestionState
from ingestion.operational_repository import upsert_run
from ingestion.parquet_repository import (
    atomic_write_parquet,
    merge_frames,
    observation_bounds,
    read_monthly,
)
from ingestion.state_repository import read_states, upsert_state

FaultInjector = Callable[[str], None]
_KEY = ("provider", "series_id", "observation_date")
_LOGGER = logging.getLogger(__name__)


def _no_fault(stage: str) -> None:
    del stage


@dataclass(frozen=True, slots=True)
class _Backup:
    path: Path
    content: bytes | None


def _backup(path: Path) -> _Backup:
    return _Backup(path, path.read_bytes() if path.is_file() else None)


def _restore(item: _Backup) -> None:
    if item.content is None:
        item.path.unlink(missing_ok=True)
        return
    item.path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=item.path.parent,
        prefix=f".{item.path.name}.",
        suffix=".rollback",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(item.content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, item.path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _post_bounds(frame: pl.DataFrame) -> ObservationBounds:
    if frame.is_empty():
        return ObservationBounds(None, None)
    minimum = frame.get_column("observation_date").min()
    maximum = frame.get_column("observation_date").max()
    if not isinstance(minimum, date) or not isinstance(maximum, date):
        raise TypeError("Bronze observation_date must contain Date values")
    return ObservationBounds(minimum, maximum)


def _changed_months(diff_frame: pl.DataFrame) -> tuple[tuple[int, int], ...]:
    if diff_frame.is_empty():
        return ()
    values = diff_frame.get_column("observation_date").to_list()
    if any(not isinstance(value, date) for value in values):
        raise TypeError("Bronze observation_date must contain Date values")
    return tuple(sorted({(value.year, value.month) for value in values}))


def _stable_incoming(existing: pl.DataFrame, incoming: pl.DataFrame) -> pl.DataFrame:
    """Preserve old fetched_at for logically unchanged equal-key observations."""
    if existing.is_empty() or incoming.is_empty() or "fetched_at_utc" not in incoming.columns:
        return incoming
    meaningful = [column for column in incoming.columns if column not in {*_KEY, "fetched_at_utc"}]
    old_columns = list(_KEY) + meaningful + ["fetched_at_utc"]
    old = existing.select(old_columns).rename(
        {column: f"__old_{column}" for column in [*meaningful, "fetched_at_utc"]}
    )
    joined = incoming.join(old, on=list(_KEY), how="left")
    exists = pl.col("__old_fetched_at_utc").is_not_null()
    same = (
        pl.all_horizontal(
            [pl.col(column).eq_missing(pl.col(f"__old_{column}")) for column in meaningful]
        )
        if meaningful
        else pl.lit(True)
    )
    return joined.with_columns(
        pl.when(exists & same)
        .then(pl.col("__old_fetched_at_utc"))
        .otherwise(pl.col("fetched_at_utc"))
        .alias("fetched_at_utc")
    ).select(incoming.columns)


class FilesystemBronzeUnitOfWork:
    """Repository/UoW adapter with compensation around the multi-file commit boundary."""

    def __init__(
        self,
        paths: LakePaths,
        *,
        secrets: tuple[str, ...] = (),
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._paths = paths
        self._secrets = secrets
        self._fault = fault_injector if fault_injector is not None else _no_fault

    def _series_paths(self, contract: SeriesContract) -> tuple[Path, ...]:
        root = (
            self._paths.root
            / "bronze"
            / f"provider={contract.provider.value}"
            / f"series={contract.series_id}"
        )
        if not root.exists():
            return ()
        return tuple(sorted(root.glob("year=*/month=*/data.parquet")))

    def bounds(self, contract: SeriesContract) -> ObservationBounds:
        return observation_bounds(self._series_paths(contract))

    def state(self, contract: SeriesContract) -> IngestionState:
        states = read_states(self._paths.ingestion_state())
        for state in states:
            if (state.provider, state.series_id) == (contract.provider, contract.series_id):
                return state
        return IngestionState(contract.provider, contract.series_id)

    def prepare(self, contract: SeriesContract, incoming: pl.DataFrame) -> PreparedBronze:
        existing = read_monthly(self._series_paths(contract), sort_by=_KEY)
        existing_rows = existing.height
        if not existing.columns:
            existing = incoming.head(0)
        normalized = _stable_incoming(existing, incoming)
        merged, diff = merge_frames(existing, normalized, key=_KEY)
        months = _changed_months(diff.changed)
        _LOGGER.debug(
            "Bronze prepare completed provider=%s series=%s existing=%d incoming=%d "
            "inserted=%d revised=%d partitions=%d",
            contract.provider.value,
            contract.series_id,
            existing_rows,
            incoming.height,
            diff.inserts.height,
            diff.revisions.height,
            len(months),
        )
        return PreparedBronze(
            contract=contract,
            merged=merged,
            diff=diff,
            post_bounds=_post_bounds(merged),
            written_partitions=len(months),
        )

    def commit_success(
        self,
        prepared: PreparedBronze,
        run: IngestionRunRecord,
        state: IngestionState,
    ) -> None:
        months = _changed_months(prepared.diff.changed)
        _LOGGER.debug(
            "Bronze commit started provider=%s series=%s partitions=%d",
            prepared.contract.provider.value,
            prepared.contract.series_id,
            len(months),
        )
        bronze_targets = [
            self._paths.bronze_month(
                prepared.contract.provider,
                prepared.contract.series_id,
                date(year, month, 1),
            )
            for year, month in months
        ]
        tracked = [*bronze_targets, self._paths.ingestion_runs(), self._paths.ingestion_state()]
        backups = [_backup(path) for path in tracked]
        try:
            for year, month in months:
                destination = self._paths.bronze_month(
                    prepared.contract.provider,
                    prepared.contract.series_id,
                    date(year, month, 1),
                )
                month_frame = prepared.merged.filter(
                    (pl.col("observation_date").dt.year() == year)
                    & (pl.col("observation_date").dt.month() == month)
                )
                atomic_write_parquet(month_frame, destination)
            self._fault("after_bronze")
            upsert_run(self._paths.ingestion_runs(), run, secrets=self._secrets)
            self._fault("after_run")
            upsert_state(self._paths.ingestion_state(), state)
            self._fault("after_state")
            _LOGGER.debug(
                "Bronze commit completed provider=%s series=%s partitions=%d",
                prepared.contract.provider.value,
                prepared.contract.series_id,
                len(months),
            )
        except BaseException:
            _LOGGER.debug(
                "Bronze commit rollback started provider=%s series=%s targets=%d",
                prepared.contract.provider.value,
                prepared.contract.series_id,
                len(backups),
            )
            for item in reversed(backups):
                _restore(item)
            _LOGGER.debug(
                "Bronze commit rollback completed provider=%s series=%s",
                prepared.contract.provider.value,
                prepared.contract.series_id,
            )
            raise

    def record_failure(self, run: IngestionRunRecord) -> None:
        _LOGGER.debug(
            "Bronze failure recorded provider=%s series=%s category=%s",
            run.provider.value,
            run.series_id,
            run.error_category,
        )
        upsert_run(self._paths.ingestion_runs(), run, secrets=self._secrets)

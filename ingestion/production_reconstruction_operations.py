"""Operational adapter for one explicitly authorized production reconstruction."""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import psycopg

from application.daily_pipeline import DailyMedallionPipeline
from application.postgres_conformance import PostgresConformanceReport, PostgresConformanceVerifier
from application.postgres_sync import GoldSyncResult, select_current_sync_record
from application.postgres_sync_service import GoldPostgresDeltaSync
from ingestion.gold_catalog_repository import GoldCatalogRepository
from ingestion.postgres_gold_repository import (
    POSTGRES_ADVISORY_LOCK_NAMESPACE,
    POSTGRES_HOST,
    POSTGRES_PORT,
    PostgresAdminConfig,
    PostgresGoldSchemaReconstructor,
    PostgresSyncConfig,
)

_MAINTENANCE_MARKER = ".maintenance/macro-loader-reconstruction"
_RUNNER_LOCK = ".locks/macro-loader-daily.lock"
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GuardedProductionReconstructionOperations:
    """Filesystem and PostgreSQL realization of the reconstruction application port."""

    pipeline: DailyMedallionPipeline
    sync: GoldPostgresDeltaSync
    verifier: PostgresConformanceVerifier
    reconstructor: PostgresGoldSchemaReconstructor
    runtime_config: PostgresSyncConfig
    admin_config: PostgresAdminConfig
    catalog: GoldCatalogRepository
    lake_root: Path
    project_root: Path
    today: date
    daily_runner: Path
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)
    _runner_handle: object | None = field(default=None, init=False, repr=False)
    _maintenance_connection: psycopg.Connection[tuple[object, ...]] | None = field(
        default=None, init=False, repr=False
    )

    def disable_scheduling(self) -> None:
        with self._log_operation("disable_scheduling"):
            marker = self.project_root / _MAINTENANCE_MARKER
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch(exist_ok=True)

    def acquire_locks(self) -> None:
        with self._log_operation("acquire_locks"):
            lock_path = self.project_root / _RUNNER_LOCK
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("w", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise RuntimeError("Daily runner lock is already held") from None
            self._runner_handle = handle
            try:
                connection = psycopg.connect(
                    host=self.admin_config.host,
                    port=self.admin_config.port,
                    user=self.admin_config.user,
                    dbname=self.admin_config.database,
                    password=self.admin_config.password,
                    connect_timeout=self.admin_config.timeout_policy.connect_timeout_seconds,
                    application_name=self.admin_config.timeout_policy.application_name,
                    autocommit=True,
                )
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (_lock_key(),))
                    row = cursor.fetchone()
                if row != (True,):
                    connection.close()
                    raise RuntimeError("PostgreSQL maintenance lock is already held")
                self._maintenance_connection = connection
            except Exception:
                self._release_runner_lock()
                raise

    def preflight_endpoint(self) -> None:
        with self._log_operation("preflight_endpoint"):
            if (self.runtime_config.host, self.runtime_config.port) != (
                POSTGRES_HOST,
                POSTGRES_PORT,
            ):
                raise ValueError("runtime configuration does not target the production endpoint")
            if (self.admin_config.host, self.admin_config.port) != (POSTGRES_HOST, POSTGRES_PORT):
                raise ValueError("admin configuration does not target the production endpoint")
            if self._maintenance_connection is None:
                raise RuntimeError("PostgreSQL maintenance lock is not held")

    def validate_backup(self) -> None:
        with self._log_operation("validate_backup"):
            record = select_current_sync_record(self.catalog.read())
            if not record.data_path:
                raise RuntimeError("current Gold catalog record has no data path")
            required_paths = (
                self.lake_root / "gold" / "dataset=macro_features_daily" / "manifest.parquet",
                self.lake_root / "gold" / "dataset=macro_features_daily" / record.data_path,
                self.lake_root / "state" / "ingestion_state.parquet",
            )
            if any(not path.is_file() for path in required_paths):
                raise RuntimeError("required canonical lake evidence is missing")
            backup_root = self.project_root / "artifacts" / "private" / "reconstruction-backups"
            backup_dir = backup_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup_dir.mkdir(parents=True, exist_ok=False)
            lake_snapshot = backup_dir / "lake"
            shutil.copytree(self.lake_root, lake_snapshot)
            for source in required_paths:
                self._write_checksum(lake_snapshot / source.relative_to(self.lake_root))
            database_dump = backup_dir / "loader-schemas.sql"
            environment = {**os.environ, "PGPASSWORD": self.admin_config.password}
            completed = subprocess.run(
                [
                    "pg_dump",
                    "--host",
                    self.admin_config.host,
                    "--port",
                    str(self.admin_config.port),
                    "--username",
                    self.admin_config.user,
                    "--dbname",
                    self.admin_config.database,
                    "--schema=macro_loader",
                    "--schema=macro_loader_sync",
                    "--file",
                    str(database_dump),
                ],
                check=False,
                capture_output=True,
                env=environment,
            )
            if (
                completed.returncode != 0
                or not database_dump.is_file()
                or database_dump.stat().st_size == 0
            ):
                raise RuntimeError("PostgreSQL backup validation failed")
            self._write_checksum(database_dump)
            (backup_dir / "RESTORE.txt").write_text(
                "Restore loader-schemas.sql with psql using the protected administrator account.\n",
                encoding="ascii",
            )

    def reconcile(self, series_ids: Sequence[str]) -> None:
        with self._log_operation("reconcile", series_count=len(series_ids)):
            self.pipeline.reconcile(tuple(series_ids), today=self.today)

    def rebuild_silver(self, series_ids: Sequence[str]) -> None:
        with self._log_operation("rebuild_silver", series_count=len(series_ids)):
            self.pipeline.silver_build(tuple(series_ids))

    def publish_gold(self) -> None:
        with self._log_operation("publish_gold"):
            self.pipeline.gold_build()

    def recreate_schema(self) -> None:
        with self._log_operation("recreate_schema"):
            self.reconstructor.recreate()

    def publish_postgres(self) -> GoldSyncResult:
        with self._log_operation("publish_postgres"):
            result = self.sync.sync()
        logger.debug(
            "guarded reconstruction PostgreSQL publish counts inserted=%d updated=%d "
            "deleted=%d unchanged=%d",
            result.inserted,
            result.updated,
            result.deleted,
            result.unchanged,
        )
        return result

    def verify_postgres(self) -> PostgresConformanceReport:
        with self._log_operation("verify_postgres"):
            report = self.verifier.verify()
        logger.debug(
            "guarded reconstruction PostgreSQL verification status=%s finding_count=%d",
            report.status,
            len(report.checks),
        )
        return report

    def replay_postgres(self) -> GoldSyncResult:
        with self._log_operation("replay_postgres"):
            result = self.sync.sync()
        logger.debug(
            "guarded reconstruction PostgreSQL replay counts inserted=%d updated=%d "
            "deleted=%d unchanged=%d",
            result.inserted,
            result.updated,
            result.deleted,
            result.unchanged,
        )
        return result

    def verify_daily_wrapper(self) -> None:
        with self._log_operation("verify_daily_wrapper"):
            completed = subprocess.run(
                [str(self.daily_runner)],
                cwd=self.project_root,
                check=False,
                capture_output=True,
                env={**os.environ, "MACRO_LOADER_DAILY_VERIFY_ONLY": "true"},
            )
            if completed.returncode != 0:
                raise RuntimeError("guarded Daily wrapper verification failed")

    def enable_scheduling(self) -> None:
        with self._log_operation("enable_scheduling"):
            marker = self.project_root / _MAINTENANCE_MARKER
            if not marker.is_file():
                raise RuntimeError("maintenance marker is missing")
            marker.unlink()
            self._release_postgres_lock()
            self._release_runner_lock()

    @contextmanager
    def _log_operation(self, operation: str, *, series_count: int | None = None) -> Iterator[None]:
        started_at = self.monotonic()
        if series_count is None:
            logger.debug("guarded reconstruction operation started operation=%s", operation)
        else:
            logger.debug(
                "guarded reconstruction operation started operation=%s series_count=%d",
                operation,
                series_count,
            )
        try:
            yield
        except Exception as exc:
            logger.debug(
                "guarded reconstruction operation failed operation=%s error_type=%s elapsed_ms=%d",
                operation,
                type(exc).__name__,
                round((self.monotonic() - started_at) * 1000),
            )
            raise
        else:
            logger.debug(
                "guarded reconstruction operation completed operation=%s elapsed_ms=%d",
                operation,
                round((self.monotonic() - started_at) * 1000),
            )

    def _release_runner_lock(self) -> None:
        if self._runner_handle is not None:
            handle = self._runner_handle
            assert hasattr(handle, "close")
            handle.close()
            self._runner_handle = None

    def _release_postgres_lock(self) -> None:
        if self._maintenance_connection is not None:
            self._maintenance_connection.close()
            self._maintenance_connection = None

    @staticmethod
    def _write_checksum(path: Path) -> None:
        digest = sha256(path.read_bytes()).hexdigest()
        path.with_suffix(path.suffix + ".sha256").write_text(
            f"{digest}  {path.name}\n", encoding="ascii"
        )


def _lock_key() -> str:
    return f"{POSTGRES_ADVISORY_LOCK_NAMESPACE}:maintenance"

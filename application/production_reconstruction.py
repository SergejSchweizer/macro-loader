"""Fail-closed orchestration contract for the explicit production reconstruction."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from application.postgres_conformance import PostgresConformanceReport
from application.postgres_sync import GoldSyncResult
from application.registry import SERIES_REGISTRY

_STAGES = (
    "maintenance",
    "locks",
    "endpoint",
    "backup",
    "reconcile",
    "silver",
    "gold",
    "schema",
    "publication",
    "verification",
    "replay",
    "daily_wrapper",
)
logger = logging.getLogger(__name__)
_StageResult = TypeVar("_StageResult")


@dataclass(frozen=True, slots=True)
class ProductionReconstructionReport:
    """Deterministic, credential-free evidence suitable for source control."""

    status: str
    completed_stages: tuple[str, ...]
    failed_stage: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"PASS", "FAIL"}:
            raise ValueError("reconstruction status must be PASS or FAIL")
        if len(set(self.completed_stages)) != len(self.completed_stages):
            raise ValueError("reconstruction stages must be unique")
        if any(stage not in _STAGES for stage in self.completed_stages):
            raise ValueError("reconstruction report contains an unknown stage")
        if self.completed_stages != _STAGES[: len(self.completed_stages)]:
            raise ValueError("reconstruction stages must be an ordered prefix")
        if self.status == "PASS":
            if self.completed_stages != _STAGES or self.failed_stage is not None:
                raise ValueError("PASS report requires every stage and no failure")
        elif self.failed_stage not in _STAGES:
            raise ValueError("reconstruction report contains an unknown failed stage")
        elif self.failed_stage in self.completed_stages:
            raise ValueError("FAIL report requires one uncompleted known stage")

    def as_json(self) -> str:
        return json.dumps(
            {
                "completed_stages": list(self.completed_stages),
                "failed_stage": self.failed_stage,
                "status": self.status,
            },
            ensure_ascii=True,
            sort_keys=True,
        )


class ProductionReconstructionOperations(Protocol):
    """Infrastructure boundary for guarded reconstruction stages."""

    def disable_scheduling(self) -> None: ...

    def acquire_locks(self) -> None: ...

    def preflight_endpoint(self) -> None: ...

    def validate_backup(self) -> None: ...

    def reconcile(self, series_ids: Sequence[str]) -> None: ...

    def rebuild_silver(self, series_ids: Sequence[str]) -> None: ...

    def publish_gold(self) -> None: ...

    def recreate_schema(self) -> None: ...

    def publish_postgres(self) -> GoldSyncResult: ...

    def verify_postgres(self) -> PostgresConformanceReport: ...

    def replay_postgres(self) -> GoldSyncResult: ...

    def verify_daily_wrapper(self) -> None: ...

    def enable_scheduling(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProductionReconstruction:
    """Run all destructive steps only after maintenance, lock, and backup gates pass."""

    operations: ProductionReconstructionOperations
    series_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.series_ids != tuple(SERIES_REGISTRY):
            raise ValueError("production reconstruction requires every registered series in order")

    def run(self) -> ProductionReconstructionReport:
        completed: list[str] = []
        logger.debug("production reconstruction started series_count=%d", len(self.series_ids))
        try:
            self._run_stage("maintenance", self.operations.disable_scheduling, completed)
            self._run_stage("locks", self.operations.acquire_locks, completed)
            self._run_stage("endpoint", self.operations.preflight_endpoint, completed)
            self._run_stage("backup", self.operations.validate_backup, completed)
            self._run_stage(
                "reconcile", lambda: self.operations.reconcile(self.series_ids), completed
            )
            self._run_stage(
                "silver", lambda: self.operations.rebuild_silver(self.series_ids), completed
            )
            self._run_stage("gold", self.operations.publish_gold, completed)
            self._run_stage("schema", self.operations.recreate_schema, completed)
            self._run_stage("publication", self.operations.publish_postgres, completed)
            logger.debug("production reconstruction stage started stage=verification")
            verification = self.operations.verify_postgres()
            if verification.status != "PASS":
                return self._failed_report(completed, "verification")
            completed.append("verification")
            logger.debug(
                "production reconstruction stage completed stage=verification "
                "completed_stage_count=%d",
                len(completed),
            )
            logger.debug("production reconstruction stage started stage=replay")
            replay = self.operations.replay_postgres()
            if replay.inserted or replay.updated or replay.deleted:
                return self._failed_report(completed, "replay")
            completed.append("replay")
            logger.debug(
                "production reconstruction stage completed stage=replay completed_stage_count=%d",
                len(completed),
            )
            self._run_stage("daily_wrapper", self.operations.verify_daily_wrapper, completed)
        except Exception as exc:
            failed_stage = _STAGES[len(completed)]
            logger.debug(
                "production reconstruction failed stage=%s error_type=%s completed_stage_count=%d",
                failed_stage,
                type(exc).__name__,
                len(completed),
            )
            return ProductionReconstructionReport("FAIL", tuple(completed), failed_stage)

        logger.debug("production reconstruction scheduling release started")
        try:
            self.operations.enable_scheduling()
        except Exception as exc:
            logger.debug(
                "production reconstruction scheduling release failed error_type=%s",
                type(exc).__name__,
            )
            raise
        report = ProductionReconstructionReport("PASS", tuple(completed))
        logger.debug("production reconstruction passed completed_stage_count=%d", len(completed))
        return report

    def _run_stage(
        self,
        stage: str,
        operation: Callable[[], _StageResult],
        completed: list[str],
    ) -> _StageResult:
        logger.debug("production reconstruction stage started stage=%s", stage)
        try:
            result = operation()
        except Exception as exc:
            logger.debug(
                "production reconstruction stage failed stage=%s error_type=%s",
                stage,
                type(exc).__name__,
            )
            raise
        completed.append(stage)
        logger.debug(
            "production reconstruction stage completed stage=%s completed_stage_count=%d",
            stage,
            len(completed),
        )
        return result

    @staticmethod
    def _failed_report(
        completed: Sequence[str], failed_stage: str
    ) -> ProductionReconstructionReport:
        logger.debug(
            "production reconstruction failed stage=%s completed_stage_count=%d",
            failed_stage,
            len(completed),
        )
        return ProductionReconstructionReport("FAIL", tuple(completed), failed_stage)

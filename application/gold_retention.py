"""Safe Gold retention Mark-and-Sweep policy and orchestration."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from application.gold_catalog import GoldBuildStatus, GoldCatalogRecord
from application.gold_publication import GoldCatalogPort, GoldMaterializedViewPort

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Gold retention clock must be timezone-aware")
    return value.astimezone(UTC)


class GoldBundleSweeperPort(Protocol):
    def sweep(self, build_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class GoldRetentionPolicy:
    """Strategy selecting the oldest prunable complete builds per semantic version pair."""

    retain_successful_builds: int = 5

    def __post_init__(self) -> None:
        if self.retain_successful_builds < 1:
            raise ValueError("retain_successful_builds must be at least 1")

    def select_for_mark(self, records: Sequence[GoldCatalogRecord]) -> tuple[str, ...]:
        groups: dict[tuple[int, int], list[GoldCatalogRecord]] = defaultdict(list)
        for record in records:
            if (
                record.status is GoldBuildStatus.COMPLETE
                and record.pruned_at_utc is None
                and record.artifact_paths_complete
            ):
                groups[(record.schema_version, record.feature_version)].append(record)

        selected: list[str] = []
        for key in sorted(groups):
            group = groups[key]
            excess = max(0, len(group) - self.retain_successful_builds)
            if excess == 0:
                continue
            candidates = [record for record in group if not record.current]
            candidates.sort(key=_completion_key)
            if len(candidates) < excess:
                raise ValueError("Gold retention cannot satisfy policy without pruning current")
            selected.extend(record.build_id for record in candidates[:excess])
        return tuple(selected)


def _completion_key(record: GoldCatalogRecord) -> tuple[datetime, str]:
    if record.completed_at_utc is None:
        raise ValueError("complete Gold retention candidate requires completed_at_utc")
    return record.completed_at_utc, record.build_id


@dataclass(frozen=True, slots=True)
class GoldRetentionResult:
    marked_build_ids: tuple[str, ...]
    swept_build_ids: tuple[str, ...]


@dataclass(slots=True)
class GoldRetentionService:
    """Unit of Work that tombstones catalog rows before any physical bundle deletion."""

    catalog: GoldCatalogPort
    sweeper: GoldBundleSweeperPort
    views: GoldMaterializedViewPort
    policy: GoldRetentionPolicy = GoldRetentionPolicy()
    clock: Clock = _system_utc_now

    def run(self) -> GoldRetentionResult:
        records = self.catalog.read()
        mark_ids = self.policy.select_for_mark(records)
        logger.debug(
            "gold retention started catalog_rows=%d mark_count=%d",
            len(records),
            len(mark_ids),
        )
        if mark_ids:
            marked_at = _utc(self.clock())
            mark_set = set(mark_ids)
            records = [
                self._mark(record, marked_at) if record.build_id in mark_set else record
                for record in records
            ]
            self.catalog.replace(records)
            self.views.refresh(records)
            logger.debug("gold retention marks committed marked_count=%d", len(mark_ids))

        marked = [
            record
            for record in records
            if record.status is GoldBuildStatus.COMPLETE
            and not record.current
            and record.pruned_at_utc is not None
            and record.data_path is None
            and record.build_manifest_path is None
            and record.plot_path is None
        ]
        if marked and not mark_ids:
            self.views.refresh(records)

        swept: list[str] = []
        for record in sorted(marked, key=_marked_key):
            try:
                self.sweeper.sweep(record.build_id)
            except Exception as error:
                logger.debug(
                    "gold retention sweep failed build_id=%s error_type=%s",
                    record.build_id,
                    type(error).__name__,
                )
                raise
            swept.append(record.build_id)
        result = GoldRetentionResult(marked_build_ids=mark_ids, swept_build_ids=tuple(swept))
        logger.debug(
            "gold retention completed marked_count=%d swept_count=%d",
            len(result.marked_build_ids),
            len(result.swept_build_ids),
        )
        return result

    @staticmethod
    def _mark(record: GoldCatalogRecord, marked_at: datetime) -> GoldCatalogRecord:
        if record.status is not GoldBuildStatus.COMPLETE:
            raise ValueError("only complete Gold builds may be marked for retention")
        if record.current:
            raise ValueError("current Gold build cannot be marked for retention")
        if record.pruned_at_utc is not None:
            raise ValueError("already-pruned Gold build cannot be marked again")
        if not record.artifact_paths_complete:
            raise ValueError("Gold retention mark requires complete artifact paths")
        return replace(
            record,
            current=False,
            data_path=None,
            build_manifest_path=None,
            plot_path=None,
            pruned_at_utc=marked_at,
        )


def _marked_key(record: GoldCatalogRecord) -> tuple[datetime, str]:
    if record.pruned_at_utc is None:
        raise ValueError("marked Gold retention row requires pruned_at_utc")
    return record.pruned_at_utc, record.build_id

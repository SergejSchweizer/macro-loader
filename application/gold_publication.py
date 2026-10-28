"""Gold publication State Machine and Unit-of-Work orchestration."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

import polars as pl

from application.gold_catalog import GoldBuildStatus, GoldCatalogRecord
from application.gold_frame import GOLD_FEATURE_VERSION, GOLD_SCHEMA_VERSION, SilverInputSignature

_DATASET_ID = "macro_features_daily"
logger = logging.getLogger(__name__)
Clock = Callable[[], datetime]
EventSink = Callable[[str], None]


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


def _no_event(event: str) -> None:
    del event


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Gold publication clock must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class GoldPublicationBundle:
    """Validated immutable bundle metadata accepted by the publication state machine."""

    build_id: str
    completed_at_utc: datetime
    schema_version: int
    feature_version: int
    row_count: int
    min_timestamp: datetime | None
    max_timestamp: datetime | None
    data_path: str
    build_manifest_path: str
    plot_path: str


class GoldCatalogPort(Protocol):
    def read(self) -> list[GoldCatalogRecord]: ...

    def replace(self, records: list[GoldCatalogRecord]) -> None: ...


class GoldBundlePort(Protocol):
    def next_build_id(self) -> str: ...

    def create_and_validate(
        self,
        frame: pl.DataFrame,
        *,
        build_id: str,
        started_at_utc: datetime,
        inputs: tuple[SilverInputSignature, ...],
    ) -> GoldPublicationBundle: ...


class GoldMaterializedViewPort(Protocol):
    def refresh(self, records: Sequence[GoldCatalogRecord]) -> None: ...


class GoldMirrorPort(Protocol):
    def sync(self) -> None: ...


@dataclass(slots=True)
class GoldPublisher:
    """State Machine whose catalog replacement is the sole publication commit point."""

    catalog: GoldCatalogPort
    bundle: GoldBundlePort
    views: GoldMaterializedViewPort
    mirror: GoldMirrorPort | None = None
    clock: Clock = _system_utc_now
    event_sink: EventSink = _no_event

    def reconcile(self) -> list[GoldCatalogRecord]:
        """Finalize stale non-current building rows and rebuild root materialized views."""
        records = self.catalog.read()
        logger.debug("gold publication reconciliation started catalog_rows=%d", len(records))
        if any(record.current and record.status is GoldBuildStatus.BUILDING for record in records):
            raise ValueError("current Gold building row is invalid and requires manual recovery")
        stale = [
            record
            for record in records
            if record.status is GoldBuildStatus.BUILDING and not record.current
        ]
        if stale:
            logger.debug("gold publication stale attempts found count=%d", len(stale))
            failed_at = _utc(self.clock())
            stale_ids = {record.build_id for record in stale}
            records = [
                replace(
                    record,
                    status=GoldBuildStatus.FAILED,
                    current=False,
                    completed_at_utc=failed_at,
                    min_timestamp=None,
                    max_timestamp=None,
                    row_count=None,
                    data_path=None,
                    build_manifest_path=None,
                    plot_path=None,
                    pruned_at_utc=None,
                )
                if record.build_id in stale_ids
                else record
                for record in records
            ]
            self.catalog.replace(records)
            self.event_sink("catalog:stale-building-failed")
        self.views.refresh(records)
        self.event_sink("views:reconciled")
        logger.debug("gold publication reconciliation completed catalog_rows=%d", len(records))
        return records

    def publish(
        self,
        frame: pl.DataFrame,
        *,
        inputs: tuple[SilverInputSignature, ...] = (),
    ) -> GoldCatalogRecord:
        """Build, physically validate, atomically promote, then refresh materialized views."""
        logger.debug(
            "gold publication started row_count=%d input_count=%d", frame.height, len(inputs)
        )
        records = self.reconcile()
        build_id = self.bundle.next_build_id()
        started_at = _utc(self.clock())
        building = GoldCatalogRecord(
            dataset_id=_DATASET_ID,
            build_id=build_id,
            status=GoldBuildStatus.BUILDING,
            current=False,
            started_at_utc=started_at,
            completed_at_utc=None,
            schema_version=GOLD_SCHEMA_VERSION,
            feature_version=GOLD_FEATURE_VERSION,
            min_timestamp=None,
            max_timestamp=None,
            row_count=None,
            data_path=None,
            build_manifest_path=None,
            plot_path=None,
            pruned_at_utc=None,
        )
        self.catalog.replace([*records, building])
        self.event_sink("catalog:building")
        logger.debug("gold publication attempt registered build_id=%s", build_id)
        registered = [*records, building]
        try:
            self.views.refresh(registered)
            self.event_sink("views:building")
            candidate = self.bundle.create_and_validate(
                frame,
                build_id=build_id,
                started_at_utc=started_at,
                inputs=inputs,
            )
            self.event_sink("bundle:validated")
            logger.debug(
                "gold publication bundle validated build_id=%s row_count=%d",
                candidate.build_id,
                candidate.row_count,
            )
            promoted = self._promoted_records(registered, candidate)
            self.catalog.replace(promoted)
            self.event_sink("catalog:promoted")
            logger.debug("gold publication catalog promoted build_id=%s", build_id)
        except BaseException as error:
            logger.debug(
                "gold publication failed before commit build_id=%s error_type=%s",
                build_id,
                type(error).__name__,
            )
            self._finalize_failed_if_uncommitted(build_id)
            raise

        current = next(record for record in promoted if record.build_id == build_id)
        try:
            self.views.refresh(promoted)
            self.event_sink("views:promoted")
            logger.debug("gold publication views refreshed build_id=%s", build_id)
        except BaseException as error:
            logger.debug(
                "gold publication view refresh failed after commit build_id=%s error_type=%s",
                build_id,
                type(error).__name__,
            )
            self.event_sink("views:promoted-failed")
            raise
        if self.mirror is not None:
            try:
                self.mirror.sync()
            except BaseException as error:
                logger.debug(
                    "gold publication mirror sync failed after commit build_id=%s error_type=%s",
                    build_id,
                    type(error).__name__,
                )
                raise
            self.event_sink("mirror:synchronized")
            logger.debug("gold publication mirror synchronized build_id=%s", build_id)
        logger.debug(
            "gold publication completed build_id=%s row_count=%d", build_id, current.row_count or 0
        )
        return current

    def _promoted_records(
        self,
        registered: Sequence[GoldCatalogRecord],
        candidate: GoldPublicationBundle,
    ) -> list[GoldCatalogRecord]:
        if candidate.schema_version != GOLD_SCHEMA_VERSION:
            raise ValueError("Gold candidate schema_version mismatch")
        if candidate.feature_version != GOLD_FEATURE_VERSION:
            raise ValueError("Gold candidate feature_version mismatch")
        building_matches = [
            record for record in registered if record.build_id == candidate.build_id
        ]
        if len(building_matches) != 1:
            raise ValueError(
                "Gold candidate build_id does not match exactly one registered attempt"
            )
        attempt = building_matches[0]
        if attempt.status is not GoldBuildStatus.BUILDING or attempt.current:
            raise ValueError("Gold candidate attempt is not a non-current building row")
        complete = replace(
            attempt,
            status=GoldBuildStatus.COMPLETE,
            current=True,
            completed_at_utc=_utc(candidate.completed_at_utc),
            schema_version=candidate.schema_version,
            feature_version=candidate.feature_version,
            min_timestamp=candidate.min_timestamp,
            max_timestamp=candidate.max_timestamp,
            row_count=candidate.row_count,
            data_path=candidate.data_path,
            build_manifest_path=candidate.build_manifest_path,
            plot_path=candidate.plot_path,
            pruned_at_utc=None,
        )
        return [
            complete
            if record.build_id == candidate.build_id
            else replace(record, current=False)
            if record.current
            else record
            for record in registered
        ]

    def _finalize_failed_if_uncommitted(self, build_id: str) -> None:
        """Finalize only if authoritative catalog still says the attempt is building."""
        records = self.catalog.read()
        target = next((record for record in records if record.build_id == build_id), None)
        if target is None or target.status is not GoldBuildStatus.BUILDING:
            return
        failed = replace(
            target,
            status=GoldBuildStatus.FAILED,
            current=False,
            completed_at_utc=_utc(self.clock()),
            min_timestamp=None,
            max_timestamp=None,
            row_count=None,
            data_path=None,
            build_manifest_path=None,
            plot_path=None,
            pruned_at_utc=None,
        )
        replaced = [failed if record.build_id == build_id else record for record in records]
        self.catalog.replace(replaced)
        self.event_sink("catalog:failed")
        logger.debug("gold publication attempt finalized failed build_id=%s", build_id)
        try:
            self.views.refresh(replaced)
            self.event_sink("views:failed")
        except BaseException as error:
            logger.debug(
                "gold publication failure view refresh failed build_id=%s error_type=%s",
                build_id,
                type(error).__name__,
            )
            self.event_sink("views:failed-refresh-error")

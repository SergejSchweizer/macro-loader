from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from application.gold_catalog import (
    LATEST_COMPATIBLE,
    GoldBuildStatus,
    GoldCatalogRecord,
    GoldCompatibility,
)
from application.gold_retention import GoldRetentionPolicy, GoldRetentionService

START = datetime(2026, 8, 19, 2, tzinfo=UTC)


def _complete(
    index: int,
    *,
    current: bool = False,
    schema_version: int = 1,
    feature_version: int = 1,
) -> GoldCatalogRecord:
    build_id = f"20260819T0200{index:02d}Z"
    return GoldCatalogRecord(
        dataset_id="macro_features_daily",
        build_id=build_id,
        status=GoldBuildStatus.COMPLETE,
        current=current,
        started_at_utc=START + timedelta(seconds=index),
        completed_at_utc=START + timedelta(seconds=index, milliseconds=500),
        schema_version=schema_version,
        feature_version=feature_version,
        min_timestamp=START - timedelta(days=1),
        max_timestamp=START,
        row_count=2,
        data_path=f"versions/build_id={build_id}/data.parquet",
        build_manifest_path=f"versions/build_id={build_id}/manifest.json",
        plot_path=f"versions/build_id={build_id}/feature_profile.png",
        pruned_at_utc=None,
    )


def _noncomplete(index: int, status: GoldBuildStatus) -> GoldCatalogRecord:
    build_id = f"20260819T0300{index:02d}Z"
    return GoldCatalogRecord(
        dataset_id="macro_features_daily",
        build_id=build_id,
        status=status,
        current=False,
        started_at_utc=START,
        completed_at_utc=None,
        schema_version=1,
        feature_version=1,
        min_timestamp=None,
        max_timestamp=None,
        row_count=None,
        data_path=None,
        build_manifest_path=None,
        plot_path=None,
        pruned_at_utc=None,
    )


@dataclass
class FakeCatalog:
    records: list[GoldCatalogRecord]

    def __post_init__(self) -> None:
        self.replace_count = 0

    def read(self) -> list[GoldCatalogRecord]:
        return list(self.records)

    def replace(self, records: list[GoldCatalogRecord]) -> None:
        self.records = list(records)
        self.replace_count += 1


class FakeViews:
    def __init__(self) -> None:
        self.refresh_count = 0
        self.last: list[GoldCatalogRecord] = []

    def refresh(self, records: list[GoldCatalogRecord]) -> None:
        self.refresh_count += 1
        self.last = list(records)


class AssertingSweeper:
    def __init__(self, catalog: FakeCatalog, *, fail: bool = False) -> None:
        self.catalog = catalog
        self.fail = fail
        self.calls: list[str] = []

    def sweep(self, build_id: str) -> None:
        row = next(record for record in self.catalog.records if record.build_id == build_id)
        assert row.pruned_at_utc is not None
        assert row.data_path is None
        assert row.build_manifest_path is None
        assert row.plot_path is None
        self.calls.append(build_id)
        if self.fail:
            raise OSError("injected sweep failure")


def test_default_policy_retains_five_per_semantic_pair_and_never_marks_current() -> None:
    records = [_complete(index, current=index == 0) for index in range(7)]
    selected = GoldRetentionPolicy().select_for_mark(records)
    assert selected == (records[1].build_id, records[2].build_id)
    assert records[0].build_id not in selected


def test_custom_policy_and_version_groups_are_independent() -> None:
    v1 = [_complete(index, current=index == 3) for index in range(4)]
    v2 = [
        _complete(index + 10, current=index == 2, schema_version=2, feature_version=4)
        for index in range(3)
    ]
    selected = GoldRetentionPolicy(retain_successful_builds=2).select_for_mark([*v1, *v2])
    assert selected == (v1[0].build_id, v1[1].build_id, v2[0].build_id)


def test_building_failed_and_already_pruned_rows_are_not_new_mark_candidates() -> None:
    complete = [_complete(index, current=index == 5) for index in range(6)]
    pruned = replace(
        _complete(20),
        data_path=None,
        build_manifest_path=None,
        plot_path=None,
        pruned_at_utc=START,
    )
    records = [
        *complete,
        _noncomplete(1, GoldBuildStatus.BUILDING),
        _noncomplete(2, GoldBuildStatus.FAILED),
        pruned,
    ]
    assert GoldRetentionPolicy().select_for_mark(records) == (complete[0].build_id,)


def test_mark_happens_before_sweep_and_resolver_never_selects_marked_row() -> None:
    records = [_complete(index, current=index == 5) for index in range(6)]
    catalog = FakeCatalog(records)
    views = FakeViews()
    sweeper = AssertingSweeper(catalog)
    service = GoldRetentionService(
        catalog, sweeper, views, clock=lambda: START + timedelta(hours=1)
    )
    result = service.run()
    assert result.marked_build_ids == (records[0].build_id,)
    assert result.swept_build_ids == (records[0].build_id,)
    assert catalog.replace_count == 1
    assert views.refresh_count == 1
    marked = next(record for record in catalog.records if record.build_id == records[0].build_id)
    assert marked.pruned_at_utc == START + timedelta(hours=1)
    chosen = LATEST_COMPATIBLE.resolve(catalog.records, GoldCompatibility(1, 1))
    assert chosen.build_id == records[5].build_id


def test_sweep_failure_keeps_catalog_tombstone_and_retry_is_idempotent() -> None:
    records = [_complete(index, current=index == 5) for index in range(6)]
    catalog = FakeCatalog(records)
    views = FakeViews()
    failing = AssertingSweeper(catalog, fail=True)
    service = GoldRetentionService(
        catalog, failing, views, clock=lambda: START + timedelta(hours=1)
    )
    with pytest.raises(OSError, match="sweep failure"):
        service.run()
    marked = next(record for record in catalog.records if record.build_id == records[0].build_id)
    assert marked.pruned_at_utc is not None and marked.data_path is None

    healthy = AssertingSweeper(catalog)
    retry = GoldRetentionService(catalog, healthy, views, clock=lambda: START + timedelta(hours=2))
    result = retry.run()
    assert result.marked_build_ids == ()
    assert result.swept_build_ids == (records[0].build_id,)
    assert catalog.replace_count == 1
    assert views.refresh_count == 2


def test_policy_and_mark_validation_fail_closed() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        GoldRetentionPolicy(retain_successful_builds=0)
    current = _complete(1, current=True)
    with pytest.raises(ValueError, match="current"):
        GoldRetentionService._mark(current, START)
    failed = _noncomplete(1, GoldBuildStatus.FAILED)
    with pytest.raises(ValueError, match="only complete"):
        GoldRetentionService._mark(failed, START)
    pruned = replace(
        _complete(2),
        data_path=None,
        build_manifest_path=None,
        plot_path=None,
        pruned_at_utc=START,
    )
    with pytest.raises(ValueError, match="already-pruned"):
        GoldRetentionService._mark(pruned, START + timedelta(hours=1))

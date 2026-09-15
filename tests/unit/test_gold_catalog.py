from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from application.gold_catalog import (
    LATEST_COMPATIBLE,
    STRICT_CURRENT,
    GoldBuildStatus,
    GoldCatalogRecord,
    GoldCompatibility,
)
from ingestion.gold_catalog_repository import GOLD_CATALOG_SCHEMA, GoldCatalogRepository
from ingestion.parquet_repository import atomic_write_parquet

START = datetime(2026, 8, 19, 2, tzinfo=UTC)
COMPAT = GoldCompatibility(schema_version=1, feature_version=1)


def _record(
    build_id: str,
    *,
    status: GoldBuildStatus = GoldBuildStatus.COMPLETE,
    current: bool = False,
    completed: datetime | None = START,
    schema_version: int = 1,
    feature_version: int = 1,
    pruned: datetime | None = None,
    paths: bool = True,
) -> GoldCatalogRecord:
    return GoldCatalogRecord(
        dataset_id="macro_features_daily",
        build_id=build_id,
        status=status,
        current=current,
        started_at_utc=START - timedelta(minutes=1),
        completed_at_utc=completed,
        schema_version=schema_version,
        feature_version=feature_version,
        min_timestamp=datetime(2026, 8, 1, tzinfo=UTC)
        if status is GoldBuildStatus.COMPLETE
        else None,
        max_timestamp=datetime(2026, 8, 19, tzinfo=UTC)
        if status is GoldBuildStatus.COMPLETE
        else None,
        row_count=19 if status is GoldBuildStatus.COMPLETE else None,
        data_path=f"versions/build_id={build_id}/data.parquet" if paths else None,
        build_manifest_path=f"versions/build_id={build_id}/manifest.json" if paths else None,
        plot_path=f"versions/build_id={build_id}/feature_profile.png" if paths else None,
        pruned_at_utc=pruned,
    )


def test_catalog_schema_has_exact_fifteen_fields_and_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "manifest.parquet"
    repo = GoldCatalogRepository(path)
    assert repo.read() == []
    records = [
        _record("20260818T020000Z"),
        _record("20260819T020000Z", current=True, completed=START + timedelta(minutes=1)),
    ]
    repo.replace(records)
    frame = pl.read_parquet(path)
    assert frame.schema == GOLD_CATALOG_SCHEMA
    assert frame.columns == [
        "dataset_id",
        "build_id",
        "status",
        "current",
        "started_at_utc",
        "completed_at_utc",
        "schema_version",
        "feature_version",
        "min_timestamp",
        "max_timestamp",
        "row_count",
        "data_path",
        "build_manifest_path",
        "plot_path",
        "pruned_at_utc",
    ]
    assert repo.read() == records


def test_append_is_deterministic_and_duplicate_build_id_is_rejected(tmp_path: Path) -> None:
    repo = GoldCatalogRepository(tmp_path / "manifest.parquet")
    first = _record("20260818T020000Z")
    second = _record("20260819T020000Z", current=True, completed=START + timedelta(minutes=1))
    repo.append(first)
    repo.append(second)
    assert repo.read() == [first, second]
    with pytest.raises(ValueError, match="duplicate Gold build_id"):
        repo.append(second)


def test_strict_current_requires_one_compatible_selectable_complete_current() -> None:
    old = _record("20260818T020000Z")
    current = _record("20260819T020000Z", current=True)
    assert STRICT_CURRENT.resolve([old, current], COMPAT) == current
    with pytest.raises(LookupError, match="no current"):
        STRICT_CURRENT.resolve([old], COMPAT)
    with pytest.raises(LookupError, match="not compatible"):
        STRICT_CURRENT.resolve([old, replace(current, schema_version=2)], COMPAT)
    with pytest.raises(LookupError, match="not a selectable"):
        STRICT_CURRENT.resolve(
            [old, replace(current, data_path=None, build_manifest_path=None, plot_path=None)],
            COMPAT,
        )


def test_latest_compatible_prefers_current_then_newest_with_tie_break() -> None:
    valid_current = _record(
        "20260817T020000Z",
        current=True,
        completed=START - timedelta(days=2),
    )
    newer = _record("20260819T020000Z", completed=START)
    assert LATEST_COMPATIBLE.resolve([valid_current, newer], COMPAT) == valid_current

    incompatible_current = replace(valid_current, schema_version=2)
    same_completed_a = _record("20260818T020000Z", completed=START)
    same_completed_b = _record("20260819T020000Z", completed=START)
    assert (
        LATEST_COMPATIBLE.resolve(
            [incompatible_current, same_completed_a, same_completed_b], COMPAT
        )
        == same_completed_b
    )


def test_building_failed_pruned_and_incomplete_rows_are_never_selectable() -> None:
    building = _record(
        "20260816T020000Z",
        status=GoldBuildStatus.BUILDING,
        completed=None,
        paths=False,
    )
    failed = _record(
        "20260817T020000Z",
        status=GoldBuildStatus.FAILED,
        completed=None,
        paths=False,
    )
    pruned = _record(
        "20260818T020000Z",
        pruned=START,
        paths=False,
    )
    with pytest.raises(LookupError, match="no compatible complete"):
        LATEST_COMPATIBLE.resolve([building, failed, pruned], COMPAT)


def test_multiple_current_rows_are_catalog_paradox_for_all_strategies() -> None:
    first = _record("20260818T020000Z", current=True)
    second = _record("20260819T020000Z", current=True, completed=START + timedelta(minutes=1))
    for strategy in (STRICT_CURRENT, LATEST_COMPATIBLE):
        with pytest.raises(ValueError, match="multiple current"):
            strategy.resolve([first, second], COMPAT)


def test_repository_validation_rejects_invalid_semantic_states(tmp_path: Path) -> None:
    repo = GoldCatalogRepository(tmp_path / "manifest.parquet")
    complete = _record("20260819T020000Z")
    with pytest.raises(ValueError, match="all present or all null"):
        repo.replace([replace(complete, data_path=None)])
    with pytest.raises(ValueError, match="only a complete"):
        repo.replace(
            [
                _record(
                    "20260819T020000Z",
                    status=GoldBuildStatus.BUILDING,
                    current=True,
                    completed=None,
                    paths=False,
                )
            ]
        )
    with pytest.raises(ValueError, match="cannot be current"):
        repo.replace(
            [
                replace(
                    complete,
                    current=True,
                    pruned_at_utc=START,
                    data_path=None,
                    build_manifest_path=None,
                    plot_path=None,
                )
            ]
        )
    with pytest.raises(ValueError, match="row_count"):
        repo.replace([replace(complete, row_count=-1)])
    with pytest.raises(ValueError, match="cannot precede"):
        repo.replace([replace(complete, completed_at_utc=START - timedelta(hours=2))])
    with pytest.raises(ValueError, match="timezone-aware"):
        repo.replace([replace(complete, started_at_utc=datetime(2026, 8, 19, 1))])


def test_repository_rejects_schema_drift_without_filesystem_selection_logic(tmp_path: Path) -> None:
    path = tmp_path / "manifest.parquet"
    atomic_write_parquet(pl.DataFrame({"bad": [1]}), path)
    with pytest.raises(ValueError, match="invalid Gold catalog schema"):
        GoldCatalogRepository(path).read()

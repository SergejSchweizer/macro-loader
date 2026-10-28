from __future__ import annotations

import hashlib
import logging
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from application.paths import LakePaths
from application.registry import series_contract
from ingestion.inventory_refresh import InventoryRefreshService
from ingestion.parquet_repository import atomic_write_parquet
from ingestion.silver_repository import SilverSeriesRepository

pytestmark = pytest.mark.integration
NOW = datetime(2026, 8, 19, 2, tzinfo=UTC)


def _bronze_scalar(series_id: str, rows: list[tuple[date, float]]) -> pl.DataFrame:
    contract = series_contract(series_id)
    return pl.DataFrame(
        {
            "series_id": [series_id for _ in rows],
            "provider": [contract.provider.value for _ in rows],
            "observation_date": [day for day, _ in rows],
            "fetched_at_utc": [NOW for _ in rows],
            "source_id": [contract.source_id for _ in rows],
            "source_url": ["https://source" for _ in rows],
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


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_revision_touches_only_one_month_and_noop_preserves_files(tmp_path: Path) -> None:
    paths = LakePaths(tmp_path / "lake")
    contract = series_contract("us_10y")
    july_bronze = paths.bronze_month(contract.provider, contract.series_id, date(2026, 7, 1))
    august_bronze = paths.bronze_month(contract.provider, contract.series_id, date(2026, 8, 1))
    atomic_write_parquet(_bronze_scalar("us_10y", [(date(2026, 7, 31), 4.0)]), july_bronze)
    atomic_write_parquet(_bronze_scalar("us_10y", [(date(2026, 8, 19), 4.2)]), august_bronze)

    repo = SilverSeriesRepository(paths)
    first = repo.build(contract)
    assert first.inserts.height == 2
    july = paths.silver_month("us_10y", date(2026, 7, 1))
    august = paths.silver_month("us_10y", date(2026, 8, 1))
    july_hash = _hash(july)
    august_hash = _hash(august)
    july_mtime = july.stat().st_mtime_ns
    august_mtime = august.stat().st_mtime_ns

    noop = repo.build(contract)
    assert noop.inserts.height == 0
    assert noop.revisions.height == 0
    assert _hash(july) == july_hash
    assert _hash(august) == august_hash
    assert july.stat().st_mtime_ns == july_mtime
    assert august.stat().st_mtime_ns == august_mtime

    atomic_write_parquet(_bronze_scalar("us_10y", [(date(2026, 8, 19), 9.9)]), august_bronze)
    revised = repo.build(contract)
    assert revised.revisions.height == 1
    assert _hash(july) == july_hash
    assert july.stat().st_mtime_ns == july_mtime
    assert _hash(august) != august_hash
    assert (
        repo.read(contract).filter(pl.col("observation_date") == date(2026, 8, 19)).item(0, "value")
        == 9.9
    )


def test_building_selected_series_does_not_touch_other_silver_series(tmp_path: Path) -> None:
    paths = LakePaths(tmp_path / "lake")
    us2 = series_contract("us_2y")
    us10 = series_contract("us_10y")
    atomic_write_parquet(
        _bronze_scalar("us_2y", [(date(2026, 8, 19), 3.2)]),
        paths.bronze_month(us2.provider, us2.series_id, date(2026, 8, 1)),
    )
    atomic_write_parquet(
        _bronze_scalar("us_10y", [(date(2026, 8, 19), 4.2)]),
        paths.bronze_month(us10.provider, us10.series_id, date(2026, 8, 1)),
    )
    repo = SilverSeriesRepository(paths)
    repo.build(us2)
    other = paths.silver_month("us_10y", date(2026, 8, 1))
    assert not other.exists()
    repo.build(us10)
    other_hash = _hash(other)
    repo.build(us2)
    assert _hash(other) == other_hash


def test_corrupt_persisted_silver_schema_is_rejected_without_logging_paths_or_error_text(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    paths = LakePaths(tmp_path / "lake")
    contract = series_contract("us_10y")
    target = paths.silver_month(contract.series_id, date(2026, 8, 1))
    atomic_write_parquet(pl.DataFrame({"bad": [1]}), target)
    caplog.set_level(logging.DEBUG, logger="ingestion.silver_repository")
    with pytest.raises(ValueError, match="invalid persisted Silver schema"):
        SilverSeriesRepository(paths).read(contract)

    assert (
        "Silver series read failed provider=fred series_id=us_10y error_type=ValueError"
        in caplog.text
    )
    assert "invalid persisted Silver schema" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_silver_and_inventory_adapters_log_safe_lifecycle_counts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    paths = LakePaths(tmp_path / "lake")
    contract = series_contract("us_10y")
    atomic_write_parquet(
        _bronze_scalar("us_10y", [(date(2026, 8, 19), 987654.321)]),
        paths.bronze_month(contract.provider, contract.series_id, date(2026, 8, 1)),
    )
    caplog.set_level(logging.DEBUG, logger="ingestion")

    diff = SilverSeriesRepository(paths).build(contract)
    inventory = InventoryRefreshService(paths).refresh()

    assert diff.inserts.height == 1
    assert len(inventory) == 25
    assert (
        "Silver series build completed provider=fred series_id=us_10y inserted=1 revised=0"
        in caplog.text
    )
    assert "Bronze inventory refresh completed series_count=25 nonempty_count=1" in caplog.text
    assert "987654.321" not in caplog.text
    assert str(tmp_path) not in caplog.text

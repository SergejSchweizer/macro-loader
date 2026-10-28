from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from application.contracts import Provider
from application.operational_records import (
    IngestionRunRecord,
    InventoryRecord,
    RunMode,
    RunStatus,
)
from ingestion.operational_repository import (
    INVENTORY_SCHEMA,
    RUN_SCHEMA,
    read_inventory,
    read_runs,
    upsert_run,
    write_inventory,
    write_runs,
)

START = datetime(2026, 8, 19, 2, tzinfo=UTC)
END = datetime(2026, 8, 19, 2, 1, tzinfo=UTC)


def inventory(series_id: str = "us_10y") -> InventoryRecord:
    return InventoryRecord(
        series_id=series_id,
        provider=Provider.FRED,
        min_observation_date=date(2000, 1, 3),
        max_observation_date=date(2026, 8, 18),
        row_count=6000,
        duplicate_key_count=0,
        file_count=320,
    )


def run(
    run_id: str = "run-1",
    *,
    status: RunStatus = RunStatus.SUCCESS,
    inserted: int = 1,
    revised: int = 1,
    partitions: int = 1,
    error: str | None = None,
) -> IngestionRunRecord:
    return IngestionRunRecord(
        run_id=run_id,
        provider=Provider.FRED,
        series_id="us_10y",
        mode=RunMode.UPDATE,
        requested_start=date(2026, 8, 11),
        requested_end=date(2026, 8, 19),
        fetched_rows=7,
        accepted_rows=6,
        inserted_rows=inserted,
        revised_rows=revised,
        written_partitions=partitions,
        status=status,
        started_at_utc=START,
        completed_at_utc=END,
        error_category=None if error is None else "provider",
        error_message=error,
    )


def test_inventory_roundtrip_exact_schema_and_planning_latest(tmp_path: Path) -> None:
    path = tmp_path / "inventory.parquet"
    assert read_inventory(path) == []
    first = inventory()
    second = InventoryRecord(
        "vix",
        Provider.CBOE,
        date(1990, 1, 2),
        date(2026, 8, 18),
        9000,
        0,
        440,
    )
    write_inventory(path, [first, second])
    frame = pl.read_parquet(path)
    assert frame.schema == pl.Schema(INVENTORY_SCHEMA)
    assert frame.columns == [
        "series_id",
        "provider",
        "min_observation_date",
        "max_observation_date",
        "row_count",
        "duplicate_key_count",
        "file_count",
    ]
    records = {item.series_id: item for item in read_inventory(path)}
    assert records["us_10y"].planning_latest == date(2026, 8, 18)
    assert records["us_10y"].planning_latest != records["us_10y"].min_observation_date
    assert not any("expected" in column or "missing" in column for column in frame.columns)


def test_inventory_empty_snapshot_and_duplicate_rejection(tmp_path: Path) -> None:
    path = tmp_path / "inventory.parquet"
    write_inventory(path, [])
    assert pl.read_parquet(path).schema == pl.Schema(INVENTORY_SCHEMA)
    item = inventory()
    with pytest.raises(ValueError, match="duplicate inventory"):
        write_inventory(path, [item, item])


def test_inventory_bad_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "inventory.parquet"
    pl.DataFrame({"bad": [1]}).write_parquet(path)
    with pytest.raises(ValueError, match="invalid inventory schema"):
        read_inventory(path)


def test_run_record_validates_identity_counts_times_and_failed_durability() -> None:
    with pytest.raises(ValueError, match="run_id"):
        IngestionRunRecord(
            " ",
            Provider.FRED,
            "x",
            RunMode.UPDATE,
            None,
            None,
            0,
            0,
            0,
            0,
            0,
            RunStatus.SUCCESS,
            START,
            END,
        )
    with pytest.raises(ValueError, match="counts"):
        run(inserted=-1)
    with pytest.raises(ValueError, match="cannot precede"):
        record = run()
        IngestionRunRecord(
            record.run_id,
            record.provider,
            record.series_id,
            record.mode,
            record.requested_start,
            record.requested_end,
            0,
            0,
            0,
            0,
            0,
            RunStatus.SUCCESS,
            END,
            START,
        )
    with pytest.raises(ValueError, match="failed run"):
        run(status=RunStatus.FAILED)


def test_run_roundtrip_preserves_exact_request_bounds_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "runs.parquet"
    assert read_runs(path) == []
    record = run()
    write_runs(path, [record])
    frame = pl.read_parquet(path)
    assert frame.schema == pl.Schema(RUN_SCHEMA)
    reread = read_runs(path)[0]
    assert reread.requested_start == date(2026, 8, 11)
    assert reread.requested_end == date(2026, 8, 19)
    assert reread.accepted_rows == 6
    assert reread.requested_start != date(2000, 1, 3)


def test_failed_run_is_zero_durable_and_secrets_are_redacted(tmp_path: Path) -> None:
    path = tmp_path / "runs.parquet"
    failed = run(
        status=RunStatus.FAILED,
        inserted=0,
        revised=0,
        partitions=0,
        error="FRED api_key=TOPSECRET failed",
    )
    write_runs(path, [failed], secrets=("TOPSECRET",))
    reread = read_runs(path)[0]
    assert reread.inserted_rows == 0
    assert reread.revised_rows == 0
    assert reread.written_partitions == 0
    assert reread.error_message == "FRED api_key=*** failed"
    assert "TOPSECRET" not in path.read_bytes().decode("latin1", errors="ignore")


def test_run_upsert_replaces_same_id_and_preserves_other_ids(tmp_path: Path) -> None:
    path = tmp_path / "runs.parquet"
    write_runs(path, [run("run-1"), run("run-2")])
    replacement = IngestionRunRecord(
        run_id="run-1",
        provider=Provider.FRED,
        series_id="us_10y",
        mode=RunMode.UPDATE,
        requested_start=date(2026, 8, 12),
        requested_end=date(2026, 8, 19),
        fetched_rows=2,
        accepted_rows=2,
        inserted_rows=0,
        revised_rows=0,
        written_partitions=0,
        status=RunStatus.SUCCESS,
        started_at_utc=START,
        completed_at_utc=END,
    )
    upsert_run(path, replacement)
    records = {item.run_id: item for item in read_runs(path)}
    assert set(records) == {"run-1", "run-2"}
    assert records["run-1"].requested_start == date(2026, 8, 12)


def test_run_writer_rejects_duplicate_ids_and_bad_schema(tmp_path: Path) -> None:
    path = tmp_path / "runs.parquet"
    item = run()
    with pytest.raises(ValueError, match="duplicate ingestion run_id"):
        write_runs(path, [item, item])
    pl.DataFrame({"bad": [1]}).write_parquet(path)
    with pytest.raises(ValueError, match="invalid ingestion-run schema"):
        read_runs(path)


def test_operational_repository_logs_counts_without_record_error_text(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    inventory_path = tmp_path / "inventory.parquet"
    runs_path = tmp_path / "runs.parquet"
    failed = run(
        status=RunStatus.FAILED,
        inserted=0,
        revised=0,
        partitions=0,
        error="private /source/path TOPSECRET payload=4.2",
    )
    caplog.set_level(logging.DEBUG, logger="ingestion.operational_repository")

    write_inventory(inventory_path, [inventory()])
    assert read_inventory(inventory_path) == [inventory()]
    upsert_run(runs_path, failed, secrets=("TOPSECRET",))
    assert read_runs(runs_path)[0].error_message == "private /source/path *** payload=4.2"

    assert "inventory snapshot write completed record_count=1" in caplog.text
    assert "inventory snapshot read completed record_count=1" in caplog.text
    assert "ingestion run upsert completed run_id=run-1" in caplog.text
    assert "ingestion run snapshot write completed record_count=1" in caplog.text
    assert "private /source/path" not in caplog.text
    assert "TOPSECRET" not in caplog.text
    assert "payload=4.2" not in caplog.text
    assert str(tmp_path) not in caplog.text

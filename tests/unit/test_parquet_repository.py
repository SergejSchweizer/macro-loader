from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from application.ports.lake import MonthlyFrameRepository
from ingestion import parquet_repository as repo

SCHEMA = {
    "series_id": pl.String,
    "observation_date": pl.Date,
    "value": pl.Float64,
}


def frame(rows: list[tuple[str, date, float]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=SCHEMA, orient="row")


def month_path(root: Path, day: date) -> Path:
    return root / f"year={day.year:04d}" / f"month={day.month:02d}" / "data.parquet"


def test_repository_implements_protocol_and_delegates(tmp_path: Path) -> None:
    repository: MonthlyFrameRepository = repo.PolarsMonthlyRepository()
    assert repository.read([], sort_by=["observation_date"]).is_empty()
    path = tmp_path / "data.parquet"
    frame([("x", date(2026, 8, 18), 1.0)]).write_parquet(path)
    bounds = repository.observation_bounds([path])
    assert bounds.minimum == date(2026, 8, 18)
    assert bounds.maximum == date(2026, 8, 18)


def test_protocol_stub_methods_are_executable_for_coverage() -> None:
    read_stub = MonthlyFrameRepository.read
    bounds_stub = MonthlyFrameRepository.observation_bounds
    assert read_stub(object(), [], sort_by=[]) is None
    assert bounds_stub(object(), []) is None


def test_read_zero_one_and_multiple_months_in_key_order(tmp_path: Path) -> None:
    january = month_path(tmp_path, date(2026, 1, 1))
    february = month_path(tmp_path, date(2026, 2, 1))
    repo.atomic_write_parquet(frame([("x", date(2026, 1, 2), 2.0)]), january)
    repo.atomic_write_parquet(frame([("x", date(2026, 2, 2), 3.0)]), february)

    assert repo.read_monthly([], sort_by=["observation_date"]).is_empty()
    assert repo.read_monthly([january], sort_by=["observation_date"]).height == 1
    assert repo.read_monthly([january], sort_by=[]).height == 1
    combined = repo.read_monthly([february, january], sort_by=["observation_date"])
    assert combined.get_column("value").to_list() == [2.0, 3.0]


def test_read_rejects_missing_sort_key(tmp_path: Path) -> None:
    path = tmp_path / "data.parquet"
    frame([("x", date(2026, 1, 2), 2.0)]).write_parquet(path)
    with pytest.raises(ValueError, match="sort key"):
        repo.read_monthly([path], sort_by=["missing"])


def test_observation_bounds_use_authoritative_files(tmp_path: Path) -> None:
    first = month_path(tmp_path, date(2000, 1, 1))
    latest = month_path(tmp_path, date(2026, 8, 1))
    repo.atomic_write_parquet(frame([("x", date(2000, 1, 3), 1.0)]), first)
    repo.atomic_write_parquet(frame([("x", date(2026, 8, 18), 2.0)]), latest)
    stale = latest.parent / ".data.parquet.crash.tmp"
    frame([("x", date(2099, 1, 1), 99.0)]).write_parquet(stale)

    authoritative = repo.observation_bounds([latest, first])
    assert authoritative.minimum == date(2000, 1, 3)
    assert authoritative.maximum == date(2026, 8, 18)
    assert stale.is_file()


def test_observation_bounds_empty_and_invalid_date_column(tmp_path: Path) -> None:
    assert repo.observation_bounds([]).minimum is None
    path = tmp_path / "bad.parquet"
    pl.DataFrame({"value": [1.0]}).write_parquet(path)
    with pytest.raises(ValueError, match="date column"):
        repo.observation_bounds([path])

    wrong_type = tmp_path / "wrong-type.parquet"
    pl.DataFrame({"observation_date": [1, 2]}).write_parquet(wrong_type)
    with pytest.raises(TypeError, match="is not Date"):
        repo.observation_bounds([wrong_type])


def test_diff_classifies_insert_unchanged_and_revision() -> None:
    existing = frame(
        [
            ("x", date(2026, 1, 1), 1.0),
            ("x", date(2026, 1, 2), 2.0),
        ]
    )
    incoming = frame(
        [
            ("x", date(2026, 1, 1), 1.0),
            ("x", date(2026, 1, 2), 20.0),
            ("x", date(2026, 1, 3), 3.0),
        ]
    )
    diff = repo.diff_frames(existing, incoming, key=["series_id", "observation_date"])
    assert diff.unchanged.get_column("value").to_list() == [1.0]
    assert diff.revisions.get_column("value").to_list() == [20.0]
    assert diff.inserts.get_column("value").to_list() == [3.0]
    assert diff.changed.height == 2


def test_diff_handles_null_payload_without_losing_existing_identity() -> None:
    schema = {"series_id": pl.String, "observation_date": pl.Date, "value": pl.Float64}
    existing = pl.DataFrame([("x", date(2026, 1, 1), None)], schema=schema, orient="row")
    incoming = existing.clone()
    diff = repo.diff_frames(existing, incoming, key=["series_id", "observation_date"])
    assert diff.unchanged.height == 1
    assert diff.inserts.is_empty()


def test_diff_handles_key_only_frames() -> None:
    existing = pl.DataFrame({"series_id": ["x"]})
    incoming = pl.DataFrame({"series_id": ["x", "y"]})
    diff = repo.diff_frames(existing, incoming, key=["series_id"])
    assert diff.unchanged.get_column("series_id").to_list() == ["x"]
    assert diff.inserts.get_column("series_id").to_list() == ["y"]


def test_diff_empty_existing_and_incoming_paths() -> None:
    empty = pl.DataFrame(schema=SCHEMA)
    incoming = frame([("x", date(2026, 1, 1), 1.0)])
    first = repo.diff_frames(empty, incoming, key=["series_id", "observation_date"])
    assert first.inserts.height == 1
    second = repo.diff_frames(incoming, empty, key=["series_id", "observation_date"])
    assert not second.has_changes


def test_diff_rejects_duplicate_keys_and_schema_mismatch() -> None:
    duplicate = frame(
        [
            ("x", date(2026, 1, 1), 1.0),
            ("x", date(2026, 1, 1), 2.0),
        ]
    )
    with pytest.raises(ValueError, match="duplicate natural keys"):
        repo.diff_frames(
            pl.DataFrame(schema=SCHEMA), duplicate, key=["series_id", "observation_date"]
        )
    with pytest.raises(ValueError, match="duplicate natural keys"):
        repo.diff_frames(duplicate, frame([]), key=["series_id", "observation_date"])
    with pytest.raises(ValueError, match="natural key must not be empty"):
        repo.diff_frames(frame([]), frame([]), key=[])
    with pytest.raises(ValueError, match="natural key columns missing"):
        repo.diff_frames(frame([]), frame([]), key=["missing"])

    with pytest.raises(ValueError, match="columns must match"):
        repo.diff_frames(
            frame([("x", date(2026, 1, 1), 1.0)]),
            frame([("x", date(2026, 1, 2), 2.0)]).drop("value"),
            key=["series_id", "observation_date"],
        )

    different_dtype = frame([("x", date(2026, 1, 2), 2.0)]).with_columns(
        pl.col("value").cast(pl.Float32)
    )
    with pytest.raises(ValueError, match="dtypes must match"):
        repo.diff_frames(
            frame([("x", date(2026, 1, 1), 1.0)]),
            different_dtype,
            key=["series_id", "observation_date"],
        )


def test_merge_is_idempotent_revision_replaces_once_and_empty_bootstraps() -> None:
    existing = frame([("x", date(2026, 1, 1), 1.0)])
    incoming = frame([("x", date(2026, 1, 1), 2.0)])
    merged, diff = repo.merge_frames(existing, incoming, key=["series_id", "observation_date"])
    assert diff.revisions.height == 1
    assert merged.rows() == [("x", date(2026, 1, 1), 2.0)]

    rerun, second = repo.merge_frames(merged, incoming, key=["series_id", "observation_date"])
    assert not second.has_changes
    assert rerun.equals(merged)

    bootstrapped, bootstrap_diff = repo.merge_frames(
        pl.DataFrame(schema=SCHEMA), incoming, key=["series_id", "observation_date"]
    )
    assert bootstrap_diff.inserts.height == 1
    assert bootstrapped.equals(incoming)


def test_atomic_write_replaces_destination_and_removes_temp(tmp_path: Path) -> None:
    destination = tmp_path / "data.parquet"
    repo.atomic_write_parquet(frame([("x", date(2026, 1, 1), 1.0)]), destination)
    repo.atomic_write_parquet(frame([("x", date(2026, 1, 1), 2.0)]), destination)
    assert pl.read_parquet(destination).item(0, "value") == 2.0
    assert not list(tmp_path.glob(".data.parquet.*.tmp"))


def test_atomic_write_failure_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "data.parquet"
    original = frame([("x", date(2026, 1, 1), 1.0)])
    repo.atomic_write_parquet(original, destination)
    before = destination.read_bytes()

    def fail_replace(source: Path | str, target: Path | str) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(repo.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        repo.atomic_write_parquet(frame([("x", date(2026, 1, 1), 2.0)]), destination)
    assert destination.read_bytes() == before
    assert not list(tmp_path.glob(".data.parquet.*.tmp"))


def test_atomic_write_tolerates_directory_fsync_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "data.parquet"
    real_open = repo.os.open

    def open_with_directory_failure(
        path: os.PathLike[str] | str, flags: int, mode: int = 0o777
    ) -> int:
        if Path(path) == tmp_path:
            raise OSError("directory fsync unsupported")
        return real_open(path, flags, mode)

    monkeypatch.setattr(repo.os, "open", open_with_directory_failure)
    repo.atomic_write_parquet(frame([("x", date(2026, 1, 1), 1.0)]), destination)
    assert destination.is_file()


def test_stale_temp_is_never_discovered_as_authoritative(tmp_path: Path) -> None:
    destination = tmp_path / "data.parquet"
    stale = tmp_path / ".data.parquet.stale.tmp"
    frame([("x", date(2026, 1, 1), 9.0)]).write_parquet(stale)
    assert repo.read_monthly([destination], sort_by=["observation_date"]).is_empty()


def test_monthly_upsert_rewrites_only_affected_month(tmp_path: Path) -> None:
    january_path = month_path(tmp_path, date(2026, 1, 1))
    february_path = month_path(tmp_path, date(2026, 2, 1))
    existing = frame(
        [
            ("x", date(2026, 1, 10), 1.0),
            ("x", date(2026, 2, 10), 2.0),
        ]
    )
    repo.atomic_write_parquet(
        existing.filter(pl.col("observation_date").dt.month() == 1), january_path
    )
    repo.atomic_write_parquet(
        existing.filter(pl.col("observation_date").dt.month() == 2), february_path
    )
    february_before = february_path.read_bytes()
    february_mtime = os.stat(february_path).st_mtime_ns

    incoming = frame([("x", date(2026, 1, 10), 10.0)])
    _, written = repo.upsert_monthly(
        existing,
        incoming,
        key=["series_id", "observation_date"],
        date_column="observation_date",
        path_for_date=lambda day: month_path(tmp_path, day),
    )
    assert written == (january_path,)
    assert pl.read_parquet(january_path).item(0, "value") == 10.0
    assert february_path.read_bytes() == february_before
    assert os.stat(february_path).st_mtime_ns == february_mtime


def test_monthly_upsert_rejects_missing_or_invalid_date_column(tmp_path: Path) -> None:
    existing = frame([])
    missing = pl.DataFrame({"series_id": ["x"], "value": [1.0]})
    with pytest.raises(ValueError, match="date column missing"):
        repo.upsert_monthly(
            existing,
            missing,
            key=["series_id"],
            date_column="observation_date",
            path_for_date=lambda day: month_path(tmp_path, day),
        )

    invalid = pl.DataFrame({"series_id": ["x"], "observation_date": [1], "value": [1.0]})
    with pytest.raises(TypeError, match="must contain Date"):
        repo.upsert_monthly(
            pl.DataFrame(schema=invalid.schema),
            invalid,
            key=["series_id", "observation_date"],
            date_column="observation_date",
            path_for_date=lambda day: month_path(tmp_path, day),
        )


def test_monthly_upsert_noop_performs_no_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = frame([("x", date(2026, 1, 10), 1.0)])
    called = False

    def fail_if_called(data: pl.DataFrame, destination: Path) -> None:
        nonlocal called
        called = True
        raise AssertionError("write must not happen")

    monkeypatch.setattr(repo, "atomic_write_parquet", fail_if_called)
    diff, written = repo.upsert_monthly(
        existing,
        existing,
        key=["series_id", "observation_date"],
        date_column="observation_date",
        path_for_date=lambda day: month_path(tmp_path, day),
    )
    assert not diff.has_changes
    assert written == ()
    assert not called


def test_parquet_repository_logs_safe_read_write_and_upsert_counts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = month_path(tmp_path, date(2026, 8, 1))
    existing = frame([("series", date(2026, 8, 18), 987654.321)])
    caplog.set_level(logging.DEBUG, logger="ingestion.parquet_repository")

    repo.atomic_write_parquet(existing, path)
    loaded = repo.read_monthly([path], sort_by=("observation_date",))
    diff, written = repo.upsert_monthly(
        loaded,
        frame([("series", date(2026, 8, 18), 987654.321)]),
        key=("series_id", "observation_date"),
        date_column="observation_date",
        path_for_date=lambda _: path,
    )

    assert not diff.has_changes
    assert written == ()
    assert "atomic parquet write completed row_count=1" in caplog.text
    assert "monthly parquet read completed file_count=1 row_count=1" in caplog.text
    assert (
        "monthly parquet upsert completed inserted=0 revised=0 written_partitions=0" in caplog.text
    )
    assert "987654.321" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_parquet_write_failure_logs_class_without_path_or_error_text(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        del source, destination
        raise OSError("private /deployment/path secret-value=TOPSECRET")

    monkeypatch.setattr(repo.os, "replace", fail_replace)
    caplog.set_level(logging.DEBUG, logger="ingestion.parquet_repository")

    with pytest.raises(OSError, match="TOPSECRET"):
        repo.atomic_write_parquet(
            frame([("series", date(2026, 8, 18), 1.0)]), tmp_path / "data.parquet"
        )

    assert "atomic parquet write failed error_type=OSError" in caplog.text
    assert "TOPSECRET" not in caplog.text
    assert "/deployment/path" not in caplog.text
    assert str(tmp_path) not in caplog.text

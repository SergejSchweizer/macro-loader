"""Polars/Parquet repository primitives with deterministic atomic writes."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

import polars as pl

from application.ports.lake import FrameDiff, ObservationBounds

PathForDate = Callable[[date], Path]
logger = logging.getLogger(__name__)


def _existing_paths(paths: Sequence[Path]) -> list[Path]:
    """Return exact authoritative files; temporary siblings are never discovered."""
    return [path for path in paths if path.is_file()]


def read_monthly(paths: Sequence[Path], *, sort_by: Sequence[str]) -> pl.DataFrame:
    """Read zero, one, or many exact monthly Parquet files deterministically."""
    existing = _existing_paths(paths)
    logger.debug("monthly parquet read started file_count=%d", len(existing))
    if not existing:
        logger.debug("monthly parquet read completed file_count=0 row_count=0")
        return pl.DataFrame()
    try:
        frames = [pl.read_parquet(path) for path in existing]
        frame = frames[0] if len(frames) == 1 else pl.concat(frames, how="vertical_relaxed")
        if sort_by:
            missing = [column for column in sort_by if column not in frame.columns]
            if missing:
                raise ValueError(f"sort key columns missing: {missing}")
            frame = frame.sort(list(sort_by))
    except Exception as exc:
        logger.debug("monthly parquet read failed error_type=%s", type(exc).__name__)
        raise
    logger.debug(
        "monthly parquet read completed file_count=%d row_count=%d",
        len(existing),
        frame.height,
    )
    return frame


def observation_bounds(
    paths: Sequence[Path], *, date_column: str = "observation_date"
) -> ObservationBounds:
    """Discover minimum/maximum observed dates from authoritative Parquet files."""
    existing = _existing_paths(paths)
    if not existing:
        return ObservationBounds(None, None)
    minima: list[date] = []
    maxima: list[date] = []
    for path in existing:
        schema = pl.read_parquet_schema(path)
        if date_column not in schema:
            raise ValueError(f"date column missing from {path}: {date_column}")
        bounds = (
            pl.scan_parquet(path)
            .select(
                pl.col(date_column).min().alias("minimum"),
                pl.col(date_column).max().alias("maximum"),
            )
            .collect()
        )
        minimum = bounds.item(0, "minimum")
        maximum = bounds.item(0, "maximum")
        if minimum is not None:
            if not isinstance(minimum, date):
                raise TypeError(f"{date_column} is not Date in {path}")
            minima.append(minimum)
        if maximum is not None:
            if not isinstance(maximum, date):
                raise TypeError(f"{date_column} is not Date in {path}")
            maxima.append(maximum)
    if not minima or not maxima:
        return ObservationBounds(None, None)
    return ObservationBounds(min(minima), max(maxima))


def _ensure_compatible(existing: pl.DataFrame, incoming: pl.DataFrame) -> None:
    if existing.is_empty():
        return
    if existing.columns != incoming.columns:
        raise ValueError("existing and incoming columns must match exactly")
    if existing.schema != incoming.schema:
        raise ValueError("existing and incoming dtypes must match exactly")


def _ensure_unique(frame: pl.DataFrame, key: Sequence[str], *, label: str) -> None:
    if not key:
        raise ValueError("natural key must not be empty")
    missing = [column for column in key if column not in frame.columns]
    if missing:
        raise ValueError(f"natural key columns missing from {label}: {missing}")
    if frame.is_empty():
        return
    duplicates = frame.group_by(list(key)).len().filter(pl.col("len") > 1)
    if duplicates.height:
        raise ValueError(f"duplicate natural keys in {label}")


def diff_frames(existing: pl.DataFrame, incoming: pl.DataFrame, *, key: Sequence[str]) -> FrameDiff:
    """Classify incoming rows as inserts, unchanged observations, or revisions."""
    _ensure_unique(incoming, key, label="incoming")
    _ensure_unique(existing, key, label="existing")
    if incoming.is_empty():
        empty = incoming.clone()
        return FrameDiff(empty, empty, empty)
    _ensure_compatible(existing, incoming)
    if existing.is_empty():
        empty = incoming.head(0)
        return FrameDiff(incoming.clone(), empty, empty)

    payload_columns = [column for column in incoming.columns if column not in key]
    old = existing.with_columns(pl.lit(True).alias("__exists")).rename(
        {column: f"__old_{column}" for column in payload_columns}
    )
    joined = incoming.with_row_index("__order").join(old, on=list(key), how="left")
    exists = pl.col("__exists").fill_null(False)
    equal_payload = (
        pl.all_horizontal(
            [pl.col(column).eq_missing(pl.col(f"__old_{column}")) for column in payload_columns]
        )
        if payload_columns
        else pl.lit(True)
    )

    original_columns = incoming.columns
    inserts = joined.filter(~exists).sort("__order").select(original_columns)
    unchanged = joined.filter(exists & equal_payload).sort("__order").select(original_columns)
    revisions = joined.filter(exists & ~equal_payload).sort("__order").select(original_columns)
    return FrameDiff(inserts, unchanged, revisions)


def merge_frames(
    existing: pl.DataFrame, incoming: pl.DataFrame, *, key: Sequence[str]
) -> tuple[pl.DataFrame, FrameDiff]:
    """Return deterministic upsert result without performing physical IO."""
    diff = diff_frames(existing, incoming, key=key)
    if not diff.has_changes:
        return existing.clone(), diff
    if existing.is_empty():
        return incoming.sort(list(key)), diff
    changed_keys = diff.changed.select(list(key))
    retained = existing.join(changed_keys, on=list(key), how="anti")
    merged = pl.concat([retained, diff.changed], how="vertical_relaxed").sort(list(key))
    return merged, diff


def atomic_write_parquet(frame: pl.DataFrame, destination: Path) -> None:
    """Atomically replace one Parquet destination using a same-directory temp file."""
    logger.debug(
        "atomic parquet write started row_count=%d column_count=%d",
        frame.height,
        frame.width,
    )
    temp_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        os.close(file_descriptor)
        frame.write_parquet(temp_path)
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
        except OSError as exc:
            logger.debug("atomic parquet directory sync skipped error_type=%s", type(exc).__name__)
        else:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        logger.debug("atomic parquet write failed error_type=%s", type(exc).__name__)
        raise
    logger.debug("atomic parquet write completed row_count=%d", frame.height)


def upsert_monthly(
    existing: pl.DataFrame,
    incoming: pl.DataFrame,
    *,
    key: Sequence[str],
    date_column: str,
    path_for_date: PathForDate,
) -> tuple[FrameDiff, tuple[Path, ...]]:
    """Upsert and rewrite only months containing inserted/revised observations."""
    logger.debug(
        "monthly parquet upsert started existing_rows=%d incoming_rows=%d",
        existing.height,
        incoming.height,
    )
    if date_column not in incoming.columns:
        raise ValueError(f"date column missing from incoming: {date_column}")
    merged, diff = merge_frames(existing, incoming, key=key)
    if not diff.has_changes:
        logger.debug("monthly parquet upsert completed inserted=0 revised=0 written_partitions=0")
        return diff, ()
    changed_dates = diff.changed.get_column(date_column).drop_nulls().to_list()
    if any(not isinstance(value, date) for value in changed_dates):
        raise TypeError(f"{date_column} must contain Date values")
    months = sorted({(value.year, value.month) for value in changed_dates})
    written: list[Path] = []
    for year, month in months:
        sample_date = date(year, month, 1)
        destination = path_for_date(sample_date)
        month_frame = merged.filter(
            (pl.col(date_column).dt.year() == year) & (pl.col(date_column).dt.month() == month)
        )
        atomic_write_parquet(month_frame, destination)
        written.append(destination)
    logger.debug(
        "monthly parquet upsert completed inserted=%d revised=%d written_partitions=%d",
        diff.inserts.height,
        diff.revisions.height,
        len(written),
    )
    return diff, tuple(written)


class PolarsMonthlyRepository:
    """Concrete filesystem adapter satisfying the monthly repository port."""

    def read(self, paths: Sequence[Path], *, sort_by: Sequence[str]) -> pl.DataFrame:
        return read_monthly(paths, sort_by=sort_by)

    def observation_bounds(
        self, paths: Sequence[Path], *, date_column: str = "observation_date"
    ) -> ObservationBounds:
        return observation_bounds(paths, date_column=date_column)

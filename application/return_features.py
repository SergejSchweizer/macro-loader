"""Column contracts for the PostgreSQL-owned return/volatility families."""

from __future__ import annotations

from collections.abc import Sequence

LOG_RETURN_WINDOWS: tuple[int, ...] = (1, 3, 5, 10, 20)
RETURN_WINDOWS: tuple[int, ...] = (5, 10, 20)
RETURN_MEAN_WINDOWS: tuple[int, ...] = (5, 10, 20)
VOLATILITY_WINDOWS: tuple[int, ...] = (5, 10, 20, 40)


def _validate_windows(windows: Sequence[int]) -> None:
    if any(window < 1 for window in windows):
        raise ValueError("return windows must be positive")


def return_feature_columns(
    series_ids: Sequence[str], windows: Sequence[int] = RETURN_WINDOWS
) -> tuple[str, ...]:
    """Return catalog-compatible geometric-return names."""
    _validate_windows(windows)
    return tuple(
        f"{series_id}_return_geom_{window}obs_pct" for series_id in series_ids for window in windows
    )


def log_return_feature_columns(
    series_ids: Sequence[str], windows: Sequence[int] = LOG_RETURN_WINDOWS
) -> tuple[str, ...]:
    """Return catalog-compatible log-return names; numerical values are SQL-owned."""
    _validate_windows(windows)
    return tuple(
        f"{series_id}_log_return_{window}obs" for series_id in series_ids for window in windows
    )


def return_mean_feature_columns(
    series_ids: Sequence[str], windows: Sequence[int] = RETURN_MEAN_WINDOWS
) -> tuple[str, ...]:
    """Return catalog-compatible mean-log-return names; numerical values are SQL-owned."""
    _validate_windows(windows)
    return tuple(
        f"{series_id}_return_mean_{window}obs" for series_id in series_ids for window in windows
    )


def volatility_feature_columns(
    series_ids: Sequence[str], windows: Sequence[int] = VOLATILITY_WINDOWS
) -> tuple[str, ...]:
    """Return catalog-compatible sample-volatility names; numerical values are SQL-owned."""
    _validate_windows(windows)
    return tuple(
        f"{series_id}_volatility_{window}obs" for series_id in series_ids for window in windows
    )

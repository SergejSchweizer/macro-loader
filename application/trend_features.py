"""Column contracts for the PostgreSQL-owned trend/momentum families."""

from __future__ import annotations

from collections.abc import Sequence

SMA_RATIO_WINDOWS: tuple[tuple[int, int], ...] = ((5, 20), (10, 20), (10, 40))
RSI_WINDOWS: tuple[int, ...] = (7, 14)
ROC_WINDOWS: tuple[int, ...] = (3, 5, 10, 20)
DRAWDOWN_WINDOWS: tuple[int, ...] = (20, 60)


def _validate_windows(windows: Sequence[int]) -> None:
    if any(window < 1 for window in windows):
        raise ValueError("trend windows must be positive")


def sma_ratio_feature_columns(
    series_ids: Sequence[str],
    windows: Sequence[tuple[int, int]] = SMA_RATIO_WINDOWS,
) -> tuple[str, ...]:
    """Return catalog-compatible simple-moving-average ratio names."""
    if any(short < 1 or long < 1 or short > long for short, long in windows):
        raise ValueError("SMA ratio windows must be positive and ordered")
    return tuple(
        f"{series_id}_sma_ratio_{short}_{long}"
        for series_id in series_ids
        for short, long in windows
    )


def rsi_feature_columns(
    series_ids: Sequence[str], windows: Sequence[int] = RSI_WINDOWS
) -> tuple[str, ...]:
    """Return catalog-compatible Wilder-RSI names."""
    _validate_windows(windows)
    return tuple(f"{series_id}_rsi_{window}obs" for series_id in series_ids for window in windows)


def roc_feature_columns(
    series_ids: Sequence[str], windows: Sequence[int] = ROC_WINDOWS
) -> tuple[str, ...]:
    """Return catalog-compatible rate-of-change names."""
    _validate_windows(windows)
    return tuple(f"{series_id}_roc_{window}obs" for series_id in series_ids for window in windows)


def drawdown_feature_columns(
    series_ids: Sequence[str], windows: Sequence[int] = DRAWDOWN_WINDOWS
) -> tuple[str, ...]:
    """Return catalog-compatible rolling drawdown names."""
    _validate_windows(windows)
    return tuple(
        f"{series_id}_drawdown_{window}obs" for series_id in series_ids for window in windows
    )

"""Column contracts for the PostgreSQL-owned trend/momentum families."""

from __future__ import annotations

from collections.abc import Sequence

SMA_RATIO_WINDOWS: tuple[tuple[int, int], ...] = ((5, 15), (5, 20), (10, 20), (10, 40))
RSI_WINDOWS: tuple[int, ...] = (3, 5, 7, 14)
RSI_COMPOSITE_INTERNAL_WINDOW = 15
ROC_WINDOWS: tuple[int, ...] = (1, 2, 3, 4, 5, 8, 10, 20)
DRAWDOWN_WINDOWS: tuple[int, ...] = (20, 32, 60, 96)
EMA_PERIODS: tuple[int, ...] = (5, 9, 21)
EMA_OUTPUT_SUFFIXES: tuple[str, ...] = (
    "ema5_distance",
    "ema9_distance",
    "ema21_distance",
    "ema5_ema21_spread",
    "ema9_slope_3",
    "ema5_ema9_cross",
)
MACD_PRICE_EMA_PERIODS: tuple[int, ...] = (12, 26)
MACD_SIGNAL_EMA_PERIOD = 9
MACD_OUTPUT_SUFFIXES: tuple[str, ...] = (
    "macd_line_relative",
    "macd_hist_relative",
    "macd_hist_delta_3",
)


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


def ema_feature_columns(series_ids: Sequence[str]) -> tuple[str, ...]:
    """Return the public EMA trend feature names in per-series order."""
    return tuple(
        f"{series_id}_{suffix}" for series_id in series_ids for suffix in EMA_OUTPUT_SUFFIXES
    )


def macd_feature_columns(series_ids: Sequence[str]) -> tuple[str, ...]:
    """Return the public relative-MACD feature names in per-series order."""
    return tuple(
        f"{series_id}_{suffix}" for series_id in series_ids for suffix in MACD_OUTPUT_SUFFIXES
    )

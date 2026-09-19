"""Compatibility names for the PostgreSQL-owned geometric-return family."""

from __future__ import annotations

from collections.abc import Sequence

RETURN_WINDOWS: tuple[int, ...] = (10, 25, 60, 120, 240)


def return_feature_columns(
    series_ids: Sequence[str], windows: Sequence[int] = RETURN_WINDOWS
) -> tuple[str, ...]:
    """Return catalog-compatible names; numerical values are SQL-owned."""
    if any(window < 1 for window in windows):
        raise ValueError("return windows must be positive")
    return tuple(
        f"{series_id}_return_geom_{window}obs_pct" for series_id in series_ids for window in windows
    )

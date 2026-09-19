"""Compatibility names for the PostgreSQL-owned momentum feature family."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MomentumFeaturePolicy:
    lag_windows: tuple[tuple[int, int], ...] = ((1, 60), (5, 60), (20, 120))
    minimum_change_lag: int = 1
    ddof: int = 0

    def __post_init__(self) -> None:
        if self.lag_windows != ((1, 60), (5, 60), (20, 120)):
            raise ValueError("momentum lag/windows are fixed at (1, 60), (5, 60), and (20, 120)")
        if self.minimum_change_lag != 1 or self.ddof != 0:
            raise ValueError("momentum policy is fixed at one-observation changes and ddof=0")


MOMENTUM_POLICY = MomentumFeaturePolicy()


def momentum_feature_columns(
    series_ids: Sequence[str], policy: MomentumFeaturePolicy = MOMENTUM_POLICY
) -> tuple[str, ...]:
    """Return catalog-compatible names; numerical values are SQL-owned."""
    return tuple(
        f"{series_id}_momentum_autocorr_{lag}_{window}obs"
        for series_id in series_ids
        for lag, window in policy.lag_windows
    )

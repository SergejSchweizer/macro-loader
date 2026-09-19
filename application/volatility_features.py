"""Source-series policy metadata for the PostgreSQL-owned feature view."""

from __future__ import annotations

from dataclasses import dataclass

VOLATILITY_SERIES = ("vix", "vix9d", "vix3m", "vix6m", "vix1y", "vstoxx", "move")


@dataclass(frozen=True, slots=True)
class VolatilityFeaturePolicy:
    delta_lags: tuple[int, int, int] = (1, 5, 20)
    zscore_window: int = 60
    zscore_ddof: int = 0

    def __post_init__(self) -> None:
        if self.delta_lags != (1, 5, 20):
            raise ValueError("volatility delta lags are fixed at 1, 5, and 20 observations")
        if self.zscore_window != 60 or self.zscore_ddof != 0:
            raise ValueError("volatility z-score policy is fixed at window=60 and ddof=0")


VOLATILITY_POLICY = VolatilityFeaturePolicy()

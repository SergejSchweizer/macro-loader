"""Source-series policy metadata for the PostgreSQL-owned macro feature view."""

from __future__ import annotations

from dataclasses import dataclass

MACRO_SERIES = ("ciss", "euro_hy_oas", "us_2y", "us_10y", "estr", "usd_broad")


@dataclass(frozen=True, slots=True)
class MacroFeaturePolicy:
    immediate_lag: int = 1
    short_lag: int = 5
    long_lag: int = 20

    def __post_init__(self) -> None:
        if (self.immediate_lag, self.short_lag, self.long_lag) != (1, 5, 20):
            raise ValueError("macro observation lags are fixed at 1, 5, and 20")


MACRO_POLICY = MacroFeaturePolicy()


def macro_delta_lags(policy: MacroFeaturePolicy) -> dict[str, tuple[int, ...]]:
    """Return the observation-lag contract executed by the SQL view."""
    return {
        "ciss": (policy.immediate_lag, policy.short_lag, policy.long_lag),
        "euro_hy_oas": (policy.immediate_lag, policy.short_lag, policy.long_lag),
        "us_2y": (policy.immediate_lag, policy.long_lag),
        "us_10y": (policy.immediate_lag, policy.long_lag),
        "estr": (policy.immediate_lag, policy.long_lag),
        "usd_broad": (policy.immediate_lag, policy.long_lag),
    }

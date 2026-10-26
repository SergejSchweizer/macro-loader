"""Explicit contract for the PostgreSQL materialized macro-feature library.

The catalog is deliberately source-controlled and closed-world: adding a raw
column to a serving table cannot implicitly add a feature to the public view.
The PostgreSQL migration and conformance checks consume this same ordered
contract.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from application.macro_features import MACRO_POLICY, MACRO_SERIES, macro_delta_lags
from application.momentum_features import MOMENTUM_POLICY
from application.return_features import (
    LEVEL_ZSCORE_WINDOW,
    LOG_RETURN_WINDOWS,
    RETURN_AUTOCORRELATION_LAG_WINDOWS,
    RETURN_MEAN_WINDOWS,
    RETURN_WINDOWS,
    VOLATILITY_WINDOWS,
    VOLATILITY_ZSCORE_WINDOW,
)
from application.trend_features import (
    DRAWDOWN_WINDOWS,
    EMA_OUTPUT_SUFFIXES,
    MACD_OUTPUT_SUFFIXES,
    ROC_WINDOWS,
    RSI_COMPOSITE_INTERNAL_WINDOW,
    RSI_WINDOWS,
    SMA_RATIO_WINDOWS,
)
from application.volatility_features import VOLATILITY_SERIES

MACRO_FEATURE_VIEW_VERSION = 13

RAW_SERIES: tuple[str, ...] = (*VOLATILITY_SERIES, *MACRO_SERIES)
SIGNED_LEVEL_SERIES: tuple[str, ...] = ("estr",)
PRICE_FEATURE_SERIES: tuple[str, ...] = tuple(
    series for series in RAW_SERIES if series not in SIGNED_LEVEL_SERIES
)
PRICE_FEATURE_FAMILIES: tuple[str, ...] = (
    "raw_log",
    "log_return",
    "geometric_return",
    "return_mean",
    "volatility",
    "sma_ratio",
    "wilder_rsi",
    "roc",
    "drawdown",
    "ema_trend",
    "macd",
    "rsi_composite",
    "roc_acceleration",
    "price_zscore",
    "return_autocorrelation",
)
RAW_COLUMNS: tuple[str, ...] = tuple(f"{series}_log_level" for series in PRICE_FEATURE_SERIES)


@dataclass(frozen=True, slots=True)
class MacroFeatureSpec:
    """One explicitly permitted materialized-view output column."""

    name: str
    family: str
    series: str | None
    formula: str


def _source_specs() -> tuple[MacroFeatureSpec, ...]:
    specs: list[MacroFeatureSpec] = []
    for series in PRICE_FEATURE_SERIES:
        # The serving view exposes the natural-log transform of each raw
        # source; the untransformed values remain in ``macro_raw``.
        specs.append(
            MacroFeatureSpec(
                f"{series}_log_level",
                "raw_log",
                series,
                "ln(source level) when source level is positive",
            )
        )
    for series in VOLATILITY_SERIES:
        specs.extend(
            MacroFeatureSpec(
                f"{series}_delta_{lag}obs",
                "delta",
                series,
                f"level(t)-level(t-{lag} valid observations)",
            )
            for lag in (1, 5, 20)
        )
        specs.append(
            MacroFeatureSpec(
                f"{series}_zscore_60obs",
                "zscore",
                series,
                "population z-score over 60 valid observations",
            )
        )
    for series, lags in macro_delta_lags(MACRO_POLICY).items():
        specs.extend(
            MacroFeatureSpec(
                f"{series}_delta_{lag}obs",
                "delta",
                series,
                f"level(t)-level(t-{lag} valid observations)",
            )
            for lag in lags
        )
    specs.extend(
        (
            MacroFeatureSpec("vix9d_vix_ratio", "term_structure", None, "vix9d/vix"),
            MacroFeatureSpec("vix_vix3m_ratio", "term_structure", None, "vix/vix3m"),
            MacroFeatureSpec("vix9d_vix3m_log_ratio", "term_structure", None, "ln(vix9d/vix3m)"),
            MacroFeatureSpec("vix3m_minus_vix", "term_structure", None, "vix3m-vix"),
            MacroFeatureSpec("vix6m_minus_vix", "term_structure", None, "vix6m-vix"),
            MacroFeatureSpec("vix1y_minus_vix", "term_structure", None, "vix1y-vix"),
            MacroFeatureSpec("us_10y_minus_us_2y", "cross_series", None, "us_10y-us_2y"),
        )
    )
    for series in PRICE_FEATURE_SERIES:
        specs.extend(
            MacroFeatureSpec(
                f"{series}_sma_ratio_{short}_{long}",
                "sma_ratio",
                series,
                f"sma(level,{short})/sma(level,{long})",
            )
            for short, long in SMA_RATIO_WINDOWS
        )
        specs.extend(
            MacroFeatureSpec(
                f"{series}_rsi_{window}obs",
                "wilder_rsi",
                series,
                f"Wilder RSI over {window} observations",
            )
            for window in RSI_WINDOWS
        )
        specs.extend(
            MacroFeatureSpec(
                f"{series}_roc_{window}obs",
                "roc",
                series,
                f"level(t)/level(t-{window} valid observations)-1",
            )
            for window in ROC_WINDOWS
        )
        specs.extend(
            MacroFeatureSpec(
                f"{series}_drawdown_{window}obs",
                "drawdown",
                series,
                f"level(t)/rolling_max(level,{window})-1",
            )
            for window in DRAWDOWN_WINDOWS
        )
        specs.extend(
            MacroFeatureSpec(
                f"{series}_{suffix}",
                "ema_trend",
                series,
                {
                    "ema5_distance": "level/EMA(5)-1",
                    "ema9_distance": "level/EMA(9)-1",
                    "ema21_distance": "level/EMA(21)-1",
                    "ema5_ema21_spread": "(EMA(5)-EMA(21))/level",
                    "ema9_slope_3": "(EMA(9)(t)-EMA(9)(t-3 source rows))/(3*level)",
                    "ema5_ema9_cross": "1/-1 for upward/downward EMA(5)/EMA(9) crossing, else 0",
                }[suffix],
            )
            for suffix in EMA_OUTPUT_SUFFIXES
        )
        specs.extend(
            MacroFeatureSpec(
                f"{series}_{suffix}",
                "macd",
                series,
                {
                    "macd_line_relative": "(EMA(12)-EMA(26))/positive level",
                    "macd_hist_relative": "(EMA(12)-EMA(26)-EMA(9 signal))/positive level",
                    "macd_hist_delta_3": "relative MACD histogram(t)-histogram(t-3 source rows)",
                }[suffix],
            )
            for suffix in MACD_OUTPUT_SUFFIXES
        )
        specs.extend(
            MacroFeatureSpec(
                f"{series}_log_return_{window}obs",
                "log_return",
                series,
                f"ln(level(t)/level(t-{window} valid observations))",
            )
            for window in LOG_RETURN_WINDOWS
        )
        specs.extend(
            MacroFeatureSpec(
                f"{series}_return_mean_{window}obs",
                "return_mean",
                series,
                f"mean of one-observation log returns over {window} valid observations",
            )
            for window in RETURN_MEAN_WINDOWS
        )
        specs.extend(
            MacroFeatureSpec(
                f"{series}_volatility_{window}obs",
                "volatility",
                series,
                "sample standard deviation of one-observation log returns over "
                f"{window} valid observations",
            )
            for window in VOLATILITY_WINDOWS
        )
        specs.extend(
            MacroFeatureSpec(
                f"{series}_momentum_autocorr_{lag}_{window}obs",
                "momentum_autocorrelation",
                series,
                f"positive autocorrelation of one-observation changes, lag={lag}, window={window}",
            )
            for lag, window in MOMENTUM_POLICY.lag_windows
        )
        specs.extend(
            (
                MacroFeatureSpec(
                    f"{series}_rsi_ratio_5_15",
                    "rsi_composite",
                    series,
                    "Wilder RSI(5)/Wilder RSI(15); RSI(15) internal only; null for undefined "
                    "or zero denominator",
                ),
                MacroFeatureSpec(
                    f"{series}_rsi5_slope_3",
                    "rsi_composite",
                    series,
                    "(Wilder RSI(5)(t)-Wilder RSI(5)(t-3 valid observations))/3",
                ),
                MacroFeatureSpec(
                    f"{series}_rsi5_delta_1",
                    "rsi_composite",
                    series,
                    "Wilder RSI(5)(t)-Wilder RSI(5)(t-1 valid observation)",
                ),
                MacroFeatureSpec(
                    f"{series}_roc_acceleration_4_8",
                    "roc_acceleration",
                    series,
                    "ROC(4)-ROC(8), where ROC(n)=level(t)/level(t-n)-1",
                ),
                MacroFeatureSpec(
                    f"{series}_volatility_zscore_{VOLATILITY_ZSCORE_WINDOW}",
                    "price_zscore",
                    series,
                    "sample z-score of volatility(8) over trailing 96 valid volatility "
                    "observations",
                ),
                MacroFeatureSpec(
                    f"{series}_level_zscore_{LEVEL_ZSCORE_WINDOW}",
                    "price_zscore",
                    series,
                    "(positive level-trailing mean)/trailing sample standard deviation over "
                    "96 valid levels",
                ),
            )
        )
        specs.extend(
            MacroFeatureSpec(
                f"{series}_momentum_autocorr_lag{lag}_window{window}",
                "return_autocorrelation",
                series,
                f"signed Pearson correlation of 1-observation log returns at lag {lag} "
                f"over {window} complete valid pairs; lag is measured before pair filtering",
            )
            for lag, window in RETURN_AUTOCORRELATION_LAG_WINDOWS
        )
    for series in PRICE_FEATURE_SERIES:
        specs.extend(
            MacroFeatureSpec(
                f"{series}_return_geom_{window}obs_pct",
                "geometric_return",
                series,
                "exp(sum of one-observation log returns over "
                f"{window} valid observations)-1; decimal fraction",
            )
            for window in RETURN_WINDOWS
        )
    for series in SIGNED_LEVEL_SERIES:
        specs.extend(
            MacroFeatureSpec(
                f"{series}_momentum_autocorr_{lag}_{window}obs",
                "momentum_autocorrelation",
                series,
                f"positive autocorrelation of one-observation changes, lag={lag}, window={window}",
            )
            for lag, window in MOMENTUM_POLICY.lag_windows
        )
    return tuple(specs)


FEATURE_CATALOG: tuple[MacroFeatureSpec, ...] = _source_specs()
FEATURE_COLUMNS: tuple[str, ...] = ("timestamp_m1", *(spec.name for spec in FEATURE_CATALOG))


def validate_feature_catalog(catalog: tuple[MacroFeatureSpec, ...] = FEATURE_CATALOG) -> None:
    """Validate the closed-world catalog before it is used by a migration."""
    names = [spec.name for spec in catalog]
    if len(names) != len(set(names)):
        raise ValueError("macro feature catalog contains duplicate columns")
    if any(not spec.name or not spec.family or not spec.formula for spec in catalog):
        raise ValueError("macro feature catalog contains incomplete metadata")
    if tuple(spec.name for spec in catalog if spec.family == "raw_log") != RAW_COLUMNS:
        raise ValueError("macro feature catalog raw-log order/coverage mismatch")
    for family in PRICE_FEATURE_FAMILIES:
        family_series = {spec.series for spec in catalog if spec.family == family}
        if family_series != set(PRICE_FEATURE_SERIES):
            raise ValueError(f"macro feature catalog {family} domain/coverage mismatch")
    momentum_series = {spec.series for spec in catalog if spec.family == "momentum_autocorrelation"}
    if momentum_series != set(RAW_SERIES):
        raise ValueError("macro feature catalog momentum domain/coverage mismatch")
    if "foo_log_level" in names:
        raise ValueError("unapproved wildcard feature in macro feature catalog")
    if any(spec.name.startswith("fed_") or spec.family.startswith("fed_") for spec in catalog):
        raise ValueError("retired Fed-policy output cannot be in the macro feature catalog")
    if RSI_COMPOSITE_INTERNAL_WINDOW in RSI_WINDOWS:
        raise ValueError("internal composite RSI period must not become a public RSI output")


def feature_catalog_payload(
    catalog: tuple[MacroFeatureSpec, ...] = FEATURE_CATALOG,
) -> tuple[dict[str, str | None], ...]:
    validate_feature_catalog(catalog)
    return tuple(
        {"name": spec.name, "family": spec.family, "series": spec.series, "formula": spec.formula}
        for spec in catalog
    )


def feature_catalog_fingerprint(
    catalog: tuple[MacroFeatureSpec, ...] = FEATURE_CATALOG,
) -> str:
    """Return a deterministic fingerprint of ordered names and formulas."""
    payload = {
        "version": MACRO_FEATURE_VIEW_VERSION,
        "columns": feature_catalog_payload(catalog),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


validate_feature_catalog()
MACRO_FEATURE_VIEW_FINGERPRINT = feature_catalog_fingerprint()

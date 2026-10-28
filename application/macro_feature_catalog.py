"""Explicit contract for the PostgreSQL materialized macro-feature library.

The catalog is deliberately source-controlled and closed-world: adding a raw
column to a serving table cannot implicitly add a feature to the public view.
The PostgreSQL migration and conformance checks consume this same ordered
contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from application.macro_features import MACRO_POLICY, MACRO_SERIES, macro_delta_lags
from application.momentum_features import MOMENTUM_POLICY
from application.registry import OHLC_FIELDS, OHLC_SERIES, SERIES_REGISTRY
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

MACRO_FEATURE_VIEW_VERSION = 17
logger = logging.getLogger(__name__)

RAW_SERIES: tuple[str, ...] = (*VOLATILITY_SERIES, *MACRO_SERIES)
REGISTERED_RAW_SERIES: tuple[str, ...] = tuple(SERIES_REGISTRY)
SIGNED_LEVEL_SERIES: tuple[str, ...] = ("estr",)
PRICE_FEATURE_SERIES: tuple[str, ...] = tuple(
    series for series in RAW_SERIES if series not in SIGNED_LEVEL_SERIES
)
EXTENDED_RAW_SERIES: tuple[str, ...] = tuple(
    series for series in REGISTERED_RAW_SERIES if series not in RAW_SERIES
)
EXTENDED_PRICE_FEATURE_SERIES: tuple[str, ...] = tuple(
    series for series in EXTENDED_RAW_SERIES if series not in SIGNED_LEVEL_SERIES
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
OHLC_BAR_FEATURE_FAMILIES: tuple[str, ...] = (
    "high_low_range",
    "intraday_return",
    "overnight_gap",
)
OHLC_STOCHASTIC_SUFFIXES: tuple[str, ...] = (
    "stoch_k_5",
    "stoch_d_5_3",
    "stoch_k_14",
    "stoch_d_14_3",
    "stoch_kd_spread_5",
    "stoch_k_slope_3",
)
OHLC_DONCHIAN_SUFFIXES: tuple[str, ...] = (
    "donchian_breakout_up",
    "donchian_breakout_down",
)
OHLC_BAR_WINDOWS: tuple[int, ...] = (1, 5)
RAW_COLUMNS: tuple[str, ...] = tuple(f"{series}_log_level" for series in PRICE_FEATURE_SERIES)


@dataclass(frozen=True, slots=True)
class MacroFeatureSpec:
    """One explicitly permitted materialized-view output column."""

    name: str
    family: str
    series: str | None
    formula: str
    postgres_type: str = "double precision"


def _source_specs(
    *,
    raw_series: tuple[str, ...] = RAW_SERIES,
    price_feature_series: tuple[str, ...] = PRICE_FEATURE_SERIES,
    include_ohlc: bool = True,
    include_cross_series: bool = True,
) -> tuple[MacroFeatureSpec, ...]:
    specs: list[MacroFeatureSpec] = []
    ohlc_series = (
        tuple(series for series in OHLC_SERIES if series in raw_series) if include_ohlc else ()
    )
    for series in price_feature_series:
        # The serving view exposes the natural-log transform of each raw
        # source; native values remain in their feature-specific raw tables.
        specs.append(
            MacroFeatureSpec(
                f"{series}_log_level",
                "raw_log",
                series,
                "ln(source level) when source level is positive",
            )
        )
    for series in ohlc_series:
        specs.extend(
            MacroFeatureSpec(
                f"{series}_{field}",
                "raw_ohlc",
                series,
                f"source {field} passthrough from native OHLC observation",
            )
            for field in OHLC_FIELDS
        )
    for series in ohlc_series:
        specs.extend(
            MacroFeatureSpec(
                f"{series}_{family}_{window}obs",
                family,
                series,
                formula,
            )
            for family, formula in (
                ("high_low_range", "mean((high-low)/close) over valid OHLC metrics"),
                ("intraday_return", "mean((close-open)/open) over valid OHLC metrics"),
                (
                    "overnight_gap",
                    "mean((open[t]-most recent prior finite close)/"
                    "most recent prior finite close) over valid open-close pairs",
                ),
            )
            for window in OHLC_BAR_WINDOWS
        )
        specs.extend(
            MacroFeatureSpec(
                f"{series}_{suffix}",
                "stochastic",
                series,
                {
                    "stoch_k_5": "100*(close-min(low,5))/(max(high,5)-min(low,5))",
                    "stoch_d_5_3": "mean(stoch_k_5 over three consecutive source observations)",
                    "stoch_k_14": "100*(close-min(low,14))/(max(high,14)-min(low,14))",
                    "stoch_d_14_3": "mean(stoch_k_14 over three consecutive source observations)",
                    "stoch_kd_spread_5": "stoch_k_5-stoch_d_5_3",
                    "stoch_k_slope_3": "(stoch_k_5(t)-stoch_k_5(t-3 source rows))/3",
                }[suffix],
            )
            for suffix in OHLC_STOCHASTIC_SUFFIXES
        )
        specs.extend(
            MacroFeatureSpec(
                f"{series}_{suffix}",
                "donchian",
                series,
                "current high strictly exceeds prior 20 valid source highs"
                if suffix.endswith("up")
                else "current low is strictly below prior 20 valid source lows",
                "boolean",
            )
            for suffix in OHLC_DONCHIAN_SUFFIXES
        )
    for series in VOLATILITY_SERIES:
        if series not in raw_series:
            continue
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
        if series not in raw_series:
            continue
        specs.extend(
            MacroFeatureSpec(
                f"{series}_delta_{lag}obs",
                "delta",
                series,
                f"level(t)-level(t-{lag} valid observations)",
            )
            for lag in lags
        )
    if include_cross_series:
        specs.extend(
            (
                MacroFeatureSpec("vix9d_vix_ratio", "term_structure", None, "vix9d/vix"),
                MacroFeatureSpec("vix_vix3m_ratio", "term_structure", None, "vix/vix3m"),
                MacroFeatureSpec(
                    "vix9d_vix3m_log_ratio", "term_structure", None, "ln(vix9d/vix3m)"
                ),
                MacroFeatureSpec("vix3m_minus_vix", "term_structure", None, "vix3m-vix"),
                MacroFeatureSpec("vix6m_minus_vix", "term_structure", None, "vix6m-vix"),
                MacroFeatureSpec("vix1y_minus_vix", "term_structure", None, "vix1y-vix"),
                MacroFeatureSpec("us_10y_minus_us_2y", "cross_series", None, "us_10y-us_2y"),
            )
        )
    for series in price_feature_series:
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
                    "ema5_distance": "level/EMA(5)-1 over positive source observations",
                    "ema9_distance": "level/EMA(9)-1 over positive source observations",
                    "ema21_distance": "level/EMA(21)-1 over positive source observations",
                    "ema5_ema21_spread": "(EMA(5)-EMA(21))/level",
                    "ema9_slope_3": "(EMA(9)(t)-EMA(9)(t-3 source rows))/(3*level)",
                    "ema5_ema9_cross": (
                        "1/-1 for upward/downward EMA(5)/EMA(9) crossing over source rows, else 0"
                    ),
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
                    "macd_line_relative": (
                        "(EMA(12)-EMA(26))/positive level; EMAs use source observations"
                    ),
                    "macd_hist_relative": (
                        "(EMA(12)-EMA(26)-EMA(9 signal))/positive level; "
                        "EMAs use source observations"
                    ),
                    "macd_hist_delta_3": ("relative MACD histogram(t)-histogram(t-3 source rows)"),
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
    for series in price_feature_series:
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
        if series not in raw_series:
            continue
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
FEATURE_POSTGRES_TYPES: dict[str, str] = {spec.name: spec.postgres_type for spec in FEATURE_CATALOG}
EXTENDED_FEATURE_CATALOG: tuple[MacroFeatureSpec, ...] = _source_specs(
    raw_series=EXTENDED_RAW_SERIES,
    price_feature_series=EXTENDED_PRICE_FEATURE_SERIES,
    include_ohlc=False,
    include_cross_series=False,
)
EXTENDED_FEATURE_COLUMNS: tuple[str, ...] = (
    "timestamp_m1",
    *(spec.name for spec in EXTENDED_FEATURE_CATALOG),
)
EXTENDED_FEATURE_POSTGRES_TYPES: dict[str, str] = {
    spec.name: spec.postgres_type for spec in EXTENDED_FEATURE_CATALOG
}
EXTENDED_MACRO_FEATURE_VIEW_VERSION = 2

# Long-form storage contains derived outputs, while native OHLC passthrough values
# remain in their dedicated raw tables and wide compatibility views.
FEATURE_VALUE_CATALOG: tuple[MacroFeatureSpec, ...] = tuple(
    spec for spec in (*FEATURE_CATALOG, *EXTENDED_FEATURE_CATALOG) if spec.family != "raw_ohlc"
)
FEATURE_VALUE_NAMES: tuple[str, ...] = tuple(spec.name for spec in FEATURE_VALUE_CATALOG)
FEATURE_VALUE_PRIMARY_NAMES: tuple[str, ...] = tuple(
    spec.name for spec in FEATURE_CATALOG if spec.family != "raw_ohlc"
)
FEATURE_VALUE_EXTENDED_NAMES: tuple[str, ...] = tuple(
    spec.name for spec in EXTENDED_FEATURE_CATALOG if spec.family != "raw_ohlc"
)
FEATURE_VALUE_SCHEMA_VERSION = 2
if len(FEATURE_VALUE_NAMES) != len(set(FEATURE_VALUE_NAMES)):
    raise ValueError("long-form feature catalog contains duplicate names")
FEATURE_VALUE_CATALOG_FINGERPRINT = hashlib.sha256(
    json.dumps(
        {
            "version": FEATURE_VALUE_SCHEMA_VERSION,
            "features": [
                {
                    "name": spec.name,
                    "family": spec.family,
                    "series": spec.series,
                    "formula": spec.formula,
                    "postgres_type": spec.postgres_type,
                }
                for spec in FEATURE_VALUE_CATALOG
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


def _validate_feature_catalog(
    catalog: tuple[MacroFeatureSpec, ...] = FEATURE_CATALOG,
    *,
    price_feature_series: tuple[str, ...] = PRICE_FEATURE_SERIES,
    raw_series: tuple[str, ...] = RAW_SERIES,
    include_ohlc: bool = True,
) -> None:
    """Validate the closed-world catalog before it is used by a migration."""
    names = [spec.name for spec in catalog]
    if len(names) != len(set(names)):
        raise ValueError("macro feature catalog contains duplicate columns")
    if any(not spec.name or not spec.family or not spec.formula for spec in catalog):
        raise ValueError("macro feature catalog contains incomplete metadata")
    expected_raw_columns = tuple(f"{series}_log_level" for series in price_feature_series)
    if tuple(spec.name for spec in catalog if spec.family == "raw_log") != expected_raw_columns:
        raise ValueError("macro feature catalog raw-log order/coverage mismatch")
    ohlc_series = (
        tuple(series for series in OHLC_SERIES if series in raw_series) if include_ohlc else ()
    )
    expected_ohlc = tuple(f"{series}_{field}" for series in ohlc_series for field in OHLC_FIELDS)
    if tuple(spec.name for spec in catalog if spec.family == "raw_ohlc") != expected_ohlc:
        raise ValueError("macro feature catalog raw-OHLC order/coverage mismatch")
    for family in OHLC_BAR_FEATURE_FAMILIES:
        family_specs = tuple(spec for spec in catalog if spec.family == family)
        expected = tuple(
            f"{series}_{family}_{window}obs"
            for series in ohlc_series
            for window in OHLC_BAR_WINDOWS
        )
        if tuple(spec.name for spec in family_specs) != expected:
            raise ValueError(f"macro feature catalog {family} domain/coverage mismatch")
        if {spec.series for spec in family_specs} != set(ohlc_series):
            raise ValueError(f"macro feature catalog {family} series mismatch")
    for family, suffixes in (
        ("stochastic", OHLC_STOCHASTIC_SUFFIXES),
        ("donchian", OHLC_DONCHIAN_SUFFIXES),
    ):
        family_specs = tuple(spec for spec in catalog if spec.family == family)
        expected = tuple(f"{series}_{suffix}" for series in ohlc_series for suffix in suffixes)
        if tuple(spec.name for spec in family_specs) != expected:
            raise ValueError(f"macro feature catalog {family} domain/coverage mismatch")
        if {spec.series for spec in family_specs} != set(ohlc_series):
            raise ValueError(f"macro feature catalog {family} series mismatch")
        expected_type = "boolean" if family == "donchian" else "double precision"
        if any(spec.postgres_type != expected_type for spec in family_specs):
            raise ValueError(f"macro feature catalog {family} type mismatch")
    if any(spec.postgres_type not in {"double precision", "boolean"} for spec in catalog):
        raise ValueError("macro feature catalog contains an unsupported PostgreSQL type")
    for family in PRICE_FEATURE_FAMILIES:
        family_series = {spec.series for spec in catalog if spec.family == family}
        if family_series != set(price_feature_series):
            raise ValueError(f"macro feature catalog {family} domain/coverage mismatch")
    momentum_series = {spec.series for spec in catalog if spec.family == "momentum_autocorrelation"}
    if momentum_series != set(raw_series):
        raise ValueError("macro feature catalog momentum domain/coverage mismatch")
    if "foo_log_level" in names:
        raise ValueError("unapproved wildcard feature in macro feature catalog")
    if any(spec.name.startswith("fed_") or spec.family.startswith("fed_") for spec in catalog):
        raise ValueError("retired Fed-policy output cannot be in the macro feature catalog")
    if RSI_COMPOSITE_INTERNAL_WINDOW in RSI_WINDOWS:
        raise ValueError("internal composite RSI period must not become a public RSI output")


def validate_feature_catalog(
    catalog: tuple[MacroFeatureSpec, ...] = FEATURE_CATALOG,
    *,
    price_feature_series: tuple[str, ...] = PRICE_FEATURE_SERIES,
    raw_series: tuple[str, ...] = RAW_SERIES,
    include_ohlc: bool = True,
) -> None:
    """Validate the closed-world catalog and report safe lifecycle diagnostics."""
    logger.debug(
        "macro feature catalog validation started series_count=%d feature_count=%d include_ohlc=%s",
        len(raw_series),
        len(catalog),
        include_ohlc,
    )
    try:
        _validate_feature_catalog(
            catalog,
            price_feature_series=price_feature_series,
            raw_series=raw_series,
            include_ohlc=include_ohlc,
        )
    except Exception as exc:
        logger.debug("macro feature catalog validation failed error_type=%s", type(exc).__name__)
        raise
    logger.debug(
        "macro feature catalog validation completed series_count=%d feature_count=%d",
        len(raw_series),
        len(catalog),
    )


def feature_catalog_payload(
    catalog: tuple[MacroFeatureSpec, ...] = FEATURE_CATALOG,
    *,
    price_feature_series: tuple[str, ...] = PRICE_FEATURE_SERIES,
    raw_series: tuple[str, ...] = RAW_SERIES,
    include_ohlc: bool = True,
) -> tuple[dict[str, str | None], ...]:
    validate_feature_catalog(
        catalog,
        price_feature_series=price_feature_series,
        raw_series=raw_series,
        include_ohlc=include_ohlc,
    )
    return tuple(
        {
            "name": spec.name,
            "family": spec.family,
            "series": spec.series,
            "formula": spec.formula,
            "postgres_type": spec.postgres_type,
        }
        for spec in catalog
    )


def feature_catalog_fingerprint(
    catalog: tuple[MacroFeatureSpec, ...] = FEATURE_CATALOG,
    *,
    view_version: int = MACRO_FEATURE_VIEW_VERSION,
    price_feature_series: tuple[str, ...] = PRICE_FEATURE_SERIES,
    raw_series: tuple[str, ...] = RAW_SERIES,
    include_ohlc: bool = True,
) -> str:
    """Return a deterministic fingerprint of ordered names and formulas."""
    payload = {
        "version": view_version,
        "columns": feature_catalog_payload(
            catalog,
            price_feature_series=price_feature_series,
            raw_series=raw_series,
            include_ohlc=include_ohlc,
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


validate_feature_catalog()
MACRO_FEATURE_VIEW_FINGERPRINT = feature_catalog_fingerprint()
validate_feature_catalog(
    EXTENDED_FEATURE_CATALOG,
    price_feature_series=EXTENDED_PRICE_FEATURE_SERIES,
    raw_series=EXTENDED_RAW_SERIES,
    include_ohlc=False,
)
EXTENDED_MACRO_FEATURE_VIEW_FINGERPRINT = feature_catalog_fingerprint(
    EXTENDED_FEATURE_CATALOG,
    view_version=EXTENDED_MACRO_FEATURE_VIEW_VERSION,
    price_feature_series=EXTENDED_PRICE_FEATURE_SERIES,
    raw_series=EXTENDED_RAW_SERIES,
    include_ohlc=False,
)

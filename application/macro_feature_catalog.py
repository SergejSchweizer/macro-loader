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
from application.return_features import RETURN_WINDOWS
from application.volatility_features import VOLATILITY_SERIES

MACRO_FEATURE_VIEW_VERSION = 1

RAW_SERIES: tuple[str, ...] = (*VOLATILITY_SERIES, *MACRO_SERIES)
RAW_COLUMNS: tuple[str, ...] = tuple(f"{series}_level" for series in RAW_SERIES)


@dataclass(frozen=True, slots=True)
class MacroFeatureSpec:
    """One explicitly permitted materialized-view output column."""

    name: str
    family: str
    series: str | None
    formula: str


def _source_specs() -> tuple[MacroFeatureSpec, ...]:
    specs: list[MacroFeatureSpec] = []
    for series in RAW_SERIES:
        specs.append(MacroFeatureSpec(f"{series}_level", "raw_level", series, "source level"))
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
            MacroFeatureSpec("vix3m_minus_vix", "term_structure", None, "vix3m-vix"),
            MacroFeatureSpec("vix6m_minus_vix", "term_structure", None, "vix6m-vix"),
            MacroFeatureSpec("vix1y_minus_vix", "term_structure", None, "vix1y-vix"),
            MacroFeatureSpec("us_10y_minus_us_2y", "cross_series", None, "us_10y-us_2y"),
        )
    )
    for series in RAW_SERIES:
        specs.extend(
            MacroFeatureSpec(
                f"{series}_momentum_autocorr_{lag}_{window}obs",
                "momentum_autocorrelation",
                series,
                f"positive autocorrelation of one-observation changes, lag={lag}, window={window}",
            )
            for lag, window in MOMENTUM_POLICY.lag_windows
        )
    for series in RAW_SERIES:
        specs.extend(
            MacroFeatureSpec(
                f"{series}_return_geom_{window}obs_pct",
                "geometric_return",
                series,
                f"geometric mean simple return over {window} valid observations, percent",
            )
            for window in RETURN_WINDOWS
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
    if tuple(spec.name for spec in catalog if spec.family == "raw_level") != RAW_COLUMNS:
        raise ValueError("macro feature catalog raw-level order/coverage mismatch")
    if set(spec.series for spec in catalog if spec.family == "raw_level") != set(RAW_SERIES):
        raise ValueError("macro feature catalog does not cover all registered raw series")
    if "foo_level" in names:
        raise ValueError("unapproved wildcard feature in macro feature catalog")


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

from application.macro_feature_catalog import (
    FEATURE_CATALOG,
    FEATURE_COLUMNS,
    MACRO_FEATURE_VIEW_FINGERPRINT,
    MACRO_FEATURE_VIEW_VERSION,
    RAW_SERIES,
    feature_catalog_fingerprint,
    validate_feature_catalog,
)
from application.momentum_features import MOMENTUM_POLICY
from application.return_features import RETURN_WINDOWS
from ingestion.postgres_gold_repository import _FEATURES_VIEW_DDL, _MIGRATIONS


def test_catalog_is_explicit_ordered_and_covers_all_raw_series() -> None:
    validate_feature_catalog()
    assert FEATURE_COLUMNS[0] == "timestamp_m1"
    assert tuple(spec.name for spec in FEATURE_CATALOG[: len(RAW_SERIES)]) == tuple(
        f"{series}_level" for series in RAW_SERIES
    )
    assert {spec.series for spec in FEATURE_CATALOG if spec.family == "raw_level"} == set(
        RAW_SERIES
    )
    assert len(FEATURE_COLUMNS) == len(set(FEATURE_COLUMNS))
    assert not any(name.startswith("foo_") for name in FEATURE_COLUMNS)


def test_catalog_contains_all_fixed_feature_families() -> None:
    names = set(FEATURE_COLUMNS)
    for series in RAW_SERIES:
        assert f"{series}_level" in names
        for lag, window in MOMENTUM_POLICY.lag_windows:
            assert f"{series}_momentum_autocorr_{lag}_{window}obs" in names
        for window in RETURN_WINDOWS:
            assert f"{series}_return_geom_{window}obs_pct" in names
    assert "vix9d_vix_ratio" in names
    assert "vix_vix3m_ratio" in names
    assert "vix9d_vix3m_log_ratio" in names
    assert "us_10y_minus_us_2y" in names
    assert "usd_broad_log_return_20obs" in names


def test_catalog_fingerprint_is_stable_and_versioned() -> None:
    assert MACRO_FEATURE_VIEW_VERSION == 1
    assert feature_catalog_fingerprint() == MACRO_FEATURE_VIEW_FINGERPRINT
    assert len(MACRO_FEATURE_VIEW_FINGERPRINT) == 64


def test_postgres_view_migration_is_populated_and_versioned() -> None:
    assert "WHERE FALSE" not in _FEATURES_VIEW_DDL
    assert "CREATE MATERIALIZED VIEW" in _FEATURES_VIEW_DDL
    assert '"vix_level"' in _FEATURES_VIEW_DDL
    assert '"vix9d_vix_ratio"' in _FEATURES_VIEW_DDL
    assert f"version={MACRO_FEATURE_VIEW_VERSION}" in " ".join(_MIGRATIONS[-1])
    assert MACRO_FEATURE_VIEW_FINGERPRINT in " ".join(_MIGRATIONS[-1])

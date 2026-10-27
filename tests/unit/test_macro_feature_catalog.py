import pytest

from application.macro_feature_catalog import (
    EXTENDED_FEATURE_CATALOG,
    EXTENDED_FEATURE_COLUMNS,
    EXTENDED_FEATURE_POSTGRES_TYPES,
    EXTENDED_MACRO_FEATURE_VIEW_FINGERPRINT,
    EXTENDED_MACRO_FEATURE_VIEW_VERSION,
    EXTENDED_PRICE_FEATURE_SERIES,
    EXTENDED_RAW_SERIES,
    FEATURE_CATALOG,
    FEATURE_COLUMNS,
    FEATURE_POSTGRES_TYPES,
    MACRO_FEATURE_VIEW_FINGERPRINT,
    MACRO_FEATURE_VIEW_VERSION,
    OHLC_BAR_FEATURE_FAMILIES,
    OHLC_DONCHIAN_SUFFIXES,
    OHLC_STOCHASTIC_SUFFIXES,
    PRICE_FEATURE_SERIES,
    RAW_SERIES,
    SIGNED_LEVEL_SERIES,
    feature_catalog_fingerprint,
    validate_feature_catalog,
)
from application.momentum_features import MOMENTUM_POLICY
from application.registry import OHLC_FIELDS, OHLC_SERIES
from application.return_features import (
    LOG_RETURN_WINDOWS,
    RETURN_MEAN_WINDOWS,
    RETURN_WINDOWS,
    VOLATILITY_WINDOWS,
)
from application.trend_features import (
    DRAWDOWN_WINDOWS,
    EMA_OUTPUT_SUFFIXES,
    EMA_PERIODS,
    MACD_OUTPUT_SUFFIXES,
    MACD_PRICE_EMA_PERIODS,
    MACD_SIGNAL_EMA_PERIOD,
    ROC_WINDOWS,
    RSI_WINDOWS,
    SMA_RATIO_WINDOWS,
)
from ingestion.postgres_gold_repository import (
    _EXTENDED_FEATURE_SERIES_DDLS,
    _EXTENDED_FEATURES_VIEW_COLUMN_TYPES_EXPECTED,
    _EXTENDED_FEATURES_VIEW_COLUMNS_EXPECTED,
    _EXTENDED_FEATURES_VIEW_DDL,
    _FEATURES_VIEW_COLUMN_TYPES_EXPECTED,
    _FEATURES_VIEW_COLUMNS_EXPECTED,
    _FEATURES_VIEW_DDL,
    _MIGRATIONS,
)

OHLC_BAR_OUTPUT_SUFFIXES = (
    "high_low_range_1obs",
    "high_low_range_5obs",
    "intraday_return_1obs",
    "intraday_return_5obs",
    "overnight_gap_1obs",
    "overnight_gap_5obs",
)


def test_catalog_is_explicit_ordered_and_covers_all_raw_series() -> None:
    validate_feature_catalog()
    assert FEATURE_COLUMNS[0] == "timestamp_m1"
    assert tuple(spec.name for spec in FEATURE_CATALOG[: len(PRICE_FEATURE_SERIES)]) == tuple(
        f"{series}_log_level" for series in PRICE_FEATURE_SERIES
    )
    assert {spec.series for spec in FEATURE_CATALOG if spec.family == "raw_log"} == set(
        PRICE_FEATURE_SERIES
    )
    assert tuple(spec.name for spec in FEATURE_CATALOG if spec.family == "raw_ohlc") == tuple(
        f"{series}_{field}" for series in OHLC_SERIES for field in OHLC_FIELDS
    )
    assert len(FEATURE_COLUMNS) == len(set(FEATURE_COLUMNS))
    assert not any(name.startswith("foo_") for name in FEATURE_COLUMNS)


def test_catalog_contains_all_fixed_feature_families() -> None:
    names = set(FEATURE_COLUMNS)
    for series in PRICE_FEATURE_SERIES:
        assert f"{series}_log_level" in names
        for lag, window in MOMENTUM_POLICY.lag_windows:
            assert f"{series}_momentum_autocorr_{lag}_{window}obs" in names
        for window in LOG_RETURN_WINDOWS:
            assert f"{series}_log_return_{window}obs" in names
        for window in RETURN_WINDOWS:
            assert f"{series}_return_geom_{window}obs_pct" in names
        for window in RETURN_MEAN_WINDOWS:
            assert f"{series}_return_mean_{window}obs" in names
        for window in VOLATILITY_WINDOWS:
            assert f"{series}_volatility_{window}obs" in names
        for window in (25, 60, 120, 240):
            assert f"{series}_return_geom_{window}obs_pct" not in names
        for short, long in SMA_RATIO_WINDOWS:
            assert f"{series}_sma_ratio_{short}_{long}" in names
        for window in RSI_WINDOWS:
            assert f"{series}_rsi_{window}obs" in names
        for window in ROC_WINDOWS:
            assert f"{series}_roc_{window}obs" in names
        for window in DRAWDOWN_WINDOWS:
            assert f"{series}_drawdown_{window}obs" in names
        assert all(f"{series}_{suffix}" in names for suffix in EMA_OUTPUT_SUFFIXES)
        assert all(f"{series}_{suffix}" in names for suffix in MACD_OUTPUT_SUFFIXES)
        new_feature_suffixes = (
            "rsi_ratio_5_15",
            "rsi5_slope_3",
            "rsi5_delta_1",
            "roc_acceleration_4_8",
            "volatility_zscore_96",
            "level_zscore_96",
            "momentum_autocorr_lag1_window96",
            "momentum_autocorr_lag4_window96",
            "momentum_autocorr_lag8_window192",
        )
        assert all(f"{series}_{suffix}" in names for suffix in new_feature_suffixes)
        group_end = FEATURE_COLUMNS.index(f"{series}_momentum_autocorr_lag8_window192")
        assert group_end < FEATURE_COLUMNS.index(f"{series}_return_geom_4obs_pct")
    assert "vix9d_vix_ratio" in names
    assert "vix_vix3m_ratio" in names
    assert "vix9d_vix3m_log_ratio" in names
    assert "us_10y_minus_us_2y" in names
    assert "usd_broad_log_return_20obs" in names
    assert "estr_level" not in names
    assert "estr_log_level" not in names
    assert not any(name.startswith("estr_ema") for name in names)
    assert not any("_ema_5_state" in name or "_ema_9_state" in name for name in names)
    assert not any(
        name.startswith("estr_")
        and any(
            name.startswith(f"estr_{family}")
            for family in (
                "log_return",
                "return_",
                "volatility_",
                "sma_ratio",
                "rsi_",
                "roc_",
                "drawdown_",
            )
        )
        for name in names
    )
    assert set(SIGNED_LEVEL_SERIES) == {"estr"}
    assert not any(name.startswith("fed_") for name in names)


def test_ohlc_bar_catalog_is_registry_scoped_ordered_and_nullable() -> None:
    specs = tuple(spec for spec in FEATURE_CATALOG if spec.family in OHLC_BAR_FEATURE_FAMILIES)
    expected = tuple(
        f"{series}_{suffix}" for series in OHLC_SERIES for suffix in OHLC_BAR_OUTPUT_SUFFIXES
    )
    spec_names = {spec.name for spec in specs}
    assert tuple(name for name in FEATURE_COLUMNS if name in spec_names) == expected
    assert tuple(spec.name for spec in specs) == tuple(
        name for name in expected if name in spec_names
    )
    assert {spec.family for spec in specs} == set(OHLC_BAR_FEATURE_FAMILIES)
    assert all(spec.series == spec.name.split("_", 1)[0] for spec in specs)
    assert len(specs) == 36
    assert len({spec.name for spec in specs}) == 36
    assert not any(name.startswith("estr_") for name in expected)
    assert all(spec.formula for spec in specs)
    from application.gold_frame import GOLD_COLUMNS
    from application.postgres_sync import POSTGRES_RAW_COLUMNS

    assert not set(expected).intersection(GOLD_COLUMNS)
    assert not set(expected).intersection(POSTGRES_RAW_COLUMNS)


def test_stochastic_and_donchian_catalog_is_registry_scoped_and_typed() -> None:
    expected_stochastic = tuple(
        f"{series}_{suffix}" for series in OHLC_SERIES for suffix in OHLC_STOCHASTIC_SUFFIXES
    )
    expected_donchian = tuple(
        f"{series}_{suffix}" for series in OHLC_SERIES for suffix in OHLC_DONCHIAN_SUFFIXES
    )
    assert tuple(spec.name for spec in FEATURE_CATALOG if spec.family == "stochastic") == (
        expected_stochastic
    )
    assert tuple(spec.name for spec in FEATURE_CATALOG if spec.family == "donchian") == (
        expected_donchian
    )
    assert len(expected_stochastic) == 36
    assert len(expected_donchian) == 12
    assert all(FEATURE_POSTGRES_TYPES[name] == "double precision" for name in expected_stochastic)
    assert all(FEATURE_POSTGRES_TYPES[name] == "boolean" for name in expected_donchian)
    from application.gold_frame import GOLD_COLUMNS
    from application.postgres_sync import POSTGRES_RAW_COLUMNS

    outputs = set(expected_stochastic) | set(expected_donchian)
    assert not outputs.intersection(GOLD_COLUMNS)
    assert not outputs.intersection(POSTGRES_RAW_COLUMNS)


def test_catalog_fingerprint_is_stable_and_versioned() -> None:
    assert MACRO_FEATURE_VIEW_VERSION == 16
    assert feature_catalog_fingerprint() == MACRO_FEATURE_VIEW_FINGERPRINT
    assert len(MACRO_FEATURE_VIEW_FINGERPRINT) == 64


def test_extended_catalog_covers_every_additional_registered_positive_series() -> None:
    validate_feature_catalog(
        EXTENDED_FEATURE_CATALOG,
        price_feature_series=EXTENDED_PRICE_FEATURE_SERIES,
        raw_series=EXTENDED_RAW_SERIES,
        include_ohlc=False,
    )
    assert EXTENDED_RAW_SERIES == (
        "vstoxx1m",
        "vstoxx2m",
        "vstoxx3m",
        "ciss_money",
        "ciss_bonds",
        "ciss_equity",
        "ciss_financials",
        "euro_hy_yield",
        "euro_hy_ytw",
        "euro_hy_total_return",
        "us_3m",
        "us_5y",
    )
    assert EXTENDED_PRICE_FEATURE_SERIES == EXTENDED_RAW_SERIES
    assert len(EXTENDED_FEATURE_COLUMNS) == 793
    assert len(EXTENDED_FEATURE_CATALOG) == 792
    assert len(EXTENDED_MACRO_FEATURE_VIEW_FINGERPRINT) == 64
    assert EXTENDED_MACRO_FEATURE_VIEW_VERSION == 1
    assert (
        "timestamp_m1",
        "available_at_utc",
        *EXTENDED_FEATURE_COLUMNS[1:],
    ) == _EXTENDED_FEATURES_VIEW_COLUMNS_EXPECTED
    assert len(_EXTENDED_FEATURES_VIEW_COLUMNS_EXPECTED) == 794
    assert len(_EXTENDED_FEATURES_VIEW_COLUMNS_EXPECTED) < 1600
    assert len(_EXTENDED_FEATURE_SERIES_DDLS) == len(EXTENDED_RAW_SERIES)
    assert _EXTENDED_FEATURES_VIEW_COLUMN_TYPES_EXPECTED == {
        "timestamp_m1": "timestamp(6) with time zone",
        "available_at_utc": "timestamp(6) with time zone",
        **EXTENDED_FEATURE_POSTGRES_TYPES,
    }
    assert 'CREATE MATERIALIZED VIEW IF NOT EXISTS "macro_loader"."macro_features_extended"' in (
        _EXTENDED_FEATURES_VIEW_DDL
    )
    for series, series_ddl in zip(
        EXTENDED_PRICE_FEATURE_SERIES, _EXTENDED_FEATURE_SERIES_DDLS, strict=True
    ):
        assert f'"macro_loader"."macro_raw_{series}"' in series_ddl
        for family in (
            "log_return",
            "return_geom",
            "return_mean",
            "volatility",
            "sma_ratio",
            "rsi",
            "roc",
            "drawdown",
            "ema",
            "macd",
        ):
            assert any(name.startswith(f"{series}_{family}") for name in EXTENDED_FEATURE_COLUMNS)
    for series in RAW_SERIES:
        assert all(
            f'"macro_loader"."macro_raw_{series}"' not in ddl
            for ddl in _EXTENDED_FEATURE_SERIES_DDLS
        )
    assert not any(name.startswith("estr_") for name in EXTENDED_FEATURE_COLUMNS)


def test_catalog_rejects_duplicate_columns() -> None:
    with pytest.raises(ValueError, match="duplicate columns"):
        validate_feature_catalog(FEATURE_CATALOG[:-1] + (FEATURE_CATALOG[0],))


def test_catalog_and_view_exclude_all_retired_fed_features() -> None:
    retired = (
        "fed_next_expected_move_bp",
        "fed_path_slope_m3_bp",
        "fed_next_uncertainty_bp",
        "fed_repricing_5obs_bp",
    )
    assert not any(name.startswith("fed_") for name in FEATURE_COLUMNS)
    assert all(origin not in _FEATURES_VIEW_DDL for origin in retired)


def test_postgres_view_migration_is_populated_and_versioned() -> None:
    assert "WHERE FALSE" not in _FEATURES_VIEW_DDL
    assert "CREATE MATERIALIZED VIEW" in _FEATURES_VIEW_DDL
    assert _FEATURES_VIEW_COLUMNS_EXPECTED[:2] == ("timestamp_m1", "available_at_utc")
    assert _FEATURES_VIEW_COLUMNS_EXPECTED[2:] == FEATURE_COLUMNS[1:]
    assert 'raw."available_at_utc" AS "available_at_utc"' in _FEATURES_VIEW_DDL
    for series in OHLC_SERIES:
        for field in OHLC_FIELDS:
            name = f"{series}_{field}"
            assert name in FEATURE_COLUMNS
            assert f'raw."{name}" AS "{name}"' in _FEATURES_VIEW_DDL
    assert len([spec for spec in FEATURE_CATALOG if spec.family == "raw_ohlc"]) == 24
    bar_specs = tuple(spec for spec in FEATURE_CATALOG if spec.family in OHLC_BAR_FEATURE_FAMILIES)
    assert len(bar_specs) == 36
    for spec in bar_specs:
        assert spec.name in _FEATURES_VIEW_DDL
    for series in OHLC_SERIES:
        for suffix in (*OHLC_STOCHASTIC_SUFFIXES, *OHLC_DONCHIAN_SUFFIXES):
            assert f"{series}_{suffix}" in FEATURE_COLUMNS
            assert f"{series}_{suffix}" in _FEATURES_VIEW_DDL
    assert "ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING" in _FEATURES_VIEW_DDL
    assert 'AS "vix_donchian_breakout_up"' in _FEATURES_VIEW_DDL
    assert '"vix_log_level"' in _FEATURES_VIEW_DDL
    assert 'THEN ln(raw."vix_level") END AS "vix_log_level"' in _FEATURES_VIEW_DDL
    assert '"vix9d_vix_ratio"' in _FEATURES_VIEW_DDL
    migration_sql = " ".join(statement for migration in _MIGRATIONS for statement in migration)
    assert f"version={MACRO_FEATURE_VIEW_VERSION}" in migration_sql
    assert MACRO_FEATURE_VIEW_FINGERPRINT in migration_sql
    assert '"fed_macro_raw"' in migration_sql
    assert '"fed_macro_features"' in migration_sql
    assert 'DROP COLUMN IF EXISTS "fed_next_expected_move_bp"' in migration_sql
    assert 'raw."available_at_utc" AS "available_at_utc"' in _FEATURES_VIEW_DDL
    assert _FEATURES_VIEW_COLUMN_TYPES_EXPECTED["vix_donchian_breakout_up"] == "boolean"
    assert _FEATURES_VIEW_COLUMN_TYPES_EXPECTED["vix_stoch_k_5"] == "double precision"


def test_ema_view_uses_private_sma_seeded_resettable_recurrences() -> None:
    assert len(EMA_PERIODS) == 3
    for period in EMA_PERIODS:
        assert f"2.0 / {period + 1}.0" in _FEATURES_VIEW_DDL
        assert f"/ {period}.0" in _FEATURES_VIEW_DDL
    assert "WITH RECURSIVE input AS" in _FEATURES_VIEW_DDL
    assert "OR input.levels[state.observation_index + 1] <= 0 THEN NULL" in _FEATURES_VIEW_DDL
    assert 'array_agg("vix_level" ORDER BY timestamp_m1)' in _FEATURES_VIEW_DDL
    assert 'AS "vix_ema5_distance"' in _FEATURES_VIEW_DDL
    assert 'AS "vix_ema9_slope_3"' in _FEATURES_VIEW_DDL
    assert 'AS "vix_ema5_ema9_cross"' in _FEATURES_VIEW_DDL
    assert "THEN 1.0::double precision" in _FEATURES_VIEW_DDL
    assert "ELSE 0.0::double precision END END AS" in _FEATURES_VIEW_DDL
    assert 'AS "estr_ema5_distance"' not in _FEATURES_VIEW_DDL
    assert 'AS "vix_ema_5_state"' not in _FEATURES_VIEW_DDL
    assert 'AS "vix_ema_aligned"' not in _FEATURES_VIEW_DDL
    assert len([spec for spec in FEATURE_CATALOG if spec.family == "ema_trend"]) == (
        len(PRICE_FEATURE_SERIES) * len(EMA_OUTPUT_SUFFIXES)
    )
    assert len(_MIGRATIONS) == 34
    previous_migration_sql = " ".join(_MIGRATIONS[24])
    assert 'ALTER TABLE "macro_loader"."macro_raw"' in previous_migration_sql
    assert 'ADD COLUMN IF NOT EXISTS "vix_open" DOUBLE PRECISION NULL' in previous_migration_sql
    treasury_migration_sql = " ".join(_MIGRATIONS[-5])
    assert 'ADD COLUMN IF NOT EXISTS "us_5y_level" DOUBLE PRECISION NULL' in treasury_migration_sql
    assert "DROP TABLE" not in treasury_migration_sql
    assert "DROP MATERIALIZED VIEW" in treasury_migration_sql
    assert f"version={MACRO_FEATURE_VIEW_VERSION}" in treasury_migration_sql
    assert MACRO_FEATURE_VIEW_FINGERPRINT in treasury_migration_sql
    cutover_migration_sql = " ".join(_MIGRATIONS[-4])
    assert "DROP TABLE" in cutover_migration_sql
    assert 'CREATE TABLE IF NOT EXISTS "macro_loader"."macro_raw_vix"' in cutover_migration_sql
    extended_view_migration_sql = " ".join(_MIGRATIONS[-3])
    assert _EXTENDED_FEATURES_VIEW_DDL in _MIGRATIONS[-3]
    assert "macro_features_extended" in extended_view_migration_sql
    assert EXTENDED_MACRO_FEATURE_VIEW_FINGERPRINT in extended_view_migration_sql
    assert (
        "REFRESH MATERIALIZED VIEW macro_loader.macro_features_extended"
        in extended_view_migration_sql
    )
    assert "SET jit = off" in extended_view_migration_sql


@pytest.mark.unit
def test_macd_catalog_and_sql_keep_signed_signal_ema_private_and_causal() -> None:
    macd_specs = tuple(spec.name for spec in FEATURE_CATALOG if spec.family == "macd")
    expected_columns = tuple(
        f"{series}_{suffix}" for series in PRICE_FEATURE_SERIES for suffix in MACD_OUTPUT_SUFFIXES
    )
    assert macd_specs == expected_columns
    assert len(macd_specs) == len(PRICE_FEATURE_SERIES) * len(MACD_OUTPUT_SUFFIXES)
    assert not any(name.startswith("estr_macd_") for name in FEATURE_COLUMNS)
    assert not any(
        name.endswith(("_macd_line", "_macd_signal", "_macd_signal_ema_state"))
        for name in FEATURE_COLUMNS
    )
    assert MACD_PRICE_EMA_PERIODS == (12, 26)
    assert MACD_SIGNAL_EMA_PERIOD == 9
    for series in PRICE_FEATURE_SERIES:
        for suffix in MACD_OUTPUT_SUFFIXES:
            assert f"{series}_{suffix}" in _FEATURES_VIEW_DDL
    assert "2.0 / 13.0" in _FEATURES_VIEW_DDL
    assert "2.0 / 27.0" in _FEATURES_VIEW_DDL
    assert "2.0 / 10.0" in _FEATURES_VIEW_DDL
    assert 'array_agg("macd_line" ORDER BY timestamp_m1)' in _FEATURES_VIEW_DDL
    assert 'AS "vix_macd_line_relative"' in _FEATURES_VIEW_DDL
    assert 'AS "vix_macd_hist_relative"' in _FEATURES_VIEW_DDL
    assert 'AS "vix_macd_hist_delta_3"' in _FEATURES_VIEW_DDL
    assert 'AS "estr_macd_line_relative"' not in _FEATURES_VIEW_DDL
    assert "lag(macd_hist_relative, 3) OVER ordered AS macd_hist_lag_3" in _FEATURES_VIEW_DDL
    signed_seed = "CASE WHEN input.levels[1] IS NOT NULL THEN 1 ELSE 0 END AS seed_count"
    assert signed_seed in _FEATURES_VIEW_DDL
    assert len(_MIGRATIONS) == 34
    migration_sql = " ".join(_MIGRATIONS[-4])
    assert "DROP MATERIALIZED VIEW" in migration_sql
    assert "version=16" in migration_sql
    assert MACRO_FEATURE_VIEW_FINGERPRINT in migration_sql

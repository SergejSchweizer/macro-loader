"""Real-psycopg coverage for the disposable PostgreSQL CI service."""

from __future__ import annotations

import math
import os
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from statistics import stdev
from threading import Event, Thread

import polars as pl
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import ingestion.postgres_gold_repository as postgres_module
from application.gold_catalog import GoldBuildStatus, GoldCatalogRecord
from application.gold_frame import GOLD_COLUMNS, GOLD_FEATURE_VERSION, GOLD_SCHEMA_VERSION
from application.macro_feature_catalog import (
    FEATURE_COLUMNS,
    FEATURE_POSTGRES_TYPES,
    MACRO_FEATURE_VIEW_FINGERPRINT,
    MACRO_FEATURE_VIEW_VERSION,
    PRICE_FEATURE_SERIES,
)
from application.postgres_conformance import PostgresConformanceReport
from application.postgres_conformance_service import GoldPostgresConformanceVerifier
from application.postgres_sync import (
    POSTGRES_CONSUMER_SCHEMA,
    POSTGRES_DATASET_ID,
    POSTGRES_OHLC_COLUMNS,
    POSTGRES_RAW_COLUMNS,
    POSTGRES_RAW_TABLES,
    GoldDeltaPlan,
    GoldRowDigest,
    GoldRowPayload,
    GoldSyncState,
    GoldSyncTransaction,
)
from application.postgres_sync_service import GoldPostgresDeltaSync
from application.registry import OHLC_FIELDS, OHLC_SERIES
from ingestion.postgres_conformance_verifier import PostgresLiveDatabaseConformanceInspector
from ingestion.postgres_gold_repository import (
    PostgresAdminConfig,
    PostgresGoldSchemaMigrator,
    PostgresGoldSyncRepository,
    PostgresLockContentionError,
    PostgresSyncConfig,
    PostgresTimeoutPolicy,
)
from scripts.provision_postgres_role import provision_sql

pytestmark = pytest.mark.xdist_group("postgres-real")

_ROLLBACK_STATE_SQL = """
INSERT INTO macro_loader_sync.gold_sync_state (
    dataset_id, source_build_id, data_sha256, schema_version, feature_version,
    row_count, min_timestamp, max_timestamp, synced_at_utc
) VALUES ('rollback', 'build', %s, 2, 1, 0, NULL, NULL, %s)
"""


@pytest.fixture
def postgres_dsn() -> str:
    dsn = os.environ.get("POSTGRES_TEST_DSN")
    if dsn is None:
        pytest.skip("POSTGRES_TEST_DSN is required for disposable PostgreSQL integration coverage")
    target = conninfo_to_dict(dsn)
    if (
        target.get("dbname") != "macro_loader_test"
        or target.get("user") != "macro_loader_test"
        or target.get("host", "localhost") not in {"localhost", "127.0.0.1", "::1"}
    ):
        pytest.fail(
            "real-PostgreSQL integration tests require the disposable local "
            "macro_loader_test database and macro_loader_test role"
        )
    return dsn


def _postgres_target(dsn: str) -> tuple[str, int, str]:
    """Resolve the disposable test target so local and CI ports both work."""
    settings = conninfo_to_dict(dsn)
    return (
        settings.get("host", "localhost"),
        int(settings.get("port", "5432")),
        settings.get("dbname", "macro_loader_test"),
    )


def _insert_level_values(
    connection: psycopg.Connection[object],
    timestamps: Sequence[datetime],
    series_values: dict[str, Sequence[float | None]],
) -> None:
    """Write test fixtures through the feature-table layout used by current syncs."""
    for index, timestamp in enumerate(timestamps):
        for series, observations in series_values.items():
            value = observations[index]
            if value is None:
                continue
            if series in OHLC_SERIES:
                columns = ("timestamp_m1", "close")
                row = (timestamp, value)
            else:
                columns = ("timestamp_m1", "indexvalue")
                row = (timestamp, value)
            quoted = ", ".join('"' + column + '"' for column in columns)
            placeholders = ", ".join("%s" for _ in columns)
            connection.execute(
                f'INSERT INTO macro_loader."macro_raw_{series}" ({quoted}) VALUES ({placeholders})',
                row,
            )


def _clear_feature_tables(connection: psycopg.Connection[object]) -> None:
    for table in POSTGRES_RAW_TABLES:
        connection.execute(f'DELETE FROM macro_loader."{table}"')


def _runtime_dsn(dsn: str, user: str, password: str) -> str:
    return make_conninfo(dsn, user=user, password=password)


@pytest.fixture
def repository(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> PostgresGoldSyncRepository:
    host, port, database = _postgres_target(postgres_dsn)
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS macro_loader_retired CASCADE")
        connection.execute("DROP SCHEMA IF EXISTS macro_loader_sync CASCADE")
        connection.execute("DROP SCHEMA IF EXISTS macro_loader CASCADE")
        connection.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'macro-loader-owner') "
            'THEN CREATE ROLE "macro-loader-owner" NOLOGIN; END IF; '
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'macro-loader') "
            'THEN CREATE ROLE "macro-loader" LOGIN NOSUPERUSER NOCREATEDB '
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'macro-loader-sync') "
            'THEN CREATE ROLE "macro-loader-sync" LOGIN NOSUPERUSER NOCREATEDB '
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$"
        )
        connection.execute("CREATE SCHEMA macro_loader")
        connection.execute("CREATE SCHEMA macro_loader_sync")
        connection.execute("ALTER ROLE \"macro-loader-sync\" PASSWORD 'runtime-secret'")
    monkeypatch.setattr(postgres_module, "POSTGRES_HOST", host)
    monkeypatch.setattr(postgres_module, "POSTGRES_PORT", port)
    monkeypatch.setattr(postgres_module, "POSTGRES_USER", "macro-loader")
    monkeypatch.setattr(postgres_module, "POSTGRES_SYNC_USER", "macro-loader-sync")
    return PostgresGoldSyncRepository(
        PostgresSyncConfig(host, port, "macro-loader-sync", database, "runtime-secret")
    )


@pytest.fixture
def migrator(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> PostgresGoldSchemaMigrator:
    host, port, database = _postgres_target(postgres_dsn)
    monkeypatch.setattr(postgres_module, "POSTGRES_HOST", host)
    monkeypatch.setattr(postgres_module, "POSTGRES_PORT", port)
    return PostgresGoldSchemaMigrator(
        PostgresAdminConfig(host, port, "macro_loader_admin", database, "admin"),
        connection_factory=lambda _config: psycopg.connect(postgres_dsn),
    )


def _timestamp(day: int) -> datetime:
    return datetime(2026, 8, day, 12, 34, 56, 123456, tzinfo=UTC)


# Independent frozen expectations for PR-170. Do not replace these with the
# production feature catalog: this is the integration lane's schema oracle.
_QA_RAW_SERIES = (
    "vix",
    "vix9d",
    "vix3m",
    "vix6m",
    "vix1y",
    "vstoxx",
    "move",
    "ciss",
    "euro_hy_oas",
    "us_2y",
    "us_10y",
    "estr",
    "usd_broad",
)
_QA_PRICE_SERIES = tuple(series for series in _QA_RAW_SERIES if series != "estr")
# Frozen in catalog order; intentionally independent of the production
# registry/catalog.
_QA_OHLC_COLUMNS = (
    "vix_open",
    "vix_high",
    "vix_low",
    "vix_close",
    "vix9d_open",
    "vix9d_high",
    "vix9d_low",
    "vix9d_close",
    "vix3m_open",
    "vix3m_high",
    "vix3m_low",
    "vix3m_close",
    "vix6m_open",
    "vix6m_high",
    "vix6m_low",
    "vix6m_close",
    "vix1y_open",
    "vix1y_high",
    "vix1y_low",
    "vix1y_close",
    "move_open",
    "move_high",
    "move_low",
    "move_close",
)
_QA_LOG_WINDOWS = (1, 2, 3, 4, 5, 8, 10, 20)
_QA_GEOMETRIC_WINDOWS = (4, 5, 8, 10, 16, 20)
_QA_MEAN_WINDOWS = (5, 10, 20)
_QA_VOLATILITY_WINDOWS = (5, 8, 10, 20, 32, 40, 96)
_QA_SMA_RATIOS = ((5, 15), (5, 20), (10, 20), (10, 40))
_QA_RSI_WINDOWS = (3, 5, 7, 14)
_QA_ROC_WINDOWS = (1, 2, 3, 4, 5, 8, 10, 20)
_QA_DRAWDOWN_WINDOWS = (20, 32, 60, 96)
_QA_OHLC_BAR_SUFFIXES = (
    "high_low_range_1obs",
    "high_low_range_5obs",
    "intraday_return_1obs",
    "intraday_return_5obs",
    "overnight_gap_1obs",
    "overnight_gap_5obs",
)


def _qa_expected_view_columns() -> tuple[str, ...]:
    """Frozen complete consumer order, independent of the executable catalog."""
    columns = ["timestamp_m1", "available_at_utc"]
    columns.extend(f"{series}_log_level" for series in _QA_PRICE_SERIES)
    columns.extend(_QA_OHLC_COLUMNS)
    columns.extend(
        f"{series}_{suffix}"
        for series in ("vix", "vix9d", "vix3m", "vix6m", "vix1y", "move")
        for suffix in _QA_OHLC_BAR_SUFFIXES
    )
    for series in _QA_RAW_SERIES[:7]:
        columns.extend(f"{series}_delta_{lag}obs" for lag in (1, 5, 20))
        columns.append(f"{series}_zscore_60obs")
    for series, lags in (
        ("ciss", (1, 5, 20)),
        ("euro_hy_oas", (1, 5, 20)),
        ("us_2y", (1, 20)),
        ("us_10y", (1, 20)),
        ("estr", (1, 20)),
        ("usd_broad", (1, 20)),
    ):
        columns.extend(f"{series}_delta_{lag}obs" for lag in lags)
    columns.extend(
        (
            "vix9d_vix_ratio",
            "vix_vix3m_ratio",
            "vix9d_vix3m_log_ratio",
            "vix3m_minus_vix",
            "vix6m_minus_vix",
            "vix1y_minus_vix",
            "us_10y_minus_us_2y",
        )
    )
    for series in _QA_PRICE_SERIES:
        columns.extend(f"{series}_sma_ratio_{short}_{long}" for short, long in _QA_SMA_RATIOS)
        columns.extend(f"{series}_rsi_{window}obs" for window in _QA_RSI_WINDOWS)
        columns.extend(f"{series}_roc_{window}obs" for window in _QA_ROC_WINDOWS)
        columns.extend(f"{series}_drawdown_{window}obs" for window in _QA_DRAWDOWN_WINDOWS)
        columns.extend(f"{series}_log_return_{window}obs" for window in _QA_LOG_WINDOWS)
        columns.extend(f"{series}_return_mean_{window}obs" for window in _QA_MEAN_WINDOWS)
        columns.extend(f"{series}_volatility_{window}obs" for window in _QA_VOLATILITY_WINDOWS)
        columns.extend(
            f"{series}_momentum_autocorr_{lag}_{window}obs"
            for lag, window in ((1, 60), (5, 60), (20, 120))
        )
        columns.extend(
            f"{series}_{feature}"
            for feature in (
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
        )
    for series in _QA_PRICE_SERIES:
        columns.extend(f"{series}_return_geom_{window}obs_pct" for window in _QA_GEOMETRIC_WINDOWS)
    columns.extend(
        f"estr_momentum_autocorr_{lag}_{window}obs" for lag, window in ((1, 60), (5, 60), (20, 120))
    )
    return tuple(columns)


def _qa_reference_features(levels: list[float]) -> dict[str, float | None]:
    """Independent ordinary-positive-level oracle, including Wilder recurrence."""
    log_steps: list[float | None] = [None]
    for previous, current in zip(levels[:-1], levels[1:], strict=True):
        log_steps.append(math.log(current / previous))

    expected: dict[str, float | None] = {}
    for window in _QA_LOG_WINDOWS:
        expected[f"log_return_{window}"] = (
            math.log(levels[-1] / levels[-1 - window]) if len(levels) > window else None
        )
    for window in _QA_GEOMETRIC_WINDOWS:
        expected[f"return_geom_{window}_pct"] = (
            math.exp(sum(log_steps[-window:])) - 1.0 if len(levels) > window else None
        )
    for window in _QA_MEAN_WINDOWS:
        expected[f"return_mean_{window}"] = (
            sum(log_steps[-window:]) / window if len(levels) > window else None
        )
    for window in _QA_VOLATILITY_WINDOWS:
        expected[f"volatility_{window}"] = (
            stdev([value for value in log_steps[-window:] if value is not None])
            if len(levels) > window
            else None
        )
    for short, long in _QA_SMA_RATIOS:
        expected[f"sma_ratio_{short}_{long}"] = (
            (sum(levels[-short:]) / short) / (sum(levels[-long:]) / long)
            if len(levels) >= long
            else None
        )
    for period in _QA_RSI_WINDOWS:
        gains = [max(levels[index] - levels[index - 1], 0.0) for index in range(1, len(levels))]
        losses = [max(levels[index - 1] - levels[index], 0.0) for index in range(1, len(levels))]
        if len(gains) < period:
            expected[f"rsi_{period}"] = None
            continue
        average_gain = sum(gains[:period]) / period
        average_loss = sum(losses[:period]) / period
        for gain, loss in zip(gains[period:], losses[period:], strict=True):
            average_gain = ((period - 1) * average_gain + gain) / period
            average_loss = ((period - 1) * average_loss + loss) / period
        if average_gain == 0.0 and average_loss == 0.0:
            expected[f"rsi_{period}"] = None
        elif average_loss == 0.0:
            expected[f"rsi_{period}"] = 100.0
        else:
            expected[f"rsi_{period}"] = 100.0 - 100.0 / (1.0 + average_gain / average_loss)
    for lag in _QA_ROC_WINDOWS:
        expected[f"roc_{lag}"] = levels[-1] / levels[-1 - lag] - 1.0 if len(levels) > lag else None
    for window in _QA_DRAWDOWN_WINDOWS:
        expected[f"drawdown_{window}"] = (
            levels[-1] / max(levels[-window:]) - 1.0 if len(levels) >= window else None
        )
    return expected


def _qa_public_feature_column(reference_name: str) -> str:
    """Map an independent reference label to its frozen public SQL name."""
    if reference_name.startswith("log_return_"):
        return f"vix_{reference_name}obs"
    if reference_name.startswith("return_geom_"):
        window = reference_name.removeprefix("return_geom_").removesuffix("_pct")
        return f"vix_return_geom_{window}obs_pct"
    if reference_name.startswith("return_mean_"):
        return f"vix_{reference_name}obs"
    if reference_name.startswith("volatility_"):
        return f"vix_{reference_name}obs"
    if reference_name.startswith("sma_ratio_"):
        return f"vix_{reference_name}"
    if reference_name.startswith("rsi_"):
        return f"vix_{reference_name}obs"
    if reference_name.startswith("roc_"):
        return f"vix_{reference_name}obs"
    if reference_name.startswith("drawdown_"):
        return f"vix_{reference_name}obs"
    raise AssertionError(f"unknown independent feature label: {reference_name}")


def _independent_wilder_rsi(levels: list[float], period: int) -> float | None:
    """Small reference implementation independent of the SQL view builder."""
    average_gain: float | None = None
    average_loss: float | None = None
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(levels[:-1], levels[1:], strict=True):
        if previous <= 0 or current <= 0:
            average_gain = average_loss = None
            gains.clear()
            losses.clear()
            continue
        gain = max(current - previous, 0.0)
        loss = max(previous - current, 0.0)
        if average_gain is None or average_loss is None:
            gains.append(gain)
            losses.append(loss)
            if len(gains) == period:
                average_gain = sum(gains) / period
                average_loss = sum(losses) / period
        else:
            average_gain = ((period - 1) * average_gain + gain) / period
            average_loss = ((period - 1) * average_loss + loss) / period
    if average_gain is None or average_loss is None:
        return None
    if average_gain == 0 and average_loss == 0:
        return None
    if average_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def _pr171_reference_non_null_source(levels: list[float]) -> dict[str, list[float | None]]:
    """Evaluate observation lags on the per-series non-NULL source stream."""
    size = len(levels)

    def positive(value: float | None) -> bool:
        return value is not None and math.isfinite(value) and value > 0.0

    def wilder(period: int) -> list[float | None]:
        result: list[float | None] = [None] * size
        seed: list[tuple[float, float]] = []
        avg_gain: float | None = None
        avg_loss: float | None = None
        for i in range(1, size):
            previous, current = levels[i - 1], levels[i]
            if not positive(previous) or not positive(current):
                seed.clear()
                avg_gain = avg_loss = None
                continue
            assert previous is not None and current is not None
            change = current - previous
            gain, loss = max(change, 0.0), max(-change, 0.0)
            if avg_gain is None or avg_loss is None:
                seed.append((gain, loss))
                if len(seed) < period:
                    continue
                avg_gain = sum(pair[0] for pair in seed) / period
                avg_loss = sum(pair[1] for pair in seed) / period
            else:
                avg_gain = ((period - 1) * avg_gain + gain) / period
                avg_loss = ((period - 1) * avg_loss + loss) / period
            if avg_gain == 0.0 and avg_loss == 0.0:
                result[i] = None
            elif avg_loss == 0.0:
                result[i] = 100.0
            else:
                result[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
        return result

    def pearson(xs: list[float], ys: list[float]) -> float | None:
        xbar, ybar = sum(xs) / len(xs), sum(ys) / len(ys)
        xc = [value - xbar for value in xs]
        yc = [value - ybar for value in ys]
        xss, yss = sum(value * value for value in xc), sum(value * value for value in yc)
        if xss == 0.0 or yss == 0.0:
            return None
        return sum(x * y for x, y in zip(xc, yc, strict=True)) / math.sqrt(xss * yss)

    rsi5, rsi15 = wilder(5), wilder(15)

    def ratio(i: int) -> float | None:
        numerator, denominator = rsi5[i], rsi15[i]
        if numerator is None or denominator is None or denominator == 0.0:
            return None
        return numerator / denominator

    def slope(i: int) -> float | None:
        if i < 3:
            return None
        current, prior = rsi5[i], rsi5[i - 3]
        if current is None or prior is None:
            return None
        return (current - prior) / 3.0

    def delta(i: int) -> float | None:
        if i < 1:
            return None
        current, prior = rsi5[i], rsi5[i - 1]
        if current is None or prior is None:
            return None
        return current - prior

    result: dict[str, list[float | None]] = {
        "rsi_ratio_5_15": [ratio(i) for i in range(size)],
        "rsi5_slope_3": [slope(i) for i in range(size)],
        "rsi5_delta_1": [delta(i) for i in range(size)],
    }

    roc4: list[float | None] = [None] * size
    roc8: list[float | None] = [None] * size
    for i in range(size):
        for period, target in ((4, roc4), (8, roc8)):
            if i >= period and positive(levels[i]) and positive(levels[i - period]):
                assert levels[i] is not None and levels[i - period] is not None
                target[i] = levels[i] / levels[i - period] - 1.0
    result["roc_acceleration_4_8"] = [
        None if a is None or b is None else a - b for a, b in zip(roc4, roc8, strict=True)
    ]

    valid_levels = [(i, value) for i, value in enumerate(levels) if positive(value)]
    result["level_zscore_96"] = [None] * size
    for position, (i, current) in enumerate(valid_levels):
        sample = [value for _, value in valid_levels[max(0, position - 95) : position + 1]]
        if len(sample) == 96:
            deviation = stdev(sample)
            if deviation:
                result["level_zscore_96"][i] = (current - sum(sample) / 96.0) / deviation

    logs: list[float | None] = [None] * size
    for i in range(1, size):
        if positive(levels[i - 1]) and positive(levels[i]):
            assert levels[i - 1] is not None and levels[i] is not None
            logs[i] = math.log(levels[i] / levels[i - 1])
    vol8: list[float | None] = [None] * size
    for i in range(size):
        log_window = logs[max(0, i - 7) : i + 1]
        if len(log_window) == 8 and all(value is not None for value in log_window):
            vol8[i] = stdev([value for value in log_window if value is not None])
    valid_vol = [(i, value) for i, value in enumerate(vol8) if value is not None]
    result["volatility_zscore_96"] = [None] * size
    for position, (i, current) in enumerate(valid_vol):
        sample = [value for _, value in valid_vol[max(0, position - 95) : position + 1]]
        if len(sample) == 96:
            deviation = stdev(sample)
            if deviation:
                result["volatility_zscore_96"][i] = (current - sum(sample) / 96.0) / deviation

    for lag, pair_window in ((1, 96), (4, 96), (8, 192)):
        values: list[float | None] = [None] * size
        for i in range(size):
            pairs = [
                (logs[right], logs[right - lag])
                for right in range(lag, i + 1)
                if logs[right] is not None and logs[right - lag] is not None
            ][-pair_window:]
            if len(pairs) == pair_window:
                values[i] = pearson(
                    [pair[0] for pair in pairs if pair[0] is not None],
                    [pair[1] for pair in pairs if pair[1] is not None],
                )
        result[f"momentum_autocorr_lag{lag}_window{pair_window}"] = values
    return result


def _pr171_reference(levels: Sequence[float | None]) -> dict[str, list[float | None]]:
    """Restore the compact source results to raw timestamp positions."""
    source_positions = [index for index, value in enumerate(levels) if value is not None]
    source_levels = [levels[index] for index in source_positions]
    assert all(value is not None for value in source_levels)
    calculated = _pr171_reference_non_null_source(
        [value for value in source_levels if value is not None]
    )
    aligned: dict[str, list[float | None]] = {}
    for name, values in calculated.items():
        mapped: list[float | None] = [None] * len(levels)
        for index, value in zip(source_positions, values, strict=True):
            mapped[index] = value
        aligned[name] = mapped
    return aligned


def _state(timestamp: datetime) -> GoldSyncState:
    return GoldSyncState(
        dataset_id=POSTGRES_DATASET_ID,
        source_build_id="20260828T000000Z",
        data_sha256="a" * 64,
        schema_version=GOLD_SCHEMA_VERSION,
        feature_version=GOLD_FEATURE_VERSION,
        row_count=1,
        min_timestamp=timestamp,
        max_timestamp=timestamp,
        synced_at_utc=_timestamp(28),
    )


def _gold_frame(timestamp: datetime) -> pl.DataFrame:
    data: dict[str, list[object]] = {"timestamp_m1": [timestamp]}
    for column in GOLD_COLUMNS[1:]:
        data[column] = [1.0]
    return pl.DataFrame(data).with_columns(
        pl.col("timestamp_m1").cast(pl.Datetime("us", "UTC")),
        *(pl.col(column).cast(pl.Float64) for column in GOLD_COLUMNS[1:]),
    )


def _record(timestamp: datetime) -> GoldCatalogRecord:
    return GoldCatalogRecord(
        dataset_id=POSTGRES_DATASET_ID,
        build_id="20260828T000000Z",
        status=GoldBuildStatus.COMPLETE,
        current=True,
        started_at_utc=_timestamp(1),
        completed_at_utc=_timestamp(2),
        schema_version=GOLD_SCHEMA_VERSION,
        feature_version=GOLD_FEATURE_VERSION,
        min_timestamp=timestamp,
        max_timestamp=timestamp,
        row_count=1,
        data_path="versions/build_id=20260828T000000Z/data.parquet",
        build_manifest_path="versions/build_id=20260828T000000Z/manifest.json",
        plot_path="versions/build_id=20260828T000000Z/feature_profile.png",
        pruned_at_utc=None,
    )


class _Catalog:
    def __init__(self, record: GoldCatalogRecord) -> None:
        self._record = record

    def read(self) -> list[GoldCatalogRecord]:
        return [self._record]


class _Source:
    def __init__(self, frame: pl.DataFrame) -> None:
        self._frame = frame

    def validate_bundle(self, record: GoldCatalogRecord) -> None:
        del record

    def sha256_path(self, relative_data_path: str) -> str:
        del relative_data_path
        return "a" * 64

    def read_path(self, relative_data_path: str) -> pl.DataFrame:
        del relative_data_path
        return self._frame


def _sync_service(
    repository: PostgresGoldSyncRepository, timestamp: datetime
) -> GoldPostgresDeltaSync:
    frame = _gold_frame(timestamp)
    record = _record(timestamp)
    return GoldPostgresDeltaSync(
        catalog=_Catalog(record),
        source=_Source(frame),
        repository=repository,
        clock=lambda: _timestamp(28),
    )


@pytest.mark.integration
def test_real_postgres_independent_conformance_verifier_passes(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrator.migrate()
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            provision_sql("macro_loader_test", "runtime-secret", "macro_loader_test")
        )
        connection.commit()
    timestamp = _timestamp(20)
    assert _sync_service(repository, timestamp).sync().inserted == 1

    monkeypatch.setattr(postgres_module, "POSTGRES_USER", "macro-loader")
    host, port, database = _postgres_target(postgres_dsn)
    runtime_config = PostgresSyncConfig(host, port, "macro-loader", database, "runtime-secret")
    inspector = PostgresLiveDatabaseConformanceInspector(
        runtime_config,
        connection_factory=lambda _: psycopg.connect(
            _runtime_dsn(postgres_dsn, "macro-loader", "runtime-secret")
        ),
    )
    with psycopg.connect(
        _runtime_dsn(postgres_dsn, "macro-loader", "runtime-secret")
    ) as connection:
        cursor = connection.cursor()
        try:
            for statement in postgres_module._session_configuration(runtime_config.timeout_policy):
                cursor.execute(statement)
            cursor.execute("SET TRANSACTION READ ONLY")
            inspector._assert_schema(cursor)
            inspector._assert_roles(cursor)
            inspector._assert_session(cursor)
            inspector._assert_temporal_round_trips(cursor)
        finally:
            cursor.close()
            connection.rollback()
    verifier = GoldPostgresConformanceVerifier(
        catalog=_Catalog(_record(timestamp)),
        source=_Source(_gold_frame(timestamp)),
        repository=repository,
        inspector=inspector,
    )

    assert verifier.verify() == PostgresConformanceReport(
        "PASS",
        (
            "consumer",
            "digest_index",
            "roles",
            "schema",
            "session",
            "source",
            "state",
            "summary",
            "temporal",
        ),
        {
            "consumer_row_count": 1,
            "digest_row_count": 1,
            "role_count": 3,
            "schema_table_count": 28,
            "temporal_probe_count": 2,
        },
    )


@pytest.mark.integration
def test_real_postgres_migrations_are_idempotent_and_round_trip(
    repository: PostgresGoldSyncRepository, migrator: PostgresGoldSchemaMigrator, postgres_dsn: str
) -> None:
    migrator.migrate()
    migrator.migrate()
    repository.preflight_schema()
    with psycopg.connect(postgres_dsn) as connection:
        availability_column = connection.execute(
            """SELECT data_type, datetime_precision, is_nullable, is_generated,
                      generation_expression
               FROM information_schema.columns
               WHERE table_schema = 'macro_loader' AND table_name = 'macro_raw_vix'
                 AND column_name = 'available_at_utc'"""
        ).fetchone()
        columns = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (POSTGRES_CONSUMER_SCHEMA, "macro_raw_vix"),
        ).fetchall()
        raw_tables = connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'macro_loader' AND table_type = 'BASE TABLE' "
            "AND table_name LIKE 'macro_raw_%' ORDER BY table_name"
        ).fetchall()
        assert connection.execute("SELECT to_regclass('macro_loader.macro_raw')").fetchone() == (
            None,
        )
        migrations = connection.execute(
            "SELECT version FROM macro_loader_sync.schema_migrations ORDER BY version"
        ).fetchall()
    assert {column[0] for column in columns} == {"timestamp_m1", "available_at_utc", *OHLC_FIELDS}
    assert tuple(row[0] for row in raw_tables) == tuple(sorted(POSTGRES_RAW_TABLES))
    assert not any(column[0].startswith("fed_") for column in columns)
    assert availability_column is not None
    assert availability_column[:4] == (
        "timestamp with time zone",
        6,
        "NO",
        "ALWAYS",
    )
    assert availability_column[4]
    assert migrations == [(version,) for version in range(1, len(postgres_module._MIGRATIONS) + 1)]
    assert migrations[-1] == (len(postgres_module._MIGRATIONS),)

    timestamp = _timestamp(20)
    row = GoldRowPayload(timestamp, tuple(1.0 for _ in POSTGRES_RAW_COLUMNS[1:]))
    digest = GoldRowDigest(timestamp, postgres_module.gold_row_sha256(row))
    repository.apply_delta(
        POSTGRES_DATASET_ID, GoldDeltaPlan((row,), (), (), (), (digest,)), _state(timestamp)
    )

    assert repository.summary(POSTGRES_DATASET_ID).row_count == 1
    assert repository.read_digests(POSTGRES_DATASET_ID) == (digest,)
    assert repository.read_state(POSTGRES_DATASET_ID) == _state(timestamp)
    repository_connection = repository._open()
    try:
        cursor = repository_connection.cursor()
        try:
            cursor.execute("SHOW TIME ZONE")
            assert cursor.fetchone() == ("UTC",)
        finally:
            cursor.close()
    finally:
        repository_connection.close()
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (postgres_module._advisory_lock_key(POSTGRES_DATASET_ID),),
        )
        connection.execute(_ROLLBACK_STATE_SQL, ("c" * 64, timestamp))
        connection.rollback()
        assert connection.execute(
            "SELECT COUNT(*) FROM macro_loader_sync.gold_sync_state WHERE dataset_id = 'rollback'"
        ).fetchone() == (0,)


@pytest.mark.integration
def test_real_postgres_v23_upgrade_repairs_legacy_schema_without_ohlc_columns(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    """An actual v23 layout must gain OHLC columns before its feature view is rebuilt."""
    del repository
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(postgres_module._MIGRATION_LEDGER_DDL)
        for version, migration in enumerate(postgres_module._MIGRATIONS[:23], start=1):
            for statement in migration:
                connection.execute(statement)
            connection.execute(
                f"INSERT INTO {postgres_module._MIGRATION_LEDGER} (version, applied_at_utc) "
                "VALUES (%s, CURRENT_TIMESTAMP)",
                (version,),
            )
        connection.execute('DROP MATERIALIZED VIEW "macro_loader"."macro_features"')
        drop_columns = ", ".join(f'DROP COLUMN "{column}"' for column in POSTGRES_OHLC_COLUMNS)
        drop_columns += ', DROP COLUMN "available_at_utc"'
        connection.execute(f'ALTER TABLE "macro_loader"."macro_raw" {drop_columns}')
        remaining_ohlc = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'macro_loader' AND table_name = 'macro_raw' "
            "AND (column_name LIKE '%_high' OR column_name = 'available_at_utc')"
        ).fetchall()
        assert remaining_ohlc == []

        # Simulate production drift: migration 20 is recorded, but its retired Fed
        # archive schema and the active Fed serving table have gone out of sync.
        connection.execute('DROP SCHEMA "macro_loader_retired" CASCADE')
        connection.execute(
            """CREATE TABLE "macro_loader_sync"."fed_policy_features_daily" (
                timestamp_m1 TIMESTAMPTZ(6) PRIMARY KEY,
                fed_next_expected_move_bp DOUBLE PRECISION NULL,
                fed_path_slope_m3_bp DOUBLE PRECISION NULL,
                fed_next_uncertainty_bp DOUBLE PRECISION NULL,
                fed_repricing_5obs_bp DOUBLE PRECISION NULL,
                available_at_utc TIMESTAMPTZ(6) NULL
            )"""
        )
        orphan_timestamp = _timestamp(19)
        connection.execute(
            """INSERT INTO "macro_loader_sync"."fed_policy_features_daily"
               VALUES (%s, 1.0, 2.0, 3.0, 4.0, %s)""",
            (orphan_timestamp, _timestamp(20)),
        )

    migrator.migrate()
    migrator.migrate()  # applied migrations replay without dropping the archived copy

    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT to_regclass('macro_loader.macro_raw')").fetchone() == (
            None,
        )
        raw_tables = connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'macro_loader' AND table_type = 'BASE TABLE' "
            "AND table_name LIKE 'macro_raw_%' ORDER BY table_name"
        ).fetchall()
        migration_versions = connection.execute(
            f"SELECT version FROM {postgres_module._MIGRATION_LEDGER} ORDER BY version"
        ).fetchall()
        archived_fed_row = connection.execute(
            """SELECT fed_next_expected_move_bp, fed_path_slope_m3_bp,
                      fed_next_uncertainty_bp, fed_repricing_5obs_bp
               FROM "macro_loader_retired"."fed_policy_features_daily"
               WHERE timestamp_m1 = %s""",
            (_timestamp(19),),
        ).fetchone()
        orphan_fed_relation = connection.execute(
            "SELECT to_regclass('macro_loader_sync.fed_policy_features_daily')"
        ).fetchone()
    assert tuple(row[0] for row in raw_tables) == tuple(sorted(POSTGRES_RAW_TABLES))
    assert migration_versions == [
        (version,) for version in range(1, len(postgres_module._MIGRATIONS) + 1)
    ]
    assert archived_fed_row == (1.0, 2.0, 3.0, 4.0)
    assert orphan_fed_relation == (None,)


@pytest.mark.integration
def test_real_postgres_pr172_upgrade_from_migration_22_uses_regular_runner(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    """Apply v1-v22, then prove the regular runner applies the EMA migration 23."""
    del repository
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(postgres_module._MIGRATION_LEDGER_DDL)
        for version, migration in enumerate(postgres_module._MIGRATIONS[:-1], start=1):
            for statement in migration:
                connection.execute(statement)
            connection.execute(
                f"INSERT INTO {postgres_module._MIGRATION_LEDGER} (version, applied_at_utc) "
                "VALUES (%s, CURRENT_TIMESTAMP)",
                (version,),
            )
        ledger_before_upgrade = connection.execute(
            f"SELECT version FROM {postgres_module._MIGRATION_LEDGER} ORDER BY version"
        ).fetchall()
        pre_upgrade_columns = {
            row[0]
            for row in connection.execute(
                """SELECT attname FROM pg_attribute
                   WHERE attrelid = 'macro_loader.macro_features'::regclass
                     AND attnum > 0 AND NOT attisdropped"""
            ).fetchall()
        }
        view_comment_before_upgrade = connection.execute(
            "SELECT obj_description('macro_loader.macro_features'::regclass, 'pg_class')"
        ).fetchone()

    assert ledger_before_upgrade == [(version,) for version in range(1, 23)]
    assert not any(column.endswith("_ema5_distance") for column in pre_upgrade_columns)
    assert view_comment_before_upgrade is not None
    assert view_comment_before_upgrade[0].startswith("macro feature view version=11; fingerprint=")

    # This call, not hand-executed migration-23 SQL, is the upgrade under test.
    migrator.migrate()

    with psycopg.connect(postgres_dsn) as connection:
        ledger_after_upgrade = connection.execute(
            f"SELECT version FROM {postgres_module._MIGRATION_LEDGER} ORDER BY version"
        ).fetchall()
        view_columns = {
            row[0]
            for row in connection.execute(
                """SELECT attname FROM pg_attribute
                   WHERE attrelid = 'macro_loader.macro_features'::regclass
                     AND attnum > 0 AND NOT attisdropped"""
            ).fetchall()
        }
        view_comment_after_upgrade = connection.execute(
            "SELECT obj_description('macro_loader.macro_features'::regclass, 'pg_class')"
        ).fetchone()

    assert ledger_after_upgrade == [(version,) for version in range(1, 24)]
    assert {
        f"{series}_{feature}"
        for series in PRICE_FEATURE_SERIES
        for feature in (
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
    }.issubset(view_columns)
    assert {
        f"{series}_{feature}"
        for series in PRICE_FEATURE_SERIES
        for feature in (
            "ema5_distance",
            "ema9_distance",
            "ema21_distance",
            "ema5_ema21_spread",
            "ema9_slope_3",
            "ema5_ema9_cross",
        )
    }.issubset(view_columns)
    assert view_comment_after_upgrade == (
        f"macro feature view version={MACRO_FEATURE_VIEW_VERSION}; "
        f"fingerprint={MACRO_FEATURE_VIEW_FINGERPRINT}",
    )


@pytest.mark.integration
def test_real_postgres_fed_retirement_archives_upgrade_data_and_is_replayable(
    repository: PostgresGoldSyncRepository,
    postgres_dsn: str,
) -> None:
    del repository
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(postgres_module._MIGRATION_LEDGER_DDL)
        for version, migration in enumerate(postgres_module._MIGRATIONS[:-1], start=1):
            for statement in migration:
                connection.execute(statement)
            connection.execute(
                f"INSERT INTO {postgres_module._MIGRATION_LEDGER} (version, applied_at_utc) "
                "VALUES (%s, CURRENT_TIMESTAMP)",
                (version,),
            )
    timestamp = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    expected_available = timestamp.replace(hour=0, minute=0) + timedelta(days=1)
    fed_origins = postgres_module._RETIRED_FED_ORIGIN_COLUMNS
    legacy_columns = postgres_module._RETIRED_FED_LEGACY_COLUMNS
    values = tuple(float(index + 1) for index in range(len(POSTGRES_RAW_COLUMNS[1:])))

    with psycopg.connect(postgres_dsn) as connection:
        for column in (*fed_origins, *legacy_columns):
            connection.execute(
                f'ALTER TABLE "macro_loader"."macro_raw" '
                f'ADD COLUMN "{column}" DOUBLE PRECISION NULL'
            )
        insert_columns = (*POSTGRES_RAW_COLUMNS, *fed_origins, *legacy_columns)
        quoted_insert_columns = ", ".join(f'"{column}"' for column in insert_columns)
        insert_sql = (
            'INSERT INTO "macro_loader"."macro_raw" '
            f"({quoted_insert_columns}) "
            f"VALUES ({', '.join('%s' for _ in insert_columns)})"
        )
        connection.execute(
            insert_sql,
            (timestamp, *values, 11.0, 12.0, 13.0, 14.0, 21.0, 22.0, 23.0),
        )
        connection.execute('DROP MATERIALIZED VIEW "macro_loader"."macro_features"')
        archived_view_columns = postgres_module._RETIRED_FED_VIEW_COLUMNS
        connection.execute(
            'CREATE MATERIALIZED VIEW "macro_loader"."macro_features" AS '
            "SELECT timestamp_m1, available_at_utc, "
            + ", ".join(f'51.0::DOUBLE PRECISION AS "{column}"' for column in archived_view_columns)
            + ' FROM "macro_loader"."macro_raw"'
        )
        connection.execute(
            'CREATE TABLE "macro_loader_sync"."fed_policy_features_daily" ('
            "timestamp_m1 TIMESTAMPTZ(6) PRIMARY KEY, "
            + ", ".join(f'"{column}" DOUBLE PRECISION NULL' for column in fed_origins)
            + ", available_at_utc TIMESTAMPTZ(6) NOT NULL)"
        )
        quoted_fed_columns = ", ".join('"' + column + '"' for column in fed_origins)
        connection.execute(
            'INSERT INTO "macro_loader_sync"."fed_policy_features_daily" '
            f"(timestamp_m1, {quoted_fed_columns}, available_at_utc) "
            "VALUES (%s, 31.0, 32.0, 33.0, 34.0, %s)",
            (timestamp, expected_available),
        )
        connection.execute(
            "INSERT INTO macro_loader_sync.gold_sync_state "
            "(dataset_id, source_build_id, data_sha256, schema_version, feature_version, "
            "row_count, min_timestamp, max_timestamp, synced_at_utc) "
            "VALUES ('fed_policy_features_daily', 'retired-build', %s, 1, 1, 1, %s, %s, %s)",
            ("a" * 64, timestamp, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO macro_loader_sync.gold_row_hashes "
            "(dataset_id, timestamp_m1, row_sha256) "
            "VALUES ('fed_policy_features_daily', %s, %s)",
            (timestamp, "b" * 64),
        )

        for _ in range(2):
            for statement in postgres_module._FED_POLICY_RENAME_MIGRATION:
                connection.execute(statement)
        retirement = postgres_module._RETIRED_FED_ARCHIVE_MIGRATION
        for _ in range(2):
            for statement in retirement:
                connection.execute(statement)

        raw_archive = connection.execute(
            'SELECT available_at_utc, "fed_next_expected_move_bp", "fed_path_slope_m3_bp", '
            '"fed_next_uncertainty_bp", "fed_repricing_5obs_bp" '
            'FROM "macro_loader_retired"."fed_macro_raw" WHERE timestamp_m1 = %s',
            (timestamp,),
        ).fetchone()
        policy_archive = connection.execute(
            'SELECT "fed_next_expected_move_bp", "fed_path_slope_m3_bp", '
            '"fed_next_uncertainty_bp", "fed_repricing_5obs_bp", available_at_utc '
            'FROM "macro_loader_retired"."fed_policy_features_daily" WHERE timestamp_m1 = %s',
            (timestamp,),
        ).fetchone()
        feature_archive = connection.execute(
            'SELECT available_at_utc, "fed_next_expected_move_bp", '
            '"fed_next_expected_move_bp_delta_1obs" '
            'FROM "macro_loader_retired"."fed_macro_features" WHERE timestamp_m1 = %s',
            (timestamp,),
        ).fetchone()
        legacy_archive = connection.execute(
            'SELECT "fed_3m_expected_move_bp", "fed_next_expected_move_bp_delta_5obs", '
            '"fomc_business_days_to_next" FROM "macro_loader_retired"."fed_legacy_raw_columns" '
            "WHERE timestamp_m1 = %s",
            (timestamp,),
        ).fetchone()
        state_archive = connection.execute(
            "SELECT source_build_id FROM macro_loader_retired.fed_policy_sync_state "
            "WHERE dataset_id = 'fed_policy_features_daily'"
        ).fetchone()
        hash_archive = connection.execute(
            "SELECT row_sha256 FROM macro_loader_retired.fed_policy_row_hashes "
            "WHERE dataset_id = 'fed_policy_features_daily' AND timestamp_m1 = %s",
            (timestamp,),
        ).fetchone()
        active_raw = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'macro_loader' AND table_name = 'macro_raw'"
        ).fetchall()
        active_view = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'macro_loader' AND table_name = 'macro_features'"
        ).fetchall()
        quoted_feature_columns = ", ".join(
            '"' + column + '"' for column in POSTGRES_RAW_COLUMNS[1:]
        )
        retained_values = connection.execute(
            f"SELECT {quoted_feature_columns} "
            'FROM "macro_loader"."macro_raw" '
            "WHERE timestamp_m1 = %s",
            (timestamp,),
        ).fetchone()
        archive_counts = connection.execute(
            "SELECT (SELECT count(*) FROM macro_loader_retired.fed_macro_raw), "
            "(SELECT count(*) FROM macro_loader_retired.fed_policy_features_daily), "
            "(SELECT count(*) FROM macro_loader_retired.fed_legacy_raw_columns), "
            "(SELECT count(*) FROM macro_loader_retired.fed_policy_sync_state), "
            "(SELECT count(*) FROM macro_loader_retired.fed_policy_row_hashes)"
        ).fetchone()

    assert raw_archive == (expected_available, 11.0, 12.0, 13.0, 14.0)
    assert policy_archive == (31.0, 32.0, 33.0, 34.0, expected_available)
    assert feature_archive == (expected_available, 51.0, 51.0)
    assert legacy_archive == (21.0, 22.0, 23.0)
    assert state_archive == ("retired-build",)
    assert hash_archive == ("b" * 64,)
    assert retained_values == values
    assert {column[0] for column in active_raw} == {*POSTGRES_RAW_COLUMNS, "available_at_utc"}
    assert not any(
        column[0].startswith("fed_") or column[0].startswith("fomc_")
        for column in (*active_raw, *active_view)
    )
    assert archive_counts == (1, 1, 1, 1, 1)


@pytest.mark.integration
def test_real_postgres_generated_availability_uses_next_weekday_in_raw_and_view(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    del repository
    migrator.migrate()
    observations = (
        datetime(2026, 10, 26, tzinfo=UTC),  # Monday -> Tuesday
        datetime(2026, 10, 30, tzinfo=UTC),  # Friday -> Monday
        datetime(2026, 10, 31, tzinfo=UTC),  # Saturday -> Monday
        datetime(2026, 11, 1, tzinfo=UTC),  # Sunday -> Monday
        datetime(2026, 11, 11, tzinfo=UTC),  # Veterans Day -> Thursday; holiday ignored
    )
    expected = (
        datetime(2026, 10, 27, tzinfo=UTC),
        datetime(2026, 11, 2, tzinfo=UTC),
        datetime(2026, 11, 2, tzinfo=UTC),
        datetime(2026, 11, 2, tzinfo=UTC),
        datetime(2026, 11, 12, tzinfo=UTC),
    )

    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                'INSERT INTO macro_loader.macro_raw_vix ("timestamp_m1") VALUES (%s)',
                ((timestamp,) for timestamp in observations),
            )
        connection.execute("SELECT macro_loader.refresh_macro_features_explicit()")
        raw_values = connection.execute(
            """SELECT timestamp_m1, available_at_utc FROM macro_loader.macro_raw_vix
               ORDER BY timestamp_m1"""
        ).fetchall()
        view_values = connection.execute(
            """SELECT timestamp_m1, available_at_utc FROM macro_loader.macro_features
               ORDER BY timestamp_m1"""
        ).fetchall()

    assert raw_values == list(zip(observations, expected, strict=True))
    assert view_values == list(zip(observations, expected, strict=True))


@pytest.mark.integration
@pytest.mark.parametrize(
    ("tamper", "statement"),
    (
        (
            "changed consumer row",
            'UPDATE macro_loader.macro_raw_vix SET "close" = 999.0',
        ),
        ("missing consumer row", "DELETE FROM macro_loader.macro_raw_vix"),
        (
            "changed digest",
            "UPDATE macro_loader_sync.gold_row_hashes SET row_sha256 = 'f' || repeat('f', 63)",
        ),
        ("missing digest", "DELETE FROM macro_loader_sync.gold_row_hashes"),
        (
            "stale state",
            "UPDATE macro_loader_sync.gold_sync_state SET data_sha256 = 'f' || repeat('f', 63)",
        ),
        ("missing state", "DELETE FROM macro_loader_sync.gold_sync_state"),
    ),
)
def test_real_postgres_tampering_fails_closed(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
    tamper: str,
    statement: str,
) -> None:
    migrator.migrate()
    timestamp = _timestamp(20)
    service = _sync_service(repository, timestamp)
    assert service.sync().inserted == 1

    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(statement)

    with pytest.raises(postgres_module.PostgresGoldRepositoryError, match="locked transaction"):
        service.sync()


@pytest.mark.integration
def test_real_postgres_second_locked_transaction_reads_committed_state(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
) -> None:
    migrator.migrate()
    timestamp = _timestamp(21)
    row = GoldRowPayload(timestamp, tuple(1.0 for _ in POSTGRES_RAW_COLUMNS[1:]))
    digest = GoldRowDigest(timestamp, postgres_module.gold_row_sha256(row))
    state = _state(timestamp)
    first_locked = Event()
    release_first = Event()
    second_read = Event()
    failures: list[Exception] = []
    observed_states: list[GoldSyncState | None] = []

    def first_sync() -> None:
        try:

            def operation(transaction: GoldSyncTransaction) -> None:
                first_locked.set()
                assert release_first.wait(timeout=5)
                transaction.apply_delta(
                    POSTGRES_DATASET_ID,
                    GoldDeltaPlan((row,), (), (), (), (digest,)),
                    state,
                )

            repository.run_locked(operation)
        except Exception as exc:
            failures.append(exc)

    def second_sync() -> None:
        try:

            def operation(transaction: GoldSyncTransaction) -> None:
                observed_states.append(transaction.read_state(POSTGRES_DATASET_ID))
                second_read.set()

            repository.run_locked(operation)
        except Exception as exc:
            failures.append(exc)

    first = Thread(target=first_sync)
    second = Thread(target=second_sync)
    first.start()
    assert first_locked.wait(timeout=5)
    second.start()
    assert not second_read.wait(timeout=0.1)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert observed_states == [state]


@pytest.mark.integration
def test_real_postgres_feature_view_catalog_and_unchanged_replay(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    migrator.migrate()
    service = _sync_service(repository, _timestamp(22))
    first = service.sync()
    second = service.sync()
    assert first.inserted == 1
    assert (second.inserted, second.updated, second.deleted) == (0, 0, 0)

    with psycopg.connect(postgres_dsn) as connection:
        columns = connection.execute(
            """SELECT attname FROM pg_attribute
               WHERE attrelid = 'macro_loader.macro_features'::regclass
                 AND attnum > 0 AND NOT attisdropped ORDER BY attnum"""
        ).fetchall()
        assert tuple(row[0] for row in columns) == (
            "timestamp_m1",
            "available_at_utc",
            *FEATURE_COLUMNS[1:],
        )
        comment = connection.execute(
            "SELECT obj_description('macro_loader.macro_features'::regclass, 'pg_class')"
        ).fetchone()
        assert comment == (
            f"macro feature view version={MACRO_FEATURE_VIEW_VERSION}; "
            f"fingerprint={MACRO_FEATURE_VIEW_FINGERPRINT}",
        )
        assert connection.execute(
            "SELECT count(*) FROM macro_loader.macro_features"
        ).fetchone() == (1,)


@pytest.mark.integration
def test_real_postgres_xetra_returns_match_independent_reference(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    """Compare the live view with a deliberately independent hand-calculable reference."""

    del repository
    migrator.migrate()
    levels = [100.0, 105.0, 110.0, 115.0, 120.0, 125.0]
    timestamps = [_timestamp(day) for day in range(1, len(levels) + 1)]
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            _insert_level_values(cursor.connection, timestamps, {"vix": levels})
        connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
        row = connection.execute(
            """SELECT "vix_log_return_1obs", "vix_return_geom_5obs_pct",
                      "vix_return_mean_5obs", "vix_volatility_5obs"
               FROM macro_loader.macro_features
               WHERE timestamp_m1 = %s""",
            (timestamps[-1],),
        ).fetchone()

    logs = [math.log(levels[index] / levels[index - 1]) for index in range(1, len(levels))]
    assert row is not None
    assert math.isclose(row[0], logs[-1])
    assert math.isclose(row[1], levels[-1] / levels[0] - 1.0)
    assert math.isclose(row[2], sum(logs[-5:]) / 5.0)
    assert math.isclose(row[3], stdev(logs[-5:]))
    assert not math.isclose(row[1], (levels[-1] / levels[0] - 1.0) * 100.0)


@pytest.mark.integration
def test_real_postgres_xetra_trend_momentum_matches_independent_reference(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    del repository
    migrator.migrate()
    levels = [100.0 + index for index in range(65)]
    timestamps = [datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index) for index in range(65)]
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            _insert_level_values(cursor.connection, timestamps, {"vix": levels})
        connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
        row = connection.execute(
            """SELECT "vix_sma_ratio_5_20", "vix_rsi_7obs", "vix_rsi_14obs",
                      "vix_roc_3obs", "vix_roc_5obs", "vix_roc_10obs",
                      "vix_roc_20obs", "vix_drawdown_20obs", "vix_drawdown_60obs"
               FROM macro_loader.macro_features
               WHERE timestamp_m1 = %s""",
            (timestamps[-1],),
        ).fetchone()

    assert row is not None
    expected_values = (
        (sum(levels[-5:]) / 5.0) / (sum(levels[-20:]) / 20.0),
        100.0,
        100.0,
        levels[-1] / levels[-4] - 1.0,
        levels[-1] / levels[-6] - 1.0,
        levels[-1] / levels[-11] - 1.0,
        levels[-1] / levels[-21] - 1.0,
        0.0,
        0.0,
    )
    for actual, expected_value in zip(row, expected_values, strict=True):
        assert actual is not None
        assert math.isclose(actual, expected_value)


@pytest.mark.integration
def test_real_postgres_populates_new_features_and_handles_long_invalid_history(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    del repository
    migrator.migrate()
    levels = [100.0 + index for index in range(5_005)]
    levels[30] = 0.0
    timestamps = [
        datetime(2010, 1, 1, tzinfo=UTC) + timedelta(days=index) for index in range(5_005)
    ]
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            _insert_level_values(
                cursor.connection,
                timestamps,
                {"vix": levels, "estr": [-1.0] * len(timestamps)},
            )
        connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM macro_loader.macro_raw_vix), "
            "(SELECT count(*) FROM macro_loader.macro_features)"
        ).fetchone()
        row = connection.execute(
            'SELECT "vix_rsi_7obs", "vix_rsi_14obs", '
            '"vix_momentum_autocorr_1_60obs", "vix_roc_20obs" '
            "FROM macro_loader.macro_features WHERE timestamp_m1 = %s",
            (timestamps[-1],),
        ).fetchone()
        columns = tuple(
            result[0]
            for result in connection.execute(
                "SELECT attname FROM pg_attribute "
                "WHERE attrelid = 'macro_loader.macro_features'::regclass "
                "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
            ).fetchall()
        )

    assert counts == (5_005, 5_005)
    assert row is not None
    expected_7 = _independent_wilder_rsi(levels, 7)
    expected_14 = _independent_wilder_rsi(levels, 14)
    assert expected_7 is not None and expected_14 is not None
    assert row[0] is not None and row[1] is not None
    assert math.isclose(row[0], expected_7)
    assert math.isclose(row[1], expected_14)
    assert row[2] is None
    assert math.isclose(row[3], levels[-1] / levels[-21] - 1.0)
    assert "estr_level" not in columns
    assert "estr_delta_1obs" in columns
    assert "estr_rsi_7obs" not in columns


@pytest.mark.integration
def test_real_postgres_xetra_view_contract_is_closed_world(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    del repository
    migrator.migrate()
    with psycopg.connect(postgres_dsn) as connection:
        columns = connection.execute(
            """SELECT attributes.attnum, attributes.attname,
                      format_type(attributes.atttypid, attributes.atttypmod)
               FROM pg_attribute AS attributes
               JOIN pg_class AS classes ON classes.oid = attributes.attrelid
               JOIN pg_namespace AS namespaces ON namespaces.oid = classes.relnamespace
               WHERE namespaces.nspname = 'macro_loader'
                 AND classes.relname = 'macro_features'
                 AND attributes.attnum > 0 AND NOT attributes.attisdropped
               ORDER BY attributes.attnum"""
        ).fetchall()
        comment = connection.execute(
            "SELECT obj_description('macro_loader.macro_features'::regclass, 'pg_class')"
        ).fetchone()
        view_definition = connection.execute(
            "SELECT pg_get_viewdef('macro_loader.macro_features'::regclass, true)"
        ).fetchone()

    assert tuple(column[1] for column in columns) == (
        "timestamp_m1",
        "available_at_utc",
        *FEATURE_COLUMNS[1:],
    )
    assert columns[0][2] == "timestamp(6) with time zone"
    assert columns[1][2] == "timestamp(6) with time zone"
    assert all(column[2] == FEATURE_POSTGRES_TYPES[column[1]] for column in columns[2:])
    assert comment == (
        f"macro feature view version={MACRO_FEATURE_VIEW_VERSION}; "
        f"fingerprint={MACRO_FEATURE_VIEW_FINGERPRINT}",
    )
    assert view_definition is not None
    definition = view_definition[0]
    assert all(f"return_geom_{window}obs_pct" not in definition for window in (25, 60, 120, 240))
    assert "breadth" not in definition
    assert "dispersion" not in definition


@pytest.mark.integration
def test_real_postgres_pr171_view_has_exact_independent_column_contract(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    """Check actual migrated view order against a frozen, non-catalog oracle."""
    del repository
    migrator.migrate()
    with psycopg.connect(postgres_dsn) as connection:
        actual = tuple(
            row[0]
            for row in connection.execute(
                """SELECT attname FROM pg_attribute
                   WHERE attrelid = 'macro_loader.macro_features'::regclass
                     AND attnum > 0 AND NOT attisdropped ORDER BY attnum"""
            ).fetchall()
        )

    expected = _qa_expected_view_columns()
    assert len(expected) == len(set(expected)), "independent expected contract has duplicates"
    assert actual == expected
    for series in _QA_PRICE_SERIES:
        for window in (2, 4, 8):
            assert actual.count(f"{series}_log_return_{window}obs") == 1
        for window in (4, 8, 16):
            assert actual.count(f"{series}_return_geom_{window}obs_pct") == 1
        for window in (8, 32, 96):
            assert actual.count(f"{series}_volatility_{window}obs") == 1
        assert actual.count(f"{series}_sma_ratio_5_15") == 1
        for window in (3, 5):
            assert actual.count(f"{series}_rsi_{window}obs") == 1
        for window in (1, 2, 4, 8):
            assert actual.count(f"{series}_roc_{window}obs") == 1
        for window in (32, 96):
            assert actual.count(f"{series}_drawdown_{window}obs") == 1
    assert not any(
        column.startswith("estr_")
        and any(
            token in column
            for token in (
                "log_return_",
                "return_geom_",
                "return_mean_",
                "volatility_",
                "sma_ratio_",
                "_rsi_",
                "_roc_",
                "drawdown_",
            )
        )
        for column in actual
    )


@pytest.mark.integration
def test_real_postgres_pr170_extended_windows_match_reference_and_ignore_future_rows(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    """Execute the migrated materialized view against mixed hand-checkable returns."""
    del repository
    migrator.migrate()
    levels = [100.0 + index * 0.7 + ((index % 7) - 3) * 1.1 for index in range(120)]
    timestamps = [datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index) for index in range(120)]
    target_index = 105
    target = timestamps[target_index]
    target_reference = _qa_reference_features(levels[: target_index + 1])
    feature_names = tuple(_qa_public_feature_column(name) for name in target_reference)
    select_columns = ", ".join('"' + column + '"' for column in feature_names)

    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            _insert_level_values(cursor.connection, timestamps, {"vix": levels})
        connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
        full_history_row = connection.execute(
            f"SELECT {select_columns} FROM macro_loader.macro_features WHERE timestamp_m1 = %s",
            (target,),
        ).fetchone()
        connection.execute(
            "DELETE FROM macro_loader.macro_raw_vix WHERE timestamp_m1 > %s", (target,)
        )
        connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
        truncated_row = connection.execute(
            f"SELECT {select_columns} FROM macro_loader.macro_features WHERE timestamp_m1 = %s",
            (target,),
        ).fetchone()

    assert full_history_row is not None and truncated_row is not None
    assert truncated_row == full_history_row, "future rows changed earlier view values"
    for key, actual, column in zip(target_reference, full_history_row, feature_names, strict=True):
        expected_value = target_reference[key]
        assert expected_value is not None, column
        assert actual is not None and math.isfinite(actual), column
        assert math.isclose(actual, expected_value, rel_tol=1e-10, abs_tol=1e-12), column
    geometric = full_history_row[feature_names.index("vix_return_geom_8obs_pct")]
    assert geometric is not None and abs(geometric) < 1.0


@pytest.mark.integration
def test_real_postgres_pr170_wilder_rsi_flat_gain_and_loss_boundaries(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    """Pin the three RSI zero-denominator cases against the actual materialized view."""
    del repository
    migrator.migrate()
    cases = (
        ("flat", [80.0] * 20, (None, None, None, None)),
        ("all gain", [80.0 + index for index in range(20)], (100.0,) * 4),
        ("all loss", [100.0 - index for index in range(20)], (0.0,) * 4),
    )
    timestamps = [datetime(2025, 2, 1, tzinfo=UTC) + timedelta(days=index) for index in range(20)]

    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        for _name, levels, expected in cases:
            _clear_feature_tables(connection)
            with connection.cursor() as cursor:
                _insert_level_values(cursor.connection, timestamps, {"vix": levels})
            connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
            actual = connection.execute(
                'SELECT "vix_rsi_3obs", "vix_rsi_5obs", "vix_rsi_7obs", '
                '"vix_rsi_14obs" FROM macro_loader.macro_features '
                "WHERE timestamp_m1 = %s",
                (timestamps[-1],),
            ).fetchone()
            assert actual == expected, _name


@pytest.mark.integration
def test_real_postgres_session_timeouts_bound_lock_and_statement(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch, migrator: PostgresGoldSchemaMigrator
) -> None:
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS macro_loader_sync CASCADE")
        connection.execute("DROP SCHEMA IF EXISTS macro_loader CASCADE")
        connection.execute("CREATE SCHEMA macro_loader")
        connection.execute("CREATE SCHEMA macro_loader_sync")
    host, port, database = _postgres_target(postgres_dsn)
    monkeypatch.setattr(postgres_module, "POSTGRES_HOST", host)
    monkeypatch.setattr(postgres_module, "POSTGRES_PORT", port)
    monkeypatch.setattr(postgres_module, "POSTGRES_USER", "macro_loader_test")
    policy = PostgresTimeoutPolicy(
        connect_timeout_seconds=5,
        lock_timeout_ms=200,
        statement_timeout_ms=500,
        idle_in_transaction_timeout_ms=500,
    )
    repository = PostgresGoldSyncRepository(
        PostgresSyncConfig(
            host,
            port,
            "macro_loader_test",
            database,
            "macro_loader_test",
            policy,
        )
    )
    migrator.migrate()

    session = repository._open()
    try:
        cursor = session.cursor()
        try:
            assert cursor.execute("SHOW application_name").fetchone() == ("macro-loader",)
            assert cursor.execute("SHOW lock_timeout").fetchone() == ("200ms",)
            assert cursor.execute("SHOW statement_timeout").fetchone() == ("500ms",)
            assert cursor.execute("SHOW idle_in_transaction_session_timeout").fetchone() == (
                "500ms",
            )
            started = time.monotonic()
            with pytest.raises(psycopg.errors.QueryCanceled):
                cursor.execute("SELECT pg_sleep(1)")
            assert time.monotonic() - started < 1
        finally:
            cursor.close()
    finally:
        session.close()

    with psycopg.connect(postgres_dsn) as lock_holder:
        lock_holder.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (postgres_module._advisory_lock_key(POSTGRES_DATASET_ID),),
        )
        started = time.monotonic()
        with pytest.raises(PostgresLockContentionError):
            repository.apply_delta(
                POSTGRES_DATASET_ID, GoldDeltaPlan((), (), (), (), ()), _state(_timestamp(1))
            )
        assert time.monotonic() - started < 1
        lock_holder.rollback()
    assert repository.summary(POSTGRES_DATASET_ID).row_count == 0


@pytest.mark.parametrize(
    "drift_sql",
    (
        "ALTER TABLE macro_loader_sync.gold_row_hashes DROP COLUMN row_sha256",
        "ALTER TABLE macro_loader.macro_raw_vix ADD COLUMN forbidden INTEGER",
        "ALTER TABLE macro_loader_sync.gold_sync_state "
        "ALTER COLUMN schema_version TYPE TEXT USING schema_version::text",
        "ALTER TABLE macro_loader.macro_raw_vix ALTER COLUMN timestamp_m1 TYPE TIMESTAMPTZ(3)",
        "ALTER TABLE macro_loader_sync.gold_sync_state ALTER COLUMN source_build_id DROP NOT NULL",
        "ALTER TABLE macro_loader.macro_raw_vix DROP CONSTRAINT macro_raw_vix_pkey",
    ),
    ids=("missing", "extra", "wrong-type", "wrong-precision", "wrong-nullability", "wrong-key"),
)
def test_real_postgres_schema_drift_fails_closed_before_sync(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
    drift_sql: str,
) -> None:
    migrator.migrate()
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        connection.execute(drift_sql)

    with pytest.raises(
        postgres_module.PostgresGoldRepositoryError, match="schema preflight failed"
    ):
        repository.preflight_schema()


@pytest.mark.integration
def test_real_postgres_runtime_and_sync_roles_are_least_privilege(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as admin_connection:
        admin_connection.execute(
            provision_sql("macro_loader_test", "runtime-secret", "macro_loader_test")
        )
        admin_connection.execute('DROP ROLE IF EXISTS "runtime-grant-probe"')
        admin_connection.execute('CREATE ROLE "runtime-grant-probe" NOLOGIN')
        admin_connection.execute("DROP SCHEMA IF EXISTS unrelated CASCADE")
        admin_connection.execute("CREATE SCHEMA unrelated")
        admin_connection.execute("CREATE TABLE unrelated.private_data (id INTEGER)")
        admin_connection.commit()

    runtime_dsn = _runtime_dsn(postgres_dsn, "macro-loader", "runtime-secret")
    with psycopg.connect(runtime_dsn) as runtime_connection:
        runtime_connection.execute("SELECT * FROM macro_loader_sync.schema_migrations")
        for table in POSTGRES_RAW_TABLES:
            privileges = runtime_connection.execute(
                "SELECT has_table_privilege(current_user, %s, 'SELECT'), "
                "has_table_privilege(current_user, %s, 'INSERT'), "
                "has_table_privilege(current_user, %s, 'UPDATE'), "
                "has_table_privilege(current_user, %s, 'DELETE')",
                (f"macro_loader.{table}",) * 4,
            ).fetchone()
            assert privileges == (True, False, False, False)
        for operation, statement in (
            (
                "INSERT",
                "INSERT INTO macro_loader_sync.gold_sync_state "
                "(dataset_id, source_build_id, data_sha256, schema_version, "
                "feature_version, row_count, synced_at_utc) VALUES "
                "('probe', 'probe', repeat('a', 64), 1, 1, 0, CURRENT_TIMESTAMP)",
            ),
            ("UPDATE", "UPDATE macro_loader_sync.gold_sync_state SET source_build_id = 'probe'"),
            ("DELETE", "DELETE FROM macro_loader_sync.gold_sync_state"),
            ("CREATE", "CREATE TABLE macro_loader.forbidden (id INTEGER)"),
            (
                "ALTER",
                "ALTER TABLE macro_loader.macro_raw_vix ADD COLUMN forbidden INTEGER",
            ),
            ("DROP", "DROP TABLE macro_loader.macro_raw_vix"),
            ("unrelated SELECT", "SELECT * FROM unrelated.private_data"),
        ):
            try:
                runtime_connection.execute(statement)
            except psycopg.errors.InsufficientPrivilege:
                pass
            else:
                pytest.fail(f"runtime role unexpectedly permitted {operation}")
            finally:
                runtime_connection.rollback()

        grant_option = runtime_connection.execute(
            """SELECT has_table_privilege(
                current_user,
                'macro_loader.macro_raw_vix',
                'SELECT WITH GRANT OPTION'
            )"""
        ).fetchone()
        assert grant_option == (False,)

    sync_dsn = _runtime_dsn(postgres_dsn, "macro-loader-sync", "runtime-secret")
    with psycopg.connect(sync_dsn) as sync_connection:
        for table in POSTGRES_RAW_TABLES:
            privileges = sync_connection.execute(
                "SELECT has_table_privilege(current_user, %s, 'SELECT'), "
                "has_table_privilege(current_user, %s, 'INSERT'), "
                "has_table_privilege(current_user, %s, 'UPDATE'), "
                "has_table_privilege(current_user, %s, 'DELETE')",
                (f"macro_loader.{table}",) * 4,
            ).fetchone()
            assert privileges == (True, True, True, True)
        sync_connection.execute("DELETE FROM macro_loader_sync.gold_sync_state")
        sync_connection.rollback()

    with psycopg.connect(postgres_dsn) as admin_connection:
        grant_result = admin_connection.execute(
            "SELECT has_table_privilege("
            "'runtime-grant-probe', 'macro_loader.macro_raw_vix', 'SELECT')"
        )
        assert grant_result.fetchone() == (False,)


@pytest.mark.integration
def test_real_postgres_pr171_exact_schema_and_no_raw_or_gold_leakage(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    """Assert the independent ordered contract against disposable PostgreSQL."""
    del repository
    migrator.migrate()
    with psycopg.connect(postgres_dsn) as connection:
        view_columns = connection.execute(
            """SELECT attname, format_type(atttypid, atttypmod)
               FROM pg_attribute WHERE attrelid = 'macro_loader.macro_features'::regclass
               AND attnum > 0 AND NOT attisdropped ORDER BY attnum"""
        ).fetchall()
        raw_columns = {
            row[0]
            for table in POSTGRES_RAW_TABLES
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'macro_loader' AND table_name = %s",
                (table,),
            ).fetchall()
        }
        comment = connection.execute(
            "SELECT obj_description('macro_loader.macro_features'::regclass, 'pg_class')"
        ).fetchone()

    expected = _qa_expected_view_columns()
    actual = tuple(row[0] for row in view_columns)
    additions = {
        f"{series}_{feature}"
        for series in _QA_PRICE_SERIES
        for feature in (
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
    }
    assert len(expected) == len(set(expected))
    assert actual == expected
    assert all(row[1] == FEATURE_POSTGRES_TYPES[row[0]] for row in view_columns[2:])
    assert not any(column.endswith("_rsi_15obs") for column in actual)
    assert additions.isdisjoint(raw_columns)
    assert additions.isdisjoint(GOLD_COLUMNS)
    assert comment == (
        f"macro feature view version={MACRO_FEATURE_VIEW_VERSION}; "
        f"fingerprint={MACRO_FEATURE_VIEW_FINGERPRINT}",
    )


@pytest.mark.integration
def test_real_postgres_pr171_features_match_independent_sparse_reference_and_are_causal(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    del repository
    migrator.migrate()
    count = 420
    levels: list[float | None] = [
        100.0 + index * 0.08 + ((index * 17) % 11) * 0.37 for index in range(count)
    ]
    levels[31] = 0.0
    levels[144] = None
    levels[237] = -2.0
    timestamps = [
        datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index) for index in range(count)
    ]
    series_levels = {
        series: [
            None if level is None else level * (1.0 + series_index * 0.03) + series_index * 2.0
            for level in levels
        ]
        for series_index, series in enumerate(PRICE_FEATURE_SERIES)
    }
    references = {
        series: _pr171_reference(series_values) for series, series_values in series_levels.items()
    }
    names = tuple(next(iter(references.values())))
    select_list = ", ".join(
        f'"{series}_{name}"' for series in PRICE_FEATURE_SERIES for name in names
    )
    target_index = count - 8
    target_timestamp = timestamps[target_index]
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        _insert_level_values(connection, timestamps, series_levels)
        connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
        full_rows = connection.execute(
            f"SELECT timestamp_m1, {select_list} FROM macro_loader.macro_features "
            "WHERE timestamp_m1 <= %s ORDER BY timestamp_m1",
            (target_timestamp,),
        ).fetchall()
        assert len(full_rows) == target_index + 1
        for index, row in enumerate(full_rows):
            actual_values = iter(row[1:])
            for series in PRICE_FEATURE_SERIES:
                for name in names:
                    actual = next(actual_values)
                    expected_value = references[series][name][index]
                    if expected_value is None:
                        assert actual is None, (series, name, index, actual)
                    else:
                        assert actual is not None and math.isfinite(actual), (
                            series,
                            name,
                            index,
                            actual,
                        )
                        assert math.isclose(actual, expected_value, rel_tol=2e-9, abs_tol=2e-11), (
                            series,
                            name,
                            index,
                            actual,
                            expected_value,
                        )

        full_target = full_rows[-1][1:]
        for table in POSTGRES_RAW_TABLES:
            connection.execute(
                f'DELETE FROM macro_loader."{table}" WHERE timestamp_m1 > %s',
                (target_timestamp,),
            )
        connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
        truncated_target = connection.execute(
            f"SELECT {select_list} FROM macro_loader.macro_features WHERE timestamp_m1 = %s",
            (target_timestamp,),
        ).fetchone()

    assert truncated_target == full_target
    for series in PRICE_FEATURE_SERIES:
        assert references[series]["momentum_autocorr_lag1_window96"][target_index] is not None
        assert references[series]["momentum_autocorr_lag4_window96"][target_index] is not None
        assert references[series]["momentum_autocorr_lag8_window192"][target_index] is not None


@pytest.mark.integration
def test_real_postgres_pr171_negative_correlation_and_zero_variance_are_preserved(
    repository: PostgresGoldSyncRepository,
    migrator: PostgresGoldSchemaMigrator,
    postgres_dsn: str,
) -> None:
    del repository
    migrator.migrate()
    names = (
        "vix_rsi_ratio_5_15",
        "vix_rsi5_slope_3",
        "vix_rsi5_delta_1",
        "vix_roc_acceleration_4_8",
        "vix_volatility_zscore_96",
        "vix_level_zscore_96",
        "vix_momentum_autocorr_lag1_window96",
        "vix_momentum_autocorr_lag4_window96",
        "vix_momentum_autocorr_lag8_window192",
    )
    projection = ", ".join(f'"{name}"' for name in names)
    timestamps = [datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index) for index in range(300)]
    alternating = [100.0]
    for index in range(1, len(timestamps)):
        alternating.append(alternating[-1] * (1.012 if index % 2 else 1.0 / 1.012))

    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        scenarios = (
            ("alternating", alternating),
            ("flat", [100.0] * 300),
            ("all-loss", [300.0 - index for index in range(300)]),
        )
        for scenario, levels in scenarios:
            _clear_feature_tables(connection)
            _insert_level_values(connection, timestamps, {"vix": levels})
            connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
            row = connection.execute(
                f"SELECT {projection} FROM macro_loader.macro_features WHERE timestamp_m1 = %s",
                (timestamps[-1],),
            ).fetchone()
            assert row is not None
            if scenario == "flat":
                assert row[0] is None
                assert row[4] is None and row[5] is None
                assert row[6] is None and row[7] is None and row[8] is None
            elif scenario == "all-loss":
                assert row[0] is None  # RSI-15 is zero, so the ratio is undefined.
            else:
                assert row[6] is not None and row[6] < 0.0

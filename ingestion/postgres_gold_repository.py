"""Psycopg Adapter for the canonical Gold PostgreSQL serving-plane replica."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import NoReturn, Protocol, TypeVar, cast

import psycopg

from application.fed_policy_postgres import FED_POLICY_DATASET_ID, FED_POLICY_FEATURE_COLUMNS
from application.gold_frame import GOLD_COLUMNS
from application.macro_feature_catalog import (
    FEATURE_COLUMNS,
    MACRO_FEATURE_VIEW_FINGERPRINT,
    MACRO_FEATURE_VIEW_VERSION,
    RAW_SERIES,
)
from application.macro_features import MACRO_POLICY, macro_delta_lags
from application.momentum_features import MOMENTUM_POLICY
from application.postgres_delta import gold_row_sha256
from application.postgres_sync import (
    POSTGRES_CONSUMER_SCHEMA,
    POSTGRES_CONSUMER_TABLE,
    POSTGRES_DATASET_ID,
    POSTGRES_RAW_COLUMNS,
    POSTGRES_ROW_HASH_TABLE,
    POSTGRES_SESSION_TIMEZONE,
    POSTGRES_SYNC_SCHEMA,
    POSTGRES_SYNC_STATE_TABLE,
    GoldDeltaPlan,
    GoldRowDigest,
    GoldRowPayload,
    GoldSyncState,
    GoldSyncTransaction,
    GoldTargetSummary,
)
from application.return_features import RETURN_WINDOWS
from application.volatility_features import VOLATILITY_SERIES

POSTGRES_HOST = "10.10.1.3"
POSTGRES_PORT = 54321
POSTGRES_USER = "macro-loader"
POSTGRES_SYNC_USER = "macro-loader-sync"
POSTGRES_ADVISORY_LOCK_NAMESPACE = "macro-loader:postgres-gold-sync:v1"

TransactionResult = TypeVar("TransactionResult")
POSTGRES_APPLICATION_NAME = "macro-loader"


@dataclass(frozen=True, slots=True)
class PostgresTimeoutPolicy:
    """Bounded waits for every PostgreSQL session opened by this adapter."""

    connect_timeout_seconds: int = 5
    lock_timeout_ms: int = 5_000
    statement_timeout_ms: int = 30_000
    idle_in_transaction_timeout_ms: int = 30_000
    application_name: str = POSTGRES_APPLICATION_NAME

    def __post_init__(self) -> None:
        for name, value in (
            ("connect_timeout_seconds", self.connect_timeout_seconds),
            ("lock_timeout_ms", self.lock_timeout_ms),
            ("statement_timeout_ms", self.statement_timeout_ms),
            ("idle_in_transaction_timeout_ms", self.idle_in_transaction_timeout_ms),
        ):
            if value <= 0:
                raise ValueError(f"PostgreSQL {name} must be positive")
        if self.application_name != POSTGRES_APPLICATION_NAME:
            raise ValueError(f"PostgreSQL application_name must be {POSTGRES_APPLICATION_NAME}")


class PostgresGoldRepositoryError(RuntimeError):
    """Sanitized repository error that deliberately carries no connection secret."""


class PostgresOperationTimeoutError(PostgresGoldRepositoryError):
    """A bounded PostgreSQL operation exceeded its configured wait."""


class PostgresLockContentionError(PostgresOperationTimeoutError):
    """The PostgreSQL advisory lock was unavailable before its configured deadline."""


class CursorPort(Protocol):
    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, params: Sequence[object] | None = None) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...

    def fetchall(self) -> list[tuple[object, ...]]: ...

    def close(self) -> None: ...


class ConnectionPort(Protocol):
    def cursor(self) -> CursorPort: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PostgresSyncConfig:
    host: str
    port: int
    user: str
    database: str
    password: str = field(repr=False)
    timeout_policy: PostgresTimeoutPolicy = field(default_factory=PostgresTimeoutPolicy)

    def __post_init__(self) -> None:
        if self.host != POSTGRES_HOST:
            raise ValueError(f"PostgreSQL sync host must be {POSTGRES_HOST}")
        if self.port != POSTGRES_PORT:
            raise ValueError(f"PostgreSQL sync port must be {POSTGRES_PORT}")
        if self.user not in (POSTGRES_USER, POSTGRES_SYNC_USER):
            raise ValueError(
                f"PostgreSQL sync user must be {POSTGRES_USER} or {POSTGRES_SYNC_USER}"
            )
        if not self.database:
            raise ValueError("PostgreSQL sync database is required")
        if not self.password:
            raise ValueError("PostgreSQL sync password is required")

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> PostgresSyncConfig:
        try:
            port = int(values.get("PGPORT", ""))
        except ValueError as exc:
            raise ValueError("PGPORT must be an integer") from exc
        return cls(
            host=values.get("PGHOST", ""),
            port=port,
            user=values.get("PGUSER", ""),
            database=values.get("PGDATABASE", ""),
            password=values.get("PGPASSWORD", ""),
        )

    @classmethod
    def from_env(cls) -> PostgresSyncConfig:
        return cls.from_mapping(os.environ)


@dataclass(frozen=True, slots=True)
class PostgresAdminConfig:
    """Protected configuration for explicit schema migration only."""

    host: str
    port: int
    user: str
    database: str
    password: str = field(repr=False)
    timeout_policy: PostgresTimeoutPolicy = field(default_factory=PostgresTimeoutPolicy)

    def __post_init__(self) -> None:
        if self.host != POSTGRES_HOST:
            raise ValueError(f"PostgreSQL admin host must be {POSTGRES_HOST}")
        if self.port != POSTGRES_PORT:
            raise ValueError(f"PostgreSQL admin port must be {POSTGRES_PORT}")
        if not self.user:
            raise ValueError("PostgreSQL admin user is required")
        if self.user == POSTGRES_USER:
            raise ValueError("PostgreSQL admin user must differ from runtime user")
        if not self.database:
            raise ValueError("PostgreSQL admin database is required")
        if not self.password:
            raise ValueError("PostgreSQL admin password is required")

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> PostgresAdminConfig:
        try:
            port = int(values.get("MARKET_MACRO_POSTGRES_ADMIN_PORT", ""))
        except ValueError as exc:
            raise ValueError("MARKET_MACRO_POSTGRES_ADMIN_PORT must be an integer") from exc
        config = cls(
            host=values.get("MARKET_MACRO_POSTGRES_ADMIN_HOST", ""),
            port=port,
            user=values.get("MARKET_MACRO_POSTGRES_ADMIN_USER", ""),
            database=values.get("MARKET_MACRO_POSTGRES_ADMIN_DATABASE", ""),
            password=values.get("MARKET_MACRO_POSTGRES_ADMIN_PASSWORD", ""),
        )
        runtime_password = values.get("PGPASSWORD", "")
        if runtime_password and runtime_password == config.password:
            raise ValueError("PostgreSQL admin password must differ from runtime password")
        return config

    @classmethod
    def from_env(cls) -> PostgresAdminConfig:
        return cls.from_mapping(os.environ)


PostgresConnectionConfig = PostgresSyncConfig | PostgresAdminConfig
ConnectionFactory = Callable[[PostgresConnectionConfig], ConnectionPort]


def _default_connection(config: PostgresConnectionConfig) -> ConnectionPort:
    connection = psycopg.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        dbname=config.database,
        password=config.password,
        connect_timeout=config.timeout_policy.connect_timeout_seconds,
        application_name=config.timeout_policy.application_name,
        autocommit=False,
    )
    return cast(ConnectionPort, connection)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


_CONSUMER = f"{_quote(POSTGRES_CONSUMER_SCHEMA)}.{_quote(POSTGRES_CONSUMER_TABLE)}"
_SYNC_STATE = f"{_quote(POSTGRES_SYNC_SCHEMA)}.{_quote(POSTGRES_SYNC_STATE_TABLE)}"
_ROW_HASHES = f"{_quote(POSTGRES_SYNC_SCHEMA)}.{_quote(POSTGRES_ROW_HASH_TABLE)}"
_FEATURE_COLUMNS = POSTGRES_RAW_COLUMNS[1:]
_MIGRATION_LEDGER_TABLE = "schema_migrations"
_MIGRATION_LEDGER = f"{_quote(POSTGRES_SYNC_SCHEMA)}.{_quote(_MIGRATION_LEDGER_TABLE)}"
_POSTGRES_OWNER_ROLE = "macro-loader-owner"
_LEGACY_CONSUMER = f'{_quote(POSTGRES_CONSUMER_SCHEMA)}."macro_features_daily"'
_FEATURES_VIEW = f'{_quote(POSTGRES_CONSUMER_SCHEMA)}."macro_features"'
_FED_POLICY_TABLE = f"{_quote(POSTGRES_SYNC_SCHEMA)}.{_quote(FED_POLICY_DATASET_ID)}"
_FEATURES_VIEW_COLUMNS: tuple[str, ...] = (
    "vix_delta_1obs",
    "vix_delta_5obs",
    "vix_delta_20obs",
    "vix_zscore_60obs",
    "vix9d_delta_1obs",
    "vix9d_delta_5obs",
    "vix9d_delta_20obs",
    "vix9d_zscore_60obs",
    "vix9d_momentum_autocorr_1_60obs",
    "vix9d_momentum_autocorr_5_60obs",
    "vix9d_momentum_autocorr_20_120obs",
    "vix9d_return_geom_10obs_pct",
    "vix9d_return_geom_25obs_pct",
    "vix9d_return_geom_60obs_pct",
    "vix9d_return_geom_120obs_pct",
    "vix9d_return_geom_240obs_pct",
    "vix3m_delta_1obs",
    "vix3m_delta_5obs",
    "vix3m_delta_20obs",
    "vix3m_zscore_60obs",
    "vix6m_delta_1obs",
    "vix6m_delta_5obs",
    "vix6m_delta_20obs",
    "vix6m_zscore_60obs",
    "vix1y_delta_1obs",
    "vix1y_delta_5obs",
    "vix1y_delta_20obs",
    "vix1y_zscore_60obs",
    "vstoxx_delta_1obs",
    "vstoxx_delta_5obs",
    "vstoxx_delta_20obs",
    "vstoxx_zscore_60obs",
    "move_delta_1obs",
    "move_delta_5obs",
    "move_delta_20obs",
    "vix9d_vix_ratio",
    "move_zscore_60obs",
    "vix_vix3m_ratio",
    "vix3m_minus_vix",
    "vix6m_minus_vix",
    "vix1y_minus_vix",
    "ciss_delta_1obs",
    "ciss_delta_5obs",
    "ciss_delta_20obs",
    "euro_hy_oas_delta_1obs",
    "euro_hy_oas_delta_5obs",
    "euro_hy_oas_delta_20obs",
    "us_2y_delta_1obs",
    "us_2y_delta_20obs",
    "us_10y_delta_1obs",
    "us_10y_delta_20obs",
    "usd_broad_delta_1obs",
    "usd_broad_delta_20obs",
    "us_10y_minus_us_2y",
    "vix_momentum_autocorr_1_60obs",
    "vix_momentum_autocorr_5_60obs",
    "vix_momentum_autocorr_20_120obs",
    "vix3m_momentum_autocorr_1_60obs",
    "vix3m_momentum_autocorr_5_60obs",
    "vix6m_momentum_autocorr_1_60obs",
    "vix6m_momentum_autocorr_5_60obs",
    "vix6m_momentum_autocorr_20_120obs",
    "vix1y_momentum_autocorr_1_60obs",
    "vix1y_momentum_autocorr_5_60obs",
    "vix1y_momentum_autocorr_20_120obs",
    "vstoxx_momentum_autocorr_1_60obs",
    "vstoxx_momentum_autocorr_5_60obs",
    "vstoxx_momentum_autocorr_20_120obs",
    "move_momentum_autocorr_1_60obs",
    "move_momentum_autocorr_5_60obs",
    "move_momentum_autocorr_20_120obs",
    "ciss_momentum_autocorr_1_60obs",
    "ciss_momentum_autocorr_5_60obs",
    "ciss_momentum_autocorr_20_120obs",
    "euro_hy_oas_momentum_autocorr_1_60obs",
    "euro_hy_oas_momentum_autocorr_5_60obs",
    "euro_hy_oas_momentum_autocorr_20_120obs",
    "us_2y_momentum_autocorr_1_60obs",
    "us_2y_momentum_autocorr_5_60obs",
    "us_2y_momentum_autocorr_20_120obs",
    "us_10y_momentum_autocorr_1_60obs",
    "us_10y_momentum_autocorr_5_60obs",
    "us_10y_momentum_autocorr_20_120obs",
    "usd_broad_momentum_autocorr_1_60obs",
    "usd_broad_momentum_autocorr_5_60obs",
    "usd_broad_momentum_autocorr_20_120obs",
    "vix_return_geom_10obs_pct",
    "vix_return_geom_25obs_pct",
    "vix_return_geom_60obs_pct",
    "vix_return_geom_120obs_pct",
    "vix_return_geom_240obs_pct",
    "vix3m_return_geom_10obs_pct",
    "vix3m_return_geom_25obs_pct",
    "vix3m_return_geom_60obs_pct",
    "vix6m_return_geom_10obs_pct",
    "vix6m_return_geom_25obs_pct",
    "vix6m_return_geom_60obs_pct",
    "vix6m_return_geom_120obs_pct",
    "vix6m_return_geom_240obs_pct",
    "vix1y_return_geom_10obs_pct",
    "vix1y_return_geom_25obs_pct",
    "vix1y_return_geom_60obs_pct",
    "vix1y_return_geom_120obs_pct",
    "vix1y_return_geom_240obs_pct",
    "vstoxx_return_geom_10obs_pct",
    "vstoxx_return_geom_25obs_pct",
    "vstoxx_return_geom_60obs_pct",
    "vstoxx_return_geom_120obs_pct",
    "vstoxx_return_geom_240obs_pct",
    "move_return_geom_10obs_pct",
    "move_return_geom_25obs_pct",
    "move_return_geom_60obs_pct",
    "move_return_geom_120obs_pct",
    "move_return_geom_240obs_pct",
    "ciss_return_geom_10obs_pct",
    "ciss_return_geom_25obs_pct",
    "ciss_return_geom_60obs_pct",
    "ciss_return_geom_120obs_pct",
    "ciss_return_geom_240obs_pct",
    "euro_hy_oas_return_geom_10obs_pct",
    "euro_hy_oas_return_geom_25obs_pct",
    "euro_hy_oas_return_geom_60obs_pct",
    "euro_hy_oas_return_geom_120obs_pct",
    "euro_hy_oas_return_geom_240obs_pct",
    "us_2y_return_geom_10obs_pct",
    "us_2y_return_geom_25obs_pct",
    "us_2y_return_geom_60obs_pct",
    "us_2y_return_geom_120obs_pct",
    "us_2y_return_geom_240obs_pct",
    "us_10y_return_geom_10obs_pct",
    "us_10y_return_geom_25obs_pct",
    "us_10y_return_geom_60obs_pct",
    "us_10y_return_geom_120obs_pct",
    "us_10y_return_geom_240obs_pct",
    "usd_broad_return_geom_10obs_pct",
    "usd_broad_return_geom_25obs_pct",
    "usd_broad_return_geom_60obs_pct",
    "usd_broad_return_geom_120obs_pct",
    "usd_broad_return_geom_240obs_pct",
)

# PR-81 replaces the historical hand-maintained list above with the explicit
# source-controlled catalog.  Keeping the old literal in this module eases
# upgrade compatibility for consumers that import it, but migrations and
# conformance use the catalog-derived contract from this point onward.
_FEATURES_VIEW_COLUMNS = FEATURE_COLUMNS[1:] + FED_POLICY_FEATURE_COLUMNS

_CONSUMER_DDL = f"""CREATE TABLE IF NOT EXISTS {_CONSUMER} (
    {_quote("timestamp_m1")} TIMESTAMPTZ(6) NOT NULL PRIMARY KEY,
    {",\n    ".join(f"{_quote(column)} DOUBLE PRECISION NULL" for column in _FEATURE_COLUMNS)}
)"""
_FED_POLICY_DDL = f"""CREATE TABLE IF NOT EXISTS {_FED_POLICY_TABLE} (
    {_quote("timestamp_m1")} TIMESTAMPTZ(6) NOT NULL PRIMARY KEY,
    {
    ",\n    ".join(
        f"{_quote(column)} DOUBLE PRECISION NULL" for column in FED_POLICY_FEATURE_COLUMNS
    )
},
    {_quote("available_at_utc")} TIMESTAMPTZ(6) NOT NULL
)"""
_SYNC_STATE_DDL = f"""CREATE TABLE IF NOT EXISTS {_SYNC_STATE} (
    dataset_id TEXT PRIMARY KEY,
    source_build_id TEXT NOT NULL,
    data_sha256 CHAR(64) NOT NULL,
    schema_version INTEGER NOT NULL,
    feature_version INTEGER NOT NULL,
    row_count BIGINT NOT NULL,
    min_timestamp TIMESTAMPTZ(6) NULL,
    max_timestamp TIMESTAMPTZ(6) NULL,
    synced_at_utc TIMESTAMPTZ(6) NOT NULL
)"""

_ROW_HASH_DDL = f"""CREATE TABLE IF NOT EXISTS {_ROW_HASHES} (
    dataset_id TEXT NOT NULL,
    timestamp_m1 TIMESTAMPTZ(6) NOT NULL,
    row_sha256 CHAR(64) NOT NULL,
    PRIMARY KEY (dataset_id, timestamp_m1)
)"""

_MIGRATION_LEDGER_DDL = f"""CREATE TABLE IF NOT EXISTS {_MIGRATION_LEDGER} (
    version INTEGER PRIMARY KEY,
    applied_at_utc TIMESTAMPTZ(6) NOT NULL
)"""
_OWNERSHIP_MIGRATIONS = tuple(
    f"ALTER TABLE IF EXISTS {_quote(schema)}.{_quote(table)} "
    f"OWNER TO {_quote(_POSTGRES_OWNER_ROLE)}"
    for schema, table in (
        (POSTGRES_CONSUMER_SCHEMA, POSTGRES_CONSUMER_TABLE),
        (POSTGRES_SYNC_SCHEMA, FED_POLICY_DATASET_ID),
        (POSTGRES_SYNC_SCHEMA, POSTGRES_SYNC_STATE_TABLE),
        (POSTGRES_SYNC_SCHEMA, POSTGRES_ROW_HASH_TABLE),
        (POSTGRES_SYNC_SCHEMA, _MIGRATION_LEDGER_TABLE),
    )
)
_MOMENTUM_COLUMNS = tuple(column for column in GOLD_COLUMNS if "_momentum_autocorr_" in column)
_MOMENTUM_COLUMN_MIGRATION = (
    f"ALTER TABLE {_CONSUMER} "
    + ", ".join(
        f"ADD COLUMN IF NOT EXISTS {_quote(column)} DOUBLE PRECISION NULL"
        for column in _MOMENTUM_COLUMNS
    )
    if _MOMENTUM_COLUMNS
    else "SELECT 1"
)
_RETURN_COLUMNS = tuple(column for column in GOLD_COLUMNS if "_return_geom_" in column)
_RETURN_COLUMN_MIGRATION = (
    f"ALTER TABLE {_CONSUMER} "
    + ", ".join(
        f"ADD COLUMN IF NOT EXISTS {_quote(column)} DOUBLE PRECISION NULL"
        for column in _RETURN_COLUMNS
    )
    if _RETURN_COLUMNS
    else "SELECT 1"
)
_FED_POLICY_COLUMNS = tuple(
    column
    for column in GOLD_COLUMNS
    if column.startswith("fed_") or column == "fomc_business_days_to_next"
)
_FED_POLICY_COLUMN_MIGRATION = (
    f"ALTER TABLE {_CONSUMER} "
    + ", ".join(
        f"ADD COLUMN IF NOT EXISTS {_quote(column)} DOUBLE PRECISION NULL"
        for column in _FED_POLICY_COLUMNS
    )
    if _FED_POLICY_COLUMNS
    else "SELECT 1"
)
_FED_POLICY_RENAME_MIGRATION = (
    "ALTER TABLE "
    + _CONSUMER
    + " "
    + ", ".join(
        [
            'DROP COLUMN IF EXISTS "fed_3m_expected_move_bp"',
            'DROP COLUMN IF EXISTS "fed_next_expected_move_bp_delta_5obs"',
            'DROP COLUMN IF EXISTS "fomc_business_days_to_next"',
        ]
    )
)
_CONSUMER_LAYOUT_MIGRATION = (
    f"DROP TABLE IF EXISTS {_CONSUMER}",
    _CONSUMER_DDL,
    f"GRANT SELECT ON TABLE {_CONSUMER} TO {_quote(POSTGRES_USER)}",
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {_CONSUMER} TO {_quote(POSTGRES_SYNC_USER)}",
    f"REVOKE INSERT, UPDATE, DELETE ON TABLE {_CONSUMER} FROM {_quote(POSTGRES_USER)}",
)
_RAW_ONLY_LAYOUT_MIGRATION = (
    f"DROP MATERIALIZED VIEW IF EXISTS {_FEATURES_VIEW} CASCADE",
    f"DROP TABLE IF EXISTS {_CONSUMER}",
    _CONSUMER_DDL,
    f"DELETE FROM {_SYNC_STATE}",
    f"DELETE FROM {_ROW_HASHES}",
    f"GRANT SELECT ON TABLE {_CONSUMER} TO {_quote(POSTGRES_USER)}",
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {_CONSUMER} TO {_quote(POSTGRES_SYNC_USER)}",
    f"REVOKE INSERT, UPDATE, DELETE ON TABLE {_CONSUMER} FROM {_quote(POSTGRES_USER)}",
)
_CONSUMER_RENAME_MIGRATION = f"""DO $$
BEGIN
    IF to_regclass('macro_loader.macro_features_daily') IS NOT NULL THEN
        IF to_regclass('macro_loader.macro_raw') IS NOT NULL THEN
            DROP TABLE {_CONSUMER};
        END IF;
        ALTER TABLE {_LEGACY_CONSUMER} RENAME TO {_quote(POSTGRES_CONSUMER_TABLE)};
    END IF;
END
$$"""


def _macro_features_view_query() -> str:
    """Build the closed-world SQL definition from the executable catalog."""
    source_ctes: list[str] = []
    joins: list[str] = []
    feature_select: list[str] = []
    macro_lags = macro_delta_lags(MACRO_POLICY)
    for series in RAW_SERIES:
        level = _quote(f"{series}_level")
        source = _quote(f"{series}_source")
        changes = _quote(f"{series}_changes")
        features = _quote(f"{series}_features")
        lags = macro_lags.get(series, (1, 5, 20))
        lag_sql = ", ".join(
            f"lag({level}, {lag}) OVER ordered AS {_quote(f'lag_{lag}')}" for lag in (1, 5, 20)
        )
        delta_sql = ", ".join(
            f"level - {_quote(f'lag_{lag}')} AS {_quote(f'delta_{lag}')}" for lag in (1, 5, 20)
        )
        change_lags = ", ".join(
            f"lag(level - {_quote('lag_1')}, {lag}) OVER (ORDER BY timestamp_m1) "
            f"AS {_quote(f'change_lag_{lag}')}"
            for lag in (1, 5, 20)
        )
        source_ctes.append(
            f"{source} AS (SELECT timestamp_m1, {level} AS level, {lag_sql} "
            f"FROM {_CONSUMER} WHERE {level} IS NOT NULL "
            "WINDOW ordered AS (ORDER BY timestamp_m1))"
        )
        source_ctes.append(
            f"{changes} AS (SELECT *, {delta_sql}, level - {_quote('lag_1')} AS change, "
            f"{change_lags}, CASE WHEN level > 0 AND {_quote('lag_1')} > 0 "
            f"THEN ln(level / {_quote('lag_1')}) END AS log_return FROM {source})"
        )
        expressions: list[str] = []
        for lag in lags:
            expressions.append(
                f"{_quote('delta_' + str(lag))} AS {_quote(f'{series}_delta_{lag}obs')}"
            )
        if series in VOLATILITY_SERIES:
            expressions.append(
                f"CASE WHEN count(level) OVER window_60 = 60 "
                f"AND stddev_pop(level) OVER window_60 <> 0 "
                f"THEN (level - avg(level) OVER window_60) / stddev_pop(level) OVER window_60 "
                f"END AS {_quote(f'{series}_zscore_60obs')}"
            )
        for lag, window in MOMENTUM_POLICY.lag_windows:
            expressions.append(
                f"CASE WHEN count(change) OVER window_{window} = {window} "
                f"AND count({_quote(f'change_lag_{lag}')}) OVER window_{window} = {window} "
                f"THEN greatest(corr(change, {_quote(f'change_lag_{lag}')}) "
                f"OVER window_{window}, 0.0) END AS "
                f"{_quote(f'{series}_momentum_autocorr_{lag}_{window}obs')}"
            )
        for window in RETURN_WINDOWS:
            expressions.append(
                f"CASE WHEN count(log_return) OVER window_{window} = {window} "
                f"THEN (exp(avg(log_return) OVER window_{window}) - 1.0) * 100.0 "
                f"END AS {_quote(f'{series}_return_geom_{window}obs_pct')}"
            )
        if series == "usd_broad":
            expressions.append(
                'CASE WHEN level > 0 AND "lag_20" > 0 '
                'THEN ln(level / "lag_20") END AS "usd_broad_log_return_20obs"'
            )
        source_ctes.append(
            f"{features} AS (SELECT timestamp_m1, {', '.join(expressions)} FROM {changes} "
            "WINDOW window_10 AS (ORDER BY timestamp_m1 ROWS BETWEEN 9 PRECEDING AND CURRENT ROW), "
            "window_25 AS (ORDER BY timestamp_m1 ROWS BETWEEN 24 PRECEDING AND CURRENT ROW), "
            "window_60 AS (ORDER BY timestamp_m1 ROWS BETWEEN 59 PRECEDING AND CURRENT ROW), "
            "window_120 AS (ORDER BY timestamp_m1 ROWS BETWEEN 119 PRECEDING AND CURRENT ROW), "
            "window_240 AS (ORDER BY timestamp_m1 ROWS BETWEEN 239 PRECEDING AND CURRENT ROW))"
        )
        joins.append(f"LEFT JOIN {features} ON {features}.timestamp_m1 = raw.timestamp_m1")
        for column in _FEATURES_VIEW_COLUMNS:
            if (
                column.startswith(f"{series}_delta_")
                or column.startswith(f"{series}_zscore_")
                or column.startswith(f"{series}_momentum_")
                or column.startswith(f"{series}_return_")
                or (series == "usd_broad" and column == "usd_broad_log_return_20obs")
            ):
                feature_select.append(f"{features}.{_quote(column)}")

    cross_expressions = {
        "vix9d_vix_ratio": (
            'CASE WHEN raw."vix_level" > 0 THEN raw."vix9d_level" / raw."vix_level" '
            'END AS "vix9d_vix_ratio"'
        ),
        "vix_vix3m_ratio": (
            'CASE WHEN raw."vix3m_level" > 0 THEN raw."vix_level" / raw."vix3m_level" '
            'END AS "vix_vix3m_ratio"'
        ),
        "vix9d_vix3m_log_ratio": (
            'CASE WHEN raw."vix9d_level" > 0 AND raw."vix3m_level" > 0 '
            'THEN ln(raw."vix9d_level" / raw."vix3m_level") '
            'END AS "vix9d_vix3m_log_ratio"'
        ),
        "vix3m_minus_vix": 'raw."vix3m_level" - raw."vix_level" AS "vix3m_minus_vix"',
        "vix6m_minus_vix": 'raw."vix6m_level" - raw."vix_level" AS "vix6m_minus_vix"',
        "vix1y_minus_vix": 'raw."vix1y_level" - raw."vix_level" AS "vix1y_minus_vix"',
        "us_10y_minus_us_2y": 'raw."us_10y_level" - raw."us_2y_level" AS "us_10y_minus_us_2y"',
    }
    feature_expressions = {
        expression.rsplit(".", 1)[-1].strip('"'): expression for expression in feature_select
    }
    fed_expressions = {
        column: f"fed.{_quote(column)} AS {_quote(column)}" for column in FED_POLICY_FEATURE_COLUMNS
    }
    ordered_features = [
        feature_expressions.get(
            column, cross_expressions.get(column, fed_expressions.get(column, ""))
        )
        for column in _FEATURES_VIEW_COLUMNS
        if column not in {f"{series}_log_level" for series in RAW_SERIES}
    ]
    if any(not expression for expression in ordered_features):
        raise ValueError("macro feature catalog contains an expression without SQL projection")
    raw_select = ", ".join(
        f"CASE WHEN raw.{_quote(f'{series}_level')} > 0 "
        f"THEN ln(raw.{_quote(f'{series}_level')}) END AS {_quote(f'{series}_log_level')}"
        for series in RAW_SERIES
    )
    return (
        f"CREATE MATERIALIZED VIEW IF NOT EXISTS {_FEATURES_VIEW} AS WITH {', '.join(source_ctes)} "
        f'SELECT raw."timestamp_m1", {raw_select}, {", ".join(ordered_features)} '
        f"FROM {_CONSUMER} raw LEFT JOIN {_FED_POLICY_TABLE} fed "
        'ON fed."timestamp_m1" = raw."timestamp_m1" '
        f"{' '.join(joins)} "
        "WHERE raw.\"timestamp_m1\" >= '2010-01-01 00:00:00+00'::timestamptz"
    )


_FEATURES_VIEW_DDL = _macro_features_view_query()
_FEATURES_VIEW_MIGRATION = (
    _FEATURES_VIEW_DDL,
    f"COMMENT ON MATERIALIZED VIEW {_FEATURES_VIEW} IS "
    f"'macro feature view version={MACRO_FEATURE_VIEW_VERSION}; "
    f"fingerprint={MACRO_FEATURE_VIEW_FINGERPRINT}'",
    f"ALTER MATERIALIZED VIEW {_FEATURES_VIEW} OWNER TO {_quote(_POSTGRES_OWNER_ROLE)}",
    f"GRANT SELECT ON {_FEATURES_VIEW} TO {_quote(POSTGRES_USER)}",
    f"GRANT SELECT ON {_FEATURES_VIEW} TO {_quote(POSTGRES_SYNC_USER)}",
)
_FEATURES_VIEW_REBUILD_MIGRATION = (
    f"DROP MATERIALIZED VIEW IF EXISTS {_FEATURES_VIEW}",
    _FEATURES_VIEW_DDL,
    f"COMMENT ON MATERIALIZED VIEW {_FEATURES_VIEW} IS "
    f"'macro feature view version={MACRO_FEATURE_VIEW_VERSION}; "
    f"fingerprint={MACRO_FEATURE_VIEW_FINGERPRINT}'",
    f"ALTER MATERIALIZED VIEW {_FEATURES_VIEW} OWNER TO {_quote(_POSTGRES_OWNER_ROLE)}",
    f"GRANT SELECT ON {_FEATURES_VIEW} TO {_quote(POSTGRES_USER)}",
    f"GRANT SELECT ON {_FEATURES_VIEW} TO {_quote(POSTGRES_SYNC_USER)}",
)
_FEATURES_VIEW_REFRESH_FUNCTION = """CREATE OR REPLACE FUNCTION
macro_loader.refresh_macro_features_explicit()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, macro_loader
AS $$
BEGIN
    REFRESH MATERIALIZED VIEW macro_loader.macro_features;
END
$$"""
_FEATURES_VIEW_REFRESH_MIGRATION = (
    'DROP TRIGGER IF EXISTS refresh_macro_features_after_raw_change ON "macro_loader"."macro_raw"',
    "DROP FUNCTION IF EXISTS macro_loader.refresh_macro_features()",
    _FEATURES_VIEW_REFRESH_FUNCTION,
    "ALTER FUNCTION macro_loader.refresh_macro_features_explicit() "
    f"OWNER TO {_quote(_POSTGRES_OWNER_ROLE)}",
    "REVOKE ALL ON FUNCTION macro_loader.refresh_macro_features_explicit() FROM PUBLIC",
    f"GRANT USAGE ON SCHEMA {_quote(POSTGRES_CONSUMER_SCHEMA)}, "
    f"{_quote(POSTGRES_SYNC_SCHEMA)} TO {_quote(POSTGRES_SYNC_USER)}, "
    f"{_quote(POSTGRES_USER)}, {_quote(_POSTGRES_OWNER_ROLE)}",
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {_SYNC_STATE}, {_ROW_HASHES} "
    f"TO {_quote(POSTGRES_SYNC_USER)}",
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {_FED_POLICY_TABLE} "
    f"TO {_quote(POSTGRES_SYNC_USER)}",
    f"GRANT SELECT ON TABLE {_FED_POLICY_TABLE} TO {_quote(POSTGRES_USER)}",
    f"GRANT SELECT ON TABLE {_MIGRATION_LEDGER} TO {_quote(POSTGRES_SYNC_USER)}",
    f"REVOKE ALL ON {_FEATURES_VIEW} FROM PUBLIC",
    f"GRANT SELECT ON {_FEATURES_VIEW} TO {_quote(POSTGRES_USER)}, {_quote(POSTGRES_SYNC_USER)}",
    "GRANT EXECUTE ON FUNCTION macro_loader.refresh_macro_features_explicit() "
    f"TO {_quote(POSTGRES_SYNC_USER)}",
)


def _normalize_view_definition(value: str) -> str:
    """Normalize PostgreSQL's pretty-printed view definition for contract checks."""
    return " ".join(value.strip().rstrip(";").split()).lower()


_FEATURES_VIEW_DEFINITION = _normalize_view_definition(_FEATURES_VIEW_DDL.split(" AS ", 1)[1])
_FEATURES_VIEW_COLUMNS_EXPECTED = ("timestamp_m1",) + tuple(_FEATURES_VIEW_COLUMNS)
_FEATURES_VIEW_DEFINITION_MARKERS = (
    "with vix_source as",
    "raw.timestamp_m1",
    "vix9d_vix3m_log_ratio",
    "2010-01-01",
)


@dataclass(frozen=True, slots=True)
class PostgresColumnSpecification:
    name: str
    data_type: str
    precision: int | None
    nullable: bool


@dataclass(frozen=True, slots=True)
class PostgresTableSpecification:
    schema: str
    name: str
    columns: tuple[PostgresColumnSpecification, ...]
    primary_key: tuple[str, ...]


_TIMESTAMPTZ6_NOT_NULL = PostgresColumnSpecification(
    "timestamp_m1", "timestamp with time zone", 6, False
)
_SCHEMA_SPECIFICATION = (
    PostgresTableSpecification(
        POSTGRES_CONSUMER_SCHEMA,
        POSTGRES_CONSUMER_TABLE,
        (_TIMESTAMPTZ6_NOT_NULL,)
        + tuple(
            PostgresColumnSpecification(column, "double precision", None, True)
            for column in _FEATURE_COLUMNS
        ),
        ("timestamp_m1",),
    ),
    PostgresTableSpecification(
        POSTGRES_SYNC_SCHEMA,
        FED_POLICY_DATASET_ID,
        (
            _TIMESTAMPTZ6_NOT_NULL,
            *tuple(
                PostgresColumnSpecification(column, "double precision", None, True)
                for column in FED_POLICY_FEATURE_COLUMNS
            ),
            PostgresColumnSpecification("available_at_utc", "timestamp with time zone", 6, False),
        ),
        ("timestamp_m1",),
    ),
    PostgresTableSpecification(
        POSTGRES_SYNC_SCHEMA,
        POSTGRES_SYNC_STATE_TABLE,
        (
            PostgresColumnSpecification("dataset_id", "text", None, False),
            PostgresColumnSpecification("source_build_id", "text", None, False),
            PostgresColumnSpecification("data_sha256", "character", 64, False),
            PostgresColumnSpecification("schema_version", "integer", None, False),
            PostgresColumnSpecification("feature_version", "integer", None, False),
            PostgresColumnSpecification("row_count", "bigint", None, False),
            PostgresColumnSpecification("min_timestamp", "timestamp with time zone", 6, True),
            PostgresColumnSpecification("max_timestamp", "timestamp with time zone", 6, True),
            PostgresColumnSpecification("synced_at_utc", "timestamp with time zone", 6, False),
        ),
        ("dataset_id",),
    ),
    PostgresTableSpecification(
        POSTGRES_SYNC_SCHEMA,
        POSTGRES_ROW_HASH_TABLE,
        (
            PostgresColumnSpecification("dataset_id", "text", None, False),
            _TIMESTAMPTZ6_NOT_NULL,
            PostgresColumnSpecification("row_sha256", "character", 64, False),
        ),
        ("dataset_id", "timestamp_m1"),
    ),
    PostgresTableSpecification(
        POSTGRES_SYNC_SCHEMA,
        _MIGRATION_LEDGER_TABLE,
        (
            PostgresColumnSpecification("version", "integer", None, False),
            PostgresColumnSpecification("applied_at_utc", "timestamp with time zone", 6, False),
        ),
        ("version",),
    ),
)
_MIGRATIONS = (
    (_CONSUMER_DDL, _FED_POLICY_DDL),
    (_SYNC_STATE_DDL,),
    (_ROW_HASH_DDL,),
    (_MOMENTUM_COLUMN_MIGRATION,),
    (_RETURN_COLUMN_MIGRATION,),
    (_FED_POLICY_COLUMN_MIGRATION,),
    (_FED_POLICY_RENAME_MIGRATION,),
    _CONSUMER_LAYOUT_MIGRATION,
    (_CONSUMER_RENAME_MIGRATION,),
    _FEATURES_VIEW_MIGRATION,
    _RAW_ONLY_LAYOUT_MIGRATION,
    _FEATURES_VIEW_REBUILD_MIGRATION,
    _FEATURES_VIEW_REFRESH_MIGRATION,
    # The view now publishes explicit *_log_level columns instead of levels.
    _FEATURES_VIEW_REBUILD_MIGRATION,
    # Rebuild again for the explicit *_log_level column-name contract.
    _FEATURES_VIEW_REBUILD_MIGRATION,
    (_FED_POLICY_DDL,),
    _FEATURES_VIEW_REBUILD_MIGRATION,
)
_OWNED_TABLES_SQL = """SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema IN (%s, %s) AND table_type = 'BASE TABLE'
ORDER BY table_schema, table_name"""
_OWNED_COLUMNS_SQL = """SELECT table_schema, table_name, ordinal_position, column_name,
    data_type, datetime_precision, character_maximum_length, is_nullable
FROM information_schema.columns
WHERE table_schema IN (%s, %s)
ORDER BY table_schema, table_name, ordinal_position"""
_OWNED_KEYS_SQL = """SELECT namespaces.nspname, classes.relname, constraints.contype,
    array_agg(attributes.attname::text ORDER BY keys.ordinality)
FROM pg_constraint AS constraints
JOIN pg_class AS classes ON classes.oid = constraints.conrelid
JOIN pg_namespace AS namespaces ON namespaces.oid = classes.relnamespace
JOIN unnest(constraints.conkey) WITH ORDINALITY AS keys(attnum, ordinality) ON TRUE
JOIN pg_attribute AS attributes
    ON attributes.attrelid = classes.oid AND attributes.attnum = keys.attnum
WHERE namespaces.nspname IN (%s, %s) AND constraints.contype IN ('p', 'u')
GROUP BY namespaces.nspname, classes.relname, constraints.contype
ORDER BY namespaces.nspname, classes.relname, constraints.contype"""

_INSERT_ROW_SQL = (
    f"INSERT INTO {_CONSUMER} ({', '.join(_quote(column) for column in POSTGRES_RAW_COLUMNS)}) "
    f"VALUES ({', '.join('%s' for _ in POSTGRES_RAW_COLUMNS)})"
)
_UPDATE_ROW_SQL = (
    f"UPDATE {_CONSUMER} SET "
    + ", ".join(f"{_quote(column)} = %s" for column in _FEATURE_COLUMNS)
    + f" WHERE {_quote('timestamp_m1')} = %s"
)
_DELETE_ROW_SQL = f"DELETE FROM {_CONSUMER} WHERE {_quote('timestamp_m1')} = %s"
_UPSERT_DIGEST_SQL = f"""INSERT INTO {_ROW_HASHES} (dataset_id, timestamp_m1, row_sha256)
VALUES (%s, %s, %s)
ON CONFLICT (dataset_id, timestamp_m1)
DO UPDATE SET row_sha256 = EXCLUDED.row_sha256"""
_DELETE_DIGEST_SQL = f"DELETE FROM {_ROW_HASHES} WHERE dataset_id = %s AND timestamp_m1 = %s"
_CONSUMER_ROWS_SQL = (
    f"SELECT {', '.join(_quote(column) for column in POSTGRES_RAW_COLUMNS)} "
    f"FROM {_CONSUMER} ORDER BY {_quote('timestamp_m1')}"
)
_TARGET_SUMMARY_SQL = (
    f"SELECT COUNT(*), MIN({_quote('timestamp_m1')}), MAX({_quote('timestamp_m1')}) "
    f"FROM {_CONSUMER}"
)
_UPSERT_STATE_SQL = f"""INSERT INTO {_SYNC_STATE} (
    dataset_id, source_build_id, data_sha256, schema_version, feature_version,
    row_count, min_timestamp, max_timestamp, synced_at_utc
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (dataset_id) DO UPDATE SET
    source_build_id = EXCLUDED.source_build_id,
    data_sha256 = EXCLUDED.data_sha256,
    schema_version = EXCLUDED.schema_version,
    feature_version = EXCLUDED.feature_version,
    row_count = EXCLUDED.row_count,
    min_timestamp = EXCLUDED.min_timestamp,
    max_timestamp = EXCLUDED.max_timestamp,
    synced_at_utc = EXCLUDED.synced_at_utc"""


def _session_configuration(policy: PostgresTimeoutPolicy) -> tuple[str, ...]:
    return (
        f"SET application_name = '{policy.application_name}'",
        f"SET TIME ZONE '{POSTGRES_SESSION_TIMEZONE}'",
        f"SET lock_timeout = '{policy.lock_timeout_ms}ms'",
        f"SET statement_timeout = '{policy.statement_timeout_ms}ms'",
        f"SET idle_in_transaction_session_timeout = '{policy.idle_in_transaction_timeout_ms}ms'",
    )


def _raise_sanitized_error(operation: str, error: Exception) -> NoReturn:
    sqlstate = getattr(error, "sqlstate", None)
    if sqlstate == "55P03":
        raise PostgresLockContentionError("PostgreSQL advisory lock wait timed out") from None
    if sqlstate == "57014":
        raise PostgresOperationTimeoutError(f"PostgreSQL {operation} timed out") from None
    raise PostgresGoldRepositoryError(f"PostgreSQL {operation} failed") from None


def _as_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"PostgreSQL {name} must be text")
    return value


def _as_int(value: object, name: str) -> int:
    if not isinstance(value, int):
        raise TypeError(f"PostgreSQL {name} must be integer")
    return value


def _as_datetime(value: object | None, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"PostgreSQL {name} must be datetime")
    return value


def _state_from_row(row: tuple[object, ...]) -> GoldSyncState:
    if len(row) != 9:
        raise ValueError("PostgreSQL Gold sync state row has unexpected width")
    return GoldSyncState(
        dataset_id=_as_text(row[0], "dataset_id"),
        source_build_id=_as_text(row[1], "source_build_id"),
        data_sha256=_as_text(row[2], "data_sha256"),
        schema_version=_as_int(row[3], "schema_version"),
        feature_version=_as_int(row[4], "feature_version"),
        row_count=_as_int(row[5], "row_count"),
        min_timestamp=_as_datetime(row[6], "min_timestamp"),
        max_timestamp=_as_datetime(row[7], "max_timestamp"),
        synced_at_utc=cast(datetime, _as_datetime(row[8], "synced_at_utc")),
    )


def _summary_from_row(row: tuple[object, ...]) -> GoldTargetSummary:
    if len(row) != 3:
        raise ValueError("PostgreSQL Gold target summary row has unexpected width")
    return GoldTargetSummary(
        row_count=_as_int(row[0], "row_count"),
        min_timestamp=_as_datetime(row[1], "min_timestamp"),
        max_timestamp=_as_datetime(row[2], "max_timestamp"),
    )


def _payload_from_row(row: tuple[object, ...]) -> GoldRowPayload:
    if len(row) != len(POSTGRES_RAW_COLUMNS):
        raise ValueError("PostgreSQL Gold consumer row has unexpected width")
    timestamp = _as_datetime(row[0], "timestamp_m1")
    if timestamp is None:
        raise ValueError("PostgreSQL Gold consumer timestamp cannot be null")
    values: list[float | None] = []
    for value in row[1:]:
        if value is not None and not isinstance(value, float):
            raise TypeError("PostgreSQL Gold consumer feature must be float or null")
        values.append(value)
    return GoldRowPayload(timestamp, tuple(values))


def _digest_map(digests: tuple[GoldRowDigest, ...]) -> dict[datetime, str]:
    return {digest.timestamp_m1: digest.row_sha256 for digest in digests}


def _state_params(state: GoldSyncState) -> tuple[object, ...]:
    return (
        state.dataset_id,
        state.source_build_id,
        state.data_sha256,
        state.schema_version,
        state.feature_version,
        state.row_count,
        state.min_timestamp,
        state.max_timestamp,
        state.synced_at_utc,
    )


def _row_insert_params(row: GoldRowPayload) -> tuple[object, ...]:
    return (row.timestamp_m1, *row.values)


def _row_update_params(row: GoldRowPayload) -> tuple[object, ...]:
    return (*row.values, row.timestamp_m1)


def _advisory_lock_key(dataset_id: str) -> int:
    identity = f"{POSTGRES_ADVISORY_LOCK_NAMESPACE}:{dataset_id}".encode("ascii")
    return int.from_bytes(sha256(identity).digest()[:8], byteorder="big", signed=True)


class _PostgresGoldSyncTransaction:
    def __init__(self, cursor: CursorPort) -> None:
        self._cursor = cursor

    def read_state(self, dataset_id: str) -> GoldSyncState | None:
        PostgresGoldSyncRepository._require_dataset(dataset_id)
        self._cursor.execute(
            f"SELECT dataset_id, source_build_id, data_sha256, schema_version, "
            f"feature_version, row_count, min_timestamp, max_timestamp, synced_at_utc "
            f"FROM {_SYNC_STATE} WHERE dataset_id = %s",
            (dataset_id,),
        )
        row = self._cursor.fetchone()
        return None if row is None else _state_from_row(row)

    def read_digests(self, dataset_id: str) -> tuple[GoldRowDigest, ...]:
        PostgresGoldSyncRepository._require_dataset(dataset_id)
        self._cursor.execute(
            f"SELECT timestamp_m1, row_sha256 FROM {_ROW_HASHES} "
            "WHERE dataset_id = %s ORDER BY timestamp_m1",
            (dataset_id,),
        )
        result: list[GoldRowDigest] = []
        for row in self._cursor.fetchall():
            if len(row) != 2:
                raise ValueError("PostgreSQL Gold digest row has unexpected width")
            timestamp = _as_datetime(row[0], "timestamp_m1")
            if timestamp is None:
                raise ValueError("PostgreSQL Gold digest timestamp cannot be null")
            result.append(GoldRowDigest(timestamp, _as_text(row[1], "row_sha256")))
        return tuple(result)

    def read_consumer_digests(self, dataset_id: str) -> tuple[GoldRowDigest, ...]:
        PostgresGoldSyncRepository._require_dataset(dataset_id)
        self._cursor.execute(_CONSUMER_ROWS_SQL)
        return tuple(
            GoldRowDigest(payload.timestamp_m1, gold_row_sha256(payload))
            for payload in (_payload_from_row(row) for row in self._cursor.fetchall())
        )

    def summary(self, dataset_id: str) -> GoldTargetSummary:
        PostgresGoldSyncRepository._require_dataset(dataset_id)
        self._cursor.execute(_TARGET_SUMMARY_SQL)
        row = self._cursor.fetchone()
        if row is None:
            raise ValueError("PostgreSQL Gold summary query returned no row")
        return _summary_from_row(row)

    def apply_delta(
        self,
        dataset_id: str,
        plan: GoldDeltaPlan,
        state: GoldSyncState,
    ) -> None:
        PostgresGoldSyncRepository._require_dataset(dataset_id)
        if state.dataset_id != dataset_id:
            raise ValueError("Gold sync state dataset does not match requested dataset")
        digests = PostgresGoldSyncRepository._source_digest_map(plan.source_digests)
        for row in (*plan.inserts, *plan.updates):
            if row.timestamp_m1 not in digests:
                raise ValueError("Gold delta mutation is missing its source row digest")
        for row in plan.inserts:
            self._cursor.execute(_INSERT_ROW_SQL, _row_insert_params(row))
        for row in plan.updates:
            self._cursor.execute(_UPDATE_ROW_SQL, _row_update_params(row))
            self._require_exactly_one_row("update")
        for timestamp in plan.deletes:
            self._cursor.execute(_DELETE_ROW_SQL, (timestamp,))
            self._require_exactly_one_row("delete")
        for row in (*plan.inserts, *plan.updates):
            self._cursor.execute(
                _UPSERT_DIGEST_SQL,
                (dataset_id, row.timestamp_m1, digests[row.timestamp_m1]),
            )
        for timestamp in plan.deletes:
            self._cursor.execute(_DELETE_DIGEST_SQL, (dataset_id, timestamp))
        if plan.inserts or plan.updates or plan.deletes:
            self._cursor.execute("SELECT macro_loader.refresh_macro_features_explicit()")
        expected = GoldTargetSummary(state.row_count, state.min_timestamp, state.max_timestamp)
        if self.summary(dataset_id) != expected:
            raise ValueError("PostgreSQL Gold post-write summary does not match source")
        if _digest_map(self.read_digests(dataset_id)) != _digest_map(plan.source_digests):
            raise ValueError("PostgreSQL Gold post-write digest index does not match source")
        if _digest_map(self.read_consumer_digests(dataset_id)) != _digest_map(plan.source_digests):
            raise ValueError("PostgreSQL Gold post-write consumer rows do not match source")
        self._cursor.execute(_UPSERT_STATE_SQL, _state_params(state))

    def _require_exactly_one_row(self, operation: str) -> None:
        if self._cursor.rowcount != 1:
            raise ValueError(f"PostgreSQL Gold {operation} affected an unexpected row count")


class PostgresGoldSyncRepository:
    """Transactional Repository Adapter for the rebuildable Gold serving replica."""

    def __init__(
        self,
        config: PostgresSyncConfig,
        *,
        connection_factory: ConnectionFactory = _default_connection,
    ) -> None:
        self._config = config
        self._connection_factory = connection_factory

    def _open(self) -> ConnectionPort:
        connection: ConnectionPort | None = None
        try:
            connection = self._connection_factory(self._config)
            cursor = connection.cursor()
            try:
                for statement in _session_configuration(self._config.timeout_policy):
                    cursor.execute(statement)
            finally:
                cursor.close()
            return connection
        except Exception as exc:
            if connection is not None:
                connection.close()
            _raise_sanitized_error("connection initialization", exc)

    def preflight_schema(self) -> None:
        connection = self._open()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("SET TRANSACTION READ ONLY")
                self._assert_schema_contract(cursor)
            finally:
                cursor.close()
            connection.rollback()
        except PostgresGoldRepositoryError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            _raise_sanitized_error("Gold schema preflight", exc)
        finally:
            connection.close()

    def run_locked(
        self,
        operation: Callable[[GoldSyncTransaction], TransactionResult],
    ) -> TransactionResult:
        connection = self._open()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (_advisory_lock_key(POSTGRES_DATASET_ID),),
                )
                result = operation(_PostgresGoldSyncTransaction(cursor))
            finally:
                cursor.close()
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise PostgresGoldRepositoryError("PostgreSQL Gold locked transaction failed") from None
        finally:
            connection.close()

    def read_state(self, dataset_id: str) -> GoldSyncState | None:
        self._require_dataset(dataset_id)
        connection = self._open()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    f"SELECT dataset_id, source_build_id, data_sha256, schema_version, "
                    f"feature_version, row_count, min_timestamp, max_timestamp, synced_at_utc "
                    f"FROM {_SYNC_STATE} WHERE dataset_id = %s",
                    (dataset_id,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            return None if row is None else _state_from_row(row)
        except PostgresGoldRepositoryError:
            raise
        except Exception as exc:
            _raise_sanitized_error("Gold sync-state read", exc)
        finally:
            connection.close()

    def read_digests(self, dataset_id: str) -> tuple[GoldRowDigest, ...]:
        self._require_dataset(dataset_id)
        connection = self._open()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    f"SELECT timestamp_m1, row_sha256 FROM {_ROW_HASHES} "
                    "WHERE dataset_id = %s ORDER BY timestamp_m1",
                    (dataset_id,),
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()
            result: list[GoldRowDigest] = []
            for row in rows:
                if len(row) != 2:
                    raise ValueError("PostgreSQL Gold digest row has unexpected width")
                timestamp = _as_datetime(row[0], "timestamp_m1")
                if timestamp is None:
                    raise ValueError("PostgreSQL Gold digest timestamp cannot be null")
                result.append(GoldRowDigest(timestamp, _as_text(row[1], "row_sha256")))
            return tuple(result)
        except PostgresGoldRepositoryError:
            raise
        except Exception as exc:
            _raise_sanitized_error("Gold digest read", exc)
        finally:
            connection.close()

    def read_consumer_digests(self, dataset_id: str) -> tuple[GoldRowDigest, ...]:
        self._require_dataset(dataset_id)
        connection = self._open()
        try:
            cursor = connection.cursor()
            try:
                return _PostgresGoldSyncTransaction(cursor).read_consumer_digests(dataset_id)
            finally:
                cursor.close()
        except PostgresGoldRepositoryError:
            raise
        except Exception as exc:
            _raise_sanitized_error("Gold consumer read", exc)
        finally:
            connection.close()

    def summary(self, dataset_id: str) -> GoldTargetSummary:
        self._require_dataset(dataset_id)
        connection = self._open()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(_TARGET_SUMMARY_SQL)
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None:
                raise ValueError("PostgreSQL Gold summary query returned no row")
            return _summary_from_row(row)
        except PostgresGoldRepositoryError:
            raise
        except Exception as exc:
            _raise_sanitized_error("Gold summary read", exc)
        finally:
            connection.close()

    def apply_delta(
        self,
        dataset_id: str,
        plan: GoldDeltaPlan,
        state: GoldSyncState,
    ) -> None:
        self._require_dataset(dataset_id)
        if state.dataset_id != dataset_id:
            raise ValueError("Gold sync state dataset does not match requested dataset")
        digest_by_timestamp = self._source_digest_map(plan.source_digests)
        for row in (*plan.inserts, *plan.updates):
            if row.timestamp_m1 not in digest_by_timestamp:
                raise ValueError("Gold delta mutation is missing its source row digest")

        connection = self._open()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (_advisory_lock_key(dataset_id),)
                )
                self._assert_schema_contract(cursor)
                for row in plan.inserts:
                    cursor.execute(_INSERT_ROW_SQL, _row_insert_params(row))
                for row in plan.updates:
                    cursor.execute(_UPDATE_ROW_SQL, _row_update_params(row))
                    if cursor.rowcount != 1:
                        raise ValueError("PostgreSQL Gold update affected an unexpected row count")
                for timestamp in plan.deletes:
                    cursor.execute(_DELETE_ROW_SQL, (timestamp,))
                    if cursor.rowcount != 1:
                        raise ValueError("PostgreSQL Gold delete affected an unexpected row count")

                for row in (*plan.inserts, *plan.updates):
                    cursor.execute(
                        _UPSERT_DIGEST_SQL,
                        (dataset_id, row.timestamp_m1, digest_by_timestamp[row.timestamp_m1]),
                    )
                for timestamp in plan.deletes:
                    cursor.execute(_DELETE_DIGEST_SQL, (dataset_id, timestamp))

                cursor.execute(_TARGET_SUMMARY_SQL)
                summary_row = cursor.fetchone()
                if summary_row is None:
                    raise ValueError("PostgreSQL Gold verification query returned no row")
                actual = _summary_from_row(summary_row)
                expected = GoldTargetSummary(
                    state.row_count,
                    state.min_timestamp,
                    state.max_timestamp,
                )
                if actual != expected:
                    raise ValueError("PostgreSQL Gold post-write summary does not match source")
                if _digest_map(
                    _PostgresGoldSyncTransaction(cursor).read_digests(dataset_id)
                ) != _digest_map(plan.source_digests):
                    raise ValueError(
                        "PostgreSQL Gold post-write digest index does not match source"
                    )
                if _digest_map(
                    _PostgresGoldSyncTransaction(cursor).read_consumer_digests(dataset_id)
                ) != _digest_map(plan.source_digests):
                    raise ValueError("PostgreSQL Gold post-write consumer rows do not match source")
                cursor.execute(_UPSERT_STATE_SQL, _state_params(state))
            finally:
                cursor.close()
            connection.commit()
        except PostgresGoldRepositoryError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            _raise_sanitized_error("Gold delta transaction", exc)
        finally:
            connection.close()

    @staticmethod
    def _require_dataset(dataset_id: str) -> None:
        if dataset_id != POSTGRES_DATASET_ID:
            raise ValueError("unsupported PostgreSQL Gold dataset_id")

    @staticmethod
    def _source_digest_map(digests: tuple[GoldRowDigest, ...]) -> dict[datetime, str]:
        result: dict[datetime, str] = {}
        for digest in digests:
            if digest.timestamp_m1 in result:
                raise ValueError("duplicate source Gold digest timestamp")
            result[digest.timestamp_m1] = digest.row_sha256
        return result

    @staticmethod
    def _assert_schema_contract(cursor: CursorPort) -> None:
        schemas = (POSTGRES_CONSUMER_SCHEMA, POSTGRES_SYNC_SCHEMA)
        cursor.execute(_OWNED_TABLES_SQL, schemas)
        actual_tables = tuple(
            (_as_text(row[0], "table schema"), _as_text(row[1], "table name"))
            for row in cursor.fetchall()
        )
        expected_tables = tuple(sorted((spec.schema, spec.name) for spec in _SCHEMA_SPECIFICATION))
        if actual_tables != expected_tables:
            raise ValueError("PostgreSQL owned table contract does not match specification")

        cursor.execute(_OWNED_COLUMNS_SQL, schemas)
        actual_columns: dict[tuple[str, str], list[PostgresColumnSpecification]] = {}
        for row in cursor.fetchall():
            if len(row) != 8:
                raise ValueError("PostgreSQL column contract query returned unexpected width")
            schema = _as_text(row[0], "column schema")
            table = _as_text(row[1], "column table")
            _as_int(row[2], "column ordinal position")
            precision = row[5] if row[5] is not None else row[6]
            if precision is not None:
                precision = _as_int(precision, "column precision")
            actual_columns.setdefault((schema, table), []).append(
                PostgresColumnSpecification(
                    _as_text(row[3], "column name"),
                    _as_text(row[4], "column type"),
                    precision,
                    _as_text(row[7], "column nullability") == "YES",
                )
            )
        for specification in _SCHEMA_SPECIFICATION:
            if (
                tuple(actual_columns.get((specification.schema, specification.name), ()))
                != specification.columns
            ):
                raise ValueError("PostgreSQL column contract does not match specification")

        cursor.execute(_OWNED_KEYS_SQL, schemas)
        actual_keys: dict[tuple[str, str], tuple[str, ...]] = {}
        for row in cursor.fetchall():
            if len(row) != 4:
                raise ValueError("PostgreSQL key contract query returned unexpected width")
            if _as_text(row[2], "key type") not in {"PRIMARY KEY", "p"}:
                raise ValueError("PostgreSQL schema contains an unexpected unique key")
            columns = row[3]
            if not isinstance(columns, (list, tuple)) or not all(
                isinstance(column, str) for column in columns
            ):
                raise TypeError("PostgreSQL key columns must be text")
            key = (_as_text(row[0], "key schema"), _as_text(row[1], "key table"))
            if key in actual_keys:
                raise ValueError("PostgreSQL schema contains duplicate key constraints")
            actual_keys[key] = tuple(columns)
        expected_keys = {
            (specification.schema, specification.name): specification.primary_key
            for specification in _SCHEMA_SPECIFICATION
        }
        if actual_keys != expected_keys:
            raise ValueError("PostgreSQL key contract does not match specification")
        PostgresGoldSyncRepository._assert_features_view_contract(cursor)

    @staticmethod
    def _assert_features_view_contract(cursor: CursorPort) -> None:
        """Fail closed when the published materialized view drifts from its catalog contract."""
        cursor.execute(
            """SELECT classes.relkind, roles.rolname,
                      obj_description(classes.oid, 'pg_class'),
                      pg_get_viewdef(classes.oid, true)
               FROM pg_class AS classes
               JOIN pg_namespace AS namespaces ON namespaces.oid = classes.relnamespace
               JOIN pg_roles AS roles ON roles.oid = classes.relowner
              WHERE namespaces.nspname = %s AND classes.relname = %s""",
            (POSTGRES_CONSUMER_SCHEMA, "macro_features"),
        )
        row = cursor.fetchone()
        if row is None or len(row) != 4:
            raise ValueError("PostgreSQL macro feature view is missing")
        if row[0] != "m" or row[1] != _POSTGRES_OWNER_ROLE:
            raise ValueError("PostgreSQL macro feature view kind or owner differs")
        comment = row[2] if isinstance(row[2], str) else ""
        expected_comment = (
            f"macro feature view version={MACRO_FEATURE_VIEW_VERSION}; "
            f"fingerprint={MACRO_FEATURE_VIEW_FINGERPRINT}"
        )
        if comment != expected_comment:
            raise ValueError("PostgreSQL macro feature view version differs")
        definition = row[3] if isinstance(row[3], str) else ""
        normalized_definition = _normalize_view_definition(definition).replace('"', "")
        missing_markers = tuple(
            marker
            for marker in _FEATURES_VIEW_DEFINITION_MARKERS
            if marker not in normalized_definition
        )
        if missing_markers:
            raise ValueError(
                "PostgreSQL macro feature view definition differs: "
                + ",".join(missing_markers)
                + " actual="
                + normalized_definition[:300]
            )

        cursor.execute(
            """SELECT attributes.attnum, attributes.attname,
                      format_type(attributes.atttypid, attributes.atttypmod),
                      attributes.attnotnull
               FROM pg_attribute AS attributes
               WHERE attributes.attrelid = %s::regclass
                 AND attributes.attnum > 0 AND NOT attributes.attisdropped
              ORDER BY attributes.attnum""",
            (f"{POSTGRES_CONSUMER_SCHEMA}.macro_features",),
        )
        columns = cursor.fetchall()
        actual_names = tuple(row[1] for row in columns)
        if actual_names != _FEATURES_VIEW_COLUMNS_EXPECTED:
            raise ValueError(
                "PostgreSQL macro feature view columns differ: "
                + repr((actual_names[:20], _FEATURES_VIEW_COLUMNS_EXPECTED[:20]))
            )
        for ordinal, column in enumerate(columns, start=1):
            if len(column) != 4 or column[0] != ordinal:
                raise ValueError("PostgreSQL macro feature view column order differs")
            if column[2] != ("timestamp(6) with time zone" if ordinal == 1 else "double precision"):
                raise ValueError("PostgreSQL macro feature view column type differs")
            if column[3] is not False:
                raise ValueError("PostgreSQL macro feature view column nullability differs")
        for role, select_expected in (
            (POSTGRES_USER, True),
            (POSTGRES_SYNC_USER, True),
            ("public", False),
        ):
            cursor.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')",
                (role, f"{POSTGRES_CONSUMER_SCHEMA}.macro_features"),
            )
            if cursor.fetchone() != (select_expected,):
                raise ValueError(f"PostgreSQL macro feature view grants differ for {role}")


class PostgresGoldSchemaMigrator:
    """Admin-only adapter for idempotent serving-schema migrations."""

    def __init__(
        self,
        config: PostgresAdminConfig,
        *,
        connection_factory: ConnectionFactory = _default_connection,
    ) -> None:
        self._config = config
        self._connection_factory = connection_factory

    def _open(self) -> ConnectionPort:
        connection: ConnectionPort | None = None
        try:
            connection = self._connection_factory(self._config)
            cursor = connection.cursor()
            try:
                for statement in _session_configuration(self._config.timeout_policy):
                    cursor.execute(statement)
            finally:
                cursor.close()
            return connection
        except Exception as exc:
            if connection is not None:
                connection.close()
            _raise_sanitized_error("admin connection initialization", exc)

    def migrate(self) -> None:
        connection = self._open()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(_MIGRATION_LEDGER_DDL)
                cursor.execute(f"SELECT version FROM {_MIGRATION_LEDGER} ORDER BY version")
                applied_versions = tuple(
                    _as_int(row[0], "migration version") for row in cursor.fetchall()
                )
                expected_versions = tuple(range(1, len(_MIGRATIONS) + 1))
                if any(version not in expected_versions for version in applied_versions):
                    raise ValueError(
                        "PostgreSQL schema migration ledger contains an unknown version"
                    )
                for version, statements in enumerate(_MIGRATIONS, start=1):
                    if version in applied_versions:
                        continue
                    for statement in statements:
                        cursor.execute(statement)
                    cursor.execute(
                        f"INSERT INTO {_MIGRATION_LEDGER} (version, applied_at_utc) "
                        "VALUES (%s, CURRENT_TIMESTAMP)",
                        (version,),
                    )
                cursor.execute(_FED_POLICY_DDL)
                for statement in _OWNERSHIP_MIGRATIONS:
                    cursor.execute(statement)
                PostgresGoldSyncRepository._assert_schema_contract(cursor)
            finally:
                cursor.close()
            connection.commit()
        except PostgresGoldRepositoryError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            _raise_sanitized_error("Gold schema migration", exc)
        finally:
            connection.close()


class PostgresGoldSchemaReconstructor:
    """Admin-only recreation of precisely the serving schemas before canonical migration."""

    def __init__(
        self,
        config: PostgresAdminConfig,
        *,
        connection_factory: ConnectionFactory = _default_connection,
    ) -> None:
        self._config = config
        self._connection_factory = connection_factory

    def recreate(self) -> None:
        connection: ConnectionPort | None = None
        try:
            connection = self._connection_factory(self._config)
            cursor = connection.cursor()
            try:
                for statement in _session_configuration(self._config.timeout_policy):
                    cursor.execute(statement)
                cursor.execute(f"DROP SCHEMA IF EXISTS {_quote(POSTGRES_CONSUMER_SCHEMA)} CASCADE")
                cursor.execute(f"DROP SCHEMA IF EXISTS {_quote(POSTGRES_SYNC_SCHEMA)} CASCADE")
                for schema in (POSTGRES_CONSUMER_SCHEMA, POSTGRES_SYNC_SCHEMA):
                    cursor.execute(
                        f"CREATE SCHEMA {_quote(schema)} "
                        f"AUTHORIZATION {_quote(_POSTGRES_OWNER_ROLE)}"
                    )
                    cursor.execute(f"REVOKE ALL ON SCHEMA {_quote(schema)} FROM PUBLIC")
                    cursor.execute(
                        f"REVOKE CREATE ON SCHEMA {_quote(schema)} FROM {_quote(POSTGRES_USER)}"
                    )
                    cursor.execute(
                        f"GRANT USAGE ON SCHEMA {_quote(schema)} TO {_quote(POSTGRES_USER)}"
                    )
                cursor.execute(f"SET ROLE {_quote(_POSTGRES_OWNER_ROLE)}")
                cursor.execute(_MIGRATION_LEDGER_DDL)
                for version, statements in enumerate(_MIGRATIONS, start=1):
                    for statement in statements:
                        cursor.execute(statement)
                    cursor.execute(
                        f"INSERT INTO {_MIGRATION_LEDGER} (version, applied_at_utc) "
                        "VALUES (%s, CURRENT_TIMESTAMP)",
                        (version,),
                    )
                cursor.execute("RESET ROLE")
                for schema, table in (
                    (POSTGRES_CONSUMER_SCHEMA, POSTGRES_CONSUMER_TABLE),
                    (POSTGRES_SYNC_SCHEMA, FED_POLICY_DATASET_ID),
                    (POSTGRES_SYNC_SCHEMA, POSTGRES_SYNC_STATE_TABLE),
                    (POSTGRES_SYNC_SCHEMA, POSTGRES_ROW_HASH_TABLE),
                ):
                    cursor.execute(
                        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
                        f"{_quote(schema)}.{_quote(table)} TO {_quote(POSTGRES_USER)}"
                    )
                cursor.execute(
                    f"GRANT SELECT ON TABLE {_MIGRATION_LEDGER} TO {_quote(POSTGRES_USER)}"
                )
                PostgresGoldSyncRepository._assert_schema_contract(cursor)
            finally:
                cursor.close()
            connection.commit()
        except Exception as exc:
            if connection is not None:
                connection.rollback()
            _raise_sanitized_error("Gold schema reconstruction", exc)
        finally:
            if connection is not None:
                connection.close()

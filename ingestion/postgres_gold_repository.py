"""Psycopg Adapter for the canonical Gold PostgreSQL serving-plane replica."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import NoReturn, Protocol, TypeVar, cast

import psycopg

from application.gold_frame import GOLD_COLUMNS
from application.macro_feature_catalog import (
    FEATURE_COLUMNS,
    FEATURE_POSTGRES_TYPES,
    MACRO_FEATURE_VIEW_FINGERPRINT,
    MACRO_FEATURE_VIEW_VERSION,
    OHLC_BAR_WINDOWS,
    PRICE_FEATURE_SERIES,
    RAW_SERIES,
)
from application.macro_features import MACRO_POLICY, macro_delta_lags
from application.momentum_features import MOMENTUM_POLICY
from application.postgres_delta import gold_row_sha256
from application.postgres_sync import (
    POSTGRES_CONSUMER_SCHEMA,
    POSTGRES_CONSUMER_TABLE,
    POSTGRES_DATASET_ID,
    POSTGRES_LEVEL_COLUMNS,
    POSTGRES_OHLC_COLUMNS,
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
from application.registry import OHLC_FIELDS, OHLC_SERIES
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
    EMA_PERIODS,
    MACD_OUTPUT_SUFFIXES,
    MACD_PRICE_EMA_PERIODS,
    MACD_SIGNAL_EMA_PERIOD,
    ROC_WINDOWS,
    RSI_COMPOSITE_INTERNAL_WINDOW,
    RSI_WINDOWS,
    SMA_RATIO_WINDOWS,
)
from application.volatility_features import VOLATILITY_SERIES

_RETIRED_FED_DATASET_ID = "fed_policy_features_daily"
_RETIRED_FED_ORIGIN_COLUMNS = (
    "fed_next_expected_move_bp",
    "fed_path_slope_m3_bp",
    "fed_next_uncertainty_bp",
    "fed_repricing_5obs_bp",
)
_RETIRED_FED_VIEW_COLUMNS = tuple(
    column
    for origin in _RETIRED_FED_ORIGIN_COLUMNS
    for column in (
        origin,
        *(f"{origin}_delta_{lag}obs" for lag in (1, 5, 20)),
        f"{origin}_zscore_60obs",
        *(
            f"{origin}_momentum_autocorr_{lag}_{window}obs"
            for lag, window in ((1, 60), (5, 60), (20, 120))
        ),
    )
)
_RETIRED_FED_LEGACY_COLUMNS = (
    "fed_3m_expected_move_bp",
    "fed_next_expected_move_bp_delta_5obs",
    "fomc_business_days_to_next",
)

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
    statement_timeout_ms: int = 300_000
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


def _ema_state_cte(
    series: str,
    period: int,
    *,
    source_relation: str | None = None,
    value_column: str | None = None,
    state_name: str | None = None,
    allow_nonpositive: bool = False,
) -> str:
    """Build a private SMA-seeded EMA recurrence over an ordered source relation."""
    cte = _quote(state_name or f"{series}_ema_{period}_state")
    relation = source_relation or _CONSUMER
    value = _quote(value_column or f"{series}_level")
    initial_valid = "input.levels[1] IS NOT NULL" if allow_nonpositive else "input.levels[1] > 0"
    next_index = "state.observation_index + 1"
    next_value = f"input.levels[{next_index}]"
    next_timestamp = f"input.timestamps[{next_index}]"
    alpha = f"2.0 / {period + 1}.0"
    # Resolve the conditions only after next_value has been constructed above.
    next_valid = (
        f"{next_value} IS NOT NULL"
        if allow_nonpositive
        else f"{next_value} IS NOT NULL AND {next_value} > 0"
    )
    next_invalid = (
        f"{next_value} IS NULL"
        if allow_nonpositive
        else f"{next_value} IS NULL OR {next_value} <= 0"
    )
    return (
        f"{cte} AS MATERIALIZED (WITH RECURSIVE input AS ("
        f"SELECT array_agg(timestamp_m1 ORDER BY timestamp_m1) AS timestamps, "
        f"array_agg({value} ORDER BY timestamp_m1) AS levels FROM {relation}), state AS ("
        "SELECT 1 AS observation_index, input.timestamps[1] AS timestamp_m1, "
        "input.levels[1] AS level, "
        f"CASE WHEN {initial_valid} THEN 1 ELSE 0 END AS seed_count, "
        f"CASE WHEN {initial_valid} THEN input.levels[1] ELSE 0.0 END AS seed_sum, "
        "NULL::double precision AS ema FROM input WHERE cardinality(input.timestamps) > 0 "
        "UNION ALL SELECT state.observation_index + 1, "
        f"{next_timestamp}, {next_value}, "
        f"CASE WHEN {next_valid} THEN CASE WHEN state.ema IS NULL "
        f"THEN state.seed_count + 1 ELSE {period} END ELSE 0 END AS seed_count, "
        f"CASE WHEN {next_valid} AND state.ema IS NULL "
        f"THEN state.seed_sum + {next_value} ELSE 0.0 END AS seed_sum, "
        f"CASE WHEN {next_invalid} THEN NULL "
        f"WHEN state.ema IS NOT NULL THEN {alpha} * {next_value} "
        f"+ (1.0 - {alpha}) * state.ema "
        f"WHEN state.seed_count + 1 >= {period} "
        f"THEN (state.seed_sum + {next_value}) / {period}.0 "
        "ELSE NULL END AS ema FROM state CROSS JOIN input "
        "WHERE state.observation_index < cardinality(input.timestamps)) "
        "SELECT timestamp_m1, ema FROM state)"
    )


_RETIRED_FED_RAW_UPDATES = ", ".join(
    f"{_quote(column)} = EXCLUDED.{_quote(column)}" for column in _RETIRED_FED_ORIGIN_COLUMNS
)
_RETIRED_FED_VIEW_UPDATES = ", ".join(
    f"{_quote(column)} = EXCLUDED.{_quote(column)}" for column in _RETIRED_FED_VIEW_COLUMNS
)
_RETIRED_FED_ORIGIN_COLUMN_NAMES = ", ".join(
    _quote(column) for column in _RETIRED_FED_ORIGIN_COLUMNS
)
_RETIRED_FED_ORIGIN_INFO_NAMES = ", ".join(repr(column) for column in _RETIRED_FED_ORIGIN_COLUMNS)
_RETIRED_FED_VIEW_COLUMN_NAMES = ", ".join(_quote(column) for column in _RETIRED_FED_VIEW_COLUMNS)
_RETIRED_FED_VIEW_INFO_NAMES = ", ".join(repr(column) for column in _RETIRED_FED_VIEW_COLUMNS)
_RETIRED_FED_LEGACY_COLUMN_NAMES = ", ".join(
    _quote(column) for column in _RETIRED_FED_LEGACY_COLUMNS
)
_RETIRED_FED_LEGACY_COLUMN_DEFS = ", ".join(
    f"{_quote(column)} DOUBLE PRECISION NULL" for column in _RETIRED_FED_LEGACY_COLUMNS
)
_RETIRED_FED_LEGACY_INFO_NAMES = ", ".join(repr(column) for column in _RETIRED_FED_LEGACY_COLUMNS)
_RETIRED_FED_LEGACY_VALUE_ROWS = ", ".join(
    f"({index}, '{column}')" for index, column in enumerate(_RETIRED_FED_LEGACY_COLUMNS, start=1)
)
_RETIRED_FED_LEGACY_DROP_CLAUSES = ", ".join(
    f"DROP COLUMN IF EXISTS {_quote(column)}" for column in _RETIRED_FED_LEGACY_COLUMNS
)
_RETIRED_FED_ORIGIN_COLUMN_DEFS = ", ".join(
    f"{_quote(column)} DOUBLE PRECISION NULL" for column in _RETIRED_FED_ORIGIN_COLUMNS
)
_RETIRED_FED_VIEW_COLUMN_DEFS = ", ".join(
    f"{_quote(column)} DOUBLE PRECISION NULL" for column in _RETIRED_FED_VIEW_COLUMNS
)


_CONSUMER = f"{_quote(POSTGRES_CONSUMER_SCHEMA)}.{_quote(POSTGRES_CONSUMER_TABLE)}"
_SYNC_STATE = f"{_quote(POSTGRES_SYNC_SCHEMA)}.{_quote(POSTGRES_SYNC_STATE_TABLE)}"
_ROW_HASHES = f"{_quote(POSTGRES_SYNC_SCHEMA)}.{_quote(POSTGRES_ROW_HASH_TABLE)}"
_RAW_FEATURE_COLUMNS = POSTGRES_RAW_COLUMNS[1:]
_MIGRATION_LEDGER_TABLE = "schema_migrations"
_MIGRATION_LEDGER = f"{_quote(POSTGRES_SYNC_SCHEMA)}.{_quote(_MIGRATION_LEDGER_TABLE)}"
_POSTGRES_OWNER_ROLE = "macro-loader-owner"
_RETIRED_ARCHIVE_SCHEMA = "macro_loader_retired"
_RETIRED_RAW_ARCHIVE = f'{_quote(_RETIRED_ARCHIVE_SCHEMA)}."fed_macro_raw"'
_RETIRED_FEATURE_ARCHIVE = f'{_quote(_RETIRED_ARCHIVE_SCHEMA)}."fed_macro_features"'
_RETIRED_POLICY_ARCHIVE = f'{_quote(_RETIRED_ARCHIVE_SCHEMA)}."fed_policy_features_daily"'
_RETIRED_STATE_ARCHIVE = f'{_quote(_RETIRED_ARCHIVE_SCHEMA)}."fed_policy_sync_state"'
_RETIRED_HASH_ARCHIVE = f'{_quote(_RETIRED_ARCHIVE_SCHEMA)}."fed_policy_row_hashes"'
_RETIRED_LEGACY_ARCHIVE = f'{_quote(_RETIRED_ARCHIVE_SCHEMA)}."fed_legacy_raw_columns"'
_LEGACY_CONSUMER = f'{_quote(POSTGRES_CONSUMER_SCHEMA)}."macro_features_daily"'
_FEATURES_VIEW = f'{_quote(POSTGRES_CONSUMER_SCHEMA)}."macro_features"'
_FEATURES_VIEW_COLUMNS = FEATURE_COLUMNS[1:]
_AVAILABLE_AT_EXPRESSION = """(
    (
        ("timestamp_m1" AT TIME ZONE 'UTC')::date
        + CASE EXTRACT(ISODOW FROM "timestamp_m1" AT TIME ZONE 'UTC')::integer
            WHEN 5 THEN 3
            WHEN 6 THEN 2
            WHEN 7 THEN 1
            ELSE 1
          END
    )::timestamp AT TIME ZONE 'UTC'
)"""
_AVAILABLE_AT_COLUMN_DDL = (
    f"{_quote('available_at_utc')} TIMESTAMPTZ(6) "
    f"GENERATED ALWAYS AS ({_AVAILABLE_AT_EXPRESSION}) STORED NOT NULL"
)
_ADDED_LEVEL_COLUMNS_V28 = tuple(
    f"{series}_level"
    for series in (
        "vstoxx1m",
        "vstoxx2m",
        "vstoxx3m",
        "ciss_money",
        "ciss_bonds",
        "ciss_equity",
        "ciss_financials",
    )
)
_ADDED_LEVEL_COLUMNS_V29 = (
    "euro_hy_yield_level",
    "euro_hy_ytw_level",
    "euro_hy_total_return_level",
)
_ADDED_LEVEL_COLUMNS = (*_ADDED_LEVEL_COLUMNS_V28, *_ADDED_LEVEL_COLUMNS_V29)
_LEVEL_COLUMN_DEFS = ",\n    ".join(
    f"{_quote(column)} DOUBLE PRECISION NULL"
    for column in POSTGRES_LEVEL_COLUMNS
    if column not in _ADDED_LEVEL_COLUMNS
)
_OHLC_COLUMN_DEFS = ",\n    ".join(
    f"{_quote(column)} DOUBLE PRECISION NULL" for column in POSTGRES_OHLC_COLUMNS
)
_ADDED_LEVEL_COLUMN_DEFS = ",\n    ".join(
    f"{_quote(column)} DOUBLE PRECISION NULL" for column in _ADDED_LEVEL_COLUMNS
)

_CONSUMER_DDL = f"""CREATE TABLE IF NOT EXISTS {_CONSUMER} (
    {_quote("timestamp_m1")} TIMESTAMPTZ(6) NOT NULL PRIMARY KEY,
    {_LEVEL_COLUMN_DEFS},
    {_AVAILABLE_AT_COLUMN_DDL},
    {_OHLC_COLUMN_DEFS},
    {_ADDED_LEVEL_COLUMN_DEFS}
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
_AVAILABLE_AT_COLUMN_MIGRATION = (
    f"ALTER TABLE {_CONSUMER} ADD COLUMN IF NOT EXISTS {_AVAILABLE_AT_COLUMN_DDL}",
)
_OHLC_COLUMN_MIGRATION = (
    f"ALTER TABLE {_CONSUMER} "
    + ", ".join(
        f"ADD COLUMN IF NOT EXISTS {_quote(column)} DOUBLE PRECISION NULL"
        for column in POSTGRES_OHLC_COLUMNS
    ),
)
_ADDED_LEVEL_COLUMN_MIGRATION = (
    f"ALTER TABLE {_CONSUMER} "
    + ", ".join(
        f"ADD COLUMN IF NOT EXISTS {_quote(column)} DOUBLE PRECISION NULL"
        for column in _ADDED_LEVEL_COLUMNS_V28
    ),
)
_EURO_HY_LEVEL_COLUMN_MIGRATION = (
    f"ALTER TABLE {_CONSUMER} "
    + ", ".join(
        f"ADD COLUMN IF NOT EXISTS {_quote(column)} DOUBLE PRECISION NULL"
        for column in _ADDED_LEVEL_COLUMNS_V29
    ),
)
_OWNERSHIP_MIGRATIONS = tuple(
    f"ALTER TABLE IF EXISTS {_quote(schema)}.{_quote(table)} "
    f"OWNER TO {_quote(_POSTGRES_OWNER_ROLE)}"
    for schema, table in (
        (POSTGRES_CONSUMER_SCHEMA, POSTGRES_CONSUMER_TABLE),
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
_FED_POLICY_RENAME_MIGRATION = (
    f"CREATE SCHEMA IF NOT EXISTS {_quote(_RETIRED_ARCHIVE_SCHEMA)}",
    f"ALTER SCHEMA {_quote(_RETIRED_ARCHIVE_SCHEMA)} OWNER TO {_quote(_POSTGRES_OWNER_ROLE)}",
    f"REVOKE ALL ON SCHEMA {_quote(_RETIRED_ARCHIVE_SCHEMA)} FROM PUBLIC, "
    f"{_quote(POSTGRES_USER)}, {_quote(POSTGRES_SYNC_USER)}",
    f"""CREATE TABLE IF NOT EXISTS {_RETIRED_LEGACY_ARCHIVE} (
        timestamp_m1 TIMESTAMPTZ(6) NOT NULL PRIMARY KEY,
        {_RETIRED_FED_LEGACY_COLUMN_DEFS}
    )""",
    f"ALTER TABLE {_RETIRED_LEGACY_ARCHIVE} OWNER TO {_quote(_POSTGRES_OWNER_ROLE)}",
    f"REVOKE ALL ON TABLE {_RETIRED_LEGACY_ARCHIVE} FROM PUBLIC, "
    f"{_quote(POSTGRES_USER)}, {_quote(POSTGRES_SYNC_USER)}",
    f"""DO $$
    DECLARE
        legacy_projection TEXT;
        legacy_updates TEXT;
    BEGIN
        IF to_regclass('{POSTGRES_CONSUMER_SCHEMA}.{POSTGRES_CONSUMER_TABLE}') IS NOT NULL
           AND EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema = '{POSTGRES_CONSUMER_SCHEMA}'
                         AND table_name = '{POSTGRES_CONSUMER_TABLE}'
                         AND column_name = 'timestamp_m1')
           AND EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema = '{POSTGRES_CONSUMER_SCHEMA}'
                         AND table_name = '{POSTGRES_CONSUMER_TABLE}'
                         AND column_name IN ({_RETIRED_FED_LEGACY_INFO_NAMES}))
        THEN
            SELECT string_agg(
                CASE WHEN present.column_name IS NOT NULL THEN format('%I', wanted.column_name)
                     ELSE 'NULL::double precision' END, ', ' ORDER BY wanted.ordinal
            ) INTO legacy_projection
            FROM (VALUES {_RETIRED_FED_LEGACY_VALUE_ROWS})
                 AS wanted(ordinal, column_name)
            LEFT JOIN information_schema.columns present
              ON present.table_schema = '{POSTGRES_CONSUMER_SCHEMA}'
             AND present.table_name = '{POSTGRES_CONSUMER_TABLE}'
             AND present.column_name = wanted.column_name;
            SELECT string_agg(
                format(
                    '%I = COALESCE(EXCLUDED.%I, retired.%I)',
                    column_name,
                    column_name,
                    column_name
                ),
                ', '
            ) INTO legacy_updates
            FROM unnest(ARRAY[{_RETIRED_FED_LEGACY_INFO_NAMES}]) AS legacy(column_name);
            EXECUTE 'INSERT INTO {_RETIRED_LEGACY_ARCHIVE} AS retired '
                 || '(timestamp_m1, {_RETIRED_FED_LEGACY_COLUMN_NAMES}) '
                 || 'SELECT timestamp_m1, ' || legacy_projection || ' FROM {_CONSUMER} '
                 || 'ON CONFLICT (timestamp_m1) DO UPDATE SET ' || legacy_updates;
        END IF;
    END
    $$""",
    f"""DO $$
    BEGIN
        IF to_regclass('{POSTGRES_CONSUMER_SCHEMA}.{POSTGRES_CONSUMER_TABLE}') IS NOT NULL THEN
            ALTER TABLE {_CONSUMER} {_RETIRED_FED_LEGACY_DROP_CLAUSES};
        END IF;
    END
    $$""",
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
    required_lags = tuple(sorted({1, 3, 5, 10, 20, *LOG_RETURN_WINDOWS, *ROC_WINDOWS}))
    rolling_windows = tuple(
        sorted(
            {
                *RETURN_WINDOWS,
                *RETURN_MEAN_WINDOWS,
                *VOLATILITY_WINDOWS,
                *(long for _, long in SMA_RATIO_WINDOWS),
                *DRAWDOWN_WINDOWS,
                *(window for _, window in MOMENTUM_POLICY.lag_windows),
                60,
            }
        )
    )
    for series in RAW_SERIES:
        level = _quote(f"{series}_level")
        source = _quote(f"{series}_source")
        changes = _quote(f"{series}_changes")
        features = _quote(f"{series}_features")
        lags = macro_lags.get(series, (1, 5, 20))
        lag_sql = ", ".join(
            f"lag({level}, {lag}) OVER ordered AS {_quote(f'lag_{lag}')}" for lag in required_lags
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
            f"{source} AS MATERIALIZED (SELECT timestamp_m1, {level} AS level, {lag_sql}, "
            "row_number() OVER ordered AS observation_number "
            f"FROM {_CONSUMER} WHERE {level} IS NOT NULL "
            "WINDOW ordered AS (ORDER BY timestamp_m1))"
        )
        gain_loss_sql = (
            f"CASE WHEN level > 0 AND {_quote('lag_1')} > 0 "
            f"THEN greatest(level - {_quote('lag_1')}, 0.0) END AS gain, "
            f"CASE WHEN level > 0 AND {_quote('lag_1')} > 0 "
            f"THEN greatest({_quote('lag_1')} - level, 0.0) END AS loss"
        )
        log_return_sql = ", ".join(
            f"CASE WHEN level > 0 AND {_quote(f'lag_{window}')} > 0 "
            f"THEN ln(level / {_quote(f'lag_{window}')}) END AS "
            f"{_quote(f'log_return_{window}')}"
            for window in LOG_RETURN_WINDOWS
        )
        source_ctes.append(
            f"{changes} AS MATERIALIZED (SELECT *, {delta_sql}, level - "
            f"{_quote('lag_1')} AS change, "
            f"{change_lags}, {gain_loss_sql}, {log_return_sql} FROM {source})"
        )
        rsi_names: dict[int, tuple[str, str]] = {}
        rsi_cte_names: list[str] = []
        all_rsi_periods = (*RSI_WINDOWS, RSI_COMPOSITE_INTERNAL_WINDOW)
        for period in all_rsi_periods:
            gain_name = f"avg_gain_{period}"
            loss_name = f"avg_loss_{period}"
            rsi_names[period] = (gain_name, loss_name)
            rsi = _quote(f"{series}_rsi_{period}_state")
            rsi_cte_names.append(rsi)
            source_ctes.append(
                f"{rsi} AS (WITH RECURSIVE input AS ("
                f"SELECT array_agg(timestamp_m1 ORDER BY observation_number) AS timestamps, "
                f"array_agg(gain ORDER BY observation_number) AS gains, "
                f"array_agg(loss ORDER BY observation_number) AS losses "
                f"FROM {changes}), state AS ("
                f"SELECT 1 AS observation_index, timestamps[1] AS timestamp_m1, "
                "gains[1] AS gain, losses[1] AS loss, "
                "CASE WHEN gains[1] IS NULL OR losses[1] IS NULL THEN 0 ELSE 1 END AS valid_count, "
                "coalesce(gains[1], 0.0) AS gain_sum, coalesce(losses[1], 0.0) AS loss_sum, "
                "NULL::double precision AS avg_gain, NULL::double precision AS avg_loss "
                "FROM input "
                "UNION ALL "
                "SELECT state.observation_index + 1, "
                "input.timestamps[state.observation_index + 1], "
                "input.gains[state.observation_index + 1], "
                "input.losses[state.observation_index + 1], "
                "CASE WHEN input.gains[state.observation_index + 1] IS NULL "
                "OR input.losses[state.observation_index + 1] IS NULL THEN 0 "
                f"WHEN state.avg_gain IS NULL THEN least(state.valid_count + 1, {period}) "
                f"ELSE {period} END AS valid_count, "
                "CASE WHEN input.gains[state.observation_index + 1] IS NULL "
                "OR input.losses[state.observation_index + 1] IS NULL THEN 0.0 "
                "WHEN state.avg_gain IS NULL THEN state.gain_sum "
                "+ input.gains[state.observation_index + 1] "
                "ELSE 0.0 END AS gain_sum, "
                "CASE WHEN input.gains[state.observation_index + 1] IS NULL "
                "OR input.losses[state.observation_index + 1] IS NULL THEN 0.0 "
                "WHEN state.avg_loss IS NULL THEN state.loss_sum "
                "+ input.losses[state.observation_index + 1] "
                "ELSE 0.0 END AS loss_sum, "
                "CASE WHEN input.gains[state.observation_index + 1] IS NULL "
                "OR input.losses[state.observation_index + 1] IS NULL THEN NULL "
                f"WHEN state.avg_gain IS NULL AND state.valid_count + 1 >= {period} "
                f"THEN (state.gain_sum + input.gains[state.observation_index + 1]) / {period}.0 "
                f"WHEN state.avg_gain IS NOT NULL THEN "
                f"(({period - 1}.0 * state.avg_gain) + input.gains[state.observation_index + 1]) "
                f"/ {period}.0 "
                "END AS avg_gain, "
                "CASE WHEN input.gains[state.observation_index + 1] IS NULL "
                "OR input.losses[state.observation_index + 1] IS NULL THEN NULL "
                f"WHEN state.avg_loss IS NULL AND state.valid_count + 1 >= {period} "
                f"THEN (state.loss_sum + input.losses[state.observation_index + 1]) / {period}.0 "
                f"WHEN state.avg_loss IS NOT NULL THEN "
                f"(({period - 1}.0 * state.avg_loss) + input.losses[state.observation_index + 1]) "
                f"/ {period}.0 "
                "END AS avg_loss "
                "FROM state CROSS JOIN input "
                "WHERE state.observation_index < cardinality(input.timestamps)) "
                "SELECT timestamp_m1, avg_gain, avg_loss FROM state)"
            )
        feature_input = _quote(f"{series}_rsi_input")
        rsi_select = ["changes.*"]
        rsi_joins: list[str] = []
        for period, (gain_name, loss_name) in rsi_names.items():
            rsi = _quote(f"{series}_rsi_{period}_state")
            rsi_select.extend(
                [
                    f"{rsi}.avg_gain AS {_quote(gain_name)}",
                    f"{rsi}.avg_loss AS {_quote(loss_name)}",
                ]
            )
            rsi_joins.append(f"LEFT JOIN {rsi} ON {rsi}.timestamp_m1 = changes.timestamp_m1")
        source_ctes.append(
            f"{feature_input} AS MATERIALIZED (SELECT {', '.join(rsi_select)} "
            f"FROM {changes} AS changes {' '.join(rsi_joins)})"
        )
        rsi_values = _quote(f"{series}_rsi_values")
        rsi_value_expressions: list[str] = []
        for period in all_rsi_periods:
            gain_name, loss_name = rsi_names[period]
            current_gain = f"changes.{_quote(gain_name)}"
            current_loss = f"changes.{_quote(loss_name)}"
            rsi_value_expressions.append(
                f"CASE WHEN changes.observation_number < {period + 1} THEN NULL "
                f"WHEN {current_gain} IS NULL OR {current_loss} IS NULL THEN NULL "
                f"WHEN {current_gain} = 0 AND {current_loss} = 0 THEN NULL "
                f"WHEN {current_loss} = 0 THEN 100.0 "
                f"ELSE 100.0 - 100.0 / (1.0 + {current_gain} / {current_loss}) "
                f"END AS {_quote(f'rsi_value_{period}')}"
            )
        source_ctes.append(
            f"{rsi_values} AS MATERIALIZED (SELECT changes.*, "
            f"{', '.join(rsi_value_expressions)} FROM {feature_input} AS changes)"
        )
        expressions: list[str] = []
        for window in LOG_RETURN_WINDOWS if series in PRICE_FEATURE_SERIES else ():
            expressions.append(
                f"{_quote(f'log_return_{window}')} AS {_quote(f'{series}_log_return_{window}obs')}"
            )
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
                f"THEN CASE WHEN corr(change, {_quote(f'change_lag_{lag}')}) "
                f"OVER window_{window} IS NULL THEN NULL ELSE greatest(corr(change, "
                f"{_quote(f'change_lag_{lag}')}) OVER window_{window}, 0.0) END END AS "
                f"{_quote(f'{series}_momentum_autocorr_{lag}_{window}obs')}"
            )
        for window in RETURN_WINDOWS if series in PRICE_FEATURE_SERIES else ():
            expressions.append(
                f"CASE WHEN count({_quote('log_return_1')}) OVER window_{window} = {window} "
                f"THEN exp(sum({_quote('log_return_1')}) OVER window_{window}) - 1.0 "
                f"END AS {_quote(f'{series}_return_geom_{window}obs_pct')}"
            )
        for window in RETURN_MEAN_WINDOWS if series in PRICE_FEATURE_SERIES else ():
            expressions.append(
                f"CASE WHEN count({_quote('log_return_1')}) OVER window_{window} = {window} "
                f"THEN avg({_quote('log_return_1')}) OVER window_{window} "
                f"END AS {_quote(f'{series}_return_mean_{window}obs')}"
            )
        for window in VOLATILITY_WINDOWS if series in PRICE_FEATURE_SERIES else ():
            expressions.append(
                f"CASE WHEN count({_quote('log_return_1')}) OVER window_{window} = {window} "
                f"THEN stddev_samp({_quote('log_return_1')}) OVER window_{window} "
                f"END AS {_quote(f'{series}_volatility_{window}obs')}"
            )
        for short, long in SMA_RATIO_WINDOWS if series in PRICE_FEATURE_SERIES else ():
            expressions.append(
                f"CASE WHEN count(level) OVER window_{long} = {long} THEN "
                f"avg(level) OVER window_{short} / nullif(avg(level) OVER window_{long}, 0) "
                f"END AS {_quote(f'{series}_sma_ratio_{short}_{long}')}"
            )
        for period in RSI_WINDOWS if series in PRICE_FEATURE_SERIES else ():
            expressions.append(
                f"changes.{_quote(f'rsi_value_{period}')} AS {_quote(f'{series}_rsi_{period}obs')}"
            )
        if series in PRICE_FEATURE_SERIES:
            rsi_5 = f"changes.{_quote('rsi_value_5')}"
            rsi_15 = f"changes.{_quote(f'rsi_value_{RSI_COMPOSITE_INTERNAL_WINDOW}')}"
            expressions.append(
                f"CASE WHEN {rsi_5} IS NOT NULL AND {rsi_15} IS NOT NULL "
                f"THEN {rsi_5} / NULLIF({rsi_15}, 0.0) END "
                f"AS {_quote(f'{series}_rsi_ratio_5_15')}"
            )
        for window in ROC_WINDOWS if series in PRICE_FEATURE_SERIES else ():
            expressions.append(
                f"CASE WHEN level > 0 AND {_quote(f'lag_{window}')} > 0 "
                f"THEN level / {_quote(f'lag_{window}')} - 1.0 "
                f"END AS {_quote(f'{series}_roc_{window}obs')}"
            )
        if series in PRICE_FEATURE_SERIES:
            expressions.append(
                f"CASE WHEN level > 0 AND {_quote('lag_4')} > 0 "
                f"AND {_quote('lag_8')} > 0 THEN "
                f"(level / {_quote('lag_4')} - 1.0) - (level / {_quote('lag_8')} - 1.0) "
                f"END AS {_quote(f'{series}_roc_acceleration_4_8')}"
            )
        for window in DRAWDOWN_WINDOWS if series in PRICE_FEATURE_SERIES else ():
            expressions.append(
                f"CASE WHEN count(level) OVER window_{window} = {window} "
                f"THEN level / nullif(max(level) OVER window_{window}, 0) - 1.0 "
                f"END AS {_quote(f'{series}_drawdown_{window}obs')}"
            )
        rolling_frames = ", ".join(
            f"window_{window} AS (ORDER BY changes.timestamp_m1 ROWS BETWEEN "
            f"{window - 1} PRECEDING AND CURRENT ROW)"
            for window in rolling_windows
        )
        feature_base = _quote(f"{series}_feature_base")
        source_ctes.append(
            f"{feature_base} AS MATERIALIZED (SELECT changes.timestamp_m1, changes.level, "
            f"{', '.join(expressions)} "
            f"FROM {rsi_values} AS changes "
            f"WINDOW {rolling_frames})"
        )
        helper_joins: list[str] = []
        helper_selects: list[str] = []
        if series in OHLC_SERIES:
            rolling_window = max(OHLC_BAR_WINDOWS)
            open_column = _quote(f"{series}_open")
            high_column = _quote(f"{series}_high")
            low_column = _quote(f"{series}_low")
            close_column = _quote(f"{series}_close")
            finite = "{} > '-Infinity'::double precision AND {} < 'Infinity'::double precision"
            metric_definitions = (
                (
                    "high_low_range",
                    f"({high_column} - {low_column}) / {close_column}",
                    (high_column, low_column, close_column),
                    close_column,
                ),
                (
                    "intraday_return",
                    f"({close_column} - {open_column}) / {open_column}",
                    (close_column, open_column),
                    open_column,
                ),
            )
            for family, formula, operands, denominator in metric_definitions:
                metric = _quote(f"{series}_{family}_metric")
                valid_metric = _quote(f"{series}_{family}_valid")
                rolling = _quote(f"{series}_{family}_rolling")
                finite_operands = " AND ".join(finite.format(value, value) for value in operands)
                source_ctes.append(
                    f"{metric} AS MATERIALIZED (SELECT timestamp_m1, CASE WHEN "
                    f"{finite_operands} AND {denominator} > 0.0 THEN {formula} END AS value "
                    f"FROM {_CONSUMER})"
                )
                source_ctes.append(
                    f"{valid_metric} AS MATERIALIZED (SELECT timestamp_m1, value, "
                    f"row_number() OVER (ORDER BY timestamp_m1) AS valid_number FROM {metric} "
                    "WHERE value IS NOT NULL AND value > '-Infinity'::double precision "
                    "AND value < 'Infinity'::double precision)"
                )
                source_ctes.append(
                    f"{rolling} AS MATERIALIZED (SELECT timestamp_m1, "
                    f"CASE WHEN count(*) OVER w = {rolling_window} "
                    f"AND avg(value) OVER w > '-Infinity'::double precision "
                    f"AND avg(value) OVER w < 'Infinity'::double precision "
                    "THEN avg(value) OVER w END AS mean_5 "
                    f"FROM {valid_metric} WINDOW w AS (ORDER BY valid_number ROWS BETWEEN "
                    f"{rolling_window - 1} PRECEDING AND CURRENT ROW))"
                )
                helper_joins.extend(
                    [
                        f"LEFT JOIN {valid_metric} AS {family}_current "
                        f"ON {family}_current.timestamp_m1 = base.timestamp_m1",
                        f"LEFT JOIN {rolling} AS {family}_rolling "
                        f"ON {family}_rolling.timestamp_m1 = base.timestamp_m1",
                    ]
                )
                helper_selects.extend(
                    [
                        f"{family}_current.value AS {_quote(f'{series}_{family}_1obs')}",
                        f"{family}_rolling.mean_5 AS {_quote(f'{series}_{family}_5obs')}",
                    ]
                )

            gap_source = _quote(f"{series}_overnight_source")
            gap_metric = _quote(f"{series}_overnight_metric")
            gap_valid = _quote(f"{series}_overnight_valid")
            gap_rolling = _quote(f"{series}_overnight_rolling")
            source_close = f"source_bar.{close_column}"
            current_open = f"current_bar.{open_column}"
            source_ctes.append(
                f"{gap_source} AS MATERIALIZED (SELECT current_bar.timestamp_m1, "
                f"current_bar.{open_column} AS open, previous_bar.close AS previous_close "
                f"FROM {_CONSUMER} AS current_bar LEFT JOIN LATERAL ("
                f"SELECT source_bar.{close_column} AS close FROM {_CONSUMER} AS source_bar "
                "WHERE source_bar.timestamp_m1 < current_bar.timestamp_m1 AND "
                f"{finite.format(source_close, source_close)} "
                "ORDER BY source_bar.timestamp_m1 DESC LIMIT 1) AS previous_bar ON TRUE "
                f"WHERE {finite.format(current_open, current_open)})"
            )
            source_ctes.append(
                f"{gap_metric} AS MATERIALIZED (SELECT timestamp_m1, open, previous_close "
                f"FROM {gap_source})"
            )
            source_ctes.append(
                f"{gap_valid} AS MATERIALIZED (SELECT timestamp_m1, "
                "(open - previous_close) / previous_close AS value, "
                f"row_number() OVER (ORDER BY timestamp_m1) AS valid_number FROM {gap_metric} "
                "WHERE previous_close > 0.0 AND open > '-Infinity'::double precision "
                "AND open < 'Infinity'::double precision)"
            )
            # Non-finite arithmetic results are not valid metrics and therefore do
            # not advance the rolling valid-gap sequence.
            gap_finite = _quote(f"{series}_overnight_finite")
            source_ctes.append(
                f"{gap_finite} AS MATERIALIZED (SELECT timestamp_m1, value, "
                f"row_number() OVER (ORDER BY timestamp_m1) AS valid_number FROM {gap_valid} "
                "WHERE value IS NOT NULL AND value > '-Infinity'::double precision "
                "AND value < 'Infinity'::double precision)"
            )
            source_ctes.append(
                f"{gap_rolling} AS MATERIALIZED (SELECT timestamp_m1, "
                f"CASE WHEN count(*) OVER w = {rolling_window} "
                f"AND avg(value) OVER w > '-Infinity'::double precision "
                f"AND avg(value) OVER w < 'Infinity'::double precision "
                "THEN avg(value) OVER w END AS mean_5 "
                f"FROM {gap_finite} WINDOW w AS (ORDER BY valid_number ROWS BETWEEN "
                f"{rolling_window - 1} PRECEDING AND CURRENT ROW))"
            )
            helper_joins.extend(
                [
                    f"LEFT JOIN {gap_finite} AS overnight_current "
                    "ON overnight_current.timestamp_m1 = base.timestamp_m1",
                    f"LEFT JOIN {gap_rolling} AS overnight_rolling "
                    "ON overnight_rolling.timestamp_m1 = base.timestamp_m1",
                ]
            )
            helper_selects.extend(
                [
                    f"overnight_current.value AS {_quote(f'{series}_overnight_gap_1obs')}",
                    f"overnight_rolling.mean_5 AS {_quote(f'{series}_overnight_gap_5obs')}",
                ]
            )

            # Stochastic and Donchian windows count complete native OHLC bars,
            # not calendar dates or the wider union of serving timestamps.
            ohlc_rows = _quote(f"{series}_ohlc_observations")
            ohlc_windows = _quote(f"{series}_ohlc_windows")
            stochastic_raw = _quote(f"{series}_stochastic_raw")
            stochastic_history = _quote(f"{series}_stochastic_history")
            stochastic_features = _quote(f"{series}_stochastic_features")
            finite_ohlc = " AND ".join(
                f"{_quote(f'{series}_{field}')} IS NOT NULL AND "
                f"{_quote(f'{series}_{field}')} > '-Infinity'::double precision AND "
                f"{_quote(f'{series}_{field}')} < 'Infinity'::double precision"
                for field in OHLC_FIELDS
            )
            source_ctes.append(
                f"{ohlc_rows} AS MATERIALIZED (SELECT timestamp_m1, "
                f"{open_column} AS open, {high_column} AS high, {low_column} AS low, "
                f"{close_column} AS close, row_number() OVER (ORDER BY timestamp_m1) "
                f"AS observation_number FROM {_CONSUMER} WHERE {finite_ohlc})"
            )
            source_ctes.append(
                f"{ohlc_windows} AS MATERIALIZED (SELECT *, "
                "count(*) OVER w5 AS count_5, min(low) OVER w5 AS min_low_5, "
                "max(high) OVER w5 AS max_high_5, "
                "count(*) OVER w14 AS count_14, min(low) OVER w14 AS min_low_14, "
                "max(high) OVER w14 AS max_high_14, "
                "count(*) OVER prior20 AS prior_count_20, "
                "max(high) OVER prior20 AS prior_max_high_20, "
                "min(low) OVER prior20 AS prior_min_low_20 "
                f"FROM {ohlc_rows} WINDOW "
                "w5 AS (ORDER BY observation_number ROWS BETWEEN 4 PRECEDING AND CURRENT ROW), "
                "w14 AS (ORDER BY observation_number ROWS BETWEEN 13 PRECEDING AND CURRENT ROW), "
                "prior20 AS (ORDER BY observation_number ROWS BETWEEN "
                "20 PRECEDING AND 1 PRECEDING))"
            )
            source_ctes.append(
                f"{stochastic_raw} AS MATERIALIZED (SELECT *, "
                "CASE WHEN count_5 = 5 AND max_high_5 > min_low_5 THEN "
                "CASE WHEN 100.0 * (close - min_low_5) / (max_high_5 - min_low_5) "
                "> '-Infinity'::double precision AND "
                "100.0 * (close - min_low_5) / (max_high_5 - min_low_5) "
                "< 'Infinity'::double precision THEN "
                "100.0 * (close - min_low_5) / (max_high_5 - min_low_5) END END "
                "AS stoch_k_5, "
                "CASE WHEN count_14 = 14 AND max_high_14 > min_low_14 THEN "
                "CASE WHEN 100.0 * (close - min_low_14) / (max_high_14 - min_low_14) "
                "> '-Infinity'::double precision AND "
                "100.0 * (close - min_low_14) / (max_high_14 - min_low_14) "
                "< 'Infinity'::double precision THEN "
                "100.0 * (close - min_low_14) / (max_high_14 - min_low_14) END END "
                f"AS stoch_k_14 FROM {ohlc_windows})"
            )
            source_ctes.append(
                f"{stochastic_history} AS MATERIALIZED (SELECT *, "
                "count(stoch_k_5) OVER w3 AS valid_k5_count, "
                "avg(stoch_k_5) OVER w3 AS stoch_d_5_3, "
                "count(stoch_k_14) OVER w3 AS valid_k14_count, "
                "avg(stoch_k_14) OVER w3 AS stoch_d_14_3, "
                "lag(stoch_k_5, 3) OVER ordered AS stoch_k_5_lag_3 "
                f"FROM {stochastic_raw} WINDOW "
                "ordered AS (ORDER BY observation_number), "
                "w3 AS (ORDER BY observation_number ROWS BETWEEN 2 PRECEDING AND CURRENT ROW))"
            )
            source_ctes.append(
                f"{stochastic_features} AS MATERIALIZED (SELECT timestamp_m1, "
                f"stoch_k_5 AS {_quote(f'{series}_stoch_k_5')}, "
                "CASE WHEN valid_k5_count = 3 AND stoch_d_5_3 > '-Infinity'::double precision "
                "AND stoch_d_5_3 < 'Infinity'::double precision THEN stoch_d_5_3 END "
                f"AS {_quote(f'{series}_stoch_d_5_3')}, "
                f"stoch_k_14 AS {_quote(f'{series}_stoch_k_14')}, "
                "CASE WHEN valid_k14_count = 3 AND stoch_d_14_3 > '-Infinity'::double precision "
                "AND stoch_d_14_3 < 'Infinity'::double precision THEN stoch_d_14_3 END "
                f"AS {_quote(f'{series}_stoch_d_14_3')}, "
                "CASE WHEN valid_k5_count = 3 AND stoch_k_5 - stoch_d_5_3 "
                "> '-Infinity'::double precision AND stoch_k_5 - stoch_d_5_3 "
                "< 'Infinity'::double precision THEN stoch_k_5 - stoch_d_5_3 END "
                f"AS {_quote(f'{series}_stoch_kd_spread_5')}, "
                f"CASE WHEN stoch_k_5 IS NOT NULL AND stoch_k_5_lag_3 IS NOT NULL "
                "THEN CASE WHEN (stoch_k_5 - stoch_k_5_lag_3) / 3.0 "
                "> '-Infinity'::double precision AND "
                "(stoch_k_5 - stoch_k_5_lag_3) / 3.0 < 'Infinity'::double precision "
                "THEN (stoch_k_5 - stoch_k_5_lag_3) / 3.0 END END "
                f"AS {_quote(f'{series}_stoch_k_slope_3')}, "
                f"CASE WHEN prior_count_20 = 20 THEN high > prior_max_high_20 END "
                f"AS {_quote(f'{series}_donchian_breakout_up')}, "
                f"CASE WHEN prior_count_20 = 20 THEN low < prior_min_low_20 END "
                f"AS {_quote(f'{series}_donchian_breakout_down')} FROM {stochastic_history})"
            )
            helper_joins.append(
                f"LEFT JOIN {stochastic_features} AS stochastic_features "
                "ON stochastic_features.timestamp_m1 = base.timestamp_m1"
            )
            helper_selects.extend(
                f"stochastic_features.{_quote(f'{series}_{suffix}')} "
                f"AS {_quote(f'{series}_{suffix}')}"
                for suffix in (
                    "stoch_k_5",
                    "stoch_d_5_3",
                    "stoch_k_14",
                    "stoch_d_14_3",
                    "stoch_kd_spread_5",
                    "stoch_k_slope_3",
                    "donchian_breakout_up",
                    "donchian_breakout_down",
                )
            )
        if series in PRICE_FEATURE_SERIES:
            rsi5_valid = _quote(f"{series}_rsi5_observations")
            rsi5_history = _quote(f"{series}_rsi5_history")
            slope_name = _quote(f"{series}_rsi5_slope_3")
            delta_name = _quote(f"{series}_rsi5_delta_1")
            source_ctes.append(
                f"{rsi5_valid} AS MATERIALIZED (SELECT timestamp_m1, "
                f"{_quote(f'{series}_rsi_5obs')} AS rsi5 FROM {feature_base})"
            )
            source_ctes.append(
                f"{rsi5_history} AS MATERIALIZED (SELECT timestamp_m1, "
                f"CASE WHEN rsi5 IS NOT NULL AND lag(rsi5, 3) OVER ordered IS NOT NULL "
                f"THEN (rsi5 - lag(rsi5, 3) OVER ordered) / 3.0 END AS slope_3, "
                f"CASE WHEN rsi5 IS NOT NULL AND lag(rsi5, 1) OVER ordered IS NOT NULL "
                f"THEN rsi5 - lag(rsi5, 1) OVER ordered END AS delta_1 "
                f"FROM {rsi5_valid} WINDOW ordered AS (ORDER BY timestamp_m1))"
            )
            helper_joins.append(
                f"LEFT JOIN {rsi5_history} AS rsi5_history "
                "ON rsi5_history.timestamp_m1 = base.timestamp_m1"
            )
            helper_selects.extend(
                [
                    f"rsi5_history.slope_3 AS {slope_name}",
                    f"rsi5_history.delta_1 AS {delta_name}",
                ]
            )

            for period in MACD_PRICE_EMA_PERIODS:
                source_ctes.append(_ema_state_cte(series, period))
            macd_input = _quote(f"{series}_macd_input")
            ema12_state = _quote(f"{series}_ema_12_state")
            ema26_state = _quote(f"{series}_ema_26_state")
            source_ctes.append(
                f"{macd_input} AS MATERIALIZED (SELECT raw.timestamp_m1, "
                f"CASE WHEN ema12.ema IS NOT NULL AND ema26.ema IS NOT NULL "
                f"THEN ema12.ema - ema26.ema END AS macd_line "
                f"FROM {_CONSUMER} AS raw "
                f"LEFT JOIN {ema12_state} AS ema12 ON ema12.timestamp_m1 = raw.timestamp_m1 "
                f"LEFT JOIN {ema26_state} AS ema26 ON ema26.timestamp_m1 = raw.timestamp_m1)"
            )
            signal_state_name = f"{series}_macd_signal_ema_state"
            source_ctes.append(
                _ema_state_cte(
                    series,
                    MACD_SIGNAL_EMA_PERIOD,
                    source_relation=macd_input,
                    value_column="macd_line",
                    state_name=signal_state_name,
                    allow_nonpositive=True,
                )
            )
            macd_base = _quote(f"{series}_macd_base")
            macd_relative = _quote(f"{series}_macd_relative")
            macd_history = _quote(f"{series}_macd_history")
            macd_features = _quote(f"{series}_macd_features")
            signal_state = _quote(signal_state_name)
            source_ctes.append(
                f"{macd_base} AS MATERIALIZED (SELECT raw.timestamp_m1, "
                f"raw.{_quote(f'{series}_level')} AS level, "
                f"macd_input.macd_line, signal.ema AS macd_signal "
                f"FROM {_CONSUMER} AS raw JOIN {macd_input} AS macd_input "
                f"ON macd_input.timestamp_m1 = raw.timestamp_m1 "
                f"LEFT JOIN {signal_state} AS signal ON signal.timestamp_m1 = raw.timestamp_m1)"
            )
            source_ctes.append(
                f"{macd_relative} AS MATERIALIZED (SELECT timestamp_m1, level, macd_line, "
                f"macd_signal, CASE WHEN level > 0 AND macd_line IS NOT NULL "
                f"THEN macd_line / level END AS macd_line_relative, "
                f"CASE WHEN level > 0 AND macd_line IS NOT NULL AND macd_signal IS NOT NULL "
                f"THEN (macd_line - macd_signal) / level END AS macd_hist_relative "
                f"FROM {macd_base})"
            )
            source_ctes.append(
                f"{macd_history} AS MATERIALIZED (SELECT *, "
                f"lag(macd_hist_relative, 3) OVER ordered AS macd_hist_lag_3 "
                f"FROM {macd_relative} WINDOW ordered AS (ORDER BY timestamp_m1))"
            )
            source_ctes.append(
                f"{macd_features} AS MATERIALIZED (SELECT timestamp_m1, "
                f"macd_line_relative AS {_quote(f'{series}_macd_line_relative')}, "
                f"macd_hist_relative AS {_quote(f'{series}_macd_hist_relative')}, "
                f"CASE WHEN macd_hist_relative IS NOT NULL AND macd_hist_lag_3 IS NOT NULL "
                f"THEN macd_hist_relative - macd_hist_lag_3 END "
                f"AS {_quote(f'{series}_macd_hist_delta_3')} FROM {macd_history})"
            )
            helper_joins.append(
                f"LEFT JOIN {macd_features} AS macd_features "
                "ON macd_features.timestamp_m1 = base.timestamp_m1"
            )
            helper_selects.extend(
                f"macd_features.{_quote(f'{series}_{suffix}')} AS {_quote(f'{series}_{suffix}')}"
                for suffix in MACD_OUTPUT_SUFFIXES
            )

            level_valid = _quote(f"{series}_level_valid")
            level_zscore = _quote(f"{series}_level_zscore")
            level_window = LEVEL_ZSCORE_WINDOW
            source_ctes.append(
                f"{level_valid} AS MATERIALIZED (SELECT timestamp_m1, level, "
                f"row_number() OVER (ORDER BY timestamp_m1) AS valid_number "
                f"FROM {source} WHERE level > 0)"
            )
            source_ctes.append(
                f"{level_zscore} AS MATERIALIZED (SELECT timestamp_m1, "
                f"CASE WHEN count(*) OVER z_window = {level_window} "
                f"AND stddev_samp(level) OVER z_window <> 0 "
                f"THEN (level - avg(level) OVER z_window) / stddev_samp(level) OVER z_window "
                f"END AS zscore FROM {level_valid} WINDOW z_window AS (ORDER BY valid_number "
                f"ROWS BETWEEN {level_window - 1} PRECEDING AND CURRENT ROW))"
            )
            level_zscore_column = _quote(f"{series}_level_zscore_{level_window}")
            helper_joins.append(
                f"LEFT JOIN {level_zscore} AS level_z ON level_z.timestamp_m1 = base.timestamp_m1"
            )
            helper_selects.append(f"level_z.zscore AS {level_zscore_column}")

            volatility_valid = _quote(f"{series}_volatility8_valid")
            volatility_zscore = _quote(f"{series}_volatility8_zscore")
            volatility_column = _quote(f"{series}_volatility_8obs")
            zscore_window = VOLATILITY_ZSCORE_WINDOW
            source_ctes.append(
                f"{volatility_valid} AS MATERIALIZED (SELECT timestamp_m1, volatility_value, "
                f"row_number() OVER (ORDER BY timestamp_m1) AS valid_number FROM ("
                f"SELECT timestamp_m1, {volatility_column} AS volatility_value "
                f"FROM {feature_base} WHERE {volatility_column} IS NOT NULL) valid_volatility)"
            )
            source_ctes.append(
                f"{volatility_zscore} AS MATERIALIZED (SELECT timestamp_m1, "
                f"CASE WHEN count(*) OVER z_window = {zscore_window} "
                f"AND stddev_samp(volatility_value) OVER z_window <> 0 "
                f"THEN (volatility_value - avg(volatility_value) OVER z_window) "
                f"/ stddev_samp(volatility_value) OVER z_window END AS zscore "
                f"FROM {volatility_valid} WINDOW z_window AS (ORDER BY valid_number "
                f"ROWS BETWEEN {zscore_window - 1} PRECEDING AND CURRENT ROW))"
            )
            volatility_zscore_column = _quote(f"{series}_volatility_zscore_{zscore_window}")
            helper_joins.append(
                f"LEFT JOIN {volatility_zscore} AS volatility_z "
                "ON volatility_z.timestamp_m1 = base.timestamp_m1"
            )
            helper_selects.append(f"volatility_z.zscore AS {volatility_zscore_column}")

            for lag, pair_window in RETURN_AUTOCORRELATION_LAG_WINDOWS:
                pairs = _quote(f"{series}_valid_return_pairs_{lag}")
                autocorrelation = _quote(f"{series}_return_autocorr_{lag}")
                pair_column = _quote(f"{series}_momentum_autocorr_lag{lag}_window{pair_window}")
                source_ctes.append(
                    f"{pairs} AS MATERIALIZED (SELECT timestamp_m1, current_return, "
                    f"lagged_return, row_number() OVER (ORDER BY timestamp_m1) AS pair_number "
                    f"FROM (SELECT timestamp_m1, {_quote('log_return_1')} AS current_return, "
                    f"lag({_quote('log_return_1')}, {lag}) OVER (ORDER BY timestamp_m1) "
                    f"AS lagged_return FROM {changes}) aligned_returns "
                    f"WHERE current_return IS NOT NULL AND lagged_return IS NOT NULL)"
                )
                source_ctes.append(
                    f"{autocorrelation} AS MATERIALIZED (SELECT timestamp_m1, "
                    f"CASE WHEN count(*) OVER pair_window = {pair_window} "
                    f"AND stddev_samp(current_return) OVER pair_window <> 0 "
                    f"AND stddev_samp(lagged_return) OVER pair_window <> 0 "
                    f"THEN corr(current_return, lagged_return) OVER pair_window END AS value "
                    f"FROM {pairs} "
                    f"WINDOW pair_window AS (ORDER BY pair_number ROWS BETWEEN "
                    f"{pair_window - 1} PRECEDING AND CURRENT ROW))"
                )
                helper_joins.append(
                    f"LEFT JOIN {autocorrelation} AS return_ac_{lag} "
                    "ON return_ac_" + str(lag) + ".timestamp_m1 = base.timestamp_m1"
                )
                helper_selects.append(f"return_ac_{lag}.value AS {pair_column}")

            for period in EMA_PERIODS:
                source_ctes.append(_ema_state_cte(series, period))
            ema_aligned = _quote(f"{series}_ema_aligned")
            ema_history = _quote(f"{series}_ema_history")
            ema_features = _quote(f"{series}_ema_features")
            ema5_state = _quote(f"{series}_ema_5_state")
            ema9_state = _quote(f"{series}_ema_9_state")
            ema21_state = _quote(f"{series}_ema_21_state")
            source_ctes.append(
                f"{ema_aligned} AS MATERIALIZED (SELECT raw.timestamp_m1, "
                f"raw.{_quote(f'{series}_level')} AS level, "
                f"ema5.ema AS ema5, ema9.ema AS ema9, ema21.ema AS ema21 "
                f"FROM {_CONSUMER} AS raw "
                f"LEFT JOIN {ema5_state} AS ema5 ON ema5.timestamp_m1 = raw.timestamp_m1 "
                f"LEFT JOIN {ema9_state} AS ema9 ON ema9.timestamp_m1 = raw.timestamp_m1 "
                f"LEFT JOIN {ema21_state} AS ema21 ON ema21.timestamp_m1 = raw.timestamp_m1)"
            )
            source_ctes.append(
                f"{ema_history} AS MATERIALIZED (SELECT *, "
                "lag(ema5, 1) OVER ordered AS previous_ema5, "
                "lag(ema9, 1) OVER ordered AS previous_ema9, "
                "lag(ema9, 3) OVER ordered AS ema9_lag_3 "
                f"FROM {ema_aligned} WINDOW ordered AS (ORDER BY timestamp_m1))"
            )
            source_ctes.append(
                f"{ema_features} AS MATERIALIZED (SELECT timestamp_m1, "
                f"CASE WHEN level > 0 AND ema5 IS NOT NULL "
                f"THEN level / NULLIF(ema5, 0.0) - 1.0 END AS "
                f"{_quote(f'{series}_ema5_distance')}, "
                f"CASE WHEN level > 0 AND ema9 IS NOT NULL "
                f"THEN level / NULLIF(ema9, 0.0) - 1.0 END AS "
                f"{_quote(f'{series}_ema9_distance')}, "
                f"CASE WHEN level > 0 AND ema21 IS NOT NULL "
                f"THEN level / NULLIF(ema21, 0.0) - 1.0 END AS "
                f"{_quote(f'{series}_ema21_distance')}, "
                f"CASE WHEN level > 0 AND ema5 IS NOT NULL AND ema21 IS NOT NULL "
                f"THEN (ema5 - ema21) / NULLIF(level, 0.0) END AS "
                f"{_quote(f'{series}_ema5_ema21_spread')}, "
                f"CASE WHEN level > 0 AND ema9 IS NOT NULL AND ema9_lag_3 IS NOT NULL "
                f"THEN (ema9 - ema9_lag_3) / (3.0 * level) END AS "
                f"{_quote(f'{series}_ema9_slope_3')}, "
                f"CASE WHEN level > 0 AND ema5 IS NOT NULL AND ema9 IS NOT NULL "
                f"AND previous_ema5 IS NOT NULL AND previous_ema9 IS NOT NULL THEN "
                "CASE WHEN ema5 > ema9 AND previous_ema5 <= previous_ema9 "
                "THEN 1.0::double precision "
                "WHEN ema5 < ema9 AND previous_ema5 >= previous_ema9 "
                "THEN -1.0::double precision "
                "ELSE 0.0::double precision END END AS "
                f"{_quote(f'{series}_ema5_ema9_cross')} FROM {ema_history})"
            )
            helper_joins.append(
                f"LEFT JOIN {ema_features} AS ema_features "
                "ON ema_features.timestamp_m1 = base.timestamp_m1"
            )
            helper_selects.extend(
                f"ema_features.{_quote(f'{series}_{suffix}')} AS {_quote(f'{series}_{suffix}')}"
                for suffix in EMA_OUTPUT_SUFFIXES
            )

        feature_extra = f", {', '.join(helper_selects)}" if helper_selects else ""
        source_ctes.append(
            f"{features} AS MATERIALIZED (SELECT base.*{feature_extra} FROM {feature_base} AS base "
            f"{' '.join(helper_joins)})"
        )
        joins.append(f"LEFT JOIN {features} ON {features}.timestamp_m1 = raw.timestamp_m1")
        for column in _FEATURES_VIEW_COLUMNS:
            if (
                column.startswith(f"{series}_delta_")
                or column.startswith(f"{series}_zscore_")
                or column.startswith(f"{series}_momentum_")
                or column.startswith(f"{series}_log_return_")
                or column.startswith(f"{series}_return_")
                or column.startswith(f"{series}_volatility_")
                or column.startswith(f"{series}_sma_ratio_")
                or column.startswith(f"{series}_rsi_")
                or column.startswith(f"{series}_rsi5_")
                or column.startswith(f"{series}_roc_acceleration_")
                or column.startswith(f"{series}_volatility_zscore_")
                or column.startswith(f"{series}_level_zscore_")
                or column.startswith(f"{series}_roc_")
                or column.startswith(f"{series}_drawdown_")
                or column.startswith(f"{series}_ema")
                or column.startswith(f"{series}_macd_")
                or column.startswith(f"{series}_high_low_range_")
                or column.startswith(f"{series}_intraday_return_")
                or column.startswith(f"{series}_overnight_gap_")
                or column.startswith(f"{series}_stoch_")
                or column.startswith(f"{series}_donchian_")
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
    feature_expressions.update(
        {
            f"{series}_{field}": (
                f"raw.{_quote(f'{series}_{field}')} AS {_quote(f'{series}_{field}')}"
            )
            for series in OHLC_SERIES
            for field in OHLC_FIELDS
        }
    )
    ordered_features = [
        feature_expressions.get(column, cross_expressions.get(column, ""))
        for column in _FEATURES_VIEW_COLUMNS
        if column not in {f"{series}_log_level" for series in PRICE_FEATURE_SERIES}
    ]
    if any(not expression for expression in ordered_features):
        raise ValueError("macro feature catalog contains an expression without SQL projection")
    raw_select = ", ".join(
        f"CASE WHEN raw.{_quote(f'{series}_level')} > 0 "
        f"THEN ln(raw.{_quote(f'{series}_level')}) END AS {_quote(f'{series}_log_level')}"
        for series in PRICE_FEATURE_SERIES
    )
    return (
        f"CREATE MATERIALIZED VIEW IF NOT EXISTS {_FEATURES_VIEW} AS WITH "
        f"{', '.join(source_ctes)} "
        f'SELECT raw."timestamp_m1", '
        'raw."available_at_utc" AS "available_at_utc", '
        f"{raw_select}, {', '.join(ordered_features)} "
        f"FROM {_CONSUMER} raw {' '.join(joins)}"
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
    f"GRANT SELECT ON TABLE {_MIGRATION_LEDGER} TO {_quote(POSTGRES_SYNC_USER)}",
    f"REVOKE ALL ON {_FEATURES_VIEW} FROM PUBLIC",
    f"GRANT SELECT ON {_FEATURES_VIEW} TO {_quote(POSTGRES_USER)}, {_quote(POSTGRES_SYNC_USER)}",
    "GRANT EXECUTE ON FUNCTION macro_loader.refresh_macro_features_explicit() "
    f"TO {_quote(POSTGRES_SYNC_USER)}",
)

_RETIRED_FED_ARCHIVE_MIGRATION = (
    f"CREATE SCHEMA IF NOT EXISTS {_quote(_RETIRED_ARCHIVE_SCHEMA)}",
    f"ALTER SCHEMA {_quote(_RETIRED_ARCHIVE_SCHEMA)} OWNER TO {_quote(_POSTGRES_OWNER_ROLE)}",
    f"REVOKE ALL ON SCHEMA {_quote(_RETIRED_ARCHIVE_SCHEMA)} FROM PUBLIC, "
    f"{_quote(POSTGRES_USER)}, {_quote(POSTGRES_SYNC_USER)}",
    f"""CREATE TABLE IF NOT EXISTS {_RETIRED_RAW_ARCHIVE} (
        timestamp_m1 TIMESTAMPTZ(6) NOT NULL PRIMARY KEY,
        available_at_utc TIMESTAMPTZ(6) NOT NULL,
        {_RETIRED_FED_ORIGIN_COLUMN_DEFS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS {_RETIRED_FEATURE_ARCHIVE} (
        timestamp_m1 TIMESTAMPTZ(6) NOT NULL PRIMARY KEY,
        available_at_utc TIMESTAMPTZ(6) NOT NULL,
        {_RETIRED_FED_VIEW_COLUMN_DEFS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS {_RETIRED_POLICY_ARCHIVE} (
        timestamp_m1 TIMESTAMPTZ(6) NOT NULL PRIMARY KEY,
        {_RETIRED_FED_ORIGIN_COLUMN_DEFS},
        available_at_utc TIMESTAMPTZ(6) NULL
    )""",
    f"""CREATE TABLE IF NOT EXISTS {_RETIRED_STATE_ARCHIVE} (
        dataset_id TEXT PRIMARY KEY,
        source_build_id TEXT NOT NULL,
        data_sha256 CHAR(64) NOT NULL,
        schema_version INTEGER NOT NULL,
        feature_version INTEGER NOT NULL,
        row_count BIGINT NOT NULL,
        min_timestamp TIMESTAMPTZ(6) NULL,
        max_timestamp TIMESTAMPTZ(6) NULL,
        synced_at_utc TIMESTAMPTZ(6) NOT NULL
    )""",
    f"""CREATE TABLE IF NOT EXISTS {_RETIRED_HASH_ARCHIVE} (
        dataset_id TEXT NOT NULL,
        timestamp_m1 TIMESTAMPTZ(6) NOT NULL,
        row_sha256 CHAR(64) NOT NULL,
        PRIMARY KEY (dataset_id, timestamp_m1)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {_RETIRED_LEGACY_ARCHIVE} (
        timestamp_m1 TIMESTAMPTZ(6) NOT NULL PRIMARY KEY,
        {_RETIRED_FED_LEGACY_COLUMN_DEFS}
    )""",
    *(
        f"ALTER TABLE {_quote(_RETIRED_ARCHIVE_SCHEMA)}.{_quote(table)} "
        f"OWNER TO {_quote(_POSTGRES_OWNER_ROLE)}"
        for table in (
            "fed_macro_raw",
            "fed_macro_features",
            "fed_policy_features_daily",
            "fed_policy_sync_state",
            "fed_policy_row_hashes",
            "fed_legacy_raw_columns",
        )
    ),
    f"""DO $$
    BEGIN
        IF (SELECT count(*) FROM information_schema.columns
            WHERE table_schema = '{POSTGRES_CONSUMER_SCHEMA}'
              AND table_name = '{POSTGRES_CONSUMER_TABLE}'
              AND column_name IN (
                  {_RETIRED_FED_ORIGIN_INFO_NAMES}, 'available_at_utc', 'timestamp_m1'
              ))
           = {len(_RETIRED_FED_ORIGIN_COLUMNS) + 2} THEN
            EXECUTE 'INSERT INTO {_RETIRED_RAW_ARCHIVE} '
                '(timestamp_m1, available_at_utc, {_RETIRED_FED_ORIGIN_COLUMN_NAMES}) '
                'SELECT timestamp_m1, available_at_utc, {_RETIRED_FED_ORIGIN_COLUMN_NAMES} '
                'FROM {_CONSUMER} ON CONFLICT (timestamp_m1) DO UPDATE SET '
                'available_at_utc = EXCLUDED.available_at_utc, {_RETIRED_FED_RAW_UPDATES}';
        END IF;
    END
    $$""",
    f"""DO $$
    BEGIN
        IF to_regclass('{POSTGRES_SYNC_SCHEMA}.fed_policy_features_daily') IS NOT NULL
           AND (SELECT count(*) FROM information_schema.columns
                WHERE table_schema = '{POSTGRES_SYNC_SCHEMA}'
                  AND table_name = 'fed_policy_features_daily'
                  AND column_name IN ({_RETIRED_FED_ORIGIN_INFO_NAMES}, 'timestamp_m1'))
               = {len(_RETIRED_FED_ORIGIN_COLUMNS) + 1} THEN
            EXECUTE 'INSERT INTO {_RETIRED_POLICY_ARCHIVE} AS retired '
                '(timestamp_m1, {_RETIRED_FED_ORIGIN_COLUMN_NAMES}, available_at_utc) '
                'SELECT timestamp_m1, {_RETIRED_FED_ORIGIN_COLUMN_NAMES}, ' ||
                CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_schema = '{POSTGRES_SYNC_SCHEMA}'
                                    AND table_name = 'fed_policy_features_daily'
                                    AND column_name = 'available_at_utc')
                     THEN 'available_at_utc' ELSE 'NULL::timestamptz' END ||
                ' FROM "{POSTGRES_SYNC_SCHEMA}"."fed_policy_features_daily" '
                'ON CONFLICT (timestamp_m1) DO UPDATE SET '
                'available_at_utc = COALESCE(EXCLUDED.available_at_utc, '
                'retired.available_at_utc), ' ||
                '{_RETIRED_FED_RAW_UPDATES}';
        END IF;
    END
    $$""",
    f"""DO $$
    BEGIN
        IF (SELECT count(*) FROM pg_attribute
            WHERE attrelid = to_regclass('{POSTGRES_CONSUMER_SCHEMA}.macro_features')
              AND attnum > 0 AND NOT attisdropped
              AND attname IN ({_RETIRED_FED_VIEW_INFO_NAMES}, 'available_at_utc', 'timestamp_m1'))
           = {len(_RETIRED_FED_VIEW_COLUMNS) + 2} THEN
            EXECUTE 'INSERT INTO {_RETIRED_FEATURE_ARCHIVE} '
                '(timestamp_m1, available_at_utc, {_RETIRED_FED_VIEW_COLUMN_NAMES}) '
                'SELECT timestamp_m1, available_at_utc, {_RETIRED_FED_VIEW_COLUMN_NAMES} '
                'FROM "{POSTGRES_CONSUMER_SCHEMA}"."macro_features" '
                'ON CONFLICT (timestamp_m1) DO UPDATE SET '
                'available_at_utc = EXCLUDED.available_at_utc, {_RETIRED_FED_VIEW_UPDATES}';
        END IF;
    END
    $$""",
    f"""INSERT INTO {_RETIRED_STATE_ARCHIVE}
        SELECT * FROM {_SYNC_STATE} WHERE dataset_id = '{_RETIRED_FED_DATASET_ID}'
        ON CONFLICT (dataset_id) DO UPDATE SET
            source_build_id = EXCLUDED.source_build_id,
            data_sha256 = EXCLUDED.data_sha256,
            schema_version = EXCLUDED.schema_version,
            feature_version = EXCLUDED.feature_version,
            row_count = EXCLUDED.row_count,
            min_timestamp = EXCLUDED.min_timestamp,
            max_timestamp = EXCLUDED.max_timestamp,
            synced_at_utc = EXCLUDED.synced_at_utc""",
    f"""INSERT INTO {_RETIRED_HASH_ARCHIVE}
        SELECT * FROM {_ROW_HASHES} WHERE dataset_id = '{_RETIRED_FED_DATASET_ID}'
        ON CONFLICT (dataset_id, timestamp_m1) DO UPDATE SET row_sha256 = EXCLUDED.row_sha256""",
    f"REVOKE ALL ON ALL TABLES IN SCHEMA {_quote(_RETIRED_ARCHIVE_SCHEMA)} FROM PUBLIC, "
    f"{_quote(POSTGRES_USER)}, {_quote(POSTGRES_SYNC_USER)}",
    f"DELETE FROM {_SYNC_STATE} WHERE dataset_id = '{_RETIRED_FED_DATASET_ID}'",
    f"DELETE FROM {_ROW_HASHES} WHERE dataset_id = '{_RETIRED_FED_DATASET_ID}'",
    f'DROP TABLE IF EXISTS {_quote(POSTGRES_SYNC_SCHEMA)}."fed_policy_features_daily"',
    f"DROP MATERIALIZED VIEW IF EXISTS {_FEATURES_VIEW} CASCADE",
    f"ALTER TABLE {_CONSUMER} "
    + ", ".join(
        f"DROP COLUMN IF EXISTS {_quote(column)}" for column in _RETIRED_FED_ORIGIN_COLUMNS
    ),
    _FEATURES_VIEW_DDL,
    f"COMMENT ON MATERIALIZED VIEW {_FEATURES_VIEW} IS "
    f"'macro feature view version={MACRO_FEATURE_VIEW_VERSION}; "
    f"fingerprint={MACRO_FEATURE_VIEW_FINGERPRINT}'",
    f"ALTER MATERIALIZED VIEW {_FEATURES_VIEW} OWNER TO {_quote(_POSTGRES_OWNER_ROLE)}",
    f"GRANT SELECT ON {_FEATURES_VIEW} TO {_quote(POSTGRES_USER)}, {_quote(POSTGRES_SYNC_USER)}",
)


def _normalize_view_definition(value: str) -> str:
    """Normalize PostgreSQL's pretty-printed view definition for contract checks."""
    return " ".join(value.strip().rstrip(";").split()).lower()


def _assert_generated_availability(cursor: CursorPort) -> None:
    cursor.execute(
        _GENERATED_AVAILABILITY_SQL,
        (POSTGRES_CONSUMER_SCHEMA, POSTGRES_CONSUMER_TABLE),
    )
    row = cursor.fetchone()
    if row is None or len(row) != 2 or row[0] != "ALWAYS" or not isinstance(row[1], str):
        raise ValueError("PostgreSQL available_at_utc is not a generated column")
    expression = " ".join(row[1].lower().replace('"', "").split())
    timezone_expression_present = "timezone(" in expression or "at time zone" in expression
    if not timezone_expression_present or any(
        marker not in expression for marker in _AVAILABILITY_EXPRESSION_MARKERS
    ):
        raise ValueError("PostgreSQL available_at_utc generation expression differs")


_FEATURES_VIEW_DEFINITION = _normalize_view_definition(_FEATURES_VIEW_DDL.split(" AS ", 1)[1])
_FEATURES_VIEW_COLUMNS_EXPECTED = (
    "timestamp_m1",
    "available_at_utc",
    *_FEATURES_VIEW_COLUMNS,
)
_FEATURES_VIEW_COLUMN_TYPES_EXPECTED = {
    "timestamp_m1": "timestamp(6) with time zone",
    "available_at_utc": "timestamp(6) with time zone",
    **FEATURE_POSTGRES_TYPES,
}
_FEATURES_VIEW_DEFINITION_MARKERS = (
    "with vix_source as",
    "raw.timestamp_m1",
    "raw.available_at_utc",
    "vix9d_vix3m_log_ratio",
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
            for column in POSTGRES_LEVEL_COLUMNS
            if column not in _ADDED_LEVEL_COLUMNS
        )
        + (PostgresColumnSpecification("available_at_utc", "timestamp with time zone", 6, False),)
        + tuple(
            PostgresColumnSpecification(column, "double precision", None, True)
            for column in POSTGRES_OHLC_COLUMNS
        )
        + tuple(
            PostgresColumnSpecification(column, "double precision", None, True)
            for column in _ADDED_LEVEL_COLUMNS
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
    (_CONSUMER_DDL,),
    (_SYNC_STATE_DDL,),
    (_ROW_HASH_DDL,),
    (_MOMENTUM_COLUMN_MIGRATION,),
    (_RETURN_COLUMN_MIGRATION,),
    ("SELECT 1",),
    _FED_POLICY_RENAME_MIGRATION,
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
    ("SELECT 1",),
    # Older layout migrations could recreate macro_raw before the Fed columns existed.
    ("SELECT 1",),
    _FEATURES_VIEW_REBUILD_MIGRATION,
    # Add deterministic serving availability and rebuild the public projection.
    _AVAILABLE_AT_COLUMN_MIGRATION + _FEATURES_VIEW_REBUILD_MIGRATION,
    # Archive legacy Fed data under a schema absent from runtime grants, then
    # remove it from the active raw table and materialized serving view.
    _RETIRED_FED_ARCHIVE_MIGRATION,
    # Rebuild under a new migration version so already-retired installations receive
    # the extended scalar-level feature catalog without rewriting historical migrations.
    _FEATURES_VIEW_REBUILD_MIGRATION,
    # Publish PR-171's level-only composite and statistical feature contract.
    _FEATURES_VIEW_REBUILD_MIGRATION,
    # Publish PR-172's private EMA recurrence and six EMA trend outputs.
    _FEATURES_VIEW_REBUILD_MIGRATION,
    # Publish PR-173's private signed MACD signal EMA and relative outputs.
    _FEATURES_VIEW_REBUILD_MIGRATION,
    # Preserve native OHLC fields without dropping or rewriting existing raw rows.
    _OHLC_COLUMN_MIGRATION + _FEATURES_VIEW_REBUILD_MIGRATION,
    # Publish PR-175's OHLC bar range, intraday return, and overnight gap features.
    _FEATURES_VIEW_REBUILD_MIGRATION,
    # Publish OHLC stochastic oscillators and nullable Donchian breakout flags.
    _FEATURES_VIEW_REBUILD_MIGRATION,
    # Add newly registered STOXX term-structure and CISS contribution source columns.
    _ADDED_LEVEL_COLUMN_MIGRATION + _FEATURES_VIEW_REBUILD_MIGRATION,
    # Add registered Euro-HY source columns without rewriting migration 28.
    _EURO_HY_LEVEL_COLUMN_MIGRATION + _FEATURES_VIEW_REBUILD_MIGRATION,
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
_GENERATED_AVAILABILITY_SQL = """SELECT is_generated, generation_expression
FROM information_schema.columns
WHERE table_schema = %s AND table_name = %s AND column_name = 'available_at_utc'"""
_AVAILABILITY_EXPRESSION_MARKERS = (
    "timestamp_m1",
    "extract(isodow",
    "when 5 then 3",
    "when 6 then 2",
    "when 7 then 1",
    "else 1",
)
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
    f"INSERT INTO {_CONSUMER} ("
    f"{', '.join(_quote(column) for column in POSTGRES_RAW_COLUMNS)}) "
    f"VALUES ({', '.join('%s' for _ in POSTGRES_RAW_COLUMNS)}) "
    f'ON CONFLICT ("timestamp_m1") DO UPDATE SET '
    + ", ".join(f"{_quote(column)} = EXCLUDED.{_quote(column)}" for column in _RAW_FEATURE_COLUMNS)
)
_UPDATE_ROW_SQL = (
    f"UPDATE {_CONSUMER} SET "
    + ", ".join(f"{_quote(column)} = %s" for column in _RAW_FEATURE_COLUMNS)
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
    f"FROM {_CONSUMER} WHERE ("
    + " OR ".join(f"{_quote(column)} IS NOT NULL" for column in _RAW_FEATURE_COLUMNS)
    + f") ORDER BY {_quote('timestamp_m1')}"
)
_TARGET_SUMMARY_SQL = (
    f"SELECT COUNT(*), MIN({_quote('timestamp_m1')}), MAX({_quote('timestamp_m1')}) "
    f"FROM {_CONSUMER} WHERE ("
    + " OR ".join(f"{_quote(column)} IS NOT NULL" for column in _RAW_FEATURE_COLUMNS)
    + ")"
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


def _is_features_view_publication_statement(statement: str) -> bool:
    """Identify migration SQL that publishes or alters the serving materialized view."""
    return statement.startswith(
        (
            f"CREATE MATERIALIZED VIEW IF NOT EXISTS {_FEATURES_VIEW}",
            f"COMMENT ON MATERIALIZED VIEW {_FEATURES_VIEW}",
            f"ALTER MATERIALIZED VIEW {_FEATURES_VIEW}",
            f"GRANT SELECT ON {_FEATURES_VIEW}",
            f"REVOKE ALL ON {_FEATURES_VIEW}",
            f"DROP MATERIALIZED VIEW IF EXISTS {_FEATURES_VIEW}",
        )
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
    return (
        row.timestamp_m1,
        *row.values,
    )


def _row_update_params(row: GoldRowPayload) -> tuple[object, ...]:
    return (
        *row.values,
        row.timestamp_m1,
    )


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

        _assert_generated_availability(cursor)

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
            expected_type = _FEATURES_VIEW_COLUMN_TYPES_EXPECTED[str(actual_names[ordinal - 1])]
            if column[2] != expected_type:
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
                cursor.execute(
                    "SELECT to_regclass(%s), to_regclass(%s)",
                    (_CONSUMER, _FEATURES_VIEW),
                )
                existing_market_objects = cursor.fetchone()
                if existing_market_objects is None or len(existing_market_objects) != 2:
                    raise ValueError("PostgreSQL fresh-schema probe returned unexpected shape")
                fresh_install = not applied_versions and existing_market_objects == (None, None)
                expected_versions = tuple(range(1, len(_MIGRATIONS) + 1))
                if any(version not in expected_versions for version in applied_versions):
                    raise ValueError(
                        "PostgreSQL schema migration ledger contains an unknown version"
                    )
                for version, statements in enumerate(_MIGRATIONS, start=1):
                    if version in applied_versions:
                        continue
                    for statement in statements:
                        if (
                            fresh_install
                            and version < len(_MIGRATIONS)
                            and _is_features_view_publication_statement(statement)
                        ):
                            continue
                        cursor.execute(statement)
                    cursor.execute(
                        f"INSERT INTO {_MIGRATION_LEDGER} (version, applied_at_utc) "
                        "VALUES (%s, CURRENT_TIMESTAMP)",
                        (version,),
                    )
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

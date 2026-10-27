"""Independent disposable-PostgreSQL schema and formula QA for PR-173."""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

import ingestion.postgres_gold_repository as postgres_module
from application.gold_frame import GOLD_COLUMNS
from application.macro_feature_catalog import (
    MACRO_FEATURE_VIEW_FINGERPRINT,
    MACRO_FEATURE_VIEW_VERSION,
)
from application.postgres_sync import POSTGRES_RAW_TABLES
from ingestion.postgres_gold_repository import PostgresAdminConfig, PostgresGoldSchemaMigrator

_QA_PRICE_SERIES = (
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
    "usd_broad",
)
_QA_OUTPUT_SUFFIXES = (
    "macd_line_relative",
    "macd_hist_relative",
    "macd_hist_delta_3",
)
_QA_OUTPUT_COLUMNS = tuple(
    f"{series}_{suffix}" for series in _QA_PRICE_SERIES for suffix in _QA_OUTPUT_SUFFIXES
)
_TEST_DATABASE = "macro_loader_test"
_TEST_ROLE = "macro_loader_test"
_PRE_MACD_MIGRATION_COUNT = 23


def _local_test_dsn() -> str:
    dsn = os.environ.get("POSTGRES_TEST_DSN")
    if dsn is None:
        pytest.skip("POSTGRES_TEST_DSN is required for disposable PostgreSQL MACD checks")
    target = conninfo_to_dict(dsn)
    if (
        target.get("dbname") != _TEST_DATABASE
        or target.get("user") != _TEST_ROLE
        or target.get("host", "localhost") not in {"localhost", "127.0.0.1", "::1"}
    ):
        pytest.fail(
            "MACD PostgreSQL tests require the disposable local macro_loader_test database "
            "and macro_loader_test role"
        )
    return dsn


def _prepare_empty_test_schemas(dsn: str) -> None:
    """Reset only the validated disposable schemas, following existing PG test conventions."""
    with psycopg.connect(dsn, autocommit=True) as connection:
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


@pytest.fixture
def postgres_context(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, PostgresGoldSchemaMigrator]:
    dsn = _local_test_dsn()
    _prepare_empty_test_schemas(dsn)
    target = conninfo_to_dict(dsn)
    host = target.get("host", "localhost")
    port = int(target.get("port", "5432"))
    database = target.get("dbname", _TEST_DATABASE)
    monkeypatch.setattr(postgres_module, "POSTGRES_HOST", host)
    monkeypatch.setattr(postgres_module, "POSTGRES_PORT", port)
    migrator = PostgresGoldSchemaMigrator(
        PostgresAdminConfig(host, port, "macro_loader_admin", database, "admin"),
        connection_factory=lambda _config: psycopg.connect(dsn),
    )
    return dsn, migrator


def _view_schema(connection: psycopg.Connection[object]) -> tuple[tuple[str, str, bool], ...]:
    rows = connection.execute(
        """SELECT attname, format_type(atttypid, atttypmod), attnotnull
           FROM pg_attribute WHERE attrelid = 'macro_loader.macro_features'::regclass
             AND attnum > 0 AND NOT attisdropped ORDER BY attnum"""
    ).fetchall()
    return tuple((row[0], row[1], row[2]) for row in rows)


def _apply_pre_macd_migrations(dsn: str) -> tuple[str, ...]:
    migrations = postgres_module._MIGRATIONS
    assert len(migrations) > _PRE_MACD_MIGRATION_COUNT
    assert "macd_line_relative" in " ".join(migrations[_PRE_MACD_MIGRATION_COUNT])
    with psycopg.connect(dsn) as connection:
        connection.execute(postgres_module._MIGRATION_LEDGER_DDL)
        for version, statements in enumerate(migrations[:_PRE_MACD_MIGRATION_COUNT], start=1):
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                f"INSERT INTO {postgres_module._MIGRATION_LEDGER} "
                "(version, applied_at_utc) VALUES (%s, CURRENT_TIMESTAMP)",
                (version,),
            )
        return tuple(row[0] for row in _view_schema(connection))


def _feature_raw_column_names(connection: psycopg.Connection[object]) -> set[str]:
    raw_tables = {
        row[0]
        for row in connection.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = 'macro_loader' AND table_name = ANY(%s)""",
            (list(POSTGRES_RAW_TABLES),),
        ).fetchall()
    }
    assert raw_tables == set(POSTGRES_RAW_TABLES)
    assert connection.execute("SELECT to_regclass('macro_loader.macro_raw')").fetchone() == (None,)
    return {
        row[0]
        for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'macro_loader' AND table_name = ANY(%s)",
            (list(POSTGRES_RAW_TABLES),),
        ).fetchall()
    }


def _ema_reference(
    values: Sequence[float | None], period: int, *, positive_only: bool
) -> list[float | None]:
    alpha = 2.0 / (period + 1.0)
    seed: list[float] = []
    current: float | None = None
    result: list[float | None] = []
    for value in values:
        valid = value is not None and math.isfinite(value)
        if positive_only and valid:
            valid = value > 0.0
        if not valid:
            seed.clear()
            current = None
            result.append(None)
            continue
        assert value is not None
        if current is None:
            seed.append(value)
            if len(seed) == period:
                current = sum(seed) / period
                seed.clear()
        else:
            current = alpha * value + (1.0 - alpha) * current
        result.append(current)
    return result


def _macd_reference(levels: Sequence[float | None]) -> dict[str, list[float | None]]:
    fast = _ema_reference(levels, 12, positive_only=True)
    slow = _ema_reference(levels, 26, positive_only=True)
    line = [
        left - right if left is not None and right is not None else None
        for left, right in zip(fast, slow, strict=True)
    ]
    signal = _ema_reference(line, 9, positive_only=False)
    line_relative: list[float | None] = []
    histogram: list[float | None] = []
    delta: list[float | None] = []
    for index, level in enumerate(levels):
        valid_level = level is not None and math.isfinite(level) and level > 0.0
        line_relative.append(
            line[index] / level if valid_level and line[index] is not None else None
        )
        histogram.append(
            (line[index] - signal[index]) / level
            if valid_level and line[index] is not None and signal[index] is not None
            else None
        )
        lagged = histogram[index - 3] if index >= 3 else None
        delta.append(
            histogram[-1] - lagged if histogram[-1] is not None and lagged is not None else None
        )
    return dict(zip(_QA_OUTPUT_SUFFIXES, (line_relative, histogram, delta), strict=True))


def _fixture_levels() -> list[float | None]:
    """Include positive/negative MACD, a NULL reset, and a nonpositive-level reset."""
    return [
        *[100.0 + index * 1.7 for index in range(48)],
        *[181.6 - index * 3.1 for index in range(19)],
        None,
        *[70.0 + index * 0.8 for index in range(40)],
        0.0,
        *[90.0 + index * 2.2 for index in range(42)],
    ]


@pytest.mark.integration
def test_postgres_macd_migration_23_upgrade_has_exact_current_public_schema(
    postgres_context: tuple[str, PostgresGoldSchemaMigrator],
) -> None:
    assert len(POSTGRES_RAW_TABLES) == 25
    dsn, migrator = postgres_context
    before = _apply_pre_macd_migrations(dsn)

    with psycopg.connect(dsn) as connection:
        old_comment = connection.execute(
            "SELECT obj_description('macro_loader.macro_features'::regclass, 'pg_class')"
        ).fetchone()[0]
    assert old_comment.startswith("macro feature view version=12; fingerprint=")

    # Exercise the ordinary admin migration runner from the pre-MACD baseline
    # through the current schema, including all later feature-table migrations.
    migrator.migrate()

    with psycopg.connect(dsn) as connection:
        schema = _view_schema(connection)
        ledger = connection.execute(
            f"SELECT version FROM {postgres_module._MIGRATION_LEDGER} ORDER BY version"
        ).fetchall()
        comment = connection.execute(
            "SELECT obj_description('macro_loader.macro_features'::regclass, 'pg_class')"
        ).fetchone()[0]
        raw_columns = _feature_raw_column_names(connection)

    names = tuple(column for column, _type, _not_null in schema)
    assert not set(_QA_OUTPUT_COLUMNS) & set(before)
    assert tuple(name for name in names if name in set(_QA_OUTPUT_COLUMNS)) == _QA_OUTPUT_COLUMNS
    assert len(_QA_OUTPUT_COLUMNS) == 36 and len(set(_QA_OUTPUT_COLUMNS)) == 36
    assert all(names.count(column) == 1 for column in _QA_OUTPUT_COLUMNS)
    schema_by_name = {name: (postgres_type, not_null) for name, postgres_type, not_null in schema}
    assert all(
        schema_by_name[column] == ("double precision", False) for column in _QA_OUTPUT_COLUMNS
    )
    assert not any(column in raw_columns for column in _QA_OUTPUT_COLUMNS)
    assert not any(column in GOLD_COLUMNS for column in _QA_OUTPUT_COLUMNS)
    assert not any(column.startswith("estr_") for column in names if column in _QA_OUTPUT_COLUMNS)
    assert not any(
        name.endswith(("_ema12", "_ema26", "_ema9_signal", "_macd_line", "_macd_signal"))
        for name in names
    )
    assert ledger == [(version,) for version in range(1, len(postgres_module._MIGRATIONS) + 1)]
    assert comment == (
        f"macro feature view version={MACRO_FEATURE_VIEW_VERSION}; "
        f"fingerprint={MACRO_FEATURE_VIEW_FINGERPRINT}"
    )


@pytest.mark.integration
def test_clean_postgres_macd_view_matches_independent_reference_for_all_series(
    postgres_context: tuple[str, PostgresGoldSchemaMigrator],
) -> None:
    dsn, migrator = postgres_context
    migrator.migrate()
    levels = _fixture_levels()
    expected = _macd_reference(levels)
    ohlc_series = {"vix", "vix9d", "vix3m", "vix6m", "vix1y", "move"}
    timestamps = [
        datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=index) for index in range(len(levels))
    ]

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            for series in _QA_PRICE_SERIES:
                value_column = "close" if series in ohlc_series else "indexvalue"
                relation = f'macro_loader."macro_raw_{series}"'
                cursor.executemany(
                    f'INSERT INTO {relation} ("timestamp_m1", "{value_column}") VALUES (%s, %s)',
                    zip(timestamps, levels, strict=True),
                )
        connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
        selected_columns = ", ".join(f'"{column}"' for column in _QA_OUTPUT_COLUMNS)
        rows = connection.execute(
            f"SELECT {selected_columns} FROM macro_loader.macro_features ORDER BY timestamp_m1"
        ).fetchall()
        view_names = tuple(column for column, _type, _not_null in _view_schema(connection))
        raw_names = _feature_raw_column_names(connection)

    assert len(rows) == len(levels)
    assert tuple(name for name in view_names if name in _QA_OUTPUT_COLUMNS) == _QA_OUTPUT_COLUMNS
    assert not any(name in raw_names for name in _QA_OUTPUT_COLUMNS)
    assert not any(name in GOLD_COLUMNS for name in _QA_OUTPUT_COLUMNS)
    for row_index, row in enumerate(rows):
        for series_index, _series in enumerate(_QA_PRICE_SERIES):
            for output_index, suffix in enumerate(_QA_OUTPUT_SUFFIXES):
                actual = row[series_index * len(_QA_OUTPUT_SUFFIXES) + output_index]
                wanted = expected[suffix][row_index]
                if wanted is None:
                    assert actual is None
                else:
                    assert actual == pytest.approx(wanted, rel=1e-11, abs=1e-12)

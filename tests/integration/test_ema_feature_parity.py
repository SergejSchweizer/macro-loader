"""Independent EMA reference and disposable-PostgreSQL acceptance checks for PR-172."""

from __future__ import annotations

import math
import os
import re
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
from ingestion.postgres_gold_repository import (
    PostgresAdminConfig,
    PostgresGoldSchemaMigrator,
)

# Frozen independently from application registries/catalogs: only these series
# currently have the EMA transformation family in the public view. The remaining
# raw series are asserted separately through the feature-table registry contract.
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
_QA_EMA_SUFFIXES = (
    "ema5_distance",
    "ema9_distance",
    "ema21_distance",
    "ema5_ema21_spread",
    "ema9_slope_3",
    "ema5_ema9_cross",
)
_QA_EMA_PERIODS = {5: 5, 9: 9, 21: 21}
_POSTGRES_TEST_DATABASE = "macro_loader_test"
_POSTGRES_TEST_ROLE = "macro_loader_test"


def _independent_ema(levels: Sequence[float | None], period: int) -> list[float | None]:
    """Arithmetic seed, recursive EMA, and hard reset on every invalid source row."""
    alpha = 2.0 / (period + 1.0)
    seed: list[float] = []
    current: float | None = None
    result: list[float | None] = []
    for level in levels:
        if level is None or not math.isfinite(level) or level <= 0.0:
            seed.clear()
            current = None
            result.append(None)
            continue
        if current is None:
            seed.append(level)
            if len(seed) == period:
                current = sum(seed) / period
                seed.clear()
        else:
            current = alpha * level + (1.0 - alpha) * current
        result.append(current)
    return result


def _independent_ema_features(
    levels: Sequence[float | None],
) -> dict[str, list[float | None]]:
    """Compute only public EMA outputs; no production SQL/catalog helper is reused."""
    ema5 = _independent_ema(levels, 5)
    ema9 = _independent_ema(levels, 9)
    ema21 = _independent_ema(levels, 21)
    size = len(levels)
    features: dict[str, list[float | None]] = {suffix: [None] * size for suffix in _QA_EMA_SUFFIXES}

    for index, level in enumerate(levels):
        if level is not None and math.isfinite(level) and level > 0.0:
            if ema5[index] is not None:
                features["ema5_distance"][index] = level / ema5[index] - 1.0
            if ema9[index] is not None:
                features["ema9_distance"][index] = level / ema9[index] - 1.0
            if ema21[index] is not None:
                features["ema21_distance"][index] = level / ema21[index] - 1.0
                if ema5[index] is not None:
                    features["ema5_ema21_spread"][index] = (ema5[index] - ema21[index]) / level
            if index >= 3 and ema9[index] is not None and ema9[index - 3] is not None:
                features["ema9_slope_3"][index] = (ema9[index] - ema9[index - 3]) / (3.0 * level)

        if index == 0:
            continue
        current_pair = (ema5[index], ema9[index])
        prior_pair = (ema5[index - 1], ema9[index - 1])
        if any(value is None for value in (*current_pair, *prior_pair)):
            continue
        fast, slow = current_pair
        prior_fast, prior_slow = prior_pair
        assert fast is not None and slow is not None
        assert prior_fast is not None and prior_slow is not None
        if fast > slow and prior_fast <= prior_slow:
            features["ema5_ema9_cross"][index] = 1.0
        elif fast < slow and prior_fast >= prior_slow:
            features["ema5_ema9_cross"][index] = -1.0
        else:
            features["ema5_ema9_cross"][index] = 0.0
    return features


def _postgres_dsn() -> str:
    dsn = os.environ.get("POSTGRES_TEST_DSN")
    if dsn is None:
        pytest.skip("POSTGRES_TEST_DSN is required for disposable PostgreSQL EMA parity checks")
    target = conninfo_to_dict(dsn)
    if (
        target.get("dbname") != _POSTGRES_TEST_DATABASE
        or target.get("user") != _POSTGRES_TEST_ROLE
        or target.get("host", "localhost") not in {"localhost", "127.0.0.1", "::1"}
    ):
        pytest.fail(
            "EMA PostgreSQL tests require the disposable local macro_loader_test database "
            "and macro_loader_test role"
        )
    return dsn


def _reset_disposable_schemas(dsn: str) -> None:
    """Mirror the existing real-PostgreSQL fixture's strictly local test isolation."""
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
    dsn = _postgres_dsn()
    _reset_disposable_schemas(dsn)
    target = conninfo_to_dict(dsn)
    host = target.get("host", "localhost")
    port = int(target.get("port", "5432"))
    database = target.get("dbname", _POSTGRES_TEST_DATABASE)
    monkeypatch.setattr(postgres_module, "POSTGRES_HOST", host)
    monkeypatch.setattr(postgres_module, "POSTGRES_PORT", port)
    migrator = PostgresGoldSchemaMigrator(
        PostgresAdminConfig(host, port, "macro_loader_admin", database, "admin"),
        connection_factory=lambda _config: psycopg.connect(dsn),
    )
    return dsn, migrator


def _apply_through_migration_22(dsn: str) -> None:
    migrations = postgres_module._MIGRATIONS
    assert len(migrations) >= 23, "PR-172 must append migration 23"
    with psycopg.connect(dsn) as connection:
        connection.execute(postgres_module._MIGRATION_LEDGER_DDL)
        for version, statements in enumerate(migrations[:22], start=1):
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                f"INSERT INTO {postgres_module._MIGRATION_LEDGER} "
                "(version, applied_at_utc) VALUES (%s, CURRENT_TIMESTAMP)",
                (version,),
            )


def _view_columns(connection: psycopg.Connection[object]) -> tuple[tuple[str, str, bool], ...]:
    rows = connection.execute(
        """SELECT attname, format_type(atttypid, atttypmod), attnotnull
           FROM pg_attribute WHERE attrelid = 'macro_loader.macro_features'::regclass
             AND attnum > 0 AND NOT attisdropped ORDER BY attnum"""
    ).fetchall()
    return tuple((row[0], row[1], row[2]) for row in rows)


def _assert_ema_schema(
    connection: psycopg.Connection[object],
) -> None:
    assert len(POSTGRES_RAW_TABLES) == 25
    actual = _view_columns(connection)
    actual_names = tuple(column for column, _type, _not_null in actual)
    expected_outputs = {
        f"{series}_{suffix}" for series in _QA_PRICE_SERIES for suffix in _QA_EMA_SUFFIXES
    }
    assert len(actual_names) == len(set(actual_names))
    assert expected_outputs <= set(actual_names)
    actual_ema_names = {
        column
        for column in actual_names
        if any(column.startswith(f"{series}_ema") for series in (*_QA_PRICE_SERIES, "estr"))
    }
    assert actual_ema_names == expected_outputs
    actual_by_name = {
        column: (postgres_type, not_null) for column, postgres_type, not_null in actual
    }
    assert all(actual_by_name[column] == ("double precision", False) for column in expected_outputs)
    assert all(f"estr_{suffix}" not in actual_names for suffix in _QA_EMA_SUFFIXES)

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
    raw_columns = {
        row[0]
        for row in connection.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'macro_loader' AND table_name = ANY(%s)""",
            (list(POSTGRES_RAW_TABLES),),
        ).fetchall()
    }
    assert not any("ema" in column for column in raw_columns)
    assert not any("ema" in column for column in GOLD_COLUMNS)

    comment = connection.execute(
        "SELECT obj_description('macro_loader.macro_features'::regclass, 'pg_class')"
    ).fetchone()
    assert comment is not None
    match = re.fullmatch(
        rf"macro feature view version={MACRO_FEATURE_VIEW_VERSION}; "
        r"fingerprint=([0-9a-f]{64})",
        comment[0],
    )
    assert match is not None
    assert match.group(1) == MACRO_FEATURE_VIEW_FINGERPRINT


@pytest.mark.integration
@pytest.mark.xdist_group("postgres-real")
def test_ema_view_migration_22_upgrade_and_clean_create_have_exact_schema(
    postgres_context: tuple[str, PostgresGoldSchemaMigrator],
) -> None:
    dsn, migrator = postgres_context
    _apply_through_migration_22(dsn)
    with psycopg.connect(dsn) as connection:
        baseline = _view_columns(connection)
        old_comment = connection.execute(
            "SELECT obj_description('macro_loader.macro_features'::regclass, 'pg_class')"
        ).fetchone()
    assert old_comment is not None and old_comment[0].startswith(
        "macro feature view version=11; fingerprint="
    )
    baseline_names = {column for column, _type, _not_null in baseline}
    assert (
        not {f"{series}_{suffix}" for series in _QA_PRICE_SERIES for suffix in _QA_EMA_SUFFIXES}
        & baseline_names
    )

    # Exercise the public migration runner for the v22 -> v23 upgrade.
    migrator.migrate()
    with psycopg.connect(dsn) as connection:
        ledger = connection.execute(
            "SELECT version FROM macro_loader_sync.schema_migrations ORDER BY version"
        ).fetchall()
        assert ledger == [(version,) for version in range(1, len(postgres_module._MIGRATIONS) + 1)]
        _assert_ema_schema(connection)

    # Repeat from blank disposable schemas to cover the clean-create path too.
    _reset_disposable_schemas(dsn)
    migrator.migrate()
    with psycopg.connect(dsn) as connection:
        clean_ledger = connection.execute(
            "SELECT version FROM macro_loader_sync.schema_migrations ORDER BY version"
        ).fetchall()
        assert clean_ledger == [
            (version,) for version in range(1, len(postgres_module._MIGRATIONS) + 1)
        ]
        _assert_ema_schema(connection)


@pytest.mark.integration
@pytest.mark.xdist_group("postgres-real")
def test_ema_view_matches_independent_reference_for_seed_reset_crosses_and_causality(
    postgres_context: tuple[str, PostgresGoldSchemaMigrator],
) -> None:
    dsn, migrator = postgres_context
    migrator.migrate()
    # Two separate reset markers prove that NULL, zero, and negative levels do not
    # carry or reuse the prior EMA seed. The final 30-level segment warms EMA-21.
    levels: list[float | None] = (
        [10.0] * 9
        + [20.0] * 4
        + [10.0] * 3
        + [None]
        + [30.0] * 22
        + [0.0]
        + [40.0] * 2
        + [-1.0]
        + [50.0] * 24
        + [1000.0, 2.0]
    )
    features = _independent_ema_features(levels)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    timestamps = [start + timedelta(days=index) for index in range(len(levels))]
    columns = ", ".join(f'"vix_{suffix}"' for suffix in _QA_EMA_SUFFIXES)
    insert = 'INSERT INTO macro_loader.macro_raw_vix ("timestamp_m1", "close") VALUES (%s, %s)'

    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(insert, zip(timestamps, levels, strict=True))
        connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
        actual_rows = connection.execute(
            f"SELECT {columns} FROM macro_loader.macro_features "
            "WHERE timestamp_m1 <= %s ORDER BY timestamp_m1",
            (timestamps[-3],),
        ).fetchall()

        expected_rows = [
            tuple(features[suffix][index] for suffix in _QA_EMA_SUFFIXES)
            for index in range(len(levels) - 2)
        ]
        assert len(actual_rows) == len(expected_rows)
        for index, (actual_row, expected_row) in enumerate(
            zip(actual_rows, expected_rows, strict=True)
        ):
            for suffix, actual, expected in zip(
                _QA_EMA_SUFFIXES, actual_row, expected_row, strict=True
            ):
                if expected is None:
                    assert actual is None, (suffix, index, actual)
                else:
                    assert actual is not None and math.isfinite(actual), (suffix, index, actual)
                    assert math.isclose(actual, expected, rel_tol=2e-10, abs_tol=2e-12), (
                        suffix,
                        index,
                        actual,
                        expected,
                    )

        # Future observations include extreme levels; deleting them and refreshing
        # must leave all earlier EMA results byte-for-byte identical.
        target_timestamp = timestamps[-3]
        before_truncation = actual_rows[-1]
        connection.execute(
            "DELETE FROM macro_loader.macro_raw_vix WHERE timestamp_m1 > %s", (target_timestamp,)
        )
        connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
        after_truncation = connection.execute(
            f"SELECT {columns} FROM macro_loader.macro_features WHERE timestamp_m1 = %s",
            (target_timestamp,),
        ).fetchone()

    assert after_truncation == before_truncation

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import polars as pl
import psycopg
import pytest

import ingestion.postgres_gold_repository as postgres_module
from application.fed_policy_postgres import (
    FED_POLICY_COLUMNS,
    FED_POLICY_FEATURE_COLUMNS,
)
from ingestion.fed_policy_postgres_repository import FedPolicyPostgresRepository
from ingestion.postgres_gold_repository import (
    PostgresAdminConfig,
    PostgresGoldSchemaMigrator,
    PostgresSyncConfig,
)
from scripts.provision_postgres_role import provision_sql

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres-real")]


def _frame(days: list[int], value: float = 1.0) -> pl.DataFrame:
    rows = []
    for day in days:
        timestamp = datetime(2026, 8, day, tzinfo=UTC)
        rows.append(
            (
                timestamp,
                value,
                None,
                2.0,
                value - 1.0,
                timestamp + timedelta(hours=23, minutes=59),
            )
        )
    return pl.DataFrame(
        rows,
        schema={
            "timestamp_m1": pl.Datetime("us", "UTC"),
            **{column: pl.Float64 for column in FED_POLICY_FEATURE_COLUMNS},
            "available_at_utc": pl.Datetime("us", "UTC"),
        },
        orient="row",
    ).select(FED_POLICY_COLUMNS)


@pytest.fixture
def fed_policy_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FedPolicyPostgresRepository, str, PostgresGoldSchemaMigrator]:
    dsn = os.environ.get("POSTGRES_TEST_DSN")
    if dsn is None:
        pytest.skip("POSTGRES_TEST_DSN is required for disposable PostgreSQL coverage")
    with psycopg.connect(dsn, autocommit=True) as connection:
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
    monkeypatch.setattr(postgres_module, "POSTGRES_HOST", "localhost")
    monkeypatch.setattr(postgres_module, "POSTGRES_PORT", 5432)
    monkeypatch.setattr(postgres_module, "POSTGRES_USER", "macro-loader")
    monkeypatch.setattr(postgres_module, "POSTGRES_SYNC_USER", "macro-loader-sync")
    admin = PostgresGoldSchemaMigrator(
        PostgresAdminConfig("localhost", 5432, "macro-loader-admin", "macro_loader_test", "admin"),
        connection_factory=lambda _: psycopg.connect(dsn),
    )
    admin.migrate()
    with psycopg.connect(dsn) as connection:
        connection.execute(
            provision_sql("macro_loader_test", "runtime-secret", "macro_loader_test")
        )
        connection.commit()
    config = PostgresSyncConfig(
        "localhost", 5432, "macro-loader-sync", "macro_loader_test", "runtime-secret"
    )
    return FedPolicyPostgresRepository(config), dsn, admin


def test_real_fed_policy_sync_reconciles_mutations_and_replay(
    fed_policy_repository: tuple[FedPolicyPostgresRepository, str, PostgresGoldSchemaMigrator],
) -> None:
    repository, dsn, migrator = fed_policy_repository
    first = repository.sync(
        _frame([1, 2]),
        source_build_id="build-1",
        schema_version=1,
        feature_version=1,
        synced_at_utc=datetime(2026, 8, 3, tzinfo=UTC),
    )
    replay = repository.sync(
        _frame([1, 2]),
        source_build_id="build-1",
        schema_version=1,
        feature_version=1,
        synced_at_utc=datetime(2026, 8, 3, tzinfo=UTC),
    )
    revised = repository.sync(
        _frame([1, 3], value=2.0),
        source_build_id="build-2",
        schema_version=1,
        feature_version=1,
        synced_at_utc=datetime(2026, 8, 4, tzinfo=UTC),
    )

    assert (len(first.inserts), len(first.updates), len(first.deletes)) == (2, 0, 0)
    assert not replay.mutated
    assert (len(revised.inserts), len(revised.updates), len(revised.deletes)) == (1, 1, 1)
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            'SELECT "timestamp_m1" FROM macro_loader_sync."fed_policy_features_daily" '
            'ORDER BY "timestamp_m1"'
        ).fetchall()
        assert len(rows) == 2
        assert connection.execute(
            "SELECT count(*) FROM macro_loader.macro_features"
        ).fetchone() == (0,)
    migrator.migrate()


def test_real_fed_policy_schema_exposes_exact_columns(
    fed_policy_repository: tuple[FedPolicyPostgresRepository, str, PostgresGoldSchemaMigrator],
) -> None:
    repository, dsn, _ = fed_policy_repository
    with psycopg.connect(dsn) as connection:
        connection.execute(
            'INSERT INTO macro_loader."macro_raw" ("timestamp_m1") VALUES (%s)',
            (datetime(2026, 8, 1, tzinfo=UTC),),
        )
        connection.commit()
    repository.sync(
        _frame([1]),
        source_build_id="build-schema",
        schema_version=1,
        feature_version=1,
        synced_at_utc=datetime(2026, 8, 2, tzinfo=UTC),
    )
    with psycopg.connect(dsn) as connection:
        columns = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'macro_loader_sync' AND table_name = 'fed_policy_features_daily' "
            "ORDER BY ordinal_position"
        ).fetchall()
        assert tuple(row[0] for row in columns) == FED_POLICY_COLUMNS
        raw_columns = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'macro_loader' AND table_name = 'macro_raw' "
            "ORDER BY ordinal_position"
        ).fetchall()
        assert tuple(row[0] for row in raw_columns)[-4:] == FED_POLICY_FEATURE_COLUMNS
        raw_values = connection.execute(
            'SELECT "fed_next_expected_move_bp", "fed_path_slope_m3_bp", '
            '"fed_next_uncertainty_bp", "fed_repricing_5obs_bp" '
            'FROM macro_loader."macro_raw"'
        ).fetchone()
        assert raw_values == (1.0, None, 2.0, 0.0)
        view_columns = connection.execute(
            "SELECT attname FROM pg_attribute "
            "WHERE attrelid = 'macro_loader.macro_features'::regclass "
            "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
        ).fetchall()
        assert tuple(row[0] for row in view_columns)[-32::8] == FED_POLICY_FEATURE_COLUMNS


def test_real_fed_policy_tamper_fails_closed(
    fed_policy_repository: tuple[FedPolicyPostgresRepository, str, PostgresGoldSchemaMigrator],
) -> None:
    repository, dsn, _ = fed_policy_repository
    repository.sync(
        _frame([1]),
        source_build_id="build-1",
        schema_version=1,
        feature_version=1,
        synced_at_utc=datetime(2026, 8, 2, tzinfo=UTC),
    )
    with psycopg.connect(dsn) as connection:
        connection.execute(
            'UPDATE macro_loader_sync."fed_policy_features_daily" '
            'SET "fed_next_expected_move_bp" = 999.0'
        )
        connection.commit()
    with pytest.raises(Exception, match="locked transaction|Fed policy"):
        repository.sync(
            _frame([1]),
            source_build_id="build-1",
            schema_version=1,
            feature_version=1,
            synced_at_utc=datetime(2026, 8, 2, tzinfo=UTC),
        )

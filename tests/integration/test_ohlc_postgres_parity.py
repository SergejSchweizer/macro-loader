"""Disposable-PostgreSQL schema and value parity for native OHLC passthrough."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

import application.macro_feature_catalog as feature_catalog_module
import ingestion.postgres_gold_repository as postgres_module
from application.contracts import NativeShape
from application.gold_frame import GOLD_SCHEMA_VERSION
from application.macro_feature_catalog import (
    FEATURE_COLUMNS,
    MACRO_FEATURE_VIEW_FINGERPRINT,
    MACRO_FEATURE_VIEW_VERSION,
)
from application.postgres_delta import gold_row_sha256
from application.postgres_sync import (
    POSTGRES_DATASET_ID,
    POSTGRES_RAW_COLUMNS,
    GoldDeltaPlan,
    GoldRowDigest,
    GoldRowPayload,
    GoldSyncState,
)
from application.registry import SERIES_REGISTRY
from ingestion.postgres_gold_repository import (
    PostgresAdminConfig,
    PostgresGoldSchemaMigrator,
    PostgresGoldSyncRepository,
    PostgresSyncConfig,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("ohlc-postgres")]

_OHLC_FIELDS = ("open", "high", "low", "close")
_OHLC_SERIES = tuple(
    series_id
    for series_id, contract in SERIES_REGISTRY.items()
    if contract.native_shape is NativeShape.OHLC
)
_SCALAR_SERIES = tuple(
    series_id
    for series_id, contract in SERIES_REGISTRY.items()
    if contract.native_shape is NativeShape.SCALAR
)
_OHLC_COLUMNS = tuple(
    f"{series_id}_{field}" for series_id in _OHLC_SERIES for field in _OHLC_FIELDS
)


@pytest.fixture
def postgres_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    dsn = os.environ.get("POSTGRES_TEST_DSN")
    if not dsn:
        pytest.skip("POSTGRES_TEST_DSN is not configured; skipping disposable PostgreSQL QA")

    target = conninfo_to_dict(dsn)
    host = target.get("host", "localhost")
    if (
        target.get("dbname") != "macro_loader_test"
        or target.get("user") != "macro_loader_test"
        or host not in {"localhost", "127.0.0.1", "::1"}
        or target.get("hostaddr", "") not in {"", "127.0.0.1", "::1"}
        or "service" in target
    ):
        pytest.fail(
            "OHLC PostgreSQL QA requires only the local macro_loader_test database "
            "and macro_loader_test role"
        )
    port = int(target.get("port", "5432"))
    monkeypatch.setattr(postgres_module, "POSTGRES_HOST", host)
    monkeypatch.setattr(postgres_module, "POSTGRES_PORT", port)

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
        connection.execute("ALTER ROLE \"macro-loader\" PASSWORD 'runtime-test-password'")
        connection.execute("ALTER ROLE \"macro-loader-sync\" PASSWORD 'runtime-test-password'")
    yield dsn


@pytest.fixture
def migrator(postgres_dsn: str) -> PostgresGoldSchemaMigrator:
    target = conninfo_to_dict(postgres_dsn)
    host = target.get("host", "localhost")
    port = int(target.get("port", "5432"))
    config = PostgresAdminConfig(
        host=host,
        port=port,
        user="macro_loader_admin",
        database="macro_loader_test",
        password="disposable-test-only",
    )
    return PostgresGoldSchemaMigrator(
        config,
        connection_factory=lambda _config: psycopg.connect(postgres_dsn),
    )


@pytest.fixture
def sync_repository(postgres_dsn: str) -> PostgresGoldSyncRepository:
    target = conninfo_to_dict(postgres_dsn)
    return PostgresGoldSyncRepository(
        PostgresSyncConfig(
            host=target.get("host", "localhost"),
            port=int(target.get("port", "5432")),
            user="macro-loader-sync",
            database="macro_loader_test",
            password="runtime-test-password",
        )
    )


def _expected_gold_columns() -> tuple[str, ...]:
    return (
        "timestamp_m1",
        *(f"{series_id}_level" for series_id in SERIES_REGISTRY),
        *_OHLC_COLUMNS,
    )


def _expected_raw_table_columns() -> tuple[str, ...]:
    return (
        "timestamp_m1",
        *(f"{series_id}_level" for series_id in SERIES_REGISTRY),
        "available_at_utc",
        *_OHLC_COLUMNS,
    )


def _legacy_v24_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, tuple[str, ...], str, str]:
    """Build the legacy raw table and v13 view contract without replaying old migrations."""
    legacy_catalog = tuple(
        spec for spec in feature_catalog_module.FEATURE_CATALOG if spec.family != "raw_ohlc"
    )
    legacy_view_columns = ("timestamp_m1", *(spec.name for spec in legacy_catalog))
    fingerprint_payload = {
        "version": 13,
        "columns": [
            {
                "name": spec.name,
                "family": spec.family,
                "series": spec.series,
                "formula": spec.formula,
            }
            for spec in legacy_catalog
        ],
    }
    legacy_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    legacy_levels = ",\n    ".join(
        f'"{series_id}_level" DOUBLE PRECISION NULL' for series_id in SERIES_REGISTRY
    )
    legacy_consumer_ddl = (
        'CREATE TABLE IF NOT EXISTS "macro_loader"."macro_raw" ('
        '"timestamp_m1" TIMESTAMPTZ(6) NOT NULL PRIMARY KEY,\n    '
        f"{legacy_levels},\n    "
        f"{postgres_module._AVAILABLE_AT_COLUMN_DDL}\n)"
    )

    monkeypatch.setattr(postgres_module, "_FEATURES_VIEW_COLUMNS", legacy_view_columns[1:])
    legacy_view_ddl = postgres_module._macro_features_view_query()
    return legacy_consumer_ddl, legacy_view_columns[1:], legacy_view_ddl, legacy_fingerprint


def _seed_v24_baseline(
    connection: psycopg.Connection[object],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[datetime, str, tuple[str, ...]]:
    """Create a minimal v24 schema, dependent v13 view, and completed migration ledger."""
    legacy_raw_ddl, legacy_feature_columns, legacy_view_ddl, legacy_fingerprint = (
        _legacy_v24_contract(monkeypatch)
    )
    legacy_timestamp = datetime(2026, 1, 2, tzinfo=UTC)
    legacy_levels = tuple(f"{series_id}_level" for series_id in SERIES_REGISTRY)
    legacy_columns = ("timestamp_m1", *legacy_levels)
    quoted_legacy_columns = ", ".join(f'"{column}"' for column in legacy_columns)
    legacy_values = ", ".join("%s" for _ in legacy_columns)
    for statement in (
        postgres_module._MIGRATION_LEDGER_DDL,
        legacy_raw_ddl,
        postgres_module._SYNC_STATE_DDL,
        postgres_module._ROW_HASH_DDL,
    ):
        connection.execute(statement)
    connection.execute(
        f"INSERT INTO macro_loader.macro_raw ({quoted_legacy_columns}) VALUES ({legacy_values})",
        (legacy_timestamp, *(42.0 for _ in legacy_levels)),
    )
    connection.execute(legacy_view_ddl)
    connection.execute(
        f"COMMENT ON MATERIALIZED VIEW {postgres_module._FEATURES_VIEW} IS "
        f"'macro feature view version=13; fingerprint={legacy_fingerprint}'"
    )
    connection.execute(
        f"GRANT USAGE ON SCHEMA "
        f"{postgres_module._quote(postgres_module.POSTGRES_CONSUMER_SCHEMA)}, "
        f"{postgres_module._quote(postgres_module.POSTGRES_SYNC_SCHEMA)} TO "
        f"{postgres_module._quote(postgres_module.POSTGRES_SYNC_USER)}, "
        f"{postgres_module._quote(postgres_module.POSTGRES_USER)}, "
        f"{postgres_module._quote(postgres_module._POSTGRES_OWNER_ROLE)}"
    )
    connection.execute(
        f"GRANT SELECT ON TABLE {postgres_module._CONSUMER} "
        f"TO {postgres_module._quote(postgres_module.POSTGRES_USER)}"
    )
    connection.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {postgres_module._CONSUMER} "
        f"TO {postgres_module._quote(postgres_module.POSTGRES_SYNC_USER)}"
    )
    connection.execute(
        f"REVOKE INSERT, UPDATE, DELETE ON TABLE {postgres_module._CONSUMER} "
        f"FROM {postgres_module._quote(postgres_module.POSTGRES_USER)}"
    )
    connection.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        f"{postgres_module._SYNC_STATE}, {postgres_module._ROW_HASHES} "
        f"TO {postgres_module._quote(postgres_module.POSTGRES_SYNC_USER)}"
    )
    connection.execute(
        f"GRANT SELECT ON TABLE {postgres_module._MIGRATION_LEDGER} "
        f"TO {postgres_module._quote(postgres_module.POSTGRES_SYNC_USER)}"
    )
    connection.execute(f"REVOKE ALL ON {postgres_module._FEATURES_VIEW} FROM PUBLIC")
    connection.execute(
        f"GRANT SELECT ON {postgres_module._FEATURES_VIEW} TO "
        f"{postgres_module._quote(postgres_module.POSTGRES_USER)}, "
        f"{postgres_module._quote(postgres_module.POSTGRES_SYNC_USER)}"
    )
    for version in range(1, 25):
        connection.execute(
            f"INSERT INTO {postgres_module._MIGRATION_LEDGER} (version, applied_at_utc) "
            "VALUES (%s, CURRENT_TIMESTAMP)",
            (version,),
        )
    view_columns = tuple(
        str(row[0]) for row in _column_rows(connection, "macro_loader.macro_features")
    )
    assert view_columns == (
        "timestamp_m1",
        "available_at_utc",
        *legacy_feature_columns,
    )
    return legacy_timestamp, legacy_fingerprint, legacy_feature_columns


def _column_rows(connection: psycopg.Connection[object], relation: str) -> list[tuple[object, ...]]:
    return connection.execute(
        """SELECT attribute.attname, format_type(attribute.atttypid, attribute.atttypmod),
                         attribute.attnotnull
             FROM pg_attribute AS attribute
             WHERE attribute.attrelid = %s::regclass
               AND attribute.attnum > 0 AND NOT attribute.attisdropped
             ORDER BY attribute.attnum""",
        (relation,),
    ).fetchall()


def _assert_view_contract(connection: psycopg.Connection[object]) -> None:
    rows = _column_rows(connection, "macro_loader.macro_features")
    names = tuple(str(row[0]) for row in rows)
    assert names == ("timestamp_m1", "available_at_utc", *FEATURE_COLUMNS[1:])
    assert tuple(str(row[1]) for row in rows[:2]) == (
        "timestamp(6) with time zone",
        "timestamp(6) with time zone",
    )
    assert all(row[1] == "double precision" and row[2] is False for row in rows[2:])
    assert (
        tuple(name for name in names if name.endswith(tuple(f"_{field}" for field in _OHLC_FIELDS)))
        == _OHLC_COLUMNS
    )
    for series_id in _SCALAR_SERIES:
        assert not any(f"{series_id}_{field}" in names for field in _OHLC_FIELDS)
    assert not any(
        marker in name
        for name in names
        for marker in ("high_low_range", "intraday_return", "overnight_gap", "stoch_", "donchian_")
    )


@pytest.mark.parametrize("migration_mode", ("clean_create", "upgrade_24_to_25"))
def test_disposable_postgres_ohlc_schema_migration_and_view_parity(
    postgres_dsn: str,
    migrator: PostgresGoldSchemaMigrator,
    sync_repository: PostgresGoldSyncRepository,
    monkeypatch: pytest.MonkeyPatch,
    migration_mode: str,
) -> None:
    expected_migrations = len(postgres_module._MIGRATIONS)
    assert GOLD_SCHEMA_VERSION == 8
    assert MACRO_FEATURE_VIEW_VERSION == 14
    assert len(MACRO_FEATURE_VIEW_FINGERPRINT) == 64
    assert _expected_gold_columns() == POSTGRES_RAW_COLUMNS
    assert _OHLC_SERIES == ("vix", "vix9d", "vix3m", "vix6m", "vix1y", "move")
    assert len(_OHLC_COLUMNS) == 24

    if migration_mode == "upgrade_24_to_25":
        with psycopg.connect(postgres_dsn) as connection:
            legacy_timestamp, legacy_fingerprint, legacy_feature_columns = _seed_v24_baseline(
                connection, monkeypatch
            )
            before = connection.execute(
                f"SELECT version FROM {postgres_module._MIGRATION_LEDGER} ORDER BY version"
            ).fetchall()
            assert before == [(version,) for version in range(1, 25)]
            view_comment = connection.execute(
                "SELECT obj_description('macro_loader.macro_features'::regclass, 'pg_class')"
            ).fetchone()
            assert view_comment == (
                f"macro feature view version=13; fingerprint={legacy_fingerprint}",
            )
            raw_names_before = tuple(
                str(row[0]) for row in _column_rows(connection, "macro_loader.macro_raw")
            )
            view_names_before = tuple(
                str(row[0]) for row in _column_rows(connection, "macro_loader.macro_features")
            )
            assert raw_names_before == (
                "timestamp_m1",
                *(f"{series_id}_level" for series_id in SERIES_REGISTRY),
                "available_at_utc",
            )
            assert view_names_before == (
                "timestamp_m1",
                "available_at_utc",
                *legacy_feature_columns,
            )
        migrator.migrate()
    else:
        migrator.migrate()

    with psycopg.connect(postgres_dsn) as connection:
        ledger = connection.execute(
            f"SELECT version FROM {postgres_module._MIGRATION_LEDGER} ORDER BY version"
        ).fetchall()
        raw_rows = _column_rows(connection, "macro_loader.macro_raw")
        raw_names = tuple(str(row[0]) for row in raw_rows)
        assert ledger == [(version,) for version in range(1, expected_migrations + 1)]
        assert raw_names == _expected_raw_table_columns()
        assert raw_rows[0][1:] == ("timestamp(6) with time zone", True)
        assert all(
            row[1] == "double precision" and row[2] is False
            for row in raw_rows[1 : len(SERIES_REGISTRY) + 1]
        )
        availability = raw_rows[len(SERIES_REGISTRY) + 1]
        assert availability == (
            "available_at_utc",
            "timestamp(6) with time zone",
            True,
        )
        assert all(row[1] == "double precision" and row[2] is False for row in raw_rows[-24:])
        _assert_view_contract(connection)
        view_comment = connection.execute(
            "SELECT obj_description('macro_loader.macro_features'::regclass, 'pg_class')"
        ).fetchone()
        assert view_comment == (
            f"macro feature view version=14; fingerprint={MACRO_FEATURE_VIEW_FINGERPRINT}",
        )

        if migration_mode == "upgrade_24_to_25":
            legacy_timestamp = datetime(2026, 1, 2, tzinfo=UTC)
            expected_available = datetime(2026, 1, 5, tzinfo=UTC)
            preserved_raw = connection.execute(
                "SELECT vix_level, available_at_utc FROM macro_loader.macro_raw "
                "WHERE timestamp_m1 = %s",
                (legacy_timestamp,),
            ).fetchone()
            preserved_view = connection.execute(
                "SELECT vix_log_level, available_at_utc FROM macro_loader.macro_features "
                "WHERE timestamp_m1 = %s",
                (legacy_timestamp,),
            ).fetchone()
            assert preserved_raw == (42.0, expected_available)
            assert preserved_view == (pytest.approx(math.log(preserved_raw[0])), expected_available)
            connection.execute(
                "DELETE FROM macro_loader.macro_raw WHERE timestamp_m1 = %s",
                (legacy_timestamp,),
            )
            connection.commit()

        timestamp = datetime(2026, 1, 5, tzinfo=UTC)
        values = {column: float(index + 1) for index, column in enumerate(POSTGRES_RAW_COLUMNS[1:])}
        for series_index, series_id in enumerate(_OHLC_SERIES):
            values[f"{series_id}_level"] = 100.0 + series_index
            values[f"{series_id}_open"] = 101.0 + series_index
            values[f"{series_id}_high"] = 105.0 + series_index
            values[f"{series_id}_low"] = 98.0 + series_index
            values[f"{series_id}_close"] = 100.0 + series_index
        payload = GoldRowPayload(
            timestamp, tuple(values[column] for column in POSTGRES_RAW_COLUMNS[1:])
        )
        digest = GoldRowDigest(timestamp, gold_row_sha256(payload))
        sync_state = GoldSyncState(
            dataset_id=POSTGRES_DATASET_ID,
            source_build_id="ohlc-reference-build",
            data_sha256="a" * 64,
            schema_version=8,
            feature_version=6,
            row_count=1,
            min_timestamp=timestamp,
            max_timestamp=timestamp,
            synced_at_utc=datetime(2026, 1, 6, tzinfo=UTC),
        )
        connection.commit()
        sync_repository.apply_delta(
            POSTGRES_DATASET_ID,
            GoldDeltaPlan((payload,), (), (), (), (digest,)),
            sync_state,
        )
        connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
        selected_columns = ", ".join(f'"{column}"' for column in _OHLC_COLUMNS)
        selected = connection.execute(
            f"SELECT {selected_columns} FROM macro_loader.macro_features WHERE timestamp_m1 = %s",
            (timestamp,),
        ).fetchone()
        assert selected == tuple(values[column] for column in _OHLC_COLUMNS)
        expected_passthrough = (
            values["vix_level"],
            values["vix_close"],
            values["vix_open"],
            datetime(2026, 1, 6, tzinfo=UTC),
        )
        raw_passthrough = connection.execute(
            "SELECT vix_level, vix_close, vix_open, available_at_utc "
            "FROM macro_loader.macro_raw WHERE timestamp_m1 = %s",
            (timestamp,),
        ).fetchone()
        view_passthrough = connection.execute(
            "SELECT vix_log_level, vix_close, vix_open, available_at_utc "
            "FROM macro_loader.macro_features WHERE timestamp_m1 = %s",
            (timestamp,),
        ).fetchone()
        assert raw_passthrough == expected_passthrough
        assert view_passthrough == (
            pytest.approx(math.log(values["vix_level"])),
            values["vix_close"],
            values["vix_open"],
            expected_passthrough[3],
        )
        connection.commit()

        revised_values = {**values, "vix_open": values["vix_open"] + 0.5}
        revised_payload = GoldRowPayload(
            timestamp,
            tuple(revised_values[column] for column in POSTGRES_RAW_COLUMNS[1:]),
        )
        revised_digest = GoldRowDigest(timestamp, gold_row_sha256(revised_payload))
        assert revised_digest.row_sha256 != digest.row_sha256
        sync_repository.apply_delta(
            POSTGRES_DATASET_ID,
            GoldDeltaPlan((), (revised_payload,), (), (), (revised_digest,)),
            GoldSyncState(
                dataset_id=POSTGRES_DATASET_ID,
                source_build_id="ohlc-reference-revision",
                data_sha256="b" * 64,
                schema_version=8,
                feature_version=6,
                row_count=1,
                min_timestamp=timestamp,
                max_timestamp=timestamp,
                synced_at_utc=datetime(2026, 1, 7, tzinfo=UTC),
            ),
        )
        with psycopg.connect(postgres_dsn) as revision_connection:
            revision_connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
            revised_view_value = revision_connection.execute(
                'SELECT "vix_open" FROM macro_loader.macro_features WHERE timestamp_m1 = %s',
                (timestamp,),
            ).fetchone()
        assert revised_view_value == (revised_values["vix_open"],)

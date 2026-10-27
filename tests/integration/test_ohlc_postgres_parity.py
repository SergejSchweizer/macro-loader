"""Disposable-PostgreSQL schema and value parity for native OHLC passthrough."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

import application.macro_feature_catalog as feature_catalog_module
import ingestion.postgres_gold_repository as postgres_module
from application.contracts import NativeShape
from application.gold_frame import GOLD_SCHEMA_VERSION
from application.macro_feature_catalog import (
    EXTENDED_FEATURE_COLUMNS,
    EXTENDED_FEATURE_POSTGRES_TYPES,
    EXTENDED_MACRO_FEATURE_VIEW_FINGERPRINT,
    EXTENDED_MACRO_FEATURE_VIEW_VERSION,
    FEATURE_COLUMNS,
    FEATURE_POSTGRES_TYPES,
    MACRO_FEATURE_VIEW_FINGERPRINT,
    MACRO_FEATURE_VIEW_VERSION,
    OHLC_BAR_FEATURE_FAMILIES,
    OHLC_DONCHIAN_SUFFIXES,
    OHLC_STOCHASTIC_SUFFIXES,
)
from application.postgres_delta import gold_row_sha256
from application.postgres_sync import (
    POSTGRES_DATASET_ID,
    POSTGRES_RAW_COLUMNS,
    POSTGRES_RAW_TABLES,
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


def _expected_feature_table_columns(series_id: str) -> tuple[str, ...]:
    values = _OHLC_FIELDS if series_id in _OHLC_SERIES else ("indexvalue",)
    return ("timestamp_m1", "available_at_utc", *values)


_OHLC_BAR_SUFFIXES = (
    "high_low_range_1obs",
    "high_low_range_5obs",
    "intraday_return_1obs",
    "intraday_return_5obs",
    "overnight_gap_1obs",
    "overnight_gap_5obs",
)


def _legacy_v25_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, tuple[str, ...], str, str]:
    """Build the migration-25 raw table and v14 view without replaying old migrations."""
    legacy_catalog = tuple(
        spec
        for spec in feature_catalog_module.FEATURE_CATALOG
        if spec.family not in (*OHLC_BAR_FEATURE_FAMILIES, "stochastic", "donchian")
    )
    legacy_view_columns = ("timestamp_m1", *(spec.name for spec in legacy_catalog))
    fingerprint_payload = {
        "version": 14,
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

    legacy_consumer_ddl = postgres_module._CONSUMER_DDL
    monkeypatch.setattr(postgres_module, "_FEATURES_VIEW_COLUMNS", legacy_view_columns[1:])
    legacy_view_ddl = postgres_module._macro_features_view_query()
    return legacy_consumer_ddl, legacy_view_columns[1:], legacy_view_ddl, legacy_fingerprint


def _seed_v25_baseline(
    connection: psycopg.Connection[object],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[datetime, str, tuple[str, ...]]:
    """Create a minimal v25 schema, dependent v14 view, and completed migration ledger."""
    legacy_raw_ddl, legacy_feature_columns, legacy_view_ddl, legacy_fingerprint = (
        _legacy_v25_contract(monkeypatch)
    )
    legacy_timestamp = datetime(2026, 1, 2, tzinfo=UTC)
    raw_columns = POSTGRES_RAW_COLUMNS[1:]
    quoted_legacy_columns = ", ".join(f'"{column}"' for column in ("timestamp_m1", *raw_columns))
    legacy_values = ", ".join("%s" for _ in range(len(raw_columns) + 1))
    for statement in (
        postgres_module._MIGRATION_LEDGER_DDL,
        legacy_raw_ddl,
        postgres_module._SYNC_STATE_DDL,
        postgres_module._ROW_HASH_DDL,
    ):
        connection.execute(statement)
    connection.execute(
        f"INSERT INTO macro_loader.macro_raw ({quoted_legacy_columns}) VALUES ({legacy_values})",
        (legacy_timestamp, *(42.0 for _ in raw_columns)),
    )
    connection.execute(legacy_view_ddl)
    connection.execute(
        f"COMMENT ON MATERIALIZED VIEW {postgres_module._FEATURES_VIEW} IS "
        f"'macro feature view version=14; fingerprint={legacy_fingerprint}'"
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
    for version in range(1, 26):
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


def _insert_feature_source_row(
    connection: psycopg.Connection[object], timestamp: datetime, values: dict[str, float | None]
) -> None:
    """Insert one source date into each feature table with at least one actual value."""
    for series_id, contract in SERIES_REGISTRY.items():
        names = _OHLC_FIELDS if contract.native_shape is NativeShape.OHLC else ("indexvalue",)
        source_names = (
            tuple(f"{series_id}_{field}" for field in _OHLC_FIELDS)
            if contract.native_shape is NativeShape.OHLC
            else (f"{series_id}_level",)
        )
        row_values = tuple(values.get(name) for name in source_names)
        if not any(value is not None for value in row_values):
            continue
        columns = ("timestamp_m1", *names)
        quoted_columns = ", ".join('"' + column + '"' for column in columns)
        placeholders = ", ".join("%s" for _ in columns)
        connection.execute(
            f'INSERT INTO macro_loader."macro_raw_{series_id}" '
            f"({quoted_columns}) VALUES ({placeholders})",
            (timestamp, *row_values),
        )


def _assert_view_contract(connection: psycopg.Connection[object]) -> None:
    rows = _column_rows(connection, "macro_loader.macro_features")
    names = tuple(str(row[0]) for row in rows)
    assert names == ("timestamp_m1", "available_at_utc", *FEATURE_COLUMNS[1:])
    assert tuple(str(row[1]) for row in rows[:2]) == (
        "timestamp(6) with time zone",
        "timestamp(6) with time zone",
    )
    assert all(
        row[1] == FEATURE_POSTGRES_TYPES[str(row[0])] and row[2] is False for row in rows[2:]
    )
    assert (
        tuple(name for name in names if name.endswith(tuple(f"_{field}" for field in _OHLC_FIELDS)))
        == _OHLC_COLUMNS
    )
    for series_id in _SCALAR_SERIES:
        assert not any(f"{series_id}_{field}" in names for field in _OHLC_FIELDS)
    expected_derived = tuple(
        f"{series_id}_{suffix}" for series_id in _OHLC_SERIES for suffix in _OHLC_BAR_SUFFIXES
    )
    derived_names = tuple(
        name for name in names if any(name.endswith(f"_{suffix}") for suffix in _OHLC_BAR_SUFFIXES)
    )
    assert derived_names == expected_derived
    assert len(expected_derived) == 36
    assert not any(
        name.startswith("estr_") and any(family in name for family in OHLC_BAR_FEATURE_FAMILIES)
        for name in names
    )
    expected_stochastic = tuple(
        f"{series}_{suffix}" for series in _OHLC_SERIES for suffix in OHLC_STOCHASTIC_SUFFIXES
    )
    expected_donchian = tuple(
        f"{series}_{suffix}" for series in _OHLC_SERIES for suffix in OHLC_DONCHIAN_SUFFIXES
    )
    assert tuple(name for name in names if "_stoch_" in name) == expected_stochastic
    assert tuple(name for name in names if "_donchian_" in name) == expected_donchian
    all_new = (*expected_derived, *expected_stochastic, *expected_donchian)
    raw_relations = connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'macro_loader' AND table_type = 'BASE TABLE' "
        "AND table_name LIKE 'macro_raw_%' ORDER BY table_name"
    ).fetchall()
    assert tuple(row[0] for row in raw_relations) == tuple(sorted(POSTGRES_RAW_TABLES))
    assert connection.execute("SELECT to_regclass('macro_loader.macro_raw')").fetchone() == (None,)
    raw_names = tuple(
        column[0]
        for series_id in SERIES_REGISTRY
        for column in _column_rows(connection, f"macro_loader.macro_raw_{series_id}")
    )
    assert not set(all_new).intersection(raw_names)
    assert not set(all_new).intersection(_expected_gold_columns())


def _assert_extended_view_contract(connection: psycopg.Connection[object]) -> None:
    rows = _column_rows(connection, "macro_loader.macro_features_extended")
    names = tuple(str(row[0]) for row in rows)
    assert names == ("timestamp_m1", "available_at_utc", *EXTENDED_FEATURE_COLUMNS[1:])
    assert tuple(str(row[1]) for row in rows[:2]) == (
        "timestamp(6) with time zone",
        "timestamp(6) with time zone",
    )
    assert all(
        row[1] == EXTENDED_FEATURE_POSTGRES_TYPES[str(row[0])] and row[2] is False
        for row in rows[2:]
    )
    assert len(names) == 794 < 1600
    assert connection.execute(
        "SELECT obj_description('macro_loader.macro_features_extended'::regclass, 'pg_class')"
    ).fetchone() == (
        f"macro feature view version={EXTENDED_MACRO_FEATURE_VIEW_VERSION}; "
        f"fingerprint={EXTENDED_MACRO_FEATURE_VIEW_FINGERPRINT}",
    )


@pytest.mark.parametrize("migration_mode", ("clean_create", "upgrade_25_to_31"))
def test_disposable_postgres_ohlc_schema_migration_and_view_parity(
    postgres_dsn: str,
    migrator: PostgresGoldSchemaMigrator,
    sync_repository: PostgresGoldSyncRepository,
    monkeypatch: pytest.MonkeyPatch,
    migration_mode: str,
) -> None:
    expected_migrations = len(postgres_module._MIGRATIONS)
    assert GOLD_SCHEMA_VERSION == 11
    assert MACRO_FEATURE_VIEW_VERSION == 16
    assert len(MACRO_FEATURE_VIEW_FINGERPRINT) == 64
    assert _expected_gold_columns() == POSTGRES_RAW_COLUMNS
    assert _OHLC_SERIES == ("vix", "vix9d", "vix3m", "vix6m", "vix1y", "move")
    assert len(_OHLC_COLUMNS) == 24

    if migration_mode == "upgrade_25_to_31":
        with psycopg.connect(postgres_dsn) as connection:
            legacy_timestamp, legacy_fingerprint, legacy_feature_columns = _seed_v25_baseline(
                connection, monkeypatch
            )
            before = connection.execute(
                f"SELECT version FROM {postgres_module._MIGRATION_LEDGER} ORDER BY version"
            ).fetchall()
            assert before == [(version,) for version in range(1, 26)]
            view_comment = connection.execute(
                "SELECT obj_description('macro_loader.macro_features'::regclass, 'pg_class')"
            ).fetchone()
            assert view_comment == (
                f"macro feature view version=14; fingerprint={legacy_fingerprint}",
            )
            raw_names_before = tuple(
                str(row[0]) for row in _column_rows(connection, "macro_loader.macro_raw")
            )
            view_names_before = tuple(
                str(row[0]) for row in _column_rows(connection, "macro_loader.macro_features")
            )
            assert raw_names_before == (
                "timestamp_m1",
                *(
                    f"{series_id}_level"
                    for series_id in SERIES_REGISTRY
                    if f"{series_id}_level" not in postgres_module._ADDED_LEVEL_COLUMNS
                ),
                "available_at_utc",
                *_OHLC_COLUMNS,
                *postgres_module._ADDED_LEVEL_COLUMNS,
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
        assert ledger == [(version,) for version in range(1, expected_migrations + 1)]
        table_names = tuple(
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'macro_loader' AND table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            ).fetchall()
        )
        assert table_names == tuple(sorted(POSTGRES_RAW_TABLES))
        assert connection.execute("SELECT to_regclass('macro_loader.macro_raw')").fetchone() == (
            None,
        )
        for series_id in SERIES_REGISTRY:
            raw_rows = _column_rows(connection, f"macro_loader.macro_raw_{series_id}")
            assert tuple(row[0] for row in raw_rows) == _expected_feature_table_columns(series_id)
            assert raw_rows[0][1:] == ("timestamp(6) with time zone", True)
            assert raw_rows[1][1:] == ("timestamp(6) with time zone", True)
            assert all(row[1:] == ("double precision", False) for row in raw_rows[2:])
        _assert_view_contract(connection)
        _assert_extended_view_contract(connection)
        view_comment = connection.execute(
            "SELECT obj_description('macro_loader.macro_features'::regclass, 'pg_class')"
        ).fetchone()
        assert view_comment == (
            f"macro feature view version=16; fingerprint={MACRO_FEATURE_VIEW_FINGERPRINT}",
        )

        if migration_mode == "upgrade_25_to_31":
            legacy_timestamp = datetime(2026, 1, 2, tzinfo=UTC)
            expected_available = datetime(2026, 1, 5, tzinfo=UTC)
            preserved_raw = connection.execute(
                "SELECT close, available_at_utc FROM macro_loader.macro_raw_vix "
                "WHERE timestamp_m1 = %s",
                (legacy_timestamp,),
            ).fetchone()
            preserved_view = connection.execute(
                "SELECT vix_log_level, vix_open, available_at_utc "
                "FROM macro_loader.macro_features "
                "WHERE timestamp_m1 = %s",
                (legacy_timestamp,),
            ).fetchone()
            assert preserved_raw == (42.0, expected_available)
            assert preserved_view == (
                pytest.approx(math.log(preserved_raw[0])),
                42.0,
                expected_available,
            )
            for raw_table in POSTGRES_RAW_TABLES:
                connection.execute(
                    f'DELETE FROM macro_loader."{raw_table}" WHERE timestamp_m1 = %s',
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
            schema_version=11,
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
        connection.execute("SELECT macro_loader.refresh_macro_features_explicit()")
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
            "SELECT close, open, available_at_utc "
            "FROM macro_loader.macro_raw_vix WHERE timestamp_m1 = %s",
            (timestamp,),
        ).fetchone()
        view_passthrough = connection.execute(
            "SELECT vix_log_level, vix_close, vix_open, available_at_utc "
            "FROM macro_loader.macro_features WHERE timestamp_m1 = %s",
            (timestamp,),
        ).fetchone()
        assert raw_passthrough == (values["vix_close"], values["vix_open"], expected_passthrough[3])
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
                schema_version=11,
                feature_version=6,
                row_count=1,
                min_timestamp=timestamp,
                max_timestamp=timestamp,
                synced_at_utc=datetime(2026, 1, 7, tzinfo=UTC),
            ),
        )
        with psycopg.connect(postgres_dsn) as revision_connection:
            revision_connection.execute("SELECT macro_loader.refresh_macro_features_explicit()")
            revised_view_value = revision_connection.execute(
                'SELECT "vix_open" FROM macro_loader.macro_features WHERE timestamp_m1 = %s',
                (timestamp,),
            ).fetchone()
        assert revised_view_value == (revised_values["vix_open"],)


def _valid_number(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _rolling_reference(metrics: list[float | None], position: int, window: int) -> float | None:
    current = metrics[position]
    if current is None:
        return None
    valid = [value for value in metrics[: position + 1] if value is not None]
    if len(valid) < window:
        return None
    return sum(valid[-window:]) / window


def _bar_reference(
    bars: list[tuple[float | None, float | None, float | None, float | None]],
    family: str,
) -> list[float | None]:
    metrics: list[float | None] = []
    for index, (open_value, high, low, close) in enumerate(bars):
        metric: float | None = None
        if family == "high_low_range":
            if all(_valid_number(value) for value in (high, low, close)) and close > 0:
                metric = (high - low) / close
        elif family == "intraday_return":
            if all(_valid_number(value) for value in (open_value, close)) and open_value > 0:
                metric = (close - open_value) / open_value
        else:
            if index > 0:
                previous_close = next(
                    (
                        candidate[3]
                        for candidate in reversed(bars[:index])
                        if _valid_number(candidate[3])
                    ),
                    None,
                )
                if (
                    _valid_number(open_value)
                    and _valid_number(previous_close)
                    and previous_close > 0
                ):
                    metric = (open_value - previous_close) / previous_close
        metrics.append(metric)
    return metrics


@pytest.mark.integration
def test_disposable_postgres_ohlc_bar_features_match_independent_reference(
    postgres_dsn: str,
    migrator: PostgresGoldSchemaMigrator,
) -> None:
    migrator.migrate()
    offsets = (0, 1, 3, 8, 9, 14, 15, 20, 21, 27, 35, 36)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    timestamps = [start + timedelta(days=offset) for offset in offsets]
    bars_by_series: dict[
        str, list[tuple[float | None, float | None, float | None, float | None]]
    ] = {}
    for series_index, series in enumerate(_OHLC_SERIES):
        bars: list[tuple[float | None, float | None, float | None, float | None]] = []
        for index in range(len(offsets)):
            close = 100.0 + series_index * 7.0 + index * 10.0
            bars.append((close - 2.0, close + 5.0, close - 5.0, close))
        # Sparse dates are deliberate: overnight pairing follows source rows,
        # not calendar-day adjacency.
        bars[2] = (None, None, 115.0 + series_index * 7.0, None)
        bars[3] = (
            128.0 + series_index * 7.0,
            135.0 + series_index * 7.0,
            125.0 + series_index * 7.0,
            0.0,
        )
        bars[4] = (
            0.0,
            145.0 + series_index * 7.0,
            135.0 + series_index * 7.0,
            140.0 + series_index * 7.0,
        )
        bars[5] = (
            148.0 + series_index * 7.0,
            155.0 + series_index * 7.0,
            145.0 + series_index * 7.0,
            float("nan"),
        )
        bars[6] = (
            158.0 + series_index * 7.0,
            float("inf"),
            155.0 + series_index * 7.0,
            160.0 + series_index * 7.0,
        )
        bars[7] = (
            178.0 + series_index * 7.0,
            175.0 + series_index * 7.0,
            165.0 + series_index * 7.0,
            170.0 + series_index * 7.0,
        )
        bars[9] = (
            188.0 + series_index * 7.0,
            195.0 + series_index * 7.0,
            None,
            190.0 + series_index * 7.0,
        )
        bars[10] = (
            198.0 + series_index * 7.0,
            205.0 + series_index * 7.0,
            195.0 + series_index * 7.0,
            float("inf"),
        )
        bars_by_series[series] = bars

    raw_columns = POSTGRES_RAW_COLUMNS[1:]
    with psycopg.connect(postgres_dsn) as connection:
        for row_index, timestamp in enumerate(timestamps):
            values = {column: 40.0 for column in raw_columns}
            for _, series in enumerate(_OHLC_SERIES):
                open_value, high, low, close = bars_by_series[series][row_index]
                values[f"{series}_level"] = close if _valid_number(close) else 40.0
                values[f"{series}_open"] = open_value
                values[f"{series}_high"] = high
                values[f"{series}_low"] = low
                values[f"{series}_close"] = close
            _insert_feature_source_row(connection, timestamp, values)
        connection.execute("SELECT macro_loader.refresh_macro_features_explicit()")

    output_columns = tuple(
        f"{series}_{suffix}" for series in _OHLC_SERIES for suffix in _OHLC_BAR_SUFFIXES
    )
    selection = ", ".join(f'"{column}"' for column in output_columns)
    with psycopg.connect(postgres_dsn) as connection:
        rows = connection.execute(
            f"SELECT timestamp_m1, {selection} FROM macro_loader.macro_features "
            "ORDER BY timestamp_m1"
        ).fetchall()
    assert len(rows) == len(timestamps)
    actual_by_timestamp = {row[0]: row[1:] for row in rows}

    for row_index, timestamp in enumerate(timestamps):
        actual = actual_by_timestamp[timestamp]
        expected: list[float | None] = []
        for series in _OHLC_SERIES:
            for family in OHLC_BAR_FEATURE_FAMILIES:
                metrics = _bar_reference(bars_by_series[series], family)
                expected.extend(
                    (
                        metrics[row_index],
                        _rolling_reference(metrics, row_index, 5),
                    )
                )
        assert len(actual) == len(expected) == 36
        for column, observed, reference in zip(output_columns, actual, expected, strict=True):
            if reference is None:
                assert observed is None, f"{timestamp=} {column=} {observed=}"
            else:
                assert observed == pytest.approx(reference, rel=1e-12, abs=1e-12), (
                    f"{timestamp=} {column=} {observed=} {reference=}"
                )
                assert abs(observed) <= 1.0  # decimal fraction, not percent scale

    # Index 5 has a NaN close; index 6 must use index 4's last finite close.
    for series_index, series in enumerate(_OHLC_SERIES):
        index6_gap = actual_by_timestamp[timestamps[6]][series_index * len(_OHLC_BAR_SUFFIXES) + 4]
        open6 = bars_by_series[series][6][0]
        close4 = bars_by_series[series][4][3]
        assert index6_gap == pytest.approx((open6 - close4) / close4)

    # Future appends must not alter the already-published past feature values.
    last_timestamp = timestamps[-1]
    before = actual_by_timestamp[last_timestamp]
    future_timestamp = start + timedelta(days=40)
    future_values = {column: 40.0 for column in raw_columns}
    for series_index, series in enumerate(_OHLC_SERIES):
        close = 220.0 + 7.0 * series_index
        future_values[f"{series}_level"] = close
        future_values[f"{series}_open"] = close - 2.0
        future_values[f"{series}_high"] = close + 5.0
        future_values[f"{series}_low"] = close - 5.0
        future_values[f"{series}_close"] = close
    with psycopg.connect(postgres_dsn) as connection:
        _insert_feature_source_row(connection, future_timestamp, future_values)
        connection.execute("SELECT macro_loader.refresh_macro_features_explicit()")
        after = connection.execute(
            f"SELECT {selection} FROM macro_loader.macro_features WHERE timestamp_m1 = %s",
            (last_timestamp,),
        ).fetchone()
    assert after == before


def _stochastic_donchian_reference(
    bars: list[tuple[float, float, float, float]],
) -> list[tuple[float | None, ...]]:
    k5: list[float | None] = []
    k14: list[float | None] = []
    result: list[tuple[float | None, ...]] = []
    for index, (_, high, low, close) in enumerate(bars):
        values: list[float | None] = []
        for window, stream in ((5, k5), (14, k14)):
            sample = bars[max(0, index - window + 1) : index + 1]
            value: float | None = None
            if len(sample) == window:
                high_max = max(bar[1] for bar in sample)
                low_min = min(bar[2] for bar in sample)
                if high_max > low_min:
                    value = 100.0 * (close - low_min) / (high_max - low_min)
            stream.append(value)
            values.append(value)
        k5_tail = k5[index - 2 : index + 1]
        k14_tail = k14[index - 2 : index + 1]
        d5 = (
            sum(value for value in k5_tail if value is not None) / 3.0
            if index >= 2 and all(value is not None for value in k5_tail)
            else None
        )
        d14 = (
            sum(value for value in k14_tail if value is not None) / 3.0
            if index >= 2 and all(value is not None for value in k14_tail)
            else None
        )
        values.extend((d5, d14))
        values.append(values[0] - d5 if values[0] is not None and d5 is not None else None)
        lagged_k5 = k5[index - 3] if index >= 3 else None
        current_k5 = values[0]
        values.append(
            (current_k5 - lagged_k5) / 3.0
            if current_k5 is not None and lagged_k5 is not None
            else None
        )
        prior = bars[index - 20 : index] if index >= 20 else []
        values.append(high > max(bar[1] for bar in prior) if len(prior) == 20 else None)
        values.append(low < min(bar[2] for bar in prior) if len(prior) == 20 else None)
        result.append(
            (
                values[0],
                d5,
                values[1],
                d14,
                values[4],
                values[5],
                values[6],
                values[7],
            )
        )
    return result


@pytest.mark.integration
def test_disposable_postgres_stochastic_and_donchian_match_independent_reference(
    postgres_dsn: str,
    migrator: PostgresGoldSchemaMigrator,
) -> None:
    migrator.migrate()
    start = datetime(2026, 2, 1, tzinfo=UTC)
    timestamps = [start + timedelta(days=index * 2 + index // 3) for index in range(26)]
    output_suffixes = (*OHLC_STOCHASTIC_SUFFIXES, *OHLC_DONCHIAN_SUFFIXES)
    bars_by_series: dict[str, list[tuple[float, float, float, float] | None]] = {}
    for series_index, series in enumerate(_OHLC_SERIES):
        series_bars: list[tuple[float, float, float, float] | None] = []
        ordinal = 0
        for row_index in range(len(timestamps)):
            if row_index == 3:  # A calendar row with no complete OHLC source bar.
                series_bars.append(None)
                continue
            base = series_index * 10.0
            if ordinal < 5:
                low = high = close = base + 95.0
            else:
                low = base + 90.0 + ordinal * 0.5
                high = low + 10.0
                close = low + float((ordinal * 3) % 10 + 1)
            if ordinal == 20:
                high, low, close = base + 180.0, base + 130.0, base + 160.0
            elif ordinal == 21:
                high, low, close = base + 120.0, base + 10.0, base + 60.0
            elif ordinal == 22:
                high = max(bar[1] for bar in series_bars if bar is not None)
                low = min(bar[2] for bar in series_bars if bar is not None)
                close = (high + low) / 2.0
            open_value = close if ordinal < 5 else close - 1.0
            series_bars.append((open_value, high, low, close))
            ordinal += 1
        bars_by_series[series] = series_bars

    raw_columns = POSTGRES_RAW_COLUMNS[1:]
    with psycopg.connect(postgres_dsn) as connection:
        for row_index, timestamp in enumerate(timestamps):
            values: dict[str, float | None] = {column: 40.0 for column in raw_columns}
            for series in _OHLC_SERIES:
                bar = bars_by_series[series][row_index]
                values[f"{series}_level"] = bar[3] if bar is not None else 40.0
                for field, value in zip(_OHLC_FIELDS, bar or (None, None, None, None), strict=True):
                    values[f"{series}_{field}"] = value
            _insert_feature_source_row(connection, timestamp, values)
        connection.execute("SELECT macro_loader.refresh_macro_features_explicit()")

    output_columns = tuple(
        f"{series}_{suffix}" for series in _OHLC_SERIES for suffix in output_suffixes
    )
    selection = ", ".join(f'"{column}"' for column in output_columns)
    with psycopg.connect(postgres_dsn) as connection:
        rows = connection.execute(
            f"SELECT timestamp_m1, {selection} FROM macro_loader.macro_features "
            "ORDER BY timestamp_m1"
        ).fetchall()
    actual_by_timestamp = {row[0]: row[1:] for row in rows}
    assert len(actual_by_timestamp) == len(timestamps)

    for row_index, timestamp in enumerate(timestamps):
        actual = actual_by_timestamp[timestamp]
        expected: list[object] = []
        for series in _OHLC_SERIES:
            valid_bars = [bar for bar in bars_by_series[series][: row_index + 1] if bar is not None]
            if not valid_bars or bars_by_series[series][row_index] is None:
                expected.extend((None,) * len(output_suffixes))
            else:
                expected.extend(_stochastic_donchian_reference(valid_bars)[-1])
        assert len(actual) == len(expected) == 48
        for column, observed, reference in zip(output_columns, actual, expected, strict=True):
            if reference is None:
                assert observed is None, f"{timestamp=} {column=} {observed=}"
            elif column.endswith(OHLC_DONCHIAN_SUFFIXES):
                assert observed is reference, f"{timestamp=} {column=} {observed=} {reference=}"
            else:
                assert isinstance(observed, float)
                assert observed == pytest.approx(reference, rel=1e-12, abs=1e-12), (
                    f"{timestamp=} {column=} {observed=} {reference=}"
                )

    for series_index, series in enumerate(_OHLC_SERIES):
        row_by_ordinal = [
            row_index for row_index, bar in enumerate(bars_by_series[series]) if bar is not None
        ]
        first_flat_window = actual_by_timestamp[timestamps[row_by_ordinal[4]]]
        assert first_flat_window[series_index * len(output_suffixes)] is None
        first_k5 = actual_by_timestamp[timestamps[row_by_ordinal[5]]]
        assert first_k5[series_index * len(output_suffixes)] is not None
        first_d5 = actual_by_timestamp[timestamps[row_by_ordinal[7]]]
        assert first_d5[series_index * len(output_suffixes) + 1] is not None
        first_d14 = actual_by_timestamp[timestamps[row_by_ordinal[15]]]
        assert first_d14[series_index * len(output_suffixes) + 3] is not None
        breakout_up = actual_by_timestamp[timestamps[row_by_ordinal[20]]]
        breakout_down = actual_by_timestamp[timestamps[row_by_ordinal[21]]]
        equal_channel = actual_by_timestamp[timestamps[row_by_ordinal[22]]]
        series_offset = series_index * len(output_suffixes)
        assert breakout_up[series_offset + 6] is True
        assert breakout_down[series_offset + 7] is True
        assert equal_channel[series_offset + 6] is False
        assert equal_channel[series_offset + 7] is False

    last_timestamp = timestamps[-1]
    before_future = actual_by_timestamp[last_timestamp]
    future_timestamp = last_timestamp + timedelta(days=14)
    future_values: dict[str, float | None] = {column: 40.0 for column in raw_columns}
    for series_index, series in enumerate(_OHLC_SERIES):
        close = 400.0 + series_index * 10.0
        future_values[f"{series}_level"] = close
        future_values[f"{series}_open"] = close - 1.0
        future_values[f"{series}_high"] = close + 5.0
        future_values[f"{series}_low"] = close - 5.0
        future_values[f"{series}_close"] = close
    with psycopg.connect(postgres_dsn) as connection:
        _insert_feature_source_row(connection, future_timestamp, future_values)
        connection.execute("SELECT macro_loader.refresh_macro_features_explicit()")
        after_future = connection.execute(
            f"SELECT {selection} FROM macro_loader.macro_features WHERE timestamp_m1 = %s",
            (last_timestamp,),
        ).fetchone()
    assert after_future == before_future


def test_extended_view_publishes_all_added_series_and_causal_transformations(
    postgres_dsn: str,
    migrator: PostgresGoldSchemaMigrator,
) -> None:
    from application.macro_feature_catalog import EXTENDED_RAW_SERIES

    migrator.migrate()
    origin = datetime(2025, 1, 1, tzinfo=UTC)
    timestamps = [origin + timedelta(days=index) for index in range(70)]
    with psycopg.connect(postgres_dsn) as connection:
        _assert_extended_view_contract(connection)
        assert connection.execute(
            "SELECT count(*) FROM pg_matviews WHERE schemaname = 'macro_loader' "
            "AND matviewname LIKE 'macro_feature_transform_%'"
        ).fetchone()[0] == len(EXTENDED_RAW_SERIES)
        for relation in (
            "macro_feature_raw_projection",
            "macro_feature_transform_vstoxx1m",
            "macro_feature_transform_us_5y",
        ):
            assert connection.execute(
                "SELECT has_table_privilege('macro-loader', %s, 'SELECT'), "
                "has_table_privilege('macro-loader-sync', %s, 'SELECT')",
                (f"macro_loader.{relation}", f"macro_loader.{relation}"),
            ).fetchone() == (False, False)
        with connection.cursor() as cursor:
            for series in EXTENDED_RAW_SERIES:
                cursor.executemany(
                    f'INSERT INTO macro_loader."macro_raw_{series}" '
                    '("timestamp_m1", "indexvalue") VALUES (%s, %s)',
                    ((timestamp, 100.0 + index) for index, timestamp in enumerate(timestamps)),
                )
            cursor.executemany(
                'INSERT INTO macro_loader."macro_raw_vix" '
                '("timestamp_m1", "open", "high", "low", "close") '
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    (timestamp, 99.0 + index, 102.0 + index, 98.0 + index, 100.0 + index)
                    for index, timestamp in enumerate(timestamps)
                ),
            )
        connection.execute("SELECT macro_loader.refresh_macro_features_explicit()")
        main_rows = connection.execute(
            "SELECT timestamp_m1, vix_log_return_20obs, vix_return_geom_5obs_pct, "
            "available_at_utc FROM macro_loader.macro_features "
            "WHERE timestamp_m1 IN (%s, %s) ORDER BY timestamp_m1",
            (timestamps[19], timestamps[60]),
        ).fetchall()
        main_keys = connection.execute(
            "SELECT timestamp_m1, available_at_utc FROM macro_loader.macro_features "
            "ORDER BY timestamp_m1"
        ).fetchall()
        extended_keys = connection.execute(
            "SELECT timestamp_m1, available_at_utc FROM macro_loader.macro_features_extended "
            "ORDER BY timestamp_m1"
        ).fetchall()
        rows = connection.execute(
            "SELECT timestamp_m1, vstoxx1m_log_return_20obs, "
            "vstoxx1m_return_geom_5obs_pct, vstoxx1m_return_mean_5obs, "
            "vstoxx1m_volatility_5obs, vstoxx1m_sma_ratio_5_20, "
            "vstoxx1m_rsi_14obs, vstoxx1m_roc_20obs, vstoxx1m_drawdown_20obs, "
            "vstoxx1m_ema5_distance, vstoxx1m_macd_hist_relative, available_at_utc "
            "FROM macro_loader.macro_features_extended "
            "WHERE timestamp_m1 IN (%s, %s) ORDER BY timestamp_m1",
            (timestamps[19], timestamps[60]),
        ).fetchall()
        before_future = connection.execute(
            "SELECT vstoxx1m_log_return_20obs, vstoxx1m_macd_hist_relative "
            "FROM macro_loader.macro_features_extended WHERE timestamp_m1 = %s",
            (timestamps[-1],),
        ).fetchone()
        future_timestamp = timestamps[-1] + timedelta(days=1)
        connection.execute(
            'INSERT INTO macro_loader."macro_raw_vstoxx1m" '
            '("timestamp_m1", "indexvalue") VALUES (%s, %s)',
            (future_timestamp, 9999.0),
        )
        connection.execute("SELECT macro_loader.refresh_macro_features_explicit()")
        after_future = connection.execute(
            "SELECT vstoxx1m_log_return_20obs, vstoxx1m_macd_hist_relative "
            "FROM macro_loader.macro_features_extended WHERE timestamp_m1 = %s",
            (timestamps[-1],),
        ).fetchone()

    assert rows[0][1] is None
    assert rows[0][2] is not None
    assert rows[0][3] is not None
    assert rows[0][4] is not None
    assert rows[0][5] is not None
    assert rows[0][6] is not None
    assert rows[0][7] is None
    assert rows[0][8] is not None
    assert rows[0][9] is not None
    assert rows[0][10] is None
    assert rows[0][11] is not None
    assert main_rows[0][1] is None
    assert main_rows[0][2] is not None
    assert main_rows[1][1] == pytest.approx(math.log(160.0 / 140.0))
    assert main_rows[1][2] == pytest.approx(160.0 / 155.0 - 1.0)
    assert len(main_keys) == len(extended_keys) == 70
    assert main_keys == extended_keys
    final = rows[1]
    assert final[1] == pytest.approx(math.log(160.0 / 140.0))
    assert final[2] == pytest.approx(160.0 / 155.0 - 1.0)
    assert final[3] == pytest.approx(math.log(160.0 / 155.0) / 5.0)
    returns = [math.log(value / (value - 1.0)) for value in range(156, 161)]
    mean_return = sum(returns) / len(returns)
    sample_volatility = math.sqrt(
        sum((value - mean_return) ** 2 for value in returns) / (len(returns) - 1)
    )
    assert final[4] == pytest.approx(sample_volatility)
    assert final[5] == pytest.approx(158.0 / 150.5)
    assert final[6] == pytest.approx(100.0)
    assert final[7] == pytest.approx(160.0 / 140.0 - 1.0)
    assert final[8] == pytest.approx(0.0)
    assert final[9] is not None
    assert final[10] is not None
    assert final[11] is not None
    assert before_future == after_future


def test_postgres_migration_32_upgrades_existing_v31_view_contract(
    postgres_dsn: str,
    migrator: PostgresGoldSchemaMigrator,
) -> None:
    from application.macro_feature_catalog import EXTENDED_RAW_SERIES

    migrator.migrate()
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        main_view_oid = connection.execute(
            "SELECT 'macro_loader.macro_features'::regclass::oid"
        ).fetchone()[0]
        connection.execute("DROP MATERIALIZED VIEW macro_loader.macro_features_extended")
        for series in EXTENDED_RAW_SERIES:
            connection.execute(
                f'DROP MATERIALIZED VIEW macro_loader."macro_feature_transform_{series}"'
            )
        connection.execute("DROP MATERIALIZED VIEW macro_loader.macro_feature_raw_projection")
        connection.execute("DELETE FROM macro_loader_sync.schema_migrations WHERE version = 32")

    migrator.migrate()
    with psycopg.connect(postgres_dsn) as connection:
        _assert_extended_view_contract(connection)
        assert (
            connection.execute("SELECT 'macro_loader.macro_features'::regclass::oid").fetchone()[0]
            == main_view_oid
        )
        assert connection.execute(
            "SELECT count(*) FROM pg_matviews WHERE schemaname = 'macro_loader' "
            "AND matviewname LIKE 'macro_feature_transform_%'"
        ).fetchone()[0] == len(EXTENDED_RAW_SERIES)

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
    FEATURE_COLUMNS,
    MACRO_FEATURE_VIEW_FINGERPRINT,
    MACRO_FEATURE_VIEW_VERSION,
    OHLC_BAR_FEATURE_FAMILIES,
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
        if spec.family not in OHLC_BAR_FEATURE_FAMILIES
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
    assert not any(marker in name for name in names for marker in ("stoch_", "donchian_"))
    raw_names = tuple(str(row[0]) for row in _column_rows(connection, "macro_loader.macro_raw"))
    assert not set(expected_derived).intersection(raw_names)
    assert not set(expected_derived).intersection(_expected_gold_columns())


@pytest.mark.parametrize("migration_mode", ("clean_create", "upgrade_25_to_26"))
def test_disposable_postgres_ohlc_schema_migration_and_view_parity(
    postgres_dsn: str,
    migrator: PostgresGoldSchemaMigrator,
    sync_repository: PostgresGoldSyncRepository,
    monkeypatch: pytest.MonkeyPatch,
    migration_mode: str,
) -> None:
    expected_migrations = len(postgres_module._MIGRATIONS)
    assert GOLD_SCHEMA_VERSION == 8
    assert MACRO_FEATURE_VIEW_VERSION == 15
    assert len(MACRO_FEATURE_VIEW_FINGERPRINT) == 64
    assert _expected_gold_columns() == POSTGRES_RAW_COLUMNS
    assert _OHLC_SERIES == ("vix", "vix9d", "vix3m", "vix6m", "vix1y", "move")
    assert len(_OHLC_COLUMNS) == 24

    if migration_mode == "upgrade_25_to_26":
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
                *(f"{series_id}_level" for series_id in SERIES_REGISTRY),
                "available_at_utc",
                *_OHLC_COLUMNS,
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
            f"macro feature view version=15; fingerprint={MACRO_FEATURE_VIEW_FINGERPRINT}",
        )

        if migration_mode == "upgrade_25_to_26":
            legacy_timestamp = datetime(2026, 1, 2, tzinfo=UTC)
            expected_available = datetime(2026, 1, 5, tzinfo=UTC)
            preserved_raw = connection.execute(
                "SELECT vix_level, available_at_utc FROM macro_loader.macro_raw "
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
    quoted_columns = ", ".join(f'"{column}"' for column in ("timestamp_m1", *raw_columns))
    placeholders = ", ".join("%s" for _ in range(len(raw_columns) + 1))
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
            connection.execute(
                f"INSERT INTO macro_loader.macro_raw ({quoted_columns}) VALUES ({placeholders})",
                (timestamp, *(values[column] for column in raw_columns)),
            )
        connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")

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
        connection.execute(
            f"INSERT INTO macro_loader.macro_raw ({quoted_columns}) VALUES ({placeholders})",
            (future_timestamp, *(future_values[column] for column in raw_columns)),
        )
        connection.execute("REFRESH MATERIALIZED VIEW macro_loader.macro_features")
        after = connection.execute(
            f"SELECT {selection} FROM macro_loader.macro_features WHERE timestamp_m1 = %s",
            (last_timestamp,),
        ).fetchone()
    assert after == before

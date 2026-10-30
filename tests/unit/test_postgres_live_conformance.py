from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from application.macro_feature_catalog import (
    FEATURE_VALUE_CATALOG_FINGERPRINT,
    FEATURE_VALUE_SCHEMA_VERSION,
)
from application.postgres_conformance import PostgresDatabaseConformanceEvidence
from application.postgres_sync import POSTGRES_CONSUMER_SCHEMA, POSTGRES_SYNC_SCHEMA
from ingestion.postgres_conformance_verifier import (
    _FEATURE_VALUE_CATALOG_DRIFT_SQL,
    PostgresLiveDatabaseConformanceInspector,
)
from ingestion.postgres_gold_repository import (
    _FEATURE_VALUE_POPULATION_QUERIES,
    _POSTGRES_OWNER_ROLE,
    _SCHEMA_SPECIFICATION,
    POSTGRES_HOST,
    POSTGRES_PORT,
    POSTGRES_USER,
    PostgresGoldRepositoryError,
    PostgresSyncConfig,
)
from scripts.provision_postgres_role import POSTGRES_OWNER_ROLE, POSTGRES_ROLE, POSTGRES_SYNC_ROLE


class FakeCursor:
    def __init__(self, *, timezone: str = "UTC") -> None:
        self.timezone = timezone
        self.queries: list[str] = []
        self._one: tuple[object, ...] | None = None
        self._many: list[tuple[object, ...]] = []

    @property
    def rowcount(self) -> int:
        return 1

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> FakeCursor:
        self.queries.append(query)
        if "FROM information_schema.tables" in query:
            self._many = sorted((table.schema, table.name) for table in _SCHEMA_SPECIFICATION)
        elif "FROM information_schema.columns" in query and "table_name = %s" not in query:
            self._many = [
                (
                    table.schema,
                    table.name,
                    ordinal,
                    column.name,
                    column.data_type,
                    column.precision if column.data_type == "timestamp with time zone" else None,
                    column.precision if column.data_type == "character" else None,
                    "YES" if column.nullable else "NO",
                )
                for table in _SCHEMA_SPECIFICATION
                for ordinal, column in enumerate(table.columns, start=1)
            ]
        elif "generation_expression" in query:
            self._one = (
                "ALWAYS",
                "timezone('UTC'::text, timestamp_m1)::date + CASE "
                "EXTRACT(isodow FROM timezone('UTC'::text, timestamp_m1))::integer "
                "WHEN 5 THEN 3 WHEN 6 THEN 2 WHEN 7 THEN 1 ELSE 1 END",
            )
        elif "FROM pg_constraint" in query:
            self._many = [
                (table.schema, table.name, "p", table.primary_key)
                for table in sorted(
                    _SCHEMA_SPECIFICATION, key=lambda item: (item.schema, item.name)
                )
            ]
        elif "FROM pg_roles" in query:
            self._many = [
                (POSTGRES_OWNER_ROLE, False, False, False, False, False, False),
                (POSTGRES_ROLE, True, False, False, False, False, False),
                (POSTGRES_SYNC_ROLE, True, False, False, False, False, False),
            ]
        elif "FROM pg_namespace AS namespaces" in query and "classes" not in query:
            self._many = [
                (schema, POSTGRES_OWNER_ROLE)
                for schema in sorted((POSTGRES_CONSUMER_SCHEMA, POSTGRES_SYNC_SCHEMA))
            ]
        elif "obj_description(classes.oid" in query and params == (
            POSTGRES_CONSUMER_SCHEMA,
            "macro_features",
        ):
            self._one = (
                "m",
                _POSTGRES_OWNER_ROLE,
                f"macro feature view version={FEATURE_VALUE_SCHEMA_VERSION}; "
                f"fingerprint={FEATURE_VALUE_CATALOG_FINGERPRINT}",
            )
        elif (
            "FROM pg_attribute AS attributes" in query
            and isinstance(params, tuple)
            and str(params[0]).endswith(".macro_features")
        ):
            self._many = [
                (1, "timestamp_m1", "timestamp(6) with time zone"),
                (2, "available_at_utc", "timestamp(6) with time zone"),
                (3, "feature_name", "text"),
                (4, "value", "double precision"),
            ]
        elif "SELECT relname FROM pg_class AS relations" in query:
            self._many = []
        elif "SELECT pg_get_viewdef(classes.oid, true)" in query:
            from ingestion.postgres_gold_repository import _FEATURE_VALUES_DDL

            self._one = (_FEATURE_VALUES_DDL,)
        elif "FROM pg_indexes" in query:
            unique = "timestamp_feature" in query
            index_name = (
                "macro_features_timestamp_feature_uidx"
                if unique
                else "macro_features_feature_timestamp_idx"
            )
            index_columns = (
                "(timestamp_m1, feature_name)" if unique else "(feature_name, timestamp_m1)"
            )
            unique_sql = "UNIQUE " if unique else ""
            self._one = (
                f"CREATE {unique_sql}INDEX {index_name} "
                f"ON macro_loader.macro_features USING btree {index_columns}",
            )
        elif query == _FEATURE_VALUE_CATALOG_DRIFT_SQL:
            self._one = (0,)
        elif query.startswith("WITH selected_names AS MATERIALIZED"):
            assert params is not None and isinstance(params[0], list)
            feature_count = len(params[0])
            self._one = (
                feature_count,
                feature_count,
                feature_count,
                feature_count,
                0,
                0,
                0,
                0,
                None,
            )
        elif "FROM pg_class AS classes" in query:
            self._many = [
                (table.schema, table.name, POSTGRES_OWNER_ROLE)
                for table in sorted(
                    _SCHEMA_SPECIFICATION, key=lambda item: (item.schema, item.name)
                )
            ]
        elif "has_schema_privilege" in query:
            self._one = (True, False)
        elif "has_table_privilege" in query:
            assert params is not None
            if "macro_features" in str(params) and "'SELECT')" not in query:
                self._one = (False,)
                return self
            if "'SELECT')" in query and "'INSERT')" not in query:
                self._one = (params[0] != "public",)
                return self
            has_dml = (
                params[0] == POSTGRES_SYNC_ROLE
                and "schema_migrations" not in str(params[1])
                and "macro_features" not in str(params[1])
            )
            self._one = (True, has_dml)
            self._one = (self._one[0], self._one[1], self._one[1], self._one[1])
        elif query == "SHOW TIME ZONE":
            self._one = (self.timezone,)
        elif query.startswith("SHOW "):
            settings: dict[str, object] = {
                "SHOW application_name": "macro-loader",
                "SHOW lock_timeout": "5s",
                "SHOW statement_timeout": "300s",
                "SHOW idle_in_transaction_session_timeout": "30s",
                "SHOW jit": "off",
            }
            self._one = (settings[query],)
        elif query.startswith("SELECT %s::TIMESTAMPTZ(6)"):
            assert params is not None
            self._one = (params[0],)
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._many

    def close(self) -> None:
        return None


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        raise AssertionError("live conformance inspection must not commit")

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _config() -> PostgresSyncConfig:
    return PostgresSyncConfig(POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, "macro_loader", "secret")


def _fake_connection_factory(
    *, timezone: str = "UTC"
) -> tuple[Callable[[PostgresSyncConfig], FakeConnection], list[FakeConnection]]:
    connections: list[FakeConnection] = []

    def create_connection(_: PostgresSyncConfig) -> FakeConnection:
        if connections:
            assert connections[0].closed, "schema transaction must close before population scans"
        connection = FakeConnection(FakeCursor(timezone=timezone))
        connections.append(connection)
        return connection

    return create_connection, connections


def test_population_verification_partitions_the_complete_catalog_into_source_groups() -> None:
    assert len(_FEATURE_VALUE_POPULATION_QUERIES) == 26
    covered_names = tuple(
        name for _, names, _ in _FEATURE_VALUE_POPULATION_QUERIES for name in names
    )
    assert len(covered_names) == 1720
    assert len(set(covered_names)) == len(covered_names)
    for group_name, _, query in _FEATURE_VALUE_POPULATION_QUERIES:
        assert "source_values AS MATERIALIZED" in query
        assert "feature_count_differences" in query
        assert "jsonb_typeof(value) = 'boolean'" in query
        assert "jsonb_typeof(value) = 'number'" in query
        assert "invalid_source AS" in query
        if group_name == "cross_series":
            assert "WITH RECURSIVE" not in query
            assert all(
                f'"macro_loader"."macro_raw_{series}"' in query for series in _RAW_TABLE_SERIES
            )
        else:
            assert f'"macro_loader"."macro_raw_{group_name}"' in query
            assert not any(
                f'"macro_loader"."macro_raw_{series}"' in query
                for series in _RAW_TABLE_SERIES
                if series != group_name
            )


_RAW_TABLE_SERIES = tuple(
    group_name
    for group_name, _, _ in _FEATURE_VALUE_POPULATION_QUERIES
    if group_name != "cross_series"
)


def test_live_inspector_independently_checks_schema_roles_session_and_temporal_probes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection_factory, connections = _fake_connection_factory()
    inspector = PostgresLiveDatabaseConformanceInspector(
        _config(), connection_factory=connection_factory
    )

    with caplog.at_level(logging.DEBUG, logger="ingestion.postgres_conformance_verifier"):
        evidence = inspector.inspect()

    assert evidence == PostgresDatabaseConformanceEvidence(28, 3, 2, 1720)
    assert "postgres live conformance inspection started" in caplog.text
    assert (
        "schema_table_count=28 feature_value_row_count=1720 temporal_probe_count=2" in caplog.text
    )
    assert "secret" not in caplog.text
    assert connections[0].rollbacks == 1
    assert all(connection.closed for connection in connections)
    final_cursor = connections[-1].cursor()
    assert final_cursor.queries.count("SELECT %s::TIMESTAMPTZ(6)") == 2
    worker_queries = [
        query
        for connection in connections[1:]
        for query in connection.cursor().queries
        if query.startswith("WITH selected_names AS MATERIALIZED")
    ]
    assert len(worker_queries) == len(_FEATURE_VALUE_POPULATION_QUERIES)


def test_live_inspector_fails_closed_and_rolls_back_when_session_timezone_drifts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection_factory, connections = _fake_connection_factory(timezone="Europe/Berlin")
    inspector = PostgresLiveDatabaseConformanceInspector(
        _config(), connection_factory=connection_factory
    )

    with (
        caplog.at_level(logging.DEBUG, logger="ingestion.postgres_conformance_verifier"),
        pytest.raises(PostgresGoldRepositoryError, match="live conformance inspection failed"),
    ):
        inspector.inspect()
    assert connections[0].rollbacks == 1
    assert all(connection.closed for connection in connections)
    assert all(connection.rollbacks == 1 for connection in connections)
    assert "error_type=ValueError" in caplog.text
    assert "session timezone differs" not in caplog.text
    assert "secret" not in caplog.text


def test_live_inspector_rejects_feature_names_outside_the_catalog() -> None:
    class DriftCursor(FakeCursor):
        def execute(self, query: str, params: tuple[object, ...] | None = None) -> DriftCursor:
            result = super().execute(query, params)
            if query == _FEATURE_VALUE_CATALOG_DRIFT_SQL:
                self._one = (1,)
            return result

    cursor = DriftCursor()
    inspector = PostgresLiveDatabaseConformanceInspector(_config())

    with pytest.raises(ValueError, match="unknown feature names"):
        inspector._assert_feature_value_catalog(cursor)


def test_live_inspector_rejects_duplicate_primary_key_metadata() -> None:
    class DuplicateKeyCursor(FakeCursor):
        def execute(
            self, query: str, params: tuple[object, ...] | None = None
        ) -> DuplicateKeyCursor:
            result = super().execute(query, params)
            if "FROM pg_constraint" in query:
                self._many.append(self._many[0])
            return result

    inspector = PostgresLiveDatabaseConformanceInspector(
        _config(), connection_factory=lambda _: FakeConnection(DuplicateKeyCursor())
    )

    with pytest.raises(PostgresGoldRepositoryError, match="live conformance inspection failed"):
        inspector.inspect()
    assert not inspector._setting_matches("unexpected", 5_000)


def test_live_inspector_closes_every_connection_when_a_population_group_fails() -> None:
    class FailingWorkerCursor(FakeCursor):
        def execute(
            self, query: str, params: tuple[object, ...] | None = None
        ) -> FailingWorkerCursor:
            if query.startswith("WITH selected_names AS MATERIALIZED"):
                raise RuntimeError("private worker failure")
            return super().execute(query, params)

    connections: list[FakeConnection] = []

    def factory(_: PostgresSyncConfig) -> FakeConnection:
        if connections:
            assert connections[0].closed
        connection = FakeConnection(FailingWorkerCursor())
        connections.append(connection)
        return connection

    inspector = PostgresLiveDatabaseConformanceInspector(_config(), connection_factory=factory)

    with pytest.raises(PostgresGoldRepositoryError, match="live conformance inspection failed"):
        inspector.inspect()

    assert len(connections) > 1
    assert all(connection.closed for connection in connections)
    assert all(connection.rollbacks == 1 for connection in connections)

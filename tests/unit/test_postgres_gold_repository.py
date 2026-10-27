from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

import ingestion.postgres_gold_repository as module
from application.postgres_sync import (
    POSTGRES_DATASET_ID,
    POSTGRES_RAW_COLUMNS,
    GoldDeltaPlan,
    GoldRowDigest,
    GoldRowPayload,
    GoldSyncState,
    GoldSyncTransaction,
    GoldTargetSummary,
)
from ingestion.postgres_gold_repository import (
    POSTGRES_HOST,
    POSTGRES_PORT,
    POSTGRES_USER,
    PostgresAdminConfig,
    PostgresGoldRepositoryError,
    PostgresGoldSchemaMigrator,
    PostgresGoldSchemaReconstructor,
    PostgresGoldSyncRepository,
    PostgresLockContentionError,
    PostgresSyncConfig,
    PostgresTimeoutPolicy,
)


def _ts(day: int) -> datetime:
    return datetime(2026, 8, day, tzinfo=UTC)


def _config(password: str = "repo-secret") -> PostgresSyncConfig:
    return PostgresSyncConfig(POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, "macro_loader", password)


def _admin_config(password: str = "admin-secret") -> PostgresAdminConfig:
    return PostgresAdminConfig(
        POSTGRES_HOST, POSTGRES_PORT, "macro-loader-admin", "macro_loader", password
    )


def _row(day: int, value: float) -> GoldRowPayload:
    return GoldRowPayload(_ts(day), tuple(value for _ in POSTGRES_RAW_COLUMNS[1:]))


def _state(
    *, count: int = 2, minimum: datetime | None = None, maximum: datetime | None = None
) -> GoldSyncState:
    if minimum is None and count:
        minimum = _ts(1)
    if maximum is None and count:
        maximum = _ts(2)
    return GoldSyncState(
        dataset_id=POSTGRES_DATASET_ID,
        source_build_id="20260822T100000Z",
        data_sha256="a" * 64,
        schema_version=1,
        feature_version=1,
        row_count=count,
        min_timestamp=minimum,
        max_timestamp=maximum,
        synced_at_utc=_ts(22),
    )


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.one: tuple[object, ...] | None = None
        self.many: list[tuple[object, ...]] = []
        self._rowcount = 1

    @property
    def rowcount(self) -> int:
        return self._rowcount

    def execute(self, query: str, params: object = None) -> object:
        self.connection.events.append(("execute", query, params))
        self._rowcount = self.connection.rowcounts.get(query, 1)
        if self.connection.fail_on and self.connection.fail_on in query:
            raise RuntimeError("database failure repo-secret")
        if query.startswith("SELECT to_regclass"):
            self.one = (None, None)
        elif query.startswith("SELECT version"):
            self.many = []
        elif "FROM information_schema.tables" in query:
            self.many = sorted(
                (specification.schema, specification.name)
                for specification in module._SCHEMA_SPECIFICATION
            )
        elif "FROM information_schema.columns" in query and "table_name = %s" not in query:
            self.many = [
                (
                    specification.schema,
                    specification.name,
                    ordinal,
                    column.name,
                    column.data_type,
                    column.precision if column.data_type == "timestamp with time zone" else None,
                    column.precision if column.data_type == "character" else None,
                    "YES" if column.nullable else "NO",
                )
                for specification in module._SCHEMA_SPECIFICATION
                for ordinal, column in enumerate(specification.columns, start=1)
            ]
        elif "generation_expression" in query:
            self.one = (
                "ALWAYS",
                "timezone('UTC'::text, timestamp_m1)::date + CASE "
                "EXTRACT(isodow FROM timezone('UTC'::text, timestamp_m1))::integer "
                "WHEN 5 THEN 3 WHEN 6 THEN 2 WHEN 7 THEN 1 ELSE 1 END",
            )
        elif "FROM pg_constraint" in query:
            self.many = [
                (
                    specification.schema,
                    specification.name,
                    "p",
                    specification.primary_key,
                )
                for specification in module._SCHEMA_SPECIFICATION
            ]
        elif "FROM pg_class AS classes" in query:
            if "SELECT pg_get_viewdef(classes.oid, true)" in query:
                self.one = (module._FEATURE_VALUES_DDL,)
            elif isinstance(params, tuple) and params[1] == module.POSTGRES_FEATURE_VALUES_TABLE:
                self.one = (
                    "m",
                    module._POSTGRES_OWNER_ROLE,
                    "macro feature values schema "
                    f"version={module.FEATURE_VALUE_SCHEMA_VERSION}; "
                    f"fingerprint={module.FEATURE_VALUE_CATALOG_FINGERPRINT}",
                )
            elif isinstance(params, tuple) and params[1] == "macro_features_extended":
                self.one = (
                    "m",
                    module._POSTGRES_OWNER_ROLE,
                    f"macro feature view version={module.EXTENDED_MACRO_FEATURE_VIEW_VERSION}; "
                    f"fingerprint={module.EXTENDED_MACRO_FEATURE_VIEW_FINGERPRINT}",
                    module._EXTENDED_FEATURES_VIEW_DEFINITION,
                )
            else:
                self.one = (
                    "m",
                    module._POSTGRES_OWNER_ROLE,
                    f"macro feature view version={module.MACRO_FEATURE_VIEW_VERSION}; "
                    f"fingerprint={module.MACRO_FEATURE_VIEW_FINGERPRINT}",
                    module._FEATURES_VIEW_DEFINITION,
                )
        elif (
            "FROM pg_attribute AS attributes" in query
            and isinstance(params, tuple)
            and str(params[0]).endswith(".macro_feature_values")
        ):
            self.many = [
                (1, "timestamp_m1", "timestamp(6) with time zone"),
                (2, "available_at_utc", "timestamp(6) with time zone"),
                (3, "feature_name", "text"),
                (4, "value", "double precision"),
            ]
        elif "FROM pg_indexes" in query:
            unique = "timestamp_feature" in query
            index_name = (
                "macro_feature_values_timestamp_feature_uidx"
                if unique
                else "macro_feature_values_feature_timestamp_idx"
            )
            index_columns = (
                "(timestamp_m1, feature_name)" if unique else "(feature_name, timestamp_m1)"
            )
            unique_sql = "UNIQUE " if unique else ""
            self.one = (
                f"CREATE {unique_sql}INDEX {index_name} "
                f"ON macro_loader.macro_feature_values USING btree {index_columns}",
            )
        elif "FROM pg_attribute AS attributes" in query:
            extended_view = isinstance(params, tuple) and str(params[0]).endswith(
                ".macro_features_extended"
            )
            feature_values_table = isinstance(params, tuple) and str(params[0]).endswith(
                ".macro_feature_values"
            )
            if feature_values_table:
                specification = next(
                    item
                    for item in module._SCHEMA_SPECIFICATION
                    if item.name == module.POSTGRES_FEATURE_VALUES_TABLE
                )
                self.many = [
                    (
                        ordinal,
                        column.name,
                        (
                            "timestamp(6) with time zone"
                            if column.data_type == "timestamp with time zone"
                            else column.data_type
                        ),
                        False,
                    )
                    for ordinal, column in enumerate(specification.columns, start=1)
                ]
                return self
            expected_names = (
                module._EXTENDED_FEATURES_VIEW_COLUMNS_EXPECTED
                if extended_view
                else module._FEATURES_VIEW_COLUMNS_EXPECTED
            )
            expected_types = (
                module._EXTENDED_FEATURES_VIEW_COLUMN_TYPES_EXPECTED
                if extended_view
                else module._FEATURES_VIEW_COLUMN_TYPES_EXPECTED
            )
            expected_columns = [
                (
                    ordinal,
                    name,
                    expected_types[name],
                    False,
                )
                for ordinal, name in enumerate(expected_names, start=1)
            ]
            self.many = (
                list(self.connection.extended_view_columns)
                if extended_view and self.connection.extended_view_columns is not None
                else expected_columns
            )
        elif "has_table_privilege" in query:
            if "'SELECT'" in query:
                self.one = (params[0] != "public",) if isinstance(params, tuple) else (True,)
            else:
                self.one = (False,)
        elif query.startswith("SELECT dataset_id"):
            self.one = self.connection.state_row
        elif query.startswith("SELECT timestamp_m1, row_sha256"):
            self.many = list(self.connection.digest_rows)
        elif query.startswith('SELECT "timestamp_m1"'):
            self.many = list(self.connection.consumer_rows)
        elif query.startswith("SELECT COUNT"):
            self.one = self.connection.summary_row
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return self.one

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.many

    def close(self) -> None:
        self.connection.events.append(("cursor-close", None, None))


class FakeConnection:
    def __init__(
        self,
        *,
        state_row: tuple[object, ...] | None = None,
        digest_rows: tuple[tuple[object, ...], ...] = (),
        consumer_rows: tuple[tuple[object, ...], ...] = (),
        rowcounts: dict[str, int] | None = None,
        summary_row: tuple[object, ...] = (0, None, None),
        fail_on: str | None = None,
        extended_view_columns: tuple[tuple[object, ...], ...] | None = None,
    ) -> None:
        self.state_row = state_row
        self.digest_rows = digest_rows
        self.consumer_rows = consumer_rows
        self.rowcounts = {} if rowcounts is None else rowcounts
        self.summary_row = summary_row
        self.fail_on = fail_on
        self.extended_view_columns = extended_view_columns
        self.events: list[tuple[str, object, object]] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.events.append(("commit", None, None))

    def rollback(self) -> None:
        self.events.append(("rollback", None, None))

    def close(self) -> None:
        self.events.append(("close", None, None))


class Factory:
    def __init__(self, *connections: FakeConnection) -> None:
        self.connections = list(connections)
        self.configs: list[PostgresSyncConfig] = []

    def __call__(self, config: PostgresSyncConfig) -> FakeConnection:
        self.configs.append(config)
        if not self.connections:
            raise RuntimeError("no fake connection")
        return self.connections.pop(0)


def _execute_queries(connection: FakeConnection) -> list[str]:
    return [str(query) for kind, query, _ in connection.events if kind == "execute"]


def test_schema_contract_verifies_extended_materialized_view() -> None:
    module.PostgresGoldSyncRepository._assert_features_view_contract(FakeConnection().cursor())


@pytest.mark.parametrize(
    ("problem", "error_message"),
    (
        ("missing", "columns differ"),
        ("reordered", "columns differ"),
        ("wrong_type", "type differs"),
    ),
)
def test_schema_contract_rejects_extended_view_column_drift(
    problem: str, error_message: str
) -> None:
    expected = [
        (
            ordinal,
            name,
            module._EXTENDED_FEATURES_VIEW_COLUMN_TYPES_EXPECTED[name],
            False,
        )
        for ordinal, name in enumerate(module._EXTENDED_FEATURES_VIEW_COLUMNS_EXPECTED, start=1)
    ]
    if problem == "missing":
        columns = tuple(expected[:-1])
    elif problem == "reordered":
        expected[2], expected[3] = expected[3], expected[2]
        columns = tuple(expected)
    else:
        expected[2] = (expected[2][0], expected[2][1], "text", expected[2][3])
        columns = tuple(expected)
    cursor = FakeConnection(extended_view_columns=columns).cursor()
    with pytest.raises(ValueError, match=error_message):
        module.PostgresGoldSyncRepository._assert_features_view_contract(cursor)


def test_schema_reconstructor_recreates_only_loader_schemas_and_revalidates_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    verified = False

    def verify(cursor: object) -> None:
        nonlocal verified
        verified = True

    monkeypatch.setattr(module.PostgresGoldSyncRepository, "_assert_schema_contract", verify)

    PostgresGoldSchemaReconstructor(
        _admin_config(), connection_factory=Factory(connection)
    ).recreate()

    queries = _execute_queries(connection)
    assert 'DROP SCHEMA IF EXISTS "macro_loader" CASCADE' in queries
    assert 'DROP SCHEMA IF EXISTS "macro_loader_sync" CASCADE' in queries
    assert all("DROP SCHEMA" not in query or "macro_loader" in query for query in queries)
    assert 'SET ROLE "macro-loader-owner"' in queries
    assert 'GRANT USAGE ON SCHEMA "macro_loader" TO "macro-loader"' in queries
    assert verified
    assert ("commit", None, None) in connection.events


def test_schema_reconstructor_rolls_back_and_sanitizes_failure() -> None:
    connection = FakeConnection(fail_on="DROP SCHEMA")

    with pytest.raises(PostgresGoldRepositoryError) as error:
        PostgresGoldSchemaReconstructor(
            _admin_config(), connection_factory=Factory(connection)
        ).recreate()

    assert "repo-secret" not in str(error.value)
    assert ("rollback", None, None) in connection.events


def test_config_requires_exact_endpoint_role_and_hides_password() -> None:
    config = PostgresSyncConfig.from_mapping(
        {
            "PGHOST": "10.10.1.3",
            "PGPORT": "54321",
            "PGUSER": "macro-loader",
            "PGDATABASE": "macro_loader",
            "PGPASSWORD": "repo-secret",
        }
    )
    assert (config.host, config.port, config.user) == ("10.10.1.3", 54321, "macro-loader")
    assert "repo-secret" not in repr(config)
    with pytest.raises(ValueError, match="host"):
        PostgresSyncConfig("localhost", 54321, POSTGRES_USER, "macro_loader", "x")
    with pytest.raises(ValueError, match="port"):
        PostgresSyncConfig(POSTGRES_HOST, 5432, POSTGRES_USER, "macro_loader", "x")
    with pytest.raises(ValueError, match="user"):
        PostgresSyncConfig(POSTGRES_HOST, POSTGRES_PORT, "postgres", "macro_loader", "x")
    with pytest.raises(ValueError, match="database"):
        PostgresSyncConfig(POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, "", "x")
    with pytest.raises(ValueError, match="password"):
        PostgresSyncConfig(POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, "macro_loader", "")
    with pytest.raises(ValueError, match="PGPORT"):
        PostgresSyncConfig.from_mapping({"PGPORT": "invalid"})


def test_admin_config_uses_distinct_namespace_role_and_redacted_password() -> None:
    config = PostgresAdminConfig.from_mapping(
        {
            "MARKET_MACRO_POSTGRES_ADMIN_HOST": "10.10.1.3",
            "MARKET_MACRO_POSTGRES_ADMIN_PORT": "54321",
            "MARKET_MACRO_POSTGRES_ADMIN_USER": "macro-loader-admin",
            "MARKET_MACRO_POSTGRES_ADMIN_DATABASE": "macro_loader",
            "MARKET_MACRO_POSTGRES_ADMIN_PASSWORD": "admin-secret",
        }
    )
    assert config.user == "macro-loader-admin"
    assert "admin-secret" not in repr(config)
    with pytest.raises(ValueError, match="host"):
        PostgresAdminConfig("localhost", POSTGRES_PORT, "macro-loader-admin", "macro_loader", "x")
    with pytest.raises(ValueError, match="port"):
        PostgresAdminConfig(POSTGRES_HOST, 5432, "macro-loader-admin", "macro_loader", "x")
    with pytest.raises(ValueError, match="user"):
        PostgresAdminConfig(POSTGRES_HOST, POSTGRES_PORT, "", "macro_loader", "x")
    with pytest.raises(ValueError, match="database"):
        PostgresAdminConfig(POSTGRES_HOST, POSTGRES_PORT, "macro-loader-admin", "", "x")
    with pytest.raises(ValueError, match="password"):
        PostgresAdminConfig(POSTGRES_HOST, POSTGRES_PORT, "macro-loader-admin", "macro_loader", "")
    with pytest.raises(ValueError, match="differ from runtime"):
        PostgresAdminConfig(POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, "macro_loader", "x")
    with pytest.raises(ValueError, match="password must differ"):
        PostgresAdminConfig.from_mapping(
            {
                "MARKET_MACRO_POSTGRES_ADMIN_HOST": "10.10.1.3",
                "MARKET_MACRO_POSTGRES_ADMIN_PORT": "54321",
                "MARKET_MACRO_POSTGRES_ADMIN_USER": "macro-loader-admin",
                "MARKET_MACRO_POSTGRES_ADMIN_DATABASE": "macro_loader",
                "MARKET_MACRO_POSTGRES_ADMIN_PASSWORD": "shared-secret",
                "PGPASSWORD": "shared-secret",
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("connect_timeout_seconds", 0),
        ("lock_timeout_ms", 0),
        ("statement_timeout_ms", -1),
        ("idle_in_transaction_timeout_ms", 0),
    ],
)
def test_timeout_policy_rejects_non_positive_bounds(field: str, value: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        PostgresTimeoutPolicy(**{field: value})


def test_default_connection_passes_connect_timeout_and_application_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def connect(**kwargs: object) -> FakeConnection:
        captured.update(kwargs)
        return FakeConnection()

    monkeypatch.setattr(module.psycopg, "connect", connect)
    policy = PostgresTimeoutPolicy(connect_timeout_seconds=9)
    module._default_connection(
        PostgresSyncConfig(
            POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, "macro_loader", "secret", policy
        )
    )
    assert captured["connect_timeout"] == 9
    assert captured["application_name"] == "macro-loader"


def test_admin_schema_migrations_are_gold_only_timestamptz_and_idempotent() -> None:
    connection = FakeConnection()
    factory = Factory(connection)
    migrator = PostgresGoldSchemaMigrator(_admin_config(), connection_factory=factory)
    migrator.migrate()
    queries = _execute_queries(connection)
    assert queries[:6] == [
        "SET application_name = 'macro-loader'",
        "SET TIME ZONE 'UTC'",
        "SET lock_timeout = '5000ms'",
        "SET statement_timeout = '900000ms'",
        "SET idle_in_transaction_session_timeout = '30000ms'",
        "SET jit = off",
    ]
    ddl = "\n".join(queries[5:])
    assert '"timestamp_m1" TIMESTAMPTZ(6) NOT NULL PRIMARY KEY' in ddl
    for column in POSTGRES_RAW_COLUMNS[1:]:
        assert f'"{column}" DOUBLE PRECISION NULL' in ddl
    assert '"macro_loader"."macro_raw"' in ddl
    assert '"macro_loader_sync"."gold_sync_state"' in ddl
    assert '"macro_loader_sync"."gold_row_hashes"' in ddl
    assert '"macro_loader_sync"."schema_migrations"' in ddl
    assert queries.count(module._CONSUMER_DDL) == 3
    assert queries.count(module._SYNC_STATE_DDL) == 1
    assert queries.count(module._ROW_HASH_DDL) == 1
    assert queries.count(module._FEATURES_VIEW_DDL) == 1
    assert "TRUNCATE TABLE" not in module._FEATURE_VALUE_REFRESH_FUNCTION
    assert "REFRESH MATERIALIZED VIEW" in module._FEATURE_VALUE_REFRESH_FUNCTION
    assert module._FEATURE_VALUE_SOURCE_SQL in module._FEATURE_VALUES_DDL
    assert "CREATE TEMP TABLE macro_feature_values_staging" not in (
        module._FEATURE_VALUE_REFRESH_FUNCTION
    )
    assert "macro_loader.refresh_macro_feature_values()" not in (
        module._FEATURE_VALUE_INVALID_SOURCE_SQL
    )
    assert 'DROP TABLE IF EXISTS "macro_loader"."macro_raw"' in ddl
    assert "to_regclass('macro_loader.macro_features_daily')" in ddl
    assert 'ALTER TABLE "macro_loader"."macro_features_daily" RENAME TO "macro_raw"' in ddl
    assert 'CREATE SCHEMA IF NOT EXISTS "macro_loader_retired"' in ddl
    assert 'CREATE SCHEMA IF NOT EXISTS "macro_loader"' not in ddl
    assert all(table.name != "fed_policy_features_daily" for table in module._SCHEMA_SPECIFICATION)
    assert ("commit", None, None) in connection.events


def test_admin_migration_timeout_is_bounded_and_longer_than_runtime_sync() -> None:
    admin = module.PostgresAdminConfig("10.10.1.3", 54321, "postgres", "macro_loader", "secret")
    runtime = module.PostgresTimeoutPolicy()

    assert admin.timeout_policy.statement_timeout_ms == 900_000
    assert admin.timeout_policy.statement_timeout_ms > runtime.statement_timeout_ms == 300_000
    assert module.POSTGRES_FEATURE_REFRESH_TIMEOUT_MS == 900_000


@pytest.mark.parametrize("version", (24, 25, 28, 30))
def test_intermediate_legacy_view_publication_is_deferred_until_cutover(version: int) -> None:
    assert module._defer_intermediate_feature_view_statement(
        version, module._LEGACY_FEATURES_VIEW_REBUILD_MIGRATION[0]
    )
    assert module._defer_intermediate_feature_view_statement(
        version, module._LEGACY_FEATURES_VIEW_REBUILD_MIGRATION[1]
    )
    assert not module._defer_intermediate_feature_view_statement(version, "SELECT 1")


def test_feature_view_publication_is_not_deferred_at_or_after_cutover() -> None:
    drop_view = module._LEGACY_FEATURES_VIEW_REBUILD_MIGRATION[0]

    assert not module._defer_intermediate_feature_view_statement(23, drop_view)
    assert not module._defer_intermediate_feature_view_statement(31, drop_view)
    assert not module._defer_intermediate_feature_view_statement(32, drop_view)


def test_retirement_migration_archives_fed_data_before_removing_active_columns() -> None:
    migration = module._RETIRED_FED_ARCHIVE_MIGRATION
    sql = "\n".join(migration)
    assert 'CREATE SCHEMA IF NOT EXISTS "macro_loader_retired"' in sql
    assert '"fed_macro_raw"' in sql
    assert '"fed_macro_features"' in sql
    assert '"fed_policy_features_daily"' in sql
    assert 'INSERT INTO "macro_loader_retired"."fed_policy_sync_state"' in sql
    assert 'INSERT INTO "macro_loader_retired"."fed_policy_row_hashes"' in sql
    assert 'DELETE FROM "macro_loader_sync"."gold_sync_state"' in sql
    assert 'DELETE FROM "macro_loader_sync"."gold_row_hashes"' in sql
    assert 'REVOKE ALL ON SCHEMA "macro_loader_retired" FROM PUBLIC' in sql
    assert '"macro-loader"' in sql and '"macro-loader-sync"' in sql
    assert sql.index('INSERT INTO "macro_loader_retired"."fed_macro_raw"') < sql.index(
        'DROP COLUMN IF EXISTS "fed_next_expected_move_bp"'
    )
    assert sql.index('INSERT INTO "macro_loader_retired"."fed_policy_features_daily"') < sql.index(
        'DROP TABLE IF EXISTS "macro_loader_sync"."fed_policy_features_daily"'
    )
    assert 'DROP COLUMN IF EXISTS "available_at_utc"' not in sql
    assert module._LEGACY_FEATURES_VIEW_DDL in migration
    feature_migration = "\n".join(module._MIGRATIONS[24])
    assert module._LEGACY_FEATURES_VIEW_DDL in feature_migration

    ohlc_feature_migration = "\n".join(module._MIGRATIONS[25])
    assert module._LEGACY_FEATURES_VIEW_DDL in ohlc_feature_migration
    assert f"version={module.MACRO_FEATURE_VIEW_VERSION}" in ohlc_feature_migration
    assert module.MACRO_FEATURE_VIEW_FINGERPRINT in ohlc_feature_migration
    legacy_migration = "\n".join(module._FED_POLICY_RENAME_MIGRATION)
    assert (
        'CREATE TABLE IF NOT EXISTS "macro_loader_retired"."fed_legacy_raw_columns"'
        in legacy_migration
    )
    assert legacy_migration.index("INSERT INTO") < legacy_migration.index(
        'DROP COLUMN IF EXISTS "fed_3m_expected_move_bp"'
    )
    assert "to_regclass('macro_loader.macro_raw') IS NOT NULL" in legacy_migration


def test_legacy_view_migration_adds_ohlc_columns_before_rebuilding_the_view() -> None:
    legacy_upgrade_migration = module._MIGRATIONS[23]

    assert legacy_upgrade_migration[0] == module._AVAILABLE_AT_COLUMN_MIGRATION[0]
    assert legacy_upgrade_migration[1] == module._OHLC_COLUMN_MIGRATION[0]
    assert legacy_upgrade_migration[2] == module._LEGACY_FEATURES_VIEW_REBUILD_MIGRATION[0]
    assert "GENERATED ALWAYS AS" in legacy_upgrade_migration[0]
    assert all(
        f'ADD COLUMN IF NOT EXISTS "{column}" DOUBLE PRECISION NULL' in legacy_upgrade_migration[1]
        for column in module.POSTGRES_OHLC_COLUMNS
    )


def test_latest_migrations_add_generated_availability_before_rebuilding_the_view() -> None:
    availability_migration_index = next(
        index
        for index, migration in enumerate(module._MIGRATIONS)
        if any(module._AVAILABLE_AT_COLUMN_MIGRATION[0] in statement for statement in migration)
    )
    availability_sql = "\n".join(module._MIGRATIONS[availability_migration_index])

    assert '"available_at_utc"' in availability_sql
    assert "GENERATED ALWAYS AS" in availability_sql
    assert "TIMESTAMPTZ(6)" in availability_sql
    feature_views_migration_index = len(module._MIGRATIONS) - 4
    feature_views_migration = "\n".join(module._MIGRATIONS[feature_views_migration_index])
    drift_cleanup = "\n".join(module._MIGRATIONS[-3])
    feature_values_migration = "\n".join(module._MIGRATIONS[-2])
    feature_values_population_migration = "\n".join(module._MIGRATIONS[-1])
    assert availability_migration_index < feature_views_migration_index
    assert module._FEATURES_VIEW_DDL in feature_views_migration
    assert "version=16" in feature_views_migration
    assert module.MACRO_FEATURE_VIEW_FINGERPRINT in feature_views_migration
    assert module._EXTENDED_FEATURES_VIEW_DDL in feature_views_migration
    assert "version=1" in feature_views_migration
    assert module.EXTENDED_MACRO_FEATURE_VIEW_FINGERPRINT in feature_views_migration
    assert module._RETIRED_FED_ARCHIVE_MIGRATION[:24] == module._RETIRED_FED_DRIFT_CLEANUP_MIGRATION
    assert 'INSERT INTO "macro_loader_retired"."fed_policy_features_daily"' in drift_cleanup
    assert 'DROP TABLE IF EXISTS "macro_loader_sync"."fed_policy_features_daily"' in drift_cleanup
    assert module._FEATURES_VIEW_DDL not in drift_cleanup
    assert module._FEATURE_VALUES_DDL in feature_values_migration
    assert "refresh_macro_feature_values" in feature_values_migration
    assert module._FEATURE_VALUE_REFRESH_CALL not in feature_values_migration
    assert module._FEATURE_VALUE_REFRESH_FUNCTION in feature_values_population_migration
    assert module._FEATURE_VALUE_REFRESH_CALL in feature_values_population_migration


def test_long_feature_values_projection_preserves_types_and_omits_nulls() -> None:
    statements = "\n".join(module._FEATURE_VALUES_MIGRATION)
    assert len(module.FEATURE_VALUE_NAMES) == 1720
    assert module._FEATURE_VALUES_DDL.startswith("CREATE MATERIALIZED VIEW IF NOT EXISTS")
    assert "WITH NO DATA" in module._FEATURE_VALUES_DDL
    assert "UNIQUE INDEX" in module._FEATURE_VALUES_UNIQUE_INDEX
    assert "WHEN 'true' THEN 1.0::double precision" in module._FEATURE_VALUE_SOURCE_SQL
    assert "WHEN 'false' THEN 0.0::double precision" in module._FEATURE_VALUE_SOURCE_SQL
    assert module._FEATURES_VIEW not in module._FEATURE_VALUE_SOURCE_SQL
    assert module._EXTENDED_FEATURES_VIEW not in module._FEATURE_VALUE_SOURCE_SQL
    assert "macro_feature_raw_projection" not in module._FEATURE_VALUE_SOURCE_SQL
    assert "macro_feature_transform_" not in module._FEATURE_VALUE_SOURCE_SQL
    assert '"macro_loader"."macro_raw_vix"' in module._FEATURE_VALUE_SOURCE_SQL
    assert '"macro_loader"."macro_raw_us_5y"' in module._FEATURE_VALUE_SOURCE_SQL
    assert "jsonb_typeof(fields.value) = 'boolean'" in module._FEATURE_VALUE_SOURCE_SQL
    assert "jsonb_typeof(fields.value) = 'number'" in module._FEATURE_VALUE_SOURCE_SQL
    assert "fields.value AS value" in module._FEATURE_VALUE_ALL_SOURCE_SQL
    assert "value <> 'null'::jsonb" in module._FEATURE_VALUE_INVALID_SOURCE_SQL
    assert "jsonb_typeof(value) = 'number'" in module._FEATURE_VALUE_INVALID_SOURCE_SQL
    assert "value <> 'null'::jsonb" not in module._FEATURE_VALUE_SOURCE_SQL
    assert "raw_ohlc" not in module._FEATURE_VALUE_SOURCE_SQL
    assert "(feature_name, timestamp_m1)" in module._FEATURE_VALUES_INDEX
    assert "REFRESH MATERIALIZED VIEW" in module._FEATURE_VALUE_REFRESH_FUNCTION
    assert "PERFORM macro_loader.refresh_macro_feature_values()" in (
        module._FEATURE_VALUES_REFRESH_FUNCTION
    )
    assert "GRANT SELECT ON" in statements
    assert "REVOKE INSERT, UPDATE, DELETE, TRUNCATE" in statements


def test_feature_table_cutover_migration_backfills_all_registered_series_and_drops_wide_table() -> (
    None
):
    statements = module._FEATURE_RAW_TABLE_MIGRATION
    table_creates = tuple(
        statement for statement in statements if statement.startswith("CREATE TABLE IF NOT EXISTS")
    )
    backfills = tuple(statement for statement in statements if statement.startswith("INSERT INTO"))

    assert len(module.POSTGRES_RAW_TABLES) == 25
    assert tuple(
        spec.name
        for spec in module._SCHEMA_SPECIFICATION
        if spec.schema == module.POSTGRES_CONSUMER_SCHEMA and spec.name.startswith("macro_raw_")
    ) == tuple(module.POSTGRES_RAW_TABLES)
    assert len(table_creates) == len(module.POSTGRES_RAW_TABLES)
    assert len(backfills) == len(module.POSTGRES_RAW_TABLES)
    assert all("TIMESTAMPTZ(6) NOT NULL PRIMARY KEY" in statement for statement in table_creates)
    assert all("GENERATED ALWAYS AS" in statement for statement in table_creates)
    assert all('FROM "macro_loader"."macro_raw"' in statement for statement in backfills)
    euro_oas = next(sql for sql in backfills if "macro_raw_euro_hy_oas" in sql)
    assert '"euro_hy_oas_level"' in euro_oas
    assert '"euro_hy_yield_level"' in next(
        sql for sql in backfills if "macro_raw_euro_hy_yield" in sql
    )
    assert statements.index('DROP TABLE "macro_loader"."macro_raw"') > max(
        statements.index(sql) for sql in backfills
    )
    assert statements[-1] == module._FEATURES_VIEW_REBUILD_MIGRATION[-1]
    assert 'FROM "macro_loader"."macro_raw"' not in module._CONSUMER_ROWS_SQL
    assert 'FROM "macro_loader"."macro_raw"' not in module._FEATURES_VIEW_DDL


def test_feature_row_writer_routes_scalar_and_ohlc_values_to_their_own_tables() -> None:
    class RecordingCursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def execute(self, query: str, params: object = None) -> None:
            self.calls.append((query, params))

    cursor = RecordingCursor()
    row = _row(1, 2.5)
    module._apply_feature_row(cursor, row)
    inserts = [(query, params) for query, params in cursor.calls if query.startswith("INSERT INTO")]

    assert len(inserts) == len(module.POSTGRES_RAW_TABLES)
    vix_insert = next(item for item in inserts if '"macro_raw_vix"' in item[0])
    assert '"open", "high", "low", "close"' in vix_insert[0]
    assert vix_insert[1] == (_ts(1), 2.5, 2.5, 2.5, 2.5)
    scalar_insert = next(item for item in inserts if '"macro_raw_us_3m"' in item[0])
    assert '"indexvalue"' in scalar_insert[0]
    assert scalar_insert[1] == (_ts(1), 2.5)


def test_feature_row_writer_rejects_divergent_ohlc_close_and_level() -> None:
    class RecordingCursor:
        def execute(self, query: str, params: object = None) -> None:
            raise AssertionError("invalid rows must be rejected before database writes")

    values = [None] * (len(POSTGRES_RAW_COLUMNS) - 1)
    positions = {name: index - 1 for index, name in enumerate(POSTGRES_RAW_COLUMNS)}
    values[positions["vix_level"]] = 10.0
    values[positions["vix_close"]] = 9.0
    with pytest.raises(ValueError, match="must equal vix_close"):
        module._apply_feature_row(RecordingCursor(), GoldRowPayload(_ts(1), tuple(values)))


def test_ohlc_migration_is_additive_and_schema_contract_preserves_available_at_order() -> None:
    migration = "\n".join(module._MIGRATIONS[24])
    raw_spec = next(
        spec
        for spec in module._SCHEMA_SPECIFICATION
        if spec.schema == "macro_loader" and spec.name == "macro_raw_vix"
    )
    names = tuple(column.name for column in raw_spec.columns)
    expected_raw_ohlc = tuple(f"vix_{field}" for field in module.OHLC_FIELDS)

    assert names.index("available_at_utc") < names.index("open")
    assert tuple(name for name in names if name in module.OHLC_FIELDS) == module.OHLC_FIELDS
    assert all(
        f'"{column}" DOUBLE PRECISION NULL' in module._CONSUMER_DDL for column in expected_raw_ohlc
    )
    assert tuple(
        migration.index(f'ADD COLUMN IF NOT EXISTS "{column}" DOUBLE PRECISION NULL')
        for column in expected_raw_ohlc
    ) == tuple(
        sorted(
            migration.index(f'ADD COLUMN IF NOT EXISTS "{column}" DOUBLE PRECISION NULL')
            for column in expected_raw_ohlc
        )
    )
    assert "DROP TABLE" not in migration
    assert "DELETE FROM" not in migration
    assert module._LEGACY_FEATURES_VIEW_DDL in module._MIGRATIONS[24]
    prior_euro_hy_migration = "\n".join(module._MIGRATIONS[-7])
    assert 'ADD COLUMN IF NOT EXISTS "vstoxx1m_level" DOUBLE PRECISION NULL' in (
        "\n".join(module._MIGRATIONS[-8])
    )
    assert 'ADD COLUMN IF NOT EXISTS "euro_hy_total_return_level" DOUBLE PRECISION NULL' in (
        prior_euro_hy_migration
    )
    treasury_migration = "\n".join(module._MIGRATIONS[-6])
    assert 'ADD COLUMN IF NOT EXISTS "us_3m_level" DOUBLE PRECISION NULL' in treasury_migration
    assert 'ADD COLUMN IF NOT EXISTS "us_5y_level" DOUBLE PRECISION NULL' in treasury_migration
    assert treasury_migration.count("ADD COLUMN IF NOT EXISTS") == 2
    cutover_migration = "\n".join(module._MIGRATIONS[-5])
    assert 'DROP TABLE "macro_loader"."macro_raw"' in cutover_migration
    assert 'INSERT INTO "macro_loader"."macro_raw_euro_hy_oas"' in cutover_migration
    assert "macro_raw_vix" in cutover_migration and "macro_raw_usd_broad" in cutover_migration
    assert len(module.POSTGRES_RAW_TABLES) == 25
    assert not any(spec.name == "macro_raw" for spec in module._SCHEMA_SPECIFICATION)
    extended_migration = "\n".join(module._MIGRATIONS[-4])
    assert "macro_features_extended" in extended_migration


@pytest.mark.parametrize(
    ("metadata", "error_message"),
    [
        (None, "not a generated column"),
        (("NEVER", "timestamp_m1"), "not a generated column"),
        (("ALWAYS", "timestamp_m1 + 1"), "generation expression differs"),
    ],
)
def test_generated_availability_contract_fails_closed(
    metadata: tuple[str, str] | None, error_message: str
) -> None:
    class MetadataCursor:
        def execute(self, query: str, params: object = None) -> None:
            assert query == module._GENERATED_AVAILABILITY_SQL
            assert params is not None and params[0] == "macro_loader"
            assert params[1] in (
                *module.POSTGRES_RAW_TABLES,
                module.POSTGRES_FEATURE_VALUES_TABLE,
            )

        def fetchone(self) -> tuple[str, str] | None:
            return metadata

    with pytest.raises(ValueError, match=error_message):
        module._assert_generated_availability(MetadataCursor())


def test_generated_availability_accepts_postgres_at_time_zone_deparser() -> None:
    expression = (
        "((timestamp_m1 AT TIME ZONE 'UTC')::date + CASE "
        "EXTRACT(ISODOW FROM timestamp_m1 AT TIME ZONE 'UTC')::integer "
        "WHEN 5 THEN 3 WHEN 6 THEN 2 WHEN 7 THEN 1 ELSE 1 END)::timestamp "
        "AT TIME ZONE 'UTC'"
    )

    class MetadataCursor:
        def execute(self, query: str, params: object = None) -> None:
            assert query == module._GENERATED_AVAILABILITY_SQL
            assert params is not None and params[0] == "macro_loader"
            assert params[1] in (
                *module.POSTGRES_RAW_TABLES,
                module.POSTGRES_FEATURE_VALUES_TABLE,
            )

        def fetchone(self) -> tuple[str, str]:
            return ("ALWAYS", expression)

    module._assert_generated_availability(MetadataCursor())


def test_macro_features_materialized_view_projects_non_fed_derived_features() -> None:
    ddl = module._FEATURES_VIEW_DDL

    assert 'CREATE MATERIALIZED VIEW IF NOT EXISTS "macro_loader"."macro_features"' in ddl
    assert '"macro_loader"."macro_raw_vix"' in ddl
    assert '"macro_loader"."macro_raw_usd_broad"' in ddl
    assert 'FROM "macro_loader"."macro_raw"' not in ddl
    assert module._FEATURES_VIEW_COLUMNS_EXPECTED[:2] == (
        "timestamp_m1",
        "available_at_utc",
    )
    assert 'raw."available_at_utc" AS "available_at_utc"' in ddl
    assert "2010-01-01" not in ddl
    assert len(module._FEATURES_VIEW_COLUMNS) == len(module.FEATURE_COLUMNS) - 1
    assert not any(column.startswith("fed_") for column in module._FEATURES_VIEW_COLUMNS)
    assert not any(origin in ddl for origin in module._RETIRED_FED_ORIGIN_COLUMNS)
    assert all(
        column.endswith("_log_level")
        for column in module._FEATURES_VIEW_COLUMNS[: len(module.PRICE_FEATURE_SERIES)]
    )
    assert 'THEN ln(raw."vix_level") END AS "vix_log_level"' in ddl
    assert 'AS "vix_delta_1obs"' in ddl
    assert 'AS "vix9d_delta_1obs"' in ddl
    assert 'AS "vix9d_vix_ratio"' in ddl
    assert 'AS "vix9d_vix3m_log_ratio"' in ddl
    assert 'AS "vix_log_return_1obs"' in ddl
    assert 'AS "vix_return_geom_5obs_pct"' in ddl
    assert 'AS "vix_return_mean_5obs"' in ddl
    assert 'AS "vix_volatility_40obs"' in ddl
    assert 'AS "vix_sma_ratio_5_20"' in ddl
    assert 'AS "vix_rsi_7obs"' in ddl
    assert "WITH RECURSIVE input AS" in ddl
    assert "power(" not in ddl
    assert "coalesce(gains[1], 0.0)" in ddl
    assert "state.valid_count + 1 >= 7" in ddl
    assert 'corr(change, "change_lag_20") OVER window_120 IS NULL THEN NULL ELSE greatest' in ddl
    assert 'AS "vix_roc_20obs"' in ddl
    assert 'AS "vix_drawdown_60obs"' in ddl
    assert 'AS "vix_return_geom_240obs_pct"' not in ddl
    assert 'AS "vix_return_geom_25obs_pct"' not in ddl
    assert 'AS "usd_broad_log_return_20obs"' in ddl
    assert "WHERE FALSE" not in ddl
    assert '"estr_return_geom_10obs_pct"' not in ddl
    assert 'raw."estr_level" AS "estr_level"' not in ddl


def test_gold_sync_sql_contains_only_non_fed_raw_columns() -> None:
    assert not any(
        f'"{column}"' in module._INSERT_ROW_SQL for column in module._RETIRED_FED_ORIGIN_COLUMNS
    )
    assert not any(
        f'"{column}"' in module._UPDATE_ROW_SQL for column in module._RETIRED_FED_ORIGIN_COLUMNS
    )
    assert not any(
        f'"{column}"' in module._CONSUMER_ROWS_SQL for column in module._RETIRED_FED_ORIGIN_COLUMNS
    )
    assert '"vix_level" IS NOT NULL' in module._CONSUMER_ROWS_SQL
    assert "per-feature upserts" in module._INSERT_ROW_SQL
    assert '"vix_level" IS NOT NULL' in module._TARGET_SUMMARY_SQL


def test_runtime_schema_preflight_is_read_only_and_contains_no_ddl() -> None:
    connection = FakeConnection()
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))

    repository.preflight_schema()

    queries = _execute_queries(connection)
    assert "SET TRANSACTION READ ONLY" in queries
    prohibited = ("CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT")
    assert not any(query.startswith(prohibited) for query in queries)
    assert ("rollback", None, None) in connection.events
    assert ("commit", None, None) not in connection.events


def test_read_state_round_trips_exact_sync_metadata() -> None:
    state = _state()
    connection = FakeConnection(
        state_row=(
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
    )
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))
    assert repository.read_state(POSTGRES_DATASET_ID) == state

    empty = FakeConnection(state_row=None)
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(empty))
    assert repository.read_state(POSTGRES_DATASET_ID) is None


@pytest.mark.parametrize(
    "invalid_timestamp",
    [
        datetime(2026, 8, 1),
        datetime(2026, 8, 1, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_read_state_rejects_invalid_database_timestamp(invalid_timestamp: datetime) -> None:
    state = _state()
    connection = FakeConnection(
        state_row=(
            state.dataset_id,
            state.source_build_id,
            state.data_sha256,
            state.schema_version,
            state.feature_version,
            state.row_count,
            invalid_timestamp,
            state.max_timestamp,
            state.synced_at_utc,
        )
    )
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))

    with pytest.raises(PostgresGoldRepositoryError, match="sync-state read failed"):
        repository.read_state(POSTGRES_DATASET_ID)


def test_read_digests_fetches_only_timestamp_and_hash_in_order() -> None:
    connection = FakeConnection(digest_rows=((_ts(1), "a" * 64), (_ts(2), "b" * 64)))
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))
    assert repository.read_digests(POSTGRES_DATASET_ID) == (
        GoldRowDigest(_ts(1), "a" * 64),
        GoldRowDigest(_ts(2), "b" * 64),
    )
    digest_query = next(
        query for query in _execute_queries(connection) if query.startswith("SELECT timestamp")
    )
    assert "row_sha256" in digest_query
    for feature in POSTGRES_RAW_COLUMNS[1:]:
        assert feature not in digest_query


def test_read_digests_rejects_invalid_database_timestamp() -> None:
    connection = FakeConnection(digest_rows=((datetime(2026, 8, 1), "a" * 64),))
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))

    with pytest.raises(PostgresGoldRepositoryError, match="digest read failed"):
        repository.read_digests(POSTGRES_DATASET_ID)


def test_read_consumer_digests_hashes_complete_rows_in_timestamp_order() -> None:
    first = _row(1, 1.0)
    second = _row(2, 2.0)
    connection = FakeConnection(
        consumer_rows=(
            (first.timestamp_m1, *first.values),
            (second.timestamp_m1, *second.values),
        )
    )
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))

    assert repository.read_consumer_digests(POSTGRES_DATASET_ID) == (
        GoldRowDigest(first.timestamp_m1, module.gold_row_sha256(first)),
        GoldRowDigest(second.timestamp_m1, module.gold_row_sha256(second)),
    )
    consumer_query = next(
        query for query in _execute_queries(connection) if query.startswith('SELECT "timestamp_m1"')
    )
    assert all(f'"{column}"' in consumer_query for column in POSTGRES_RAW_COLUMNS)
    assert '"fed_next_expected_move_bp"' not in consumer_query


def test_summary_returns_count_and_utc_bounds() -> None:
    connection = FakeConnection(summary_row=(2, _ts(1), _ts(2)))
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))
    assert repository.summary(POSTGRES_DATASET_ID) == GoldTargetSummary(2, _ts(1), _ts(2))


def test_summary_rejects_non_utc_database_timestamp() -> None:
    connection = FakeConnection(
        summary_row=(2, _ts(1), datetime(2026, 8, 2, tzinfo=timezone(timedelta(hours=2))))
    )
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))

    with pytest.raises(PostgresGoldRepositoryError, match="summary read failed"):
        repository.summary(POSTGRES_DATASET_ID)


def test_apply_delta_is_locked_exact_and_state_is_last_before_commit() -> None:
    insert = _row(2, 2.0)
    update = _row(1, 10.0)
    delete = _ts(3)
    digests = (
        GoldRowDigest(update.timestamp_m1, module.gold_row_sha256(update)),
        GoldRowDigest(insert.timestamp_m1, module.gold_row_sha256(insert)),
    )
    plan = GoldDeltaPlan((insert,), (update,), (delete,), (), digests)
    state = _state()
    connection = FakeConnection(
        digest_rows=tuple((digest.timestamp_m1, digest.row_sha256) for digest in digests),
        consumer_rows=(
            (update.timestamp_m1, *update.values),
            (insert.timestamp_m1, *insert.values),
        ),
        summary_row=(2, _ts(1), _ts(2)),
    )
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))
    repository.apply_delta(POSTGRES_DATASET_ID, plan, state)

    queries = _execute_queries(connection)
    lock_index = next(i for i, query in enumerate(queries) if "pg_advisory_xact_lock" in query)
    insert_index = next(
        i
        for i, query in enumerate(queries)
        if query.startswith('INSERT INTO "macro_loader"."macro_raw_')
    )
    delete_index = next(
        i
        for i, query in enumerate(queries)
        if query.startswith('DELETE FROM "macro_loader"."macro_raw_')
    )
    summary_index = next(i for i, query in enumerate(queries) if query.startswith("SELECT COUNT"))
    state_index = next(
        i
        for i, query in enumerate(queries)
        if 'INSERT INTO "macro_loader_sync"."gold_sync_state"' in query
    )
    assert lock_index < insert_index < delete_index < summary_index < state_index
    assert sum(query.startswith('INSERT INTO "macro_loader"."macro_raw_') for query in queries) > 0
    assert sum(query.startswith('DELETE FROM "macro_loader"."macro_raw_') for query in queries) > 0
    assert "SELECT macro_loader.refresh_macro_features_explicit()" in queries
    refresh_index = queries.index("SELECT macro_loader.refresh_macro_features_explicit()")
    assert queries[refresh_index - 1] == "SET LOCAL statement_timeout = '900000ms'"
    assert not any(query.startswith("TRUNCATE TABLE") for query in queries)
    assert ("commit", None, None) in connection.events
    assert ("rollback", None, None) not in connection.events


def test_locked_transaction_uses_deterministic_namespaced_key_before_reads() -> None:
    connection = FakeConnection()
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))
    expected_key = module._advisory_lock_key(POSTGRES_DATASET_ID)

    def operation(transaction: GoldSyncTransaction) -> None:
        transaction.read_state(POSTGRES_DATASET_ID)

    repository.run_locked(operation)

    queries = _execute_queries(connection)
    lock_index = next(i for i, query in enumerate(queries) if "pg_advisory_xact_lock" in query)
    state_index = next(
        i for i, query in enumerate(queries) if query.startswith("SELECT dataset_id")
    )
    lock_event = next(
        event
        for event in connection.events
        if event[0] == "execute" and "pg_advisory_xact_lock" in str(event[1])
    )
    assert lock_index < state_index
    assert lock_event[2] == (expected_key,)
    assert expected_key == module._advisory_lock_key(POSTGRES_DATASET_ID)
    assert "macro-loader" in module.POSTGRES_ADVISORY_LOCK_NAMESPACE


def test_first_bootstrap_can_insert_complete_source_without_full_reload_sql() -> None:
    rows = (_row(1, 1.0), _row(2, 2.0))
    digests = (
        GoldRowDigest(rows[0].timestamp_m1, module.gold_row_sha256(rows[0])),
        GoldRowDigest(rows[1].timestamp_m1, module.gold_row_sha256(rows[1])),
    )
    plan = GoldDeltaPlan(rows, (), (), (), digests)
    connection = FakeConnection(
        digest_rows=tuple((digest.timestamp_m1, digest.row_sha256) for digest in digests),
        consumer_rows=tuple((row.timestamp_m1, *row.values) for row in rows),
        summary_row=(2, _ts(1), _ts(2)),
    )
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))
    repository.apply_delta(POSTGRES_DATASET_ID, plan, _state())
    queries = _execute_queries(connection)
    assert sum(query.startswith('INSERT INTO "macro_loader"."macro_raw_') for query in queries) == (
        2 * len(module.POSTGRES_RAW_TABLES)
    )
    assert all(
        not query.startswith("TRUNCATE TABLE") and "DROP TABLE" not in query for query in queries
    )


def test_transaction_failure_rolls_back_and_error_is_sanitized() -> None:
    row = _row(1, 1.0)
    plan = GoldDeltaPlan(
        (),
        (row,),
        (),
        (),
        (GoldRowDigest(_ts(1), "a" * 64),),
    )
    connection = FakeConnection(
        summary_row=(1, _ts(1), _ts(1)), fail_on='INSERT INTO "macro_loader"."macro_raw_vix"'
    )
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))
    with pytest.raises(PostgresGoldRepositoryError) as exc_info:
        repository.apply_delta(
            POSTGRES_DATASET_ID,
            plan,
            _state(count=1, minimum=_ts(1), maximum=_ts(1)),
        )
    assert "repo-secret" not in str(exc_info.value)
    assert ("rollback", None, None) in connection.events
    assert ("commit", None, None) not in connection.events


def test_missing_planned_update_rolls_back_before_state_write() -> None:
    row = _row(1, 1.0)
    plan = GoldDeltaPlan(
        (),
        (row,),
        (),
        (),
        (GoldRowDigest(row.timestamp_m1, module.gold_row_sha256(row)),),
    )
    connection = FakeConnection(
        rowcounts={module._UPDATE_ROW_SQL: 0},
        summary_row=(1, _ts(1), _ts(1)),
    )
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))

    with pytest.raises(PostgresGoldRepositoryError):
        repository.apply_delta(
            POSTGRES_DATASET_ID,
            plan,
            _state(count=1, minimum=_ts(1), maximum=_ts(1)),
        )

    queries = _execute_queries(connection)
    assert not any(
        'INSERT INTO "macro_loader_sync"."gold_sync_state"' in query for query in queries
    )
    assert ("rollback", None, None) in connection.events


def test_lock_timeout_is_typed_sanitized_and_rolls_back() -> None:
    class LockTimeoutError(RuntimeError):
        sqlstate = "55P03"

    class LockTimeoutCursor(FakeCursor):
        def execute(self, query: str, params: object = None) -> object:
            if "pg_advisory_xact_lock" in query:
                raise LockTimeoutError("postgresql://secret@host")
            return super().execute(query, params)

    class LockTimeoutConnection(FakeConnection):
        def cursor(self) -> LockTimeoutCursor:
            return LockTimeoutCursor(self)

    connection = LockTimeoutConnection(summary_row=(0, None, None))
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))
    with pytest.raises(PostgresLockContentionError) as exc_info:
        repository.apply_delta(
            POSTGRES_DATASET_ID, GoldDeltaPlan((), (), (), (), ()), _state(count=0)
        )
    assert "secret" not in str(exc_info.value)
    assert ("rollback", None, None) in connection.events


def test_statement_timeout_is_typed_and_sanitized() -> None:
    class StatementTimeoutError(RuntimeError):
        sqlstate = "57014"

    class StatementTimeoutCursor(FakeCursor):
        def execute(self, query: str, params: object = None) -> object:
            if query.startswith("SELECT COUNT"):
                raise StatementTimeoutError("postgresql://secret@host")
            return super().execute(query, params)

    class StatementTimeoutConnection(FakeConnection):
        def cursor(self) -> StatementTimeoutCursor:
            return StatementTimeoutCursor(self)

    connection = StatementTimeoutConnection()
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))
    with pytest.raises(module.PostgresOperationTimeoutError) as exc_info:
        repository.summary(POSTGRES_DATASET_ID)
    assert "secret" not in str(exc_info.value)


def test_post_write_verification_failure_rolls_back_before_state_write() -> None:
    connection = FakeConnection(summary_row=(99, _ts(1), _ts(2)))
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory(connection))
    with pytest.raises(PostgresGoldRepositoryError):
        repository.apply_delta(POSTGRES_DATASET_ID, GoldDeltaPlan((), (), (), (), ()), _state())
    queries = _execute_queries(connection)
    assert not any(
        'INSERT INTO "macro_loader_sync"."gold_sync_state"' in query for query in queries
    )
    assert ("rollback", None, None) in connection.events


def test_connection_failure_does_not_echo_password() -> None:
    def broken_factory(config: PostgresSyncConfig) -> FakeConnection:
        raise RuntimeError(f"cannot connect with {config.password}")

    repository = PostgresGoldSyncRepository(_config(), connection_factory=broken_factory)
    with pytest.raises(PostgresGoldRepositoryError) as exc_info:
        repository.preflight_schema()
    assert "repo-secret" not in str(exc_info.value)


def test_unsupported_dataset_and_missing_digest_fail_before_mutation() -> None:
    repository = PostgresGoldSyncRepository(_config(), connection_factory=Factory())
    with pytest.raises(ValueError, match="unsupported"):
        repository.read_state("silver")
    row = _row(1, 1.0)
    with pytest.raises(ValueError, match="missing"):
        repository.apply_delta(
            POSTGRES_DATASET_ID,
            GoldDeltaPlan((row,), (), (), (), ()),
            _state(count=1, minimum=_ts(1), maximum=_ts(1)),
        )

from __future__ import annotations

import logging
import subprocess

import pytest

from application.postgres_sync import POSTGRES_FEATURE_VALUES_TABLE, POSTGRES_RAW_TABLES
from scripts.provision_postgres_role import (
    POSTGRES_HOST,
    POSTGRES_MATERIALIZED_VIEWS,
    POSTGRES_OWNER_ROLE,
    POSTGRES_PORT,
    POSTGRES_ROLE,
    POSTGRES_SCHEMAS,
    POSTGRES_SYNC_ROLE,
    POSTGRES_TABLES,
    ProvisioningConfig,
    provision_sql,
    psql_command,
    run,
    sanitized,
)


def _env() -> dict[str, str]:
    return {
        "MARKET_MACRO_POSTGRES_HOST": "10.10.1.3",
        "MARKET_MACRO_POSTGRES_PORT": "54321",
        "MARKET_MACRO_POSTGRES_DATABASE": "macro_loader",
        "MARKET_MACRO_POSTGRES_ADMIN_USER": "postgres",
        "MARKET_MACRO_POSTGRES_ADMIN_PASSWORD": "admin-secret",
        "MARKET_MACRO_POSTGRES_PASSWORD": "repo-secret",
    }


def test_exact_endpoint_and_repository_role() -> None:
    config = ProvisioningConfig.from_env(_env())
    assert (config.host, config.port) == (POSTGRES_HOST, POSTGRES_PORT)
    assert POSTGRES_ROLE == "macro-loader"
    command = psql_command(config)
    assert command[command.index("--host") + 1] == "10.10.1.3"
    assert command[command.index("--port") + 1] == "54321"


def test_sql_is_least_privilege_and_schema_scoped() -> None:
    sql = provision_sql("macro_loader", "repo-secret", "postgres-admin")
    assert 'CREATE ROLE "macro-loader"' in sql
    assert f'CREATE ROLE "{POSTGRES_OWNER_ROLE}" NOLOGIN' in sql
    for token in (
        "LOGIN PASSWORD",
        "NOSUPERUSER",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOBYPASSRLS",
    ):
        assert token in sql
    assert POSTGRES_SCHEMAS == ("macro_loader", "macro_loader_sync")
    raw_tables = tuple(
        table
        for schema, table in POSTGRES_TABLES
        if schema == "macro_loader" and table.startswith("macro_raw_")
    )
    assert raw_tables == POSTGRES_RAW_TABLES
    assert len(raw_tables) == 25
    assert "macro_raw" not in raw_tables
    assert ("macro_loader", POSTGRES_FEATURE_VALUES_TABLE) in POSTGRES_MATERIALIZED_VIEWS
    assert all(table != "fed_policy_features_daily" for _, table in POSTGRES_TABLES)
    assert all(schema != "macro_loader_retired" for schema, _ in POSTGRES_TABLES)
    assert (
        f'CREATE SCHEMA IF NOT EXISTS "macro_loader" AUTHORIZATION "{POSTGRES_OWNER_ROLE}"' in sql
    )
    assert (
        f'CREATE SCHEMA IF NOT EXISTS "macro_loader_sync" AUTHORIZATION "{POSTGRES_OWNER_ROLE}"'
        in sql
    )
    assert 'REVOKE CREATE ON SCHEMA "macro_loader" FROM "macro-loader"' in sql
    assert 'GRANT USAGE ON SCHEMA "macro_loader" TO "macro-loader"' in sql
    assert "to_regclass('macro_loader.macro_features_extended') IS NOT NULL" not in sql
    assert 'ALTER MATERIALIZED VIEW "macro_loader"."macro_features"' in sql
    assert 'GRANT SELECT ON TABLE "macro_loader"."macro_features"' in sql
    assert "GRANT USAGE, CREATE" not in sql
    for schema, table in POSTGRES_TABLES:
        expected_grant = (
            "GRANT SELECT ON TABLE"
            if table
            in {
                "schema_migrations",
            }
            else "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE"
        )
        assert f'{expected_grant} "{schema}"."{table}" TO "{POSTGRES_SYNC_ROLE}"' in sql
    for table in POSTGRES_RAW_TABLES:
        assert f"to_regclass('macro_loader.{table}') IS NOT NULL" in sql
        assert f'GRANT SELECT ON TABLE "macro_loader"."{table}" TO "macro-loader";' in sql
        assert (
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "macro_loader"."{table}" '
            f'TO "{POSTGRES_SYNC_ROLE}";' in sql
        )
    assert 'GRANT SELECT ON TABLE "macro_loader_sync"."schema_migrations" TO "macro-loader"' in sql
    assert "GRANT ALL" not in sql
    assert "macro_loader_retired" not in sql
    assert "public" not in {schema.lower() for schema in POSTGRES_SCHEMAS}


def test_sql_is_idempotent_and_fails_on_incompatible_existing_state() -> None:
    sql = provision_sql("macro_loader", "repo-secret", "postgres-admin")
    assert "IF NOT EXISTS (SELECT 1 FROM pg_roles" in sql
    assert "CREATE SCHEMA IF NOT EXISTS" in sql
    assert "existing macro-loader role has incompatible privileges" in sql
    assert f'ALTER SCHEMA "macro_loader" OWNER TO "{POSTGRES_OWNER_ROLE}"' in sql
    assert f'ALTER DEFAULT PRIVILEGES FOR ROLE "{POSTGRES_OWNER_ROLE}"' in sql
    assert "to_regclass('macro_loader_sync.schema_migrations') IS NOT NULL" in sql


def test_admin_and_application_passwords_must_be_distinct() -> None:
    env = _env()
    env["MARKET_MACRO_POSTGRES_PASSWORD"] = env["MARKET_MACRO_POSTGRES_ADMIN_PASSWORD"]
    try:
        ProvisioningConfig.from_env(env)
    except ValueError as exc:
        assert "must differ" in str(exc)
    else:
        raise AssertionError("shared admin/application password must fail")


def test_wrong_endpoint_and_missing_secrets_fail() -> None:
    env = _env()
    env["MARKET_MACRO_POSTGRES_HOST"] = "localhost"
    try:
        ProvisioningConfig.from_env(env)
    except ValueError as exc:
        assert "10.10.1.3:54321" in str(exc)
    else:
        raise AssertionError("wrong endpoint must fail")

    env = _env()
    env["MARKET_MACRO_POSTGRES_PASSWORD"] = ""
    try:
        ProvisioningConfig.from_env(env)
    except ValueError as exc:
        assert "MARKET_MACRO_POSTGRES_PASSWORD" in str(exc)
    else:
        raise AssertionError("missing application password must fail")


def test_sanitizer_removes_both_credentials() -> None:
    config = ProvisioningConfig.from_env(_env())
    text = "admin-secret failed while repo-secret was applied"
    result = sanitized(text, config)
    assert "admin-secret" not in result
    assert "repo-secret" not in result
    assert result.count("***") == 2


def test_run_logs_safe_provisioning_counts_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = ProvisioningConfig.from_env(_env())
    monkeypatch.setattr(
        "scripts.provision_postgres_role.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    caplog.set_level(logging.DEBUG, logger="scripts.provision_postgres_role")

    run(config)

    assert "PostgreSQL role provisioning started" in caplog.text
    assert "schema_count=2 table_count=28 materialized_view_count=1" in caplog.text
    assert "admin-secret" not in caplog.text
    assert "repo-secret" not in caplog.text
    assert "LOGIN PASSWORD" not in caplog.text


def test_run_failure_logs_only_error_class_and_no_psql_output(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = ProvisioningConfig.from_env(_env())
    stderr = "admin-secret repo-secret failed on CREATE ROLE private-role"
    monkeypatch.setattr(
        "scripts.provision_postgres_role.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", stderr),
    )
    caplog.set_level(logging.DEBUG, logger="scripts.provision_postgres_role")

    with pytest.raises(RuntimeError):
        run(config)

    assert "PostgreSQL role provisioning failed error_type=RuntimeError" in caplog.text
    assert "admin-secret" not in caplog.text
    assert "repo-secret" not in caplog.text
    assert "CREATE ROLE" not in caplog.text
    assert "private-role" not in caplog.text


def test_main_logs_configuration_failure_class_only(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from scripts import provision_postgres_role

    monkeypatch.setattr(provision_postgres_role, "configure_debug_logging", lambda _: None)
    monkeypatch.setattr(provision_postgres_role.sys, "argv", ["provision_postgres_role"])
    caplog.set_level(logging.DEBUG, logger="scripts.provision_postgres_role")

    assert provision_postgres_role.main() == 2

    assert "PostgreSQL role provisioning command started" in caplog.text
    assert "command failed error_type=ValueError" in caplog.text
    assert "MARKET_MACRO_POSTGRES_ADMIN_PASSWORD" not in caplog.text

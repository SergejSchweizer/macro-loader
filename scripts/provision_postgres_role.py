"""Provision the dedicated least-privilege PostgreSQL role for this repository."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from api.logging_config import configure_debug_logging
from application.postgres_sync import POSTGRES_FEATURE_VALUES_TABLE, POSTGRES_RAW_TABLES

logger = logging.getLogger(__name__)

POSTGRES_HOST = "10.10.1.3"
POSTGRES_PORT = 54321
POSTGRES_ROLE = "macro_reader"
POSTGRES_SYNC_ROLE = "macro_writer"
LEGACY_POSTGRES_ROLE = "macro-loader"
LEGACY_POSTGRES_SYNC_ROLE = "macro-loader-sync"
LEGACY_POSTGRES_OWNER_ROLE = "macro-loader-owner"
POSTGRES_OWNER_ROLE = POSTGRES_SYNC_ROLE
POSTGRES_SCHEMAS = ("macro_loader", "macro_loader_sync")
POSTGRES_TABLES = (
    *(("macro_loader", table) for table in POSTGRES_RAW_TABLES),
    ("macro_loader_sync", "gold_sync_state"),
    ("macro_loader_sync", "gold_row_hashes"),
    ("macro_loader_sync", "schema_migrations"),
)
POSTGRES_MATERIALIZED_VIEWS = (("macro_loader", POSTGRES_FEATURE_VALUES_TABLE),)


def _identifier(value: str) -> str:
    if not value:
        raise ValueError("PostgreSQL identifier cannot be empty")
    return '"' + value.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _table_identifier(schema: str, table: str) -> str:
    return f"{_identifier(schema)}.{_identifier(table)}"


def _conditional_table_grant(schema: str, table: str, role: str) -> str:
    privileges = (
        "SELECT"
        if table in {"schema_migrations", POSTGRES_FEATURE_VALUES_TABLE}
        else "SELECT, INSERT, UPDATE, DELETE"
    )
    return (
        f"IF to_regclass({_literal(f'{schema}.{table}')}) IS NOT NULL THEN\n"
        f"    GRANT {privileges} ON TABLE {_table_identifier(schema, table)} TO {role};\n"
        "END IF;"
    )


def _conditional_read_grant(schema: str, table: str, role: str) -> str:
    return (
        f"IF to_regclass({_literal(f'{schema}.{table}')}) IS NOT NULL THEN\n"
        f"    GRANT SELECT ON TABLE {_table_identifier(schema, table)} TO {role};\n"
        "END IF;"
    )


@dataclass(frozen=True, slots=True)
class ProvisioningConfig:
    host: str
    port: int
    database: str
    admin_user: str
    admin_password: str = field(repr=False)
    reader_password: str = field(repr=False)
    writer_password: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ProvisioningConfig:
        host = env.get("MARKET_MACRO_POSTGRES_HOST", POSTGRES_HOST).strip()
        port_text = env.get("MARKET_MACRO_POSTGRES_PORT", str(POSTGRES_PORT)).strip()
        database = env.get("MARKET_MACRO_POSTGRES_DATABASE", "").strip()
        admin_user = env.get("MARKET_MACRO_POSTGRES_ADMIN_USER", "").strip()
        admin_password = env.get("MARKET_MACRO_POSTGRES_ADMIN_PASSWORD", "")
        reader_password = env.get("MARKET_MACRO_POSTGRES_READER_PASSWORD", "")
        writer_password = env.get("MARKET_MACRO_POSTGRES_WRITER_PASSWORD", "")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError("MARKET_MACRO_POSTGRES_PORT must be an integer") from exc
        if host != POSTGRES_HOST or port != POSTGRES_PORT:
            raise ValueError(f"provisioning endpoint must be {POSTGRES_HOST}:{POSTGRES_PORT}")
        if not database:
            raise ValueError("MARKET_MACRO_POSTGRES_DATABASE is required")
        if database != "macro_loader":
            raise ValueError("MARKET_MACRO_POSTGRES_DATABASE must be macro_loader")
        if not admin_user:
            raise ValueError("MARKET_MACRO_POSTGRES_ADMIN_USER is required")
        if not admin_password:
            raise ValueError("MARKET_MACRO_POSTGRES_ADMIN_PASSWORD is required")
        if not reader_password:
            raise ValueError("MARKET_MACRO_POSTGRES_READER_PASSWORD is required")
        if not writer_password:
            raise ValueError("MARKET_MACRO_POSTGRES_WRITER_PASSWORD is required")
        if admin_password in (reader_password, writer_password):
            raise ValueError("application passwords must differ from administrator password")
        if reader_password == writer_password:
            raise ValueError("reader and writer passwords must differ")
        return cls(
            host, port, database, admin_user, admin_password, reader_password, writer_password
        )


def provision_sql(
    database: str, reader_password: str, writer_password: str, admin_user: str
) -> str:
    """Return idempotent fail-closed DDL without exposing administrator credentials."""
    if database not in {"macro_loader", "macro_loader_test"}:
        raise ValueError("provisioning database must be macro_loader or macro_loader_test")
    role_i = _identifier(POSTGRES_ROLE)
    role_l = _literal(POSTGRES_ROLE)
    sync_role_i = _identifier(POSTGRES_SYNC_ROLE)
    sync_role_l = _literal(POSTGRES_SYNC_ROLE)
    owner_i = _identifier(POSTGRES_OWNER_ROLE)
    admin_i = _identifier(admin_user)
    database_i = _identifier(database)
    reader_password_l = _literal(reader_password)
    writer_password_l = _literal(writer_password)
    schemas = "\n".join(
        f"CREATE SCHEMA IF NOT EXISTS {_identifier(schema)} AUTHORIZATION {owner_i};\n"
        f"ALTER SCHEMA {_identifier(schema)} OWNER TO {owner_i};\n"
        f"REVOKE ALL ON SCHEMA {_identifier(schema)} FROM PUBLIC;\n"
        f"REVOKE CREATE ON SCHEMA {_identifier(schema)} FROM {role_i}, {sync_role_i};\n"
        f"GRANT USAGE ON SCHEMA {_identifier(schema)} TO {role_i}, {sync_role_i};"
        for schema in POSTGRES_SCHEMAS
    )
    table_ownership = "\n".join(
        f"ALTER TABLE IF EXISTS {_table_identifier(schema, table)} OWNER TO {owner_i};"
        for schema, table in POSTGRES_TABLES
    )
    materialized_view_ownership = "\n".join(
        "DO $$ BEGIN\n"
        f"IF to_regclass({_literal(f'{schema}.{view}')}) IS NOT NULL THEN\n"
        f"    ALTER MATERIALIZED VIEW {_table_identifier(schema, view)} OWNER TO {owner_i};\n"
        "END IF;\nEND $$;"
        for schema, view in POSTGRES_MATERIALIZED_VIEWS
    )
    table_grants = "\n".join(
        [
            *(_conditional_read_grant(schema, table, role_i) for schema, table in POSTGRES_TABLES),
            *(
                _conditional_table_grant(schema, table, sync_role_i)
                for schema, table in POSTGRES_TABLES
            ),
        ]
    )
    view_grant = (
        f"IF to_regclass('macro_loader.{POSTGRES_FEATURE_VALUES_TABLE}') IS NOT NULL THEN\n"
        f'    GRANT SELECT ON TABLE "macro_loader"."{POSTGRES_FEATURE_VALUES_TABLE}" '
        f"TO {role_i}, {sync_role_i};\n"
        "END IF;"
    )
    feature_value_grant = (
        f"IF to_regclass('macro_loader.{POSTGRES_FEATURE_VALUES_TABLE}') IS NOT NULL THEN\n"
        f"    REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE "
        f'"macro_loader"."{POSTGRES_FEATURE_VALUES_TABLE}" FROM {role_i}, {sync_role_i};\n'
        "END IF;"
    )
    default_privileges = "\n".join(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_i} IN SCHEMA {_identifier(schema)} "
        f"GRANT SELECT ON TABLES TO {role_i};\n"
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_i} IN SCHEMA {_identifier(schema)} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {sync_role_i};\n"
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {admin_i} IN SCHEMA {_identifier(schema)} "
        f"GRANT SELECT ON TABLES TO {role_i};\n"
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {admin_i} IN SCHEMA {_identifier(schema)} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {sync_role_i};"
        for schema in POSTGRES_SCHEMAS
    )
    database_owner_statement = (
        f"ALTER DATABASE {database_i} OWNER TO {owner_i};" if database == "macro_loader" else ""
    )
    return f"""DO $provision$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_literal(LEGACY_POSTGRES_ROLE)}) THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role_l}) THEN
            RAISE EXCEPTION 'legacy reader and target reader roles both exist';
        END IF;
        ALTER ROLE {_identifier(LEGACY_POSTGRES_ROLE)} RENAME TO {role_i};
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_literal(LEGACY_POSTGRES_SYNC_ROLE)}) THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {sync_role_l}) THEN
            RAISE EXCEPTION 'legacy writer and target writer roles both exist';
        END IF;
        ALTER ROLE {_identifier(LEGACY_POSTGRES_SYNC_ROLE)} RENAME TO {sync_role_i};
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role_l}) THEN
        CREATE ROLE {role_i}
            LOGIN PASSWORD {reader_password_l}
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    ELSE
        IF EXISTS (
            SELECT 1 FROM pg_roles
            WHERE rolname = {role_l}
              AND (NOT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
                   OR rolreplication OR rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'existing macro_reader role has incompatible privileges';
        END IF;
        ALTER ROLE {role_i} PASSWORD {reader_password_l};
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {sync_role_l}) THEN
        CREATE ROLE {sync_role_i}
            LOGIN PASSWORD {writer_password_l}
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    ELSE
        IF EXISTS (
            SELECT 1 FROM pg_roles
            WHERE rolname = {sync_role_l}
              AND (NOT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
                   OR rolreplication OR rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'existing macro_writer role has incompatible privileges';
        END IF;
        ALTER ROLE {sync_role_i} PASSWORD {writer_password_l};
    END IF;
END
$provision$;

GRANT CONNECT ON DATABASE {database_i} TO {role_i};
GRANT {owner_i} TO {admin_i};
REVOKE {owner_i} FROM {role_i};
{database_owner_statement}
DO $retire_owner$
BEGIN
    IF current_database() = 'macro_loader'
       AND EXISTS (
           SELECT 1 FROM pg_roles
           WHERE rolname = {_literal(LEGACY_POSTGRES_OWNER_ROLE)}
       ) THEN
        IF EXISTS (
            SELECT 1 FROM pg_roles
            WHERE rolname = {_literal(LEGACY_POSTGRES_OWNER_ROLE)}
              AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
                   OR rolreplication OR rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'legacy macro owner has incompatible role attributes';
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_database
            WHERE datdba = (SELECT oid FROM pg_roles
                            WHERE rolname = {_literal(LEGACY_POSTGRES_OWNER_ROLE)})
              AND datname <> current_database()
        ) OR EXISTS (
            SELECT 1 FROM pg_tablespace
            WHERE spcowner = (SELECT oid FROM pg_roles
                              WHERE rolname = {_literal(LEGACY_POSTGRES_OWNER_ROLE)})
        ) OR EXISTS (
            SELECT 1 FROM pg_namespace
            WHERE nspowner = (SELECT oid FROM pg_roles
                              WHERE rolname = {_literal(LEGACY_POSTGRES_OWNER_ROLE)})
              AND nspname NOT IN ('macro_loader', 'macro_loader_sync',
                                  'macro_loader_retired', 'pg_toast')
        ) OR EXISTS (
            SELECT 1 FROM pg_class AS classes
            JOIN pg_namespace AS namespaces ON namespaces.oid = classes.relnamespace
            WHERE classes.relowner = (SELECT oid FROM pg_roles
                                      WHERE rolname = {_literal(LEGACY_POSTGRES_OWNER_ROLE)})
              AND namespaces.nspname NOT IN ('macro_loader', 'macro_loader_sync',
                                             'macro_loader_retired', 'pg_toast')
        ) OR EXISTS (
            SELECT 1 FROM pg_proc AS procedures
            JOIN pg_namespace AS namespaces ON namespaces.oid = procedures.pronamespace
            WHERE procedures.proowner = (SELECT oid FROM pg_roles
                                         WHERE rolname = {_literal(LEGACY_POSTGRES_OWNER_ROLE)})
              AND namespaces.nspname NOT IN ('macro_loader', 'macro_loader_sync',
                                             'macro_loader_retired')
        ) OR EXISTS (
            SELECT 1 FROM pg_type AS types
            JOIN pg_namespace AS namespaces ON namespaces.oid = types.typnamespace
            WHERE types.typowner = (SELECT oid FROM pg_roles
                                    WHERE rolname = {_literal(LEGACY_POSTGRES_OWNER_ROLE)})
              AND namespaces.nspname NOT IN ('macro_loader', 'macro_loader_sync',
                                             'macro_loader_retired', 'pg_toast')
        ) OR EXISTS (
            SELECT 1 FROM pg_default_acl AS defaults
            LEFT JOIN pg_namespace AS namespaces ON namespaces.oid = defaults.defaclnamespace
            WHERE defaults.defaclrole = (SELECT oid FROM pg_roles
                                         WHERE rolname = {_literal(LEGACY_POSTGRES_OWNER_ROLE)})
              AND (namespaces.nspname IS NULL OR namespaces.nspname NOT IN
                   ('macro_loader', 'macro_loader_sync', 'macro_loader_retired'))
        ) OR EXISTS (
            SELECT 1 FROM pg_shdepend
            WHERE refclassid = 'pg_authid'::regclass
              AND refobjid = (SELECT oid FROM pg_roles
                              WHERE rolname = {_literal(LEGACY_POSTGRES_OWNER_ROLE)})
              AND deptype = 'a'
        ) THEN
            RAISE EXCEPTION 'legacy macro owner has objects outside macro-loader schemas';
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_auth_members
            WHERE roleid = (SELECT oid FROM pg_roles
                            WHERE rolname = {_literal(LEGACY_POSTGRES_OWNER_ROLE)})
              AND member <> (SELECT oid FROM pg_roles WHERE rolname = {_literal(admin_user)})
        ) OR EXISTS (
            SELECT 1 FROM pg_auth_members
            WHERE member = (SELECT oid FROM pg_roles
                            WHERE rolname = {_literal(LEGACY_POSTGRES_OWNER_ROLE)})
        ) THEN
            RAISE EXCEPTION 'legacy macro owner has unexpected role memberships';
        END IF;
        EXECUTE 'REASSIGN OWNED BY {_identifier(LEGACY_POSTGRES_OWNER_ROLE)} '
                'TO {_identifier(POSTGRES_OWNER_ROLE)}';
        EXECUTE 'DROP OWNED BY {_identifier(LEGACY_POSTGRES_OWNER_ROLE)}';
        EXECUTE 'REVOKE {_identifier(LEGACY_POSTGRES_OWNER_ROLE)} '
                'FROM {_identifier(admin_user)}';
        EXECUTE 'DROP ROLE {_identifier(LEGACY_POSTGRES_OWNER_ROLE)}';
    END IF;
END
$retire_owner$;
{schemas}
{table_ownership}
{materialized_view_ownership}
REVOKE ALL ON ALL TABLES IN SCHEMA "macro_loader" FROM {role_i};
REVOKE ALL ON ALL TABLES IN SCHEMA "macro_loader_sync" FROM {role_i};
REVOKE ALL ON ALL TABLES IN SCHEMA "macro_loader" FROM {sync_role_i};
REVOKE ALL ON ALL TABLES IN SCHEMA "macro_loader_sync" FROM {sync_role_i};
REVOKE GRANT OPTION FOR SELECT ON ALL TABLES IN SCHEMA "macro_loader" FROM {role_i};
REVOKE GRANT OPTION FOR SELECT ON ALL TABLES IN SCHEMA "macro_loader_sync" FROM {role_i};
DO $grants$
BEGIN
{table_grants}
{feature_value_grant}
{view_grant}
END
$grants$;
REVOKE INSERT, UPDATE, DELETE ON TABLE "macro_loader_sync"."schema_migrations"
    FROM {role_i}, {sync_role_i};
{default_privileges}
"""


def psql_command(config: ProvisioningConfig) -> list[str]:
    return [
        "psql",
        "--host",
        config.host,
        "--port",
        str(config.port),
        "--username",
        config.admin_user,
        "--dbname",
        config.database,
        "--no-psqlrc",
        "--set",
        "ON_ERROR_STOP=1",
    ]


def sanitized(text: str, config: ProvisioningConfig) -> str:
    result = text
    for secret in (config.admin_password, config.reader_password, config.writer_password):
        if secret:
            result = result.replace(secret, "***")
    return result


def run(config: ProvisioningConfig) -> None:
    logger.debug("PostgreSQL role provisioning started")
    environment = dict(os.environ)
    environment["PGPASSWORD"] = config.admin_password
    try:
        completed = subprocess.run(
            psql_command(config),
            input=provision_sql(
                config.database,
                config.reader_password,
                config.writer_password,
                config.admin_user,
            ),
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
    except Exception as exc:
        logger.debug("PostgreSQL role provisioning failed error_type=%s", type(exc).__name__)
        raise
    if completed.returncode != 0:
        detail = sanitized(completed.stderr.strip(), config)
        logger.debug("PostgreSQL role provisioning failed error_type=RuntimeError")
        raise RuntimeError(f"PostgreSQL role provisioning failed: {detail or 'psql failed'}")
    logger.debug(
        "PostgreSQL role provisioning completed schema_count=%d table_count=%d "
        "materialized_view_count=%d",
        len(POSTGRES_SCHEMAS),
        len(POSTGRES_TABLES),
        len(POSTGRES_MATERIALIZED_VIEWS),
    )


def main() -> int:
    configure_debug_logging(Path.cwd())
    logger.debug("PostgreSQL role provisioning command started")
    try:
        run(ProvisioningConfig.from_env(os.environ))
    except (OSError, ValueError, RuntimeError) as exc:
        logger.debug(
            "PostgreSQL role provisioning command failed error_type=%s",
            type(exc).__name__,
        )
        print(str(exc), file=sys.stderr)
        return 2
    logger.debug("PostgreSQL role provisioning command completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

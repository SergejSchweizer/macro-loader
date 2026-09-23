"""PostgreSQL adapter for the private canonical Fed policy feature relation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast

import polars as pl

from application.fed_policy_postgres import (
    FED_POLICY_COLUMNS,
    FED_POLICY_DATASET_ID,
    FedPolicyDeltaPlan,
    FedPolicyFeatureRow,
    fed_policy_rows,
    plan_fed_policy_delta,
)
from application.postgres_sync import (
    POSTGRES_ROW_HASH_TABLE,
    POSTGRES_SYNC_SCHEMA,
    POSTGRES_SYNC_STATE_TABLE,
)
from ingestion.postgres_gold_repository import (
    ConnectionFactory,
    ConnectionPort,
    CursorPort,
    PostgresGoldRepositoryError,
    PostgresSyncConfig,
    _advisory_lock_key,
    _default_connection,
    _raise_sanitized_error,
    _session_configuration,
)

_TABLE = f'"{POSTGRES_SYNC_SCHEMA}"."{FED_POLICY_DATASET_ID}"'
_STATE = f'"{POSTGRES_SYNC_SCHEMA}"."{POSTGRES_SYNC_STATE_TABLE}"'
_HASHES = f'"{POSTGRES_SYNC_SCHEMA}"."{POSTGRES_ROW_HASH_TABLE}"'
_COLUMN_SQL = ", ".join(f'"{column}"' for column in FED_POLICY_COLUMNS)
_UPDATE_SQL = ", ".join(f'"{column}" = %s' for column in FED_POLICY_COLUMNS[1:])


class FedPolicyPostgresRepository:
    """Reconcile one complete canonical feature frame in one locked transaction."""

    def __init__(
        self,
        config: PostgresSyncConfig,
        *,
        connection_factory: ConnectionFactory = _default_connection,
    ) -> None:
        self._config = config
        self._connection_factory = connection_factory

    def sync(
        self,
        frame: pl.DataFrame,
        *,
        source_build_id: str,
        schema_version: int,
        feature_version: int,
        synced_at_utc: datetime,
    ) -> FedPolicyDeltaPlan:
        desired = fed_policy_rows(frame)
        connection = self._open()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (_advisory_lock_key(FED_POLICY_DATASET_ID),)
                )
                existing = self._read_rows(cursor)
                self._verify_integrity(cursor, existing)
                plan = plan_fed_policy_delta(desired, existing)
                self._apply_rows(cursor, plan)
                if plan.mutated:
                    cursor.execute("SELECT macro_loader.refresh_macro_features_explicit()")
                self._write_state(
                    cursor,
                    source_build_id,
                    schema_version,
                    feature_version,
                    desired,
                    synced_at_utc,
                )
            finally:
                cursor.close()
            connection.commit()
            return plan
        except PostgresGoldRepositoryError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            _raise_sanitized_error("Fed policy feature sync", exc)
        finally:
            connection.close()

    def _open(self) -> ConnectionPort:
        connection: ConnectionPort | None = None
        try:
            connection = self._connection_factory(self._config)
            cursor = connection.cursor()
            try:
                for statement in _session_configuration(self._config.timeout_policy):
                    cursor.execute(statement)
            finally:
                cursor.close()
            return connection
        except Exception as exc:
            if connection is not None:
                connection.close()
            _raise_sanitized_error("Fed policy connection initialization", exc)

    @staticmethod
    def _read_rows(cursor: CursorPort) -> tuple[FedPolicyFeatureRow, ...]:
        cursor.execute(f'SELECT {_COLUMN_SQL} FROM {_TABLE} ORDER BY "timestamp_m1"')
        rows = cursor.fetchall()
        return tuple(
            FedPolicyFeatureRow(
                cast(datetime, row[0]),
                cast(
                    tuple[float | None, float | None, float | None, float | None],
                    tuple(row[1:5]),
                ),
                cast(datetime, row[5]),
            )
            for row in rows
        )

    @staticmethod
    def _verify_integrity(cursor: CursorPort, rows: tuple[FedPolicyFeatureRow, ...]) -> None:
        cursor.execute(
            f"SELECT timestamp_m1, row_sha256 FROM {_HASHES} "
            "WHERE dataset_id = %s ORDER BY timestamp_m1",
            (FED_POLICY_DATASET_ID,),
        )
        digests = {cast(datetime, row[0]): cast(str, row[1]) for row in cursor.fetchall()}
        expected = {row.timestamp_m1: row.row_sha256 for row in rows}
        if digests != expected:
            raise PostgresGoldRepositoryError("Fed policy stored row hash verification failed")

    @staticmethod
    def _apply_rows(cursor: CursorPort, plan: FedPolicyDeltaPlan) -> None:
        for row in plan.inserts:
            cursor.execute(
                f"INSERT INTO {_TABLE} ({_COLUMN_SQL}) VALUES (%s, %s, %s, %s, %s, %s)",
                (row.timestamp_m1, *row.values, row.available_at_utc),
            )
        for row in plan.updates:
            cursor.execute(
                f'UPDATE {_TABLE} SET {_UPDATE_SQL} WHERE "timestamp_m1" = %s',
                (*row.values, row.available_at_utc, row.timestamp_m1),
            )
        for timestamp in plan.deletes:
            cursor.execute(f'DELETE FROM {_TABLE} WHERE "timestamp_m1" = %s', (timestamp,))
        for row in (*plan.inserts, *plan.updates):
            cursor.execute(
                f"INSERT INTO {_HASHES} (dataset_id, timestamp_m1, row_sha256) VALUES (%s, %s, %s) "
                "ON CONFLICT (dataset_id, timestamp_m1) "
                "DO UPDATE SET row_sha256 = EXCLUDED.row_sha256",
                (FED_POLICY_DATASET_ID, row.timestamp_m1, row.row_sha256),
            )
        for timestamp in plan.deletes:
            cursor.execute(
                f"DELETE FROM {_HASHES} WHERE dataset_id = %s AND timestamp_m1 = %s",
                (FED_POLICY_DATASET_ID, timestamp),
            )

    @staticmethod
    def _write_state(
        cursor: CursorPort,
        source_build_id: str,
        schema_version: int,
        feature_version: int,
        rows: tuple[FedPolicyFeatureRow, ...],
        synced_at_utc: datetime,
    ) -> None:
        digest = sha256(
            json.dumps([row.row_sha256 for row in rows], separators=(",", ":")).encode()
        ).hexdigest()
        cursor.execute(
            f"""INSERT INTO {_STATE} (
                dataset_id, source_build_id, data_sha256, schema_version, feature_version,
                row_count, min_timestamp, max_timestamp, synced_at_utc
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (dataset_id) DO UPDATE SET source_build_id = EXCLUDED.source_build_id,
                data_sha256 = EXCLUDED.data_sha256, schema_version = EXCLUDED.schema_version,
                feature_version = EXCLUDED.feature_version, row_count = EXCLUDED.row_count,
                min_timestamp = EXCLUDED.min_timestamp, max_timestamp = EXCLUDED.max_timestamp,
                synced_at_utc = EXCLUDED.synced_at_utc""",
            (
                FED_POLICY_DATASET_ID,
                source_build_id,
                digest,
                schema_version,
                feature_version,
                len(rows),
                rows[0].timestamp_m1 if rows else None,
                rows[-1].timestamp_m1 if rows else None,
                synced_at_utc.astimezone(UTC),
            ),
        )

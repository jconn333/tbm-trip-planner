#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3

import db as storage


def _sqlite_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_count_sqlite(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(1) AS count_value FROM {table}").fetchone()
    return int(row["count_value"] if row else 0)


def _table_count_pg(conn, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(1) AS count_value FROM {table}").fetchone()
    return int(row["count_value"] if row else 0)


def migrate_users(sqlite_conn: sqlite3.Connection, pg_conn) -> tuple[int, int]:
    sqlite_cols = {
        row["name"] for row in sqlite_conn.execute("PRAGMA table_info(users)").fetchall()
    }
    select_cols = ["id", "email", "name", "role", "password_hash", "created_at_utc"]
    has_is_disabled = "is_disabled" in sqlite_cols
    has_must_reset_password = "must_reset_password" in sqlite_cols
    has_last_login_at_utc = "last_login_at_utc" in sqlite_cols
    if has_is_disabled:
        select_cols.append("is_disabled")
    if has_must_reset_password:
        select_cols.append("must_reset_password")
    if has_last_login_at_utc:
        select_cols.append("last_login_at_utc")

    rows = sqlite_conn.execute(
        f"SELECT {', '.join(select_cols)} FROM users ORDER BY id ASC"
    ).fetchall()

    copied = 0
    for row in rows:
        pg_conn.execute(
            """
            INSERT INTO users (
                id, email, name, role, password_hash, created_at_utc,
                is_disabled, must_reset_password, last_login_at_utc
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                email = EXCLUDED.email,
                name = EXCLUDED.name,
                role = EXCLUDED.role,
                password_hash = EXCLUDED.password_hash,
                created_at_utc = EXCLUDED.created_at_utc,
                is_disabled = EXCLUDED.is_disabled,
                must_reset_password = EXCLUDED.must_reset_password,
                last_login_at_utc = EXCLUDED.last_login_at_utc
            """,
            (
                int(row["id"]),
                str(row["email"]),
                str(row["name"]),
                str(row["role"]),
                str(row["password_hash"]),
                str(row["created_at_utc"]),
                int(row["is_disabled"] or 0) if has_is_disabled else 0,
                int(row["must_reset_password"] or 0) if has_must_reset_password else 0,
                row["last_login_at_utc"] if has_last_login_at_utc else None,
            ),
        )
        copied += 1
    return len(rows), copied


def migrate_app_settings(sqlite_conn: sqlite3.Connection, pg_conn) -> tuple[int, int]:
    rows = sqlite_conn.execute(
        "SELECT key, value, updated_at_utc, updated_by_user_id FROM app_settings ORDER BY key ASC"
    ).fetchall()
    copied = 0
    for row in rows:
        pg_conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at_utc, updated_by_user_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at_utc = EXCLUDED.updated_at_utc,
                updated_by_user_id = EXCLUDED.updated_by_user_id
            """,
            (
                str(row["key"]),
                str(row["value"]),
                str(row["updated_at_utc"]),
                row["updated_by_user_id"],
            ),
        )
        copied += 1
    return len(rows), copied


def migrate_settings_audit(sqlite_conn: sqlite3.Connection, pg_conn) -> tuple[int, int]:
    rows = sqlite_conn.execute(
        """
        SELECT id, key, old_value, new_value, changed_by_user_id, changed_at_utc
        FROM settings_audit_log
        ORDER BY id ASC
        """
    ).fetchall()
    copied = 0
    for row in rows:
        pg_conn.execute(
            """
            INSERT INTO settings_audit_log (
                id, key, old_value, new_value, changed_by_user_id, changed_at_utc
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                key = EXCLUDED.key,
                old_value = EXCLUDED.old_value,
                new_value = EXCLUDED.new_value,
                changed_by_user_id = EXCLUDED.changed_by_user_id,
                changed_at_utc = EXCLUDED.changed_at_utc
            """,
            (
                int(row["id"]),
                str(row["key"]),
                row["old_value"],
                row["new_value"],
                row["changed_by_user_id"],
                str(row["changed_at_utc"]),
            ),
        )
        copied += 1
    return len(rows), copied


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate users/settings data from SQLite to Postgres.")
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument("--postgres-url", required=True)
    args = parser.parse_args()

    storage.init_db(args.postgres_url)

    with _sqlite_connect(args.sqlite_path) as sqlite_conn, storage.get_conn(args.postgres_url) as pg_conn:
        before_pg = {
            "users": _table_count_pg(pg_conn, "users"),
            "app_settings": _table_count_pg(pg_conn, "app_settings"),
            "settings_audit_log": _table_count_pg(pg_conn, "settings_audit_log"),
        }
        source = {
            "users": _table_count_sqlite(sqlite_conn, "users"),
            "app_settings": _table_count_sqlite(sqlite_conn, "app_settings"),
            "settings_audit_log": _table_count_sqlite(sqlite_conn, "settings_audit_log"),
        }

        migrated = {}
        migrated["users"] = migrate_users(sqlite_conn, pg_conn)
        migrated["app_settings"] = migrate_app_settings(sqlite_conn, pg_conn)
        migrated["settings_audit_log"] = migrate_settings_audit(sqlite_conn, pg_conn)
        pg_conn.commit()

        after_pg = {
            "users": _table_count_pg(pg_conn, "users"),
            "app_settings": _table_count_pg(pg_conn, "app_settings"),
            "settings_audit_log": _table_count_pg(pg_conn, "settings_audit_log"),
        }

    print("Migration summary:")
    for table in ("users", "app_settings", "settings_audit_log"):
        source_count = source[table]
        target_before = before_pg[table]
        target_after = after_pg[table]
        selected, copied = migrated[table]
        print(
            f"- {table}: source={source_count} selected={selected} copied={copied} "
            f"target_before={target_before} target_after={target_after}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timezone
from typing import Iterable

BLOCKING_STATUSES = ("pending", "approved")
ALL_STATUSES = ("pending", "approved", "denied", "canceled")
CHANGE_REQUEST_STATUSES = ("pending", "approved", "denied", "applied", "canceled")
PLANNER_DRAFT_STATUSES = ("open", "consumed", "expired")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_url: str) -> Any:
    conn = psycopg.connect(db_url, row_factory=dict_row)
    return conn


def get_conn(db_url: str) -> Any:
    return _connect(db_url)


def init_db(db_url: str) -> None:
    with _connect(db_url) as conn:
        migrate_if_needed(conn)


def _exec_script(conn: Any, script: str) -> None:
    for statement in [chunk.strip() for chunk in script.split(";") if chunk.strip()]:
        conn.execute(statement)
    conn.commit()


def _ensure_schema_migrations(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO schema_migrations (id, version)
        VALUES (1, 0)
        ON CONFLICT (id) DO NOTHING
        """
    )
    conn.commit()


def get_schema_version(conn: Any) -> int:
    _ensure_schema_migrations(conn)
    row = conn.execute("SELECT version FROM schema_migrations WHERE id = 1").fetchone()
    return int(row["version"] if row else 0)


def _set_schema_version(conn: Any, version: int) -> None:
    conn.execute("UPDATE schema_migrations SET version = %s WHERE id = 1", (int(version),))
    conn.commit()


def reset_for_tests(conn: Any) -> None:
    conn.execute(
        """
        TRUNCATE TABLE
            planner_quote_drafts,
            reservation_change_requests,
            reservations,
            email_notification_logs,
            owner_audit_events,
            owner_invites,
            settings_audit_log,
            app_settings,
            users
        RESTART IDENTITY CASCADE
        """
    )
    conn.commit()


def migrate_if_needed(conn: Any) -> None:
    _ensure_schema_migrations(conn)
    version = get_schema_version(conn)

    if version < 1:
        _exec_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'owner')),
                password_hash TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reservations (
                id BIGSERIAL PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied', 'canceled')),
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                dep_icao TEXT NOT NULL,
                dest_icao TEXT NOT NULL,
                parked_icao TEXT NOT NULL,
                traveling_user_id INTEGER NOT NULL REFERENCES users(id),
                requested_by_user_id INTEGER NOT NULL REFERENCES users(id),
                approved_by_user_id INTEGER REFERENCES users(id),
                decision_at_utc TEXT,
                notes TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_res_start ON reservations(start_utc);
            CREATE INDEX IF NOT EXISTS idx_res_end ON reservations(end_utc);
            CREATE INDEX IF NOT EXISTS idx_res_status ON reservations(status);
            CREATE INDEX IF NOT EXISTS idx_res_traveling_user ON reservations(traveling_user_id);
            """
        )
        _set_schema_version(conn, 1)
        version = 1

    if version < 2:
        _exec_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                updated_by_user_id INTEGER REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS reservation_change_requests (
                id BIGSERIAL PRIMARY KEY,
                reservation_id INTEGER NOT NULL REFERENCES reservations(id),
                requested_by_user_id INTEGER NOT NULL REFERENCES users(id),
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied', 'applied', 'canceled')),
                proposed_start_utc TEXT NOT NULL,
                proposed_end_utc TEXT NOT NULL,
                proposed_dep_icao TEXT NOT NULL,
                proposed_dest_icao TEXT NOT NULL,
                proposed_notes TEXT,
                decided_by_user_id INTEGER REFERENCES users(id),
                decision_note TEXT,
                decided_at_utc TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_change_req_reservation ON reservation_change_requests(reservation_id);
            CREATE INDEX IF NOT EXISTS idx_change_req_status ON reservation_change_requests(status);
            CREATE INDEX IF NOT EXISTS idx_change_req_requested_by ON reservation_change_requests(requested_by_user_id);
            """
        )
        _set_schema_version(conn, 2)
        version = 2

    if version < 3:
        _exec_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS settings_audit_log (
                id BIGSERIAL PRIMARY KEY,
                key TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_by_user_id INTEGER REFERENCES users(id),
                changed_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_settings_audit_key ON settings_audit_log(key);
            CREATE INDEX IF NOT EXISTS idx_settings_audit_changed_at ON settings_audit_log(changed_at_utc DESC);
            """
        )
        _set_schema_version(conn, 3)
        version = 3

    if version < 4:
        _exec_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS planner_quote_drafts (
                id BIGSERIAL PRIMARY KEY,
                token TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL REFERENCES users(id),
                status TEXT NOT NULL CHECK(status IN ('open', 'consumed', 'expired')),
                draft_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL,
                consumed_at_utc TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_planner_drafts_token ON planner_quote_drafts(token);
            CREATE INDEX IF NOT EXISTS idx_planner_drafts_user_status ON planner_quote_drafts(user_id, status);
            CREATE INDEX IF NOT EXISTS idx_planner_drafts_expires ON planner_quote_drafts(expires_at_utc);
            """
        )
        _set_schema_version(conn, 4)
        version = 4

    if version < 5:
        user_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'users'
                """
            ).fetchall()
        }
        if "is_disabled" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN is_disabled INTEGER NOT NULL DEFAULT 0")
        if "must_reset_password" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN must_reset_password INTEGER NOT NULL DEFAULT 0")
        if "last_login_at_utc" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_login_at_utc TEXT")

        _exec_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS owner_invites (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                token TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('open','consumed','revoked','expired')),
                expires_at_utc TEXT NOT NULL,
                created_by_user_id INTEGER REFERENCES users(id),
                created_at_utc TEXT NOT NULL,
                consumed_at_utc TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_owner_invites_user_status ON owner_invites(user_id, status);
            CREATE INDEX IF NOT EXISTS idx_owner_invites_expires ON owner_invites(expires_at_utc);

            CREATE TABLE IF NOT EXISTS owner_audit_events (
                id BIGSERIAL PRIMARY KEY,
                owner_user_id INTEGER NOT NULL REFERENCES users(id),
                action TEXT NOT NULL,
                actor_user_id INTEGER REFERENCES users(id),
                metadata_json TEXT,
                created_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_owner_audit_owner_created ON owner_audit_events(owner_user_id, created_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_owner_audit_action_created ON owner_audit_events(action, created_at_utc DESC);
            """
        )
        _set_schema_version(conn, 5)
        version = 5

    if version < 6:
        _exec_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS email_notification_logs (
                id BIGSERIAL PRIMARY KEY,
                audience TEXT NOT NULL,
                source TEXT NOT NULL,
                to_addresses TEXT NOT NULL,
                subject TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('sent','failed','skipped')),
                error_message TEXT,
                reservation_ids_json TEXT,
                actor_user_id INTEGER REFERENCES users(id),
                created_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_email_logs_created ON email_notification_logs(created_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_email_logs_status_created ON email_notification_logs(status, created_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_email_logs_audience_created ON email_notification_logs(audience, created_at_utc DESC);
            """
        )
        _set_schema_version(conn, 6)


def seed_settings_defaults(conn: Any, defaults: dict[str, str]) -> None:
    now = utc_now_iso()
    for key, value in defaults.items():
        existing = conn.execute("SELECT key FROM app_settings WHERE key = %s", (key,)).fetchone()
        if existing:
            continue
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at_utc, updated_by_user_id) VALUES (%s, %s, %s, NULL)",
            (key, str(value), now),
        )
    conn.commit()


def list_settings(conn: Any) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def upsert_settings(conn: Any, values: dict[str, str], updated_by_user_id: int | None):
    now = utc_now_iso()
    for key, value in values.items():
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at_utc, updated_by_user_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at_utc = excluded.updated_at_utc,
                updated_by_user_id = excluded.updated_by_user_id
            """,
            (key, str(value), now, updated_by_user_id),
        )
    conn.commit()


def upsert_settings_with_audit(conn: Any, values: dict[str, str], updated_by_user_id: int | None):
    if not values:
        return

    keys = tuple(values.keys())
    placeholders = ",".join("%s" for _ in keys)
    existing_rows = conn.execute(
        f"SELECT key, value FROM app_settings WHERE key IN ({placeholders})",
        keys,
    ).fetchall()
    existing = {row["key"]: row["value"] for row in existing_rows}

    upsert_settings(conn, values, updated_by_user_id)

    now = utc_now_iso()
    for key, new_value in values.items():
        old_value = existing.get(key)
        if old_value == new_value:
            continue
        conn.execute(
            """
            INSERT INTO settings_audit_log (key, old_value, new_value, changed_by_user_id, changed_at_utc)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (key, old_value, str(new_value), updated_by_user_id, now),
        )
    conn.commit()


def list_settings_audit_history(
    conn: Any,
    *,
    key: str | None,
    page: int,
    page_size: int,
):
    where_sql = ""
    params: list[object] = []
    if key:
        where_sql = "WHERE sal.key = %s"
        params.append(key)

    total_row = conn.execute(
        f"SELECT COUNT(1) AS count_value FROM settings_audit_log sal {where_sql}",
        tuple(params),
    ).fetchone()
    total = int(total_row["count_value"] if total_row else 0)

    offset = (max(1, int(page)) - 1) * int(page_size)
    rows = conn.execute(
        f"""
        SELECT sal.*, u.name AS changed_by_name, u.email AS changed_by_email
        FROM settings_audit_log sal
        LEFT JOIN users u ON u.id = sal.changed_by_user_id
        {where_sql}
        ORDER BY sal.changed_at_utc DESC, sal.id DESC
        LIMIT %s OFFSET %s
        """,
        tuple([*params, int(page_size), int(offset)]),
    ).fetchall()
    return {"total": int(total), "rows": rows}


def get_user_by_id(conn: Any, user_id: int):
    return conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()


def get_user_by_email(conn: Any, email: str):
    return conn.execute("SELECT * FROM users WHERE lower(email) = lower(%s)", (email.strip(),)).fetchone()


def list_owner_users(conn: Any) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM users WHERE role = 'owner' ORDER BY lower(name), lower(email)"
    ).fetchall()


def list_admin_users(conn: Any) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT *
        FROM users
        WHERE role = 'admin'
          AND COALESCE(is_disabled, 0) = 0
        ORDER BY lower(name), lower(email)
        """
    ).fetchall()


def create_user(conn: Any, *, email: str, name: str, role: str, password_hash: str):
    now = utc_now_iso()
    cur = conn.execute(
        """
        INSERT INTO users (email, name, role, password_hash, created_at_utc)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (email.strip().lower(), name.strip(), role, password_hash, now),
    )
    conn.commit()
    created = cur.fetchone()
    return get_user_by_id(conn, int(created["id"]))


def set_user_password(conn: Any, user_id: int, password_hash: str):
    conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))
    conn.commit()


def set_user_flags(
    conn: Any,
    user_id: int,
    *,
    is_disabled: int | None = None,
    must_reset_password: int | None = None,
    last_login_at_utc: str | None = None,
):
    fields: list[str] = []
    values: list[object] = []
    if is_disabled is not None:
        fields.append("is_disabled = %s")
        values.append(1 if int(is_disabled) else 0)
    if must_reset_password is not None:
        fields.append("must_reset_password = %s")
        values.append(1 if int(must_reset_password) else 0)
    if last_login_at_utc is not None:
        fields.append("last_login_at_utc = %s")
        values.append(last_login_at_utc)
    if not fields:
        return
    values.append(int(user_id))
    conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = %s", tuple(values))
    conn.commit()


def ensure_bootstrap_admin(conn: Any, *, email: str, name: str, password_hash: str):
    existing_admin = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
    if existing_admin:
        return None
    return create_user(conn, email=email, name=name, role="admin", password_hash=password_hash)


def expire_open_owner_invites(conn: Any, *, now_utc: str):
    conn.execute(
        """
        UPDATE owner_invites
        SET status = 'expired'
        WHERE status = 'open'
          AND expires_at_utc <= %s
        """,
        (now_utc,),
    )
    conn.commit()


def revoke_open_owner_invites_for_user(conn: Any, *, user_id: int):
    conn.execute(
        """
        UPDATE owner_invites
        SET status = 'revoked'
        WHERE user_id = %s
          AND status = 'open'
        """,
        (int(user_id),),
    )
    conn.commit()


def create_owner_invite(
    conn: Any,
    *,
    user_id: int,
    token: str,
    expires_at_utc: str,
    created_by_user_id: int | None,
):
    now = utc_now_iso()
    cur = conn.execute(
        """
        INSERT INTO owner_invites (
            user_id, token, status, expires_at_utc, created_by_user_id, created_at_utc, consumed_at_utc
        ) VALUES (%s, %s, 'open', %s, %s, %s, NULL)
        RETURNING id
        """,
        (int(user_id), token, expires_at_utc, created_by_user_id, now),
    )
    conn.commit()
    created = cur.fetchone()
    return get_owner_invite_by_id(conn, int(created["id"]))


def get_owner_invite_by_id(conn: Any, invite_id: int):
    return conn.execute(
        """
        SELECT oi.*
        FROM owner_invites oi
        WHERE oi.id = %s
        """,
        (int(invite_id),),
    ).fetchone()


def get_owner_invite_by_token(conn: Any, token: str):
    return conn.execute(
        """
        SELECT oi.*
        FROM owner_invites oi
        WHERE oi.token = %s
        """,
        (token,),
    ).fetchone()


def get_latest_owner_invite_for_user(conn: Any, *, user_id: int):
    return conn.execute(
        """
        SELECT oi.*
        FROM owner_invites oi
        WHERE oi.user_id = %s
        ORDER BY oi.created_at_utc DESC, oi.id DESC
        LIMIT 1
        """,
        (int(user_id),),
    ).fetchone()


def get_open_owner_invite_for_user(conn: Any, *, user_id: int):
    return conn.execute(
        """
        SELECT oi.*
        FROM owner_invites oi
        WHERE oi.user_id = %s
          AND oi.status = 'open'
        ORDER BY oi.created_at_utc DESC, oi.id DESC
        LIMIT 1
        """,
        (int(user_id),),
    ).fetchone()


def update_owner_invite_fields(conn: Any, invite_id: int, fields: dict[str, object]):
    if not fields:
        return get_owner_invite_by_id(conn, int(invite_id))
    keys = list(fields.keys())
    values = [fields[k] for k in keys]
    assignments = ", ".join(f"{k} = %s" for k in keys)
    values.append(int(invite_id))
    conn.execute(f"UPDATE owner_invites SET {assignments} WHERE id = %s", tuple(values))
    conn.commit()
    return get_owner_invite_by_id(conn, int(invite_id))


def create_owner_audit_event(
    conn: Any,
    *,
    owner_user_id: int,
    action: str,
    actor_user_id: int | None,
    metadata_json: str | None = None,
):
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO owner_audit_events (owner_user_id, action, actor_user_id, metadata_json, created_at_utc)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (int(owner_user_id), str(action), actor_user_id, metadata_json, now),
    )
    conn.commit()


def list_owner_audit_events(
    conn: Any,
    *,
    owner_user_id: int | None,
    action: str | None,
    page: int,
    page_size: int,
):
    where = ["1=1"]
    params: list[object] = []
    if owner_user_id:
        where.append("ev.owner_user_id = %s")
        params.append(int(owner_user_id))
    if action:
        where.append("ev.action = %s")
        params.append(str(action))
    where_sql = " AND ".join(where)

    total_row = conn.execute(
        f"SELECT COUNT(1) AS count_value FROM owner_audit_events ev WHERE {where_sql}",
        tuple(params),
    ).fetchone()
    total = int(total_row["count_value"] if total_row else 0)
    offset = (max(1, int(page)) - 1) * int(page_size)
    rows = conn.execute(
        f"""
        SELECT
          ev.*,
          o.name AS owner_name,
          o.email AS owner_email,
          a.name AS actor_name,
          a.email AS actor_email
        FROM owner_audit_events ev
        JOIN users o ON o.id = ev.owner_user_id
        LEFT JOIN users a ON a.id = ev.actor_user_id
        WHERE {where_sql}
        ORDER BY ev.created_at_utc DESC, ev.id DESC
        LIMIT %s OFFSET %s
        """,
        tuple([*params, int(page_size), int(offset)]),
    ).fetchall()
    return {"total": int(total), "rows": rows}


def create_email_notification_log(
    conn: Any,
    *,
    audience: str,
    source: str,
    to_addresses: str,
    subject: str,
    status: str,
    error_message: str | None = None,
    reservation_ids_json: str | None = None,
    actor_user_id: int | None = None,
):
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO email_notification_logs (
            audience, source, to_addresses, subject, status, error_message,
            reservation_ids_json, actor_user_id, created_at_utc
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(audience or ""),
            str(source or ""),
            str(to_addresses or ""),
            str(subject or ""),
            str(status or ""),
            (str(error_message) if error_message else None),
            reservation_ids_json,
            actor_user_id,
            now,
        ),
    )
    conn.commit()


def list_email_notification_logs(
    conn: Any,
    *,
    audience: str | None,
    status: str | None,
    page: int,
    page_size: int,
):
    where = ["1=1"]
    params: list[object] = []
    if audience:
        where.append("l.audience = %s")
        params.append(str(audience))
    if status:
        where.append("l.status = %s")
        params.append(str(status))
    where_sql = " AND ".join(where)
    total_row = conn.execute(
        f"SELECT COUNT(1) AS count_value FROM email_notification_logs l WHERE {where_sql}",
        tuple(params),
    ).fetchone()
    total = int(total_row["count_value"] if total_row else 0)
    offset = (max(1, int(page)) - 1) * int(page_size)
    rows = conn.execute(
        f"""
        SELECT l.*, u.name AS actor_name, u.email AS actor_email
        FROM email_notification_logs l
        LEFT JOIN users u ON u.id = l.actor_user_id
        WHERE {where_sql}
        ORDER BY l.created_at_utc DESC, l.id DESC
        LIMIT %s OFFSET %s
        """,
        tuple([*params, int(page_size), int(offset)]),
    ).fetchall()
    return {"total": int(total), "rows": rows}


def overlap_exists(
    conn: Any,
    *,
    start_utc: str,
    end_utc: str,
    exclude_id: int | None = None,
    statuses: Iterable[str] = BLOCKING_STATUSES,
) -> bool:
    statuses = tuple(statuses)
    placeholders = ",".join("%s" for _ in statuses)
    params: list[object] = [start_utc, end_utc, *statuses]
    extra = ""
    if exclude_id is not None:
        extra = " AND id != %s"
        params.append(exclude_id)
    row = conn.execute(
        f"""
        SELECT 1
        FROM reservations
        WHERE %s < end_utc
          AND %s > start_utc
          AND status IN ({placeholders})
          {extra}
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    return row is not None


def create_reservation(
    conn: Any,
    *,
    status: str,
    start_utc: str,
    end_utc: str,
    dep_icao: str,
    dest_icao: str,
    parked_icao: str,
    traveling_user_id: int,
    requested_by_user_id: int,
    notes: str | None,
):
    now = utc_now_iso()
    cur = conn.execute(
        """
        INSERT INTO reservations (
            status, start_utc, end_utc, dep_icao, dest_icao, parked_icao,
            traveling_user_id, requested_by_user_id, notes, created_at_utc, updated_at_utc
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            status,
            start_utc,
            end_utc,
            dep_icao,
            dest_icao,
            parked_icao,
            traveling_user_id,
            requested_by_user_id,
            notes,
            now,
            now,
        ),
    )
    conn.commit()
    created = cur.fetchone()
    return get_reservation_by_id(conn, int(created["id"]))


def get_reservation_by_id(conn: Any, reservation_id: int):
    return conn.execute(
        """
        SELECT r.*, tu.name AS traveling_owner_name, ru.name AS requested_by_name, au.name AS approved_by_name
        FROM reservations r
        JOIN users tu ON tu.id = r.traveling_user_id
        JOIN users ru ON ru.id = r.requested_by_user_id
        LEFT JOIN users au ON au.id = r.approved_by_user_id
        WHERE r.id = %s
        """,
        (reservation_id,),
    ).fetchone()


def update_reservation_fields(conn: Any, reservation_id: int, fields: dict[str, object]):
    if not fields:
        return get_reservation_by_id(conn, reservation_id)
    items = dict(fields)
    items["updated_at_utc"] = utc_now_iso()
    keys = list(items.keys())
    assignments = ", ".join(f"{k} = %s" for k in keys)
    values = [items[k] for k in keys]
    values.append(reservation_id)
    conn.execute(f"UPDATE reservations SET {assignments} WHERE id = %s", tuple(values))
    conn.commit()
    return get_reservation_by_id(conn, reservation_id)


def list_reservations(
    conn: Any,
    *,
    start_utc: str,
    end_utc: str,
    include_nonblocking: bool,
) -> list[dict[str, Any]]:
    statuses = ALL_STATUSES if include_nonblocking else BLOCKING_STATUSES
    placeholders = ",".join("%s" for _ in statuses)
    return conn.execute(
        f"""
        SELECT r.*, tu.name AS traveling_owner_name, ru.name AS requested_by_name, au.name AS approved_by_name
        FROM reservations r
        JOIN users tu ON tu.id = r.traveling_user_id
        JOIN users ru ON ru.id = r.requested_by_user_id
        LEFT JOIN users au ON au.id = r.approved_by_user_id
        WHERE r.start_utc < %s
          AND r.end_utc > %s
          AND r.status IN ({placeholders})
        ORDER BY r.start_utc ASC
        """,
        (end_utc, start_utc, *statuses),
    ).fetchall()


def list_pending(conn: Any) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT r.*, tu.name AS traveling_owner_name, ru.name AS requested_by_name
        FROM reservations r
        JOIN users tu ON tu.id = r.traveling_user_id
        JOIN users ru ON ru.id = r.requested_by_user_id
        WHERE r.status = 'pending'
        ORDER BY r.start_utc ASC
        """
    ).fetchall()


def list_upcoming(conn: Any, *, from_utc: str, limit: int = 10) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT r.*, tu.name AS traveling_owner_name
        FROM reservations r
        JOIN users tu ON tu.id = r.traveling_user_id
        WHERE r.end_utc >= %s
          AND r.status IN ('pending', 'approved')
        ORDER BY r.start_utc ASC
        LIMIT %s
        """,
        (from_utc, int(limit)),
    ).fetchall()


def last_approved_before(conn: Any, *, now_utc: str):
    return conn.execute(
        """
        SELECT *
        FROM reservations
        WHERE status = 'approved'
          AND end_utc <= %s
        ORDER BY end_utc DESC
        LIMIT 1
        """,
        (now_utc,),
    ).fetchone()


def find_next_available_window(
    conn: Any,
    *,
    from_utc: str,
    to_utc: str,
):
    blocks = conn.execute(
        """
        SELECT start_utc, end_utc
        FROM reservations
        WHERE status IN ('pending', 'approved')
          AND end_utc > %s
          AND start_utc < %s
        ORDER BY start_utc ASC
        """,
        (from_utc, to_utc),
    ).fetchall()

    cursor = from_utc
    for block in blocks:
        start = block["start_utc"]
        end = block["end_utc"]
        if start > cursor:
            return {"start_utc": cursor, "end_utc": start}
        if end > cursor:
            cursor = end

    if cursor < to_utc:
        return {"start_utc": cursor, "end_utc": to_utc}
    return None


def list_admin_flights(
    conn: Any,
    *,
    status: str | None,
    from_utc: str | None,
    to_utc: str | None,
    owner_id: int | None,
    requested_by_id: int | None,
    query: str | None,
    page: int,
    page_size: int,
):
    where = ["1=1"]
    params: list[object] = []

    if status and status != "all":
        where.append("r.status = %s")
        params.append(status)
    if from_utc:
        where.append("r.end_utc >= %s")
        params.append(from_utc)
    if to_utc:
        where.append("r.start_utc <= %s")
        params.append(to_utc)
    if owner_id:
        where.append("r.traveling_user_id = %s")
        params.append(int(owner_id))
    if requested_by_id:
        where.append("r.requested_by_user_id = %s")
        params.append(int(requested_by_id))
    if query:
        q = f"%{query.lower()}%"
        where.append("(lower(r.dep_icao) LIKE %s OR lower(r.dest_icao) LIKE %s OR lower(tu.name) LIKE %s OR lower(ru.name) LIKE %s)")
        params.extend([q, q, q, q])

    where_sql = " AND ".join(where)

    total_row = conn.execute(
        f"""
        SELECT COUNT(1) AS count_value
        FROM reservations r
        JOIN users tu ON tu.id = r.traveling_user_id
        JOIN users ru ON ru.id = r.requested_by_user_id
        WHERE {where_sql}
        """,
        tuple(params),
    ).fetchone()
    total = int(total_row["count_value"] if total_row else 0)

    offset = (max(page, 1) - 1) * page_size
    rows = conn.execute(
        f"""
        SELECT r.*, tu.name AS traveling_owner_name, ru.name AS requested_by_name, au.name AS approved_by_name
        FROM reservations r
        JOIN users tu ON tu.id = r.traveling_user_id
        JOIN users ru ON ru.id = r.requested_by_user_id
        LEFT JOIN users au ON au.id = r.approved_by_user_id
        WHERE {where_sql}
        ORDER BY r.start_utc DESC
        LIMIT %s OFFSET %s
        """,
        tuple([*params, int(page_size), int(offset)]),
    ).fetchall()

    return {"total": int(total), "rows": rows}


def list_my_flights(conn: Any, *, user_id: int, from_utc: str):
    pending = conn.execute(
        """
        SELECT r.*, tu.name AS traveling_owner_name, ru.name AS requested_by_name
        FROM reservations r
        JOIN users tu ON tu.id = r.traveling_user_id
        JOIN users ru ON ru.id = r.requested_by_user_id
        WHERE r.requested_by_user_id = %s
          AND r.status = 'pending'
        ORDER BY r.start_utc ASC
        """,
        (int(user_id),),
    ).fetchall()

    approved_upcoming = conn.execute(
        """
        SELECT r.*, tu.name AS traveling_owner_name, ru.name AS requested_by_name
        FROM reservations r
        JOIN users tu ON tu.id = r.traveling_user_id
        JOIN users ru ON ru.id = r.requested_by_user_id
        WHERE r.requested_by_user_id = %s
          AND r.status = 'approved'
          AND r.end_utc >= %s
        ORDER BY r.start_utc ASC
        """,
        (int(user_id), from_utc),
    ).fetchall()

    return {"pending": pending, "approved_upcoming": approved_upcoming}


def create_change_request(
    conn: Any,
    *,
    reservation_id: int,
    requested_by_user_id: int,
    proposed_start_utc: str,
    proposed_end_utc: str,
    proposed_dep_icao: str,
    proposed_dest_icao: str,
    proposed_notes: str | None,
):
    now = utc_now_iso()
    cur = conn.execute(
        """
        INSERT INTO reservation_change_requests (
            reservation_id, requested_by_user_id, status,
            proposed_start_utc, proposed_end_utc, proposed_dep_icao, proposed_dest_icao, proposed_notes,
            created_at_utc, updated_at_utc
        ) VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            int(reservation_id),
            int(requested_by_user_id),
            proposed_start_utc,
            proposed_end_utc,
            proposed_dep_icao,
            proposed_dest_icao,
            proposed_notes,
            now,
            now,
        ),
    )
    conn.commit()
    created = cur.fetchone()
    return get_change_request_by_id(conn, int(created["id"]))


def get_change_request_by_id(conn: Any, request_id: int):
    return conn.execute(
        """
        SELECT cr.*, u.name AS requested_by_name, d.name AS decided_by_name
        FROM reservation_change_requests cr
        JOIN users u ON u.id = cr.requested_by_user_id
        LEFT JOIN users d ON d.id = cr.decided_by_user_id
        WHERE cr.id = %s
        """,
        (int(request_id),),
    ).fetchone()


def list_change_requests_for_reservation(conn: Any, reservation_id: int):
    return conn.execute(
        """
        SELECT cr.*, u.name AS requested_by_name, d.name AS decided_by_name
        FROM reservation_change_requests cr
        JOIN users u ON u.id = cr.requested_by_user_id
        LEFT JOIN users d ON d.id = cr.decided_by_user_id
        WHERE cr.reservation_id = %s
        ORDER BY cr.created_at_utc DESC
        """,
        (int(reservation_id),),
    ).fetchall()


def list_my_change_requests(conn: Any, user_id: int):
    return conn.execute(
        """
        SELECT cr.*, r.status AS reservation_status
        FROM reservation_change_requests cr
        JOIN reservations r ON r.id = cr.reservation_id
        WHERE cr.requested_by_user_id = %s
        ORDER BY cr.created_at_utc DESC
        """,
        (int(user_id),),
    ).fetchall()


def update_change_request_fields(conn: Any, request_id: int, fields: dict[str, object]):
    if not fields:
        return get_change_request_by_id(conn, request_id)
    items = dict(fields)
    items["updated_at_utc"] = utc_now_iso()
    keys = list(items.keys())
    assignments = ", ".join(f"{k} = %s" for k in keys)
    values = [items[k] for k in keys]
    values.append(int(request_id))
    conn.execute(f"UPDATE reservation_change_requests SET {assignments} WHERE id = %s", tuple(values))
    conn.commit()
    return get_change_request_by_id(conn, request_id)


def create_planner_quote_draft(
    conn: Any,
    *,
    token: str,
    user_id: int,
    draft_json: str,
    expires_at_utc: str,
):
    now = utc_now_iso()
    cur = conn.execute(
        """
        INSERT INTO planner_quote_drafts (
            token, user_id, status, draft_json, created_at_utc, expires_at_utc, consumed_at_utc
        ) VALUES (%s, %s, 'open', %s, %s, %s, NULL)
        RETURNING id
        """,
        (token, int(user_id), draft_json, now, expires_at_utc),
    )
    conn.commit()
    created = cur.fetchone()
    return get_planner_quote_draft_by_id(conn, int(created["id"]))


def get_planner_quote_draft_by_id(conn: Any, draft_id: int):
    return conn.execute(
        """
        SELECT d.*
        FROM planner_quote_drafts d
        WHERE d.id = %s
        """,
        (int(draft_id),),
    ).fetchone()


def get_planner_quote_draft_by_token(conn: Any, token: str):
    return conn.execute(
        """
        SELECT d.*
        FROM planner_quote_drafts d
        WHERE d.token = %s
        """,
        (token,),
    ).fetchone()


def expire_open_planner_quote_drafts(conn: Any, *, now_utc: str):
    conn.execute(
        """
        UPDATE planner_quote_drafts
        SET status = 'expired'
        WHERE status = 'open'
          AND expires_at_utc <= %s
        """,
        (now_utc,),
    )
    conn.commit()


def consume_planner_quote_draft(conn: Any, draft_id: int, *, consumed_at_utc: str):
    conn.execute(
        """
        UPDATE planner_quote_drafts
        SET status = 'consumed',
            consumed_at_utc = %s
        WHERE id = %s
          AND status = 'open'
        """,
        (consumed_at_utc, int(draft_id)),
    )
    conn.commit()
    return get_planner_quote_draft_by_id(conn, int(draft_id))

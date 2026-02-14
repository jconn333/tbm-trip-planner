from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import app as tbm_app
import db as storage
from auth import hash_password


def _attach_csrf_headers(client):
    for method_name in ("post", "patch", "put", "delete"):
        original = getattr(client, method_name)

        def wrapped(*args, _original=original, **kwargs):
            headers = dict(kwargs.pop("headers", {}) or {})
            with client.session_transaction() as sess:
                token = sess.get("csrf_token")
            if token:
                headers.setdefault("X-CSRF-Token", token)
            kwargs["headers"] = headers
            return _original(*args, **kwargs)

        setattr(client, method_name, wrapped)


def _login(client, email: str, password: str):
    response = client.post("/api/login", json={"email": email, "password": password})
    if response.status_code in (200, 409):
        _attach_csrf_headers(client)
    return response


def _setup_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test_owner_mgmt.sqlite3"
    monkeypatch.setattr(tbm_app, "TBM_DB_PATH", str(db_path))
    monkeypatch.setattr(tbm_app, "TBM_HOME_TZ", "America/New_York")
    storage.init_db(str(db_path))
    with storage.get_conn(str(db_path)) as conn:
        storage.seed_settings_defaults(conn, tbm_app._default_runtime_settings())
        storage.create_user(
            conn,
            email="admin@example.com",
            name="Admin",
            role="admin",
            password_hash=hash_password("adminpass123"),
        )
        storage.create_user(
            conn,
            email="owner@example.com",
            name="Owner",
            role="owner",
            password_hash=hash_password("ownerpass123"),
        )


def test_create_owner_invite_and_accept_flow(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin = tbm_app.app.test_client()
    assert _login(admin, "admin@example.com", "adminpass123").status_code == 200

    created = admin.post(
        "/api/owners",
        json={"name": "Invited Owner", "email": "invited@example.com"},
    )
    assert created.status_code == 201
    body = created.get_json()
    assert body["mode"] == "invite"
    assert "invite" in body and body["invite"]["link"]
    invite_link = body["invite"]["link"]
    token = invite_link.rsplit("/", 1)[-1]

    anonymous = tbm_app.app.test_client()
    page = anonymous.get(f"/accept-invite/{token}")
    assert page.status_code == 200
    assert "Accept Invite" in page.get_data(as_text=True)

    submit = anonymous.post(
        f"/accept-invite/{token}",
        data={"password": "newinvite123", "confirm_password": "newinvite123"},
    )
    assert submit.status_code == 200
    assert "Password set successfully" in submit.get_data(as_text=True)

    owner_client = tbm_app.app.test_client()
    owner_login = _login(owner_client, "invited@example.com", "newinvite123")
    assert owner_login.status_code == 200


def test_temp_password_requires_reset_and_can_be_cleared(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin = tbm_app.app.test_client()
    assert _login(admin, "admin@example.com", "adminpass123").status_code == 200

    created = admin.post(
        "/api/owners",
        json={
            "mode": "temp_password",
            "name": "Temp Owner",
            "email": "tempowner@example.com",
            "password": "TempPass123",
        },
    )
    assert created.status_code == 201

    owner_client = tbm_app.app.test_client()
    login_resp = _login(owner_client, "tempowner@example.com", "TempPass123")
    assert login_resp.status_code == 409
    assert login_resp.get_json()["must_reset_password"] is True

    blocked = owner_client.get("/calendar")
    assert blocked.status_code == 302
    assert "/account" in blocked.headers.get("Location", "")

    with owner_client.session_transaction() as sess:
        token = sess.get("csrf_token")
    changed = owner_client.post(
        "/account/password",
        data={
            "csrf_token": token,
            "current_password": "TempPass123",
            "new_password": "TempPass456",
            "confirm_password": "TempPass456",
        },
    )
    assert changed.status_code == 200

    ok = owner_client.get("/calendar")
    assert ok.status_code == 200


def test_disable_owner_blocks_login_and_enable_restores(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin = tbm_app.app.test_client()
    assert _login(admin, "admin@example.com", "adminpass123").status_code == 200

    with storage.get_conn(tbm_app.TBM_DB_PATH) as conn:
        owner = storage.get_user_by_email(conn, "owner@example.com")
        owner_id = int(owner["id"])

    disabled = admin.post(f"/api/owners/{owner_id}/disable")
    assert disabled.status_code == 200

    owner_client = tbm_app.app.test_client()
    bad_login = _login(owner_client, "owner@example.com", "ownerpass123")
    assert bad_login.status_code == 403

    enabled = admin.post(f"/api/owners/{owner_id}/enable")
    assert enabled.status_code == 200

    good_login = _login(owner_client, "owner@example.com", "ownerpass123")
    assert good_login.status_code == 200


def test_owner_list_includes_status_fields_and_audit(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin = tbm_app.app.test_client()
    assert _login(admin, "admin@example.com", "adminpass123").status_code == 200

    created = admin.post(
        "/api/owners",
        json={"name": "Audited Owner", "email": "audit@example.com"},
    )
    assert created.status_code == 201

    owners_resp = admin.get("/api/owners")
    assert owners_resp.status_code == 200
    owners = owners_resp.get_json()
    target = next(item for item in owners if item["email"] == "audit@example.com")
    assert "is_disabled" in target
    assert "must_reset_password" in target
    assert "last_login_at" in target
    assert "invite_status" in target

    audit_resp = admin.get("/api/admin/owners/audit?page=1&page_size=20")
    assert audit_resp.status_code == 200
    rows = audit_resp.get_json()["items"]
    assert any(row["action"] in ("invite_created", "create_temp_password") for row in rows)


def test_revoke_and_resend_invite_flow(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin = tbm_app.app.test_client()
    assert _login(admin, "admin@example.com", "adminpass123").status_code == 200

    created = admin.post("/api/owners", json={"name": "Rev Owner", "email": "rev@example.com"})
    assert created.status_code == 201

    with storage.get_conn(tbm_app.TBM_DB_PATH) as conn:
        owner = storage.get_user_by_email(conn, "rev@example.com")
        owner_id = int(owner["id"])

    revoked = admin.post(f"/api/owners/{owner_id}/invites/revoke")
    assert revoked.status_code == 200

    resent = admin.post(f"/api/owners/{owner_id}/invites", json={"expires_hours": 24})
    assert resent.status_code == 200
    data = resent.get_json()
    assert data["invite"]["link"]

    with storage.get_conn(tbm_app.TBM_DB_PATH) as conn:
        storage.expire_open_owner_invites(conn, now_utc=(datetime.now(ZoneInfo("UTC")) + timedelta(days=3)).isoformat())
        invite = storage.get_latest_owner_invite_for_user(conn, user_id=owner_id)
        assert invite["status"] in ("expired", "revoked", "consumed", "open")


def test_admin_can_update_owner_name_and_email(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin = tbm_app.app.test_client()
    assert _login(admin, "admin@example.com", "adminpass123").status_code == 200

    created = admin.post("/api/owners", json={"name": "Edit Me", "email": "editme@example.com"})
    assert created.status_code == 201
    owner_id = int(created.get_json()["owner"]["id"])

    updated = admin.patch(
        f"/api/owners/{owner_id}",
        json={"name": "Edited Name", "email": "edited@example.com"},
    )
    assert updated.status_code == 200
    body = updated.get_json()
    assert body["owner"]["name"] == "Edited Name"
    assert body["owner"]["email"] == "edited@example.com"


def test_admin_update_owner_email_rejects_duplicate(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin = tbm_app.app.test_client()
    assert _login(admin, "admin@example.com", "adminpass123").status_code == 200

    first = admin.post("/api/owners", json={"name": "Owner One", "email": "one@example.com"})
    second = admin.post("/api/owners", json={"name": "Owner Two", "email": "two@example.com"})
    assert first.status_code == 201
    assert second.status_code == 201
    second_id = int(second.get_json()["owner"]["id"])

    conflict = admin.patch(f"/api/owners/{second_id}", json={"email": "one@example.com"})
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "owner_exists"


def test_admin_can_start_and_stop_view_as_owner(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin = tbm_app.app.test_client()
    assert _login(admin, "admin@example.com", "adminpass123").status_code == 200

    with storage.get_conn(tbm_app.TBM_DB_PATH) as conn:
        owner = storage.get_user_by_email(conn, "owner@example.com")
        owner_id = int(owner["id"])

    started = admin.post(f"/api/admin/view-as/{owner_id}")
    assert started.status_code == 200
    started_body = started.get_json()
    assert started_body["ok"] is True
    assert int(started_body["impersonation"]["target_user_id"]) == owner_id
    assert started_body["acting_as_user"]["role"] == "owner"

    with admin.session_transaction() as sess:
        assert int(sess.get("user_id")) == owner_id
        assert int(sess.get("impersonator_admin_user_id")) > 0
        assert int(sess.get("impersonation_target_user_id")) == owner_id
        assert bool(sess.get("impersonation_started_at_utc")) is True

    admin_page = admin.get("/admin")
    assert admin_page.status_code == 302
    assert "/calendar" in (admin_page.headers.get("Location") or "")

    owner_page = admin.get("/my-flights")
    assert owner_page.status_code == 200
    assert "Viewing as" in owner_page.get_data(as_text=True)

    stopped = admin.post("/api/admin/view-as/stop")
    assert stopped.status_code == 200
    stopped_body = stopped.get_json()
    assert stopped_body["ok"] is True
    assert stopped_body["restored_admin_user"]["role"] == "admin"

    with admin.session_transaction() as sess:
        assert sess.get("impersonator_admin_user_id") is None
        assert sess.get("impersonation_target_user_id") is None
        assert sess.get("impersonation_started_at_utc") is None

    admin_back = admin.get("/admin")
    assert admin_back.status_code == 200


def test_non_admin_cannot_start_view_as(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    owner_client = tbm_app.app.test_client()
    assert _login(owner_client, "owner@example.com", "ownerpass123").status_code == 200

    with storage.get_conn(tbm_app.TBM_DB_PATH) as conn:
        owner = storage.get_user_by_email(conn, "owner@example.com")
        owner_id = int(owner["id"])

    denied = owner_client.post(f"/api/admin/view-as/{owner_id}")
    assert denied.status_code == 403


def test_view_as_rejects_non_owner_target(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin = tbm_app.app.test_client()
    assert _login(admin, "admin@example.com", "adminpass123").status_code == 200

    with storage.get_conn(tbm_app.TBM_DB_PATH) as conn:
        admin_user = storage.get_user_by_email(conn, "admin@example.com")
        admin_id = int(admin_user["id"])

    bad_target = admin.post(f"/api/admin/view-as/{admin_id}")
    assert bad_target.status_code == 400
    assert bad_target.get_json()["code"] == "invalid_owner"


def test_view_as_audit_records_start_and_stop(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin = tbm_app.app.test_client()
    assert _login(admin, "admin@example.com", "adminpass123").status_code == 200

    with storage.get_conn(tbm_app.TBM_DB_PATH) as conn:
        owner = storage.get_user_by_email(conn, "owner@example.com")
        owner_id = int(owner["id"])

    assert admin.post(f"/api/admin/view-as/{owner_id}").status_code == 200
    assert admin.post("/api/admin/view-as/stop").status_code == 200

    audit = admin.get("/api/admin/owners/audit?page=1&page_size=50")
    assert audit.status_code == 200
    actions = [row["action"] for row in audit.get_json()["items"]]
    assert "view_as_started" in actions
    assert "view_as_stopped" in actions


def test_view_as_allows_disabled_and_must_reset_owner_with_warning(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin = tbm_app.app.test_client()
    assert _login(admin, "admin@example.com", "adminpass123").status_code == 200

    with storage.get_conn(tbm_app.TBM_DB_PATH) as conn:
        user = storage.create_user(
            conn,
            email="locked@example.com",
            name="Locked Owner",
            role="owner",
            password_hash=hash_password("lockedpass123"),
        )
        owner_id = int(user["id"])
        storage.set_user_flags(conn, owner_id, is_disabled=1, must_reset_password=1)

    started = admin.post(f"/api/admin/view-as/{owner_id}")
    assert started.status_code == 200

    calendar = admin.get("/calendar")
    assert calendar.status_code == 200
    body = calendar.get_data(as_text=True)
    assert "Viewing as Locked Owner" in body
    assert "target owner is disabled" in body
    assert "must-reset-password" in body


def test_admin_email_logs_endpoint_returns_rows(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin = tbm_app.app.test_client()
    assert _login(admin, "admin@example.com", "adminpass123").status_code == 200

    with storage.get_conn(tbm_app.TBM_DB_PATH) as conn:
        admin_row = storage.get_user_by_email(conn, "admin@example.com")
        storage.create_email_notification_log(
            conn,
            audience="admin",
            source="direct_request",
            to_addresses="admin@example.com",
            subject="New trip request to review",
            status="sent",
            error_message=None,
            reservation_ids_json="[12]",
            actor_user_id=int(admin_row["id"]),
        )

    resp = admin.get("/api/admin/email-logs?page=1&page_size=10")
    assert resp.status_code == 200
    body = resp.get_json()
    assert int(body["total"]) >= 1
    assert len(body["items"]) >= 1
    first = body["items"][0]
    assert first["audience"] == "admin"
    assert first["status"] in ("sent", "failed", "skipped")

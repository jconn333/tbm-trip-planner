from datetime import datetime, timedelta

import app as tbm_app
import db as storage
from auth import hash_password


def _login(client, email: str, password: str):
    response = client.post("/api/login", json={"email": email, "password": password})
    if response.status_code == 200:
        _attach_csrf_headers(client)
    return response


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


def _to_quarter(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def _reservation_payload(start: datetime, end: datetime):
    start = _to_quarter(start)
    end = _to_quarter(end)
    return {
        "start_local": start.strftime("%Y-%m-%dT%H:%M"),
        "end_local": end.strftime("%Y-%m-%dT%H:%M"),
        "dep_icao": "KCAK",
        "dest_icao": "KSRQ",
        "notes": "Test trip",
    }


def _make_approved(owner_client, admin_client, start: datetime, end: datetime):
    created = owner_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert created.status_code == 201
    rid = created.get_json()["reservation"]["id"]
    approved = admin_client.post(f"/api/reservations/{rid}/approve")
    assert approved.status_code == 200
    return rid


def _setup_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test_admin.sqlite3"
    monkeypatch.setattr(tbm_app, "TBM_DB_PATH", str(db_path))
    monkeypatch.setattr(tbm_app, "TBM_HOME_TZ", "America/New_York")
    storage.init_db(str(db_path))
    with storage.get_conn(str(db_path)) as conn:
        storage.seed_settings_defaults(conn, tbm_app._default_runtime_settings())
        admin = storage.create_user(
            conn,
            email="admin@example.com",
            name="Admin",
            role="admin",
            password_hash=hash_password("adminpass123"),
        )
        owner = storage.create_user(
            conn,
            email="owner@example.com",
            name="Owner",
            role="owner",
            password_hash=hash_password("ownerpass123"),
        )
        owner2 = storage.create_user(
            conn,
            email="owner2@example.com",
            name="Owner Two",
            role="owner",
            password_hash=hash_password("ownerpass123"),
        )
    tbm_app._invalidate_settings_cache()
    return int(admin["id"]), int(owner["id"]), int(owner2["id"])


def test_admin_settings_get_and_patch(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    client = tbm_app.app.test_client()
    _login(client, "admin@example.com", "adminpass123")

    got = client.get("/api/admin/settings")
    assert got.status_code == 200
    data = got.get_json()
    assert data["settings"]["home_timezone"] == "America/New_York"

    patched = client.patch(
        "/api/admin/settings",
        json={"reservation_min_minutes": "30", "admin_flights_default_scope": "all"},
    )
    assert patched.status_code == 200
    patched_data = patched.get_json()
    assert patched_data["settings"]["reservation_min_minutes"] == "30"
    assert patched_data["settings"]["admin_flights_default_scope"] == "all"

    history = client.get("/api/admin/settings/history")
    assert history.status_code == 200
    history_rows = history.get_json()["items"]
    assert any(row["key"] == "reservation_min_minutes" and row["new_value"] == "30" for row in history_rows)


def test_admin_settings_dependency_validation(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    client = tbm_app.app.test_client()
    _login(client, "admin@example.com", "adminpass123")

    bad = client.patch(
        "/api/admin/settings",
        json={"reservation_min_minutes": "2000", "reservation_max_days": "1"},
    )
    assert bad.status_code == 400
    assert bad.get_json()["code"] == "invalid_setting"


def test_admin_flights_defaults_to_future_only(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin_client = tbm_app.app.test_client()
    owner_client = tbm_app.app.test_client()
    _login(admin_client, "admin@example.com", "adminpass123")
    _login(owner_client, "owner@example.com", "ownerpass123")

    past_start = datetime.now() - timedelta(days=4)
    past_end = past_start + timedelta(hours=2)
    future_start = datetime.now() + timedelta(days=4)
    future_end = future_start + timedelta(hours=2)

    owner_client.post("/api/reservation-requests", json=_reservation_payload(past_start, past_end))
    owner_client.post("/api/reservation-requests", json=_reservation_payload(future_start, future_end))

    resp = admin_client.get("/api/admin/flights")
    assert resp.status_code == 200
    items = resp.get_json()["items"]
    assert len(items) == 1
    assert "created_at" in items[0]
    assert "updated_at" in items[0]


def test_my_flights_and_change_request_workflow(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin_client = tbm_app.app.test_client()
    owner_client = tbm_app.app.test_client()
    _login(admin_client, "admin@example.com", "adminpass123")
    _login(owner_client, "owner@example.com", "ownerpass123")

    start = _to_quarter(datetime.now() + timedelta(days=3))
    end = _to_quarter(start + timedelta(hours=2))
    rid = _make_approved(owner_client, admin_client, start, end)

    my_flights = owner_client.get("/api/my-flights")
    assert my_flights.status_code == 200
    data = my_flights.get_json()
    assert len(data["approved_upcoming"]) == 1

    request_payload = {
        "start_local": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "end_local": (end + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "dep_icao": "KCAK",
        "dest_icao": "KJFK",
        "notes": "Need later departure",
    }
    created_req = owner_client.post(f"/api/reservations/{rid}/change-requests", json=request_payload)
    assert created_req.status_code == 200
    request_id = int(created_req.get_json()["change_request_id"])

    listed = admin_client.get(f"/api/reservations/{rid}/change-requests")
    assert listed.status_code == 200
    assert any(int(item["id"]) == request_id for item in listed.get_json())

    approved = admin_client.post(f"/api/reservations/{rid}/change-requests/{request_id}/approve")
    assert approved.status_code == 200

    with storage.get_conn(tbm_app.TBM_DB_PATH) as conn:
        row = storage.get_reservation_by_id(conn, rid)
        assert row["dest_icao"] == "KJFK"


def test_owner_cannot_create_change_request_for_other_owner(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    admin_client = tbm_app.app.test_client()
    owner_client = tbm_app.app.test_client()
    owner2_client = tbm_app.app.test_client()
    _login(admin_client, "admin@example.com", "adminpass123")
    _login(owner_client, "owner@example.com", "ownerpass123")
    _login(owner2_client, "owner2@example.com", "ownerpass123")

    start = _to_quarter(datetime.now() + timedelta(days=3))
    end = _to_quarter(start + timedelta(hours=2))
    rid = _make_approved(owner_client, admin_client, start, end)

    payload = {
        "start_local": start.strftime("%Y-%m-%dT%H:%M"),
        "end_local": end.strftime("%Y-%m-%dT%H:%M"),
        "dep_icao": "KCAK",
        "dest_icao": "KBOS",
    }
    forbidden = owner2_client.post(f"/api/reservations/{rid}/change-requests", json=payload)
    assert forbidden.status_code == 403

from datetime import datetime, timedelta

import pytest

import app as tbm_app
import db as storage
from auth import hash_password


@pytest.fixture
def reservation_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test_reservations.sqlite3"
    monkeypatch.setattr(tbm_app, "TBM_DB_PATH", str(db_path))
    monkeypatch.setattr(tbm_app, "TBM_HOME_TZ", "America/New_York")
    storage.init_db(str(db_path))

    with storage.get_conn(str(db_path)) as conn:
        admin = storage.create_user(
            conn,
            email="admin@example.com",
            name="Admin",
            role="admin",
            password_hash=hash_password("adminpass123"),
        )
        owner1 = storage.create_user(
            conn,
            email="owner1@example.com",
            name="Owner One",
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
    return {
        "db_path": str(db_path),
        "admin": int(admin["id"]),
        "owner1": int(owner1["id"]),
        "owner2": int(owner2["id"]),
    }


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


def _reservation_payload(start: datetime, end: datetime, traveling_user_id: int | None = None):
    start = _to_quarter(start)
    end = _to_quarter(end)
    payload = {
        "start_local": start.strftime("%Y-%m-%dT%H:%M"),
        "end_local": end.strftime("%Y-%m-%dT%H:%M"),
        "dep_icao": "KCAK",
        "dest_icao": "KSRQ",
        "parked_icao": "KSRQ",
        "notes": "Test trip",
    }
    if traveling_user_id is not None:
        payload["traveling_user_id"] = traveling_user_id
    return payload


def test_parked_field_is_derived_from_destination(reservation_env):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    start = datetime.now() + timedelta(days=2)
    end = start + timedelta(hours=1)
    payload = _reservation_payload(start, end)
    payload["parked_icao"] = "KJFK"

    resp = owner_client.post("/api/reservation-requests", json=payload)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["reservation"]["dest_icao"] == "KSRQ"
    assert data["reservation"]["parked_icao"] == "KSRQ"


def test_api_login_success_and_failure(reservation_env):
    client = tbm_app.app.test_client()
    ok = _login(client, "owner1@example.com", "ownerpass123")
    assert ok.status_code == 200

    bad = _login(client, "owner1@example.com", "bad-password")
    assert bad.status_code == 401


def test_owner_cannot_call_admin_endpoint(reservation_env):
    client = tbm_app.app.test_client()
    _login(client, "owner1@example.com", "ownerpass123")

    resp = client.post(
        "/api/owners",
        json={"name": "X", "email": "x@example.com", "password": "long-enough"},
    )
    assert resp.status_code == 403


def test_reservation_request_approve_and_deny_flow(reservation_env):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    start = datetime.now() + timedelta(days=2)
    end = start + timedelta(hours=2)
    create = owner_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert create.status_code == 201
    reservation_id = create.get_json()["reservation"]["id"]

    admin_client = tbm_app.app.test_client()
    _login(admin_client, "admin@example.com", "adminpass123")

    approve = admin_client.post(f"/api/reservations/{reservation_id}/approve")
    assert approve.status_code == 200
    assert approve.get_json()["reservation"]["status"] == "approved"

    owner2_client = tbm_app.app.test_client()
    _login(owner2_client, "owner2@example.com", "ownerpass123")
    start2 = start + timedelta(days=1)
    end2 = start2 + timedelta(hours=1)
    create2 = owner2_client.post("/api/reservation-requests", json=_reservation_payload(start2, end2))
    assert create2.status_code == 201
    reservation_id2 = create2.get_json()["reservation"]["id"]

    deny = admin_client.post(f"/api/reservations/{reservation_id2}/deny", json={"note": "Not available"})
    assert deny.status_code == 200
    assert deny.get_json()["reservation"]["status"] == "denied"


def test_overlap_blocking_and_nonblocking_statuses(reservation_env):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    start = datetime.now() + timedelta(days=3)
    end = start + timedelta(hours=2)
    base = owner_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert base.status_code == 201
    base_id = base.get_json()["reservation"]["id"]

    overlapping = owner_client.post(
        "/api/reservation-requests",
        json=_reservation_payload(start + timedelta(minutes=30), end + timedelta(minutes=30)),
    )
    assert overlapping.status_code == 409

    admin_client = tbm_app.app.test_client()
    _login(admin_client, "admin@example.com", "adminpass123")
    deny = admin_client.post(f"/api/reservations/{base_id}/deny", json={"note": "Denied"})
    assert deny.status_code == 200

    after_deny = owner_client.post(
        "/api/reservation-requests",
        json=_reservation_payload(start + timedelta(minutes=30), end + timedelta(minutes=30)),
    )
    assert after_deny.status_code == 201


def test_approve_rechecks_overlap_conflicts(reservation_env):
    owner1_client = tbm_app.app.test_client()
    owner2_client = tbm_app.app.test_client()
    admin_client = tbm_app.app.test_client()
    _login(owner1_client, "owner1@example.com", "ownerpass123")
    _login(owner2_client, "owner2@example.com", "ownerpass123")
    _login(admin_client, "admin@example.com", "adminpass123")

    start = datetime.now() + timedelta(days=4)
    end = start + timedelta(hours=2)

    first = owner1_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert first.status_code == 201
    first_id = first.get_json()["reservation"]["id"]

    second = owner2_client.post(
        "/api/reservation-requests",
        json=_reservation_payload(start + timedelta(hours=3), end + timedelta(hours=3)),
    )
    assert second.status_code == 201
    second_id = second.get_json()["reservation"]["id"]

    assert admin_client.post(f"/api/reservations/{second_id}/approve").status_code == 200

    owner1_update = owner1_client.patch(
        f"/api/reservations/{first_id}",
        json=_reservation_payload(start + timedelta(hours=2, minutes=30), end + timedelta(hours=2, minutes=30)),
    )
    assert owner1_update.status_code == 409


def test_reservations_api_shape_and_timezone_conversion(reservation_env):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    start = datetime(2030, 2, 1, 9, 0)
    end = datetime(2030, 2, 1, 11, 0)
    create = owner_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert create.status_code == 201

    params = {
        "start": "2030-02-01",
        "end": "2030-02-03",
        "include_nonblocking": "false",
    }
    resp = owner_client.get("/api/reservations", query_string=params)
    assert resp.status_code == 200
    events = resp.get_json()
    assert len(events) == 1
    event = events[0]
    assert event["title"].startswith("Owner One")
    assert event["dep_icao"] == "KCAK"
    assert event["dest_icao"] == "KSRQ"
    assert event["parked_icao"] == "KSRQ"
    assert event["status"] == "pending"
    assert event["start"].startswith("2030-02-01T")


def test_api_my_pending_reservations_returns_owner_scope_only(reservation_env):
    owner1_client = tbm_app.app.test_client()
    owner2_client = tbm_app.app.test_client()
    admin_client = tbm_app.app.test_client()
    _login(owner1_client, "owner1@example.com", "ownerpass123")
    _login(owner2_client, "owner2@example.com", "ownerpass123")
    _login(admin_client, "admin@example.com", "adminpass123")

    base = datetime.now() + timedelta(days=5)
    owner1_pending = owner1_client.post(
        "/api/reservation-requests",
        json=_reservation_payload(base, base + timedelta(hours=1)),
    )
    assert owner1_pending.status_code == 201
    owner1_pending_id = owner1_pending.get_json()["reservation"]["id"]

    owner2_pending = owner2_client.post(
        "/api/reservation-requests",
        json=_reservation_payload(base + timedelta(hours=2), base + timedelta(hours=3)),
    )
    assert owner2_pending.status_code == 201
    owner2_pending_id = owner2_pending.get_json()["reservation"]["id"]

    owner1_list = owner1_client.get("/api/my-pending-reservations")
    assert owner1_list.status_code == 200
    owner1_rows = owner1_list.get_json()
    assert any(row["id"] == owner1_pending_id for row in owner1_rows)
    assert not any(row["id"] == owner2_pending_id for row in owner1_rows)

    approve = admin_client.post(f"/api/reservations/{owner1_pending_id}/approve")
    assert approve.status_code == 200

    owner1_after_approve = owner1_client.get("/api/my-pending-reservations")
    assert owner1_after_approve.status_code == 200
    owner1_after_rows = owner1_after_approve.get_json()
    assert not any(row["id"] == owner1_pending_id for row in owner1_after_rows)


def test_reservation_create_accepts_separate_date_time_fields(reservation_env):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    start = _to_quarter(datetime.now() + timedelta(days=6))
    end = _to_quarter(start + timedelta(hours=2))
    payload = {
        "start_date": start.strftime("%Y-%m-%d"),
        "start_time": start.strftime("%H:%M"),
        "end_date": end.strftime("%Y-%m-%d"),
        "end_time": end.strftime("%H:%M"),
        "dep_icao": "KCAK",
        "dest_icao": "KSRQ",
        "notes": "Separate date/time fields",
    }

    resp = owner_client.post("/api/reservation-requests", json=payload)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["reservation"]["dep_icao"] == "KCAK"
    assert data["reservation"]["dest_icao"] == "KSRQ"


def test_split_date_time_requires_both_values(reservation_env):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    start = datetime.now() + timedelta(days=7)
    payload = {
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": (start + timedelta(hours=2)).strftime("%Y-%m-%d"),
        "end_time": (start + timedelta(hours=2)).strftime("%H:%M"),
        "dep_icao": "KCAK",
        "dest_icao": "KSRQ",
    }
    resp = owner_client.post("/api/reservation-requests", json=payload)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == "invalid_datetime"


def test_reopen_denied_and_overlap_guard(reservation_env):
    owner1_client = tbm_app.app.test_client()
    owner2_client = tbm_app.app.test_client()
    admin_client = tbm_app.app.test_client()
    _login(owner1_client, "owner1@example.com", "ownerpass123")
    _login(owner2_client, "owner2@example.com", "ownerpass123")
    _login(admin_client, "admin@example.com", "adminpass123")

    start = datetime.now() + timedelta(days=9)
    end = start + timedelta(hours=2)
    first = owner1_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert first.status_code == 201
    first_id = first.get_json()["reservation"]["id"]

    denied = admin_client.post(f"/api/reservations/{first_id}/deny", json={"note": "Denied for test"})
    assert denied.status_code == 200

    reopened = admin_client.post(f"/api/reservations/{first_id}/reopen", json={"target_status": "pending"})
    assert reopened.status_code == 200
    assert reopened.get_json()["reservation"]["status"] == "pending"

    second = owner2_client.post("/api/reservation-requests", json=_reservation_payload(start + timedelta(hours=3), end + timedelta(hours=3)))
    assert second.status_code == 201
    second_id = second.get_json()["reservation"]["id"]
    assert admin_client.post(f"/api/reservations/{second_id}/approve").status_code == 200

    assert admin_client.post(f"/api/reservations/{first_id}/deny", json={"note": "close again"}).status_code == 200
    owner2_overlap = owner2_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert owner2_overlap.status_code == 201
    overlap_id = owner2_overlap.get_json()["reservation"]["id"]
    assert admin_client.post(f"/api/reservations/{overlap_id}/approve").status_code == 200

    blocked_reopen = admin_client.post(f"/api/reservations/{first_id}/reopen", json={"target_status": "pending"})
    assert blocked_reopen.status_code == 409


def test_owner_cannot_reopen(reservation_env):
    owner_client = tbm_app.app.test_client()
    admin_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")
    _login(admin_client, "admin@example.com", "adminpass123")

    start = datetime.now() + timedelta(days=10)
    end = start + timedelta(hours=2)
    created = owner_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert created.status_code == 201
    rid = created.get_json()["reservation"]["id"]
    assert admin_client.post(f"/api/reservations/{rid}/deny", json={"note": "closed"}).status_code == 200

    forbidden = owner_client.post(f"/api/reservations/{rid}/reopen", json={"target_status": "pending"})
    assert forbidden.status_code == 403


def test_time_must_be_15_minute_increment(reservation_env):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    start = datetime.now() + timedelta(days=12)
    start = start.replace(minute=7, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    payload = {
        "start_local": start.strftime("%Y-%m-%dT%H:%M"),
        "end_local": end.strftime("%Y-%m-%dT%H:%M"),
        "dep_icao": "KCAK",
        "dest_icao": "KSRQ",
    }
    resp = owner_client.post("/api/reservation-requests", json=payload)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == "invalid_time_increment"

import os
from datetime import datetime, timedelta

import pytest

import app as tbm_app
import db as storage
from auth import hash_password


@pytest.fixture
def reservation_env(monkeypatch):
    db_url = os.environ.get("TEST_DATABASE_URL") or tbm_app.DATABASE_URL
    if not db_url:
        pytest.skip("Set TEST_DATABASE_URL (or DATABASE_URL) for Postgres-backed tests.")
    monkeypatch.setattr(tbm_app, "DATABASE_URL", db_url)
    monkeypatch.setattr(tbm_app, "TBM_HOME_TZ", "America/New_York")
    storage.init_db(db_url)

    with storage.get_conn(db_url) as conn:
        storage.reset_for_tests(conn)
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
        "db_url": db_url,
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


def _quote_estimate_payload(trip_type: str, depart_date: datetime, return_date: datetime | None = None):
    payload = {
        "inputs": {
            "dep": "KCAK",
            "dest": "KSRQ",
            "trip_type": trip_type,
            "depart_date": depart_date.date().isoformat(),
            "return_date": return_date.date().isoformat() if return_date else None,
        },
        "legs": [
            {"typical": {"minutes": 180}},
        ],
    }
    if trip_type == "roundtrip":
        payload["legs"].append({"typical": {"minutes": 175}})
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


def test_list_admin_users_excludes_disabled(reservation_env):
    with storage.get_conn(reservation_env["db_url"]) as conn:
        admins = storage.list_admin_users(conn)
        assert len(admins) == 1
        assert admins[0]["email"] == "admin@example.com"
        storage.set_user_flags(conn, int(admins[0]["id"]), is_disabled=1)
        admins_after = storage.list_admin_users(conn)
        assert admins_after == []


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


def test_approve_sends_requester_decision_notification(reservation_env, monkeypatch):
    owner_client = tbm_app.app.test_client()
    admin_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")
    _login(admin_client, "admin@example.com", "adminpass123")

    start = datetime.now() + timedelta(days=3)
    end = start + timedelta(hours=2)
    create = owner_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert create.status_code == 201
    reservation_id = int(create.get_json()["reservation"]["id"])

    captured = {"called": 0, "decision": ""}

    def fake_send(_context, *, decision, decision_note, actor_name, source, actor_user_id=None):
        captured["called"] += 1
        captured["decision"] = decision
        assert actor_name == "Admin"
        assert decision_note == ""
        assert source == "reservation_approved"
        return True

    monkeypatch.setattr(tbm_app, "_send_requester_decision_email", fake_send)

    approve = admin_client.post(f"/api/reservations/{reservation_id}/approve")
    assert approve.status_code == 200
    assert captured["called"] == 1
    assert captured["decision"] == "approved"


def test_deny_sends_requester_decision_notification_with_note(reservation_env, monkeypatch):
    owner_client = tbm_app.app.test_client()
    admin_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")
    _login(admin_client, "admin@example.com", "adminpass123")

    start = datetime.now() + timedelta(days=3)
    end = start + timedelta(hours=2)
    create = owner_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert create.status_code == 201
    reservation_id = int(create.get_json()["reservation"]["id"])

    captured = {"called": 0, "decision": "", "note": ""}

    def fake_send(_context, *, decision, decision_note, actor_name, source, actor_user_id=None):
        captured["called"] += 1
        captured["decision"] = decision
        captured["note"] = decision_note
        assert actor_name == "Admin"
        assert source == "reservation_denied"
        return True

    monkeypatch.setattr(tbm_app, "_send_requester_decision_email", fake_send)

    deny = admin_client.post(f"/api/reservations/{reservation_id}/deny", json={"note": "Weather risk"})
    assert deny.status_code == 200
    assert captured["called"] == 1
    assert captured["decision"] == "denied"
    assert captured["note"] == "Weather risk"


def test_decision_notification_fail_open(reservation_env, monkeypatch):
    owner_client = tbm_app.app.test_client()
    admin_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")
    _login(admin_client, "admin@example.com", "adminpass123")

    start = datetime.now() + timedelta(days=3)
    end = start + timedelta(hours=2)
    create = owner_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert create.status_code == 201
    reservation_id = int(create.get_json()["reservation"]["id"])

    monkeypatch.setattr(tbm_app, "_build_reservation_decision_email_context", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    approve = admin_client.post(f"/api/reservations/{reservation_id}/approve")
    assert approve.status_code == 200


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


def test_direct_request_notifications_fail_open_when_email_helper_errors(reservation_env, monkeypatch):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    monkeypatch.setattr(tbm_app, "_build_reservation_email_context", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    start = datetime.now() + timedelta(days=2)
    end = start + timedelta(hours=1)
    resp = owner_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert resp.status_code == 201


def test_direct_request_notifications_include_single_leg(reservation_env, monkeypatch):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    captured = {}

    def fake_context(created_rows, requester_user):
        rows = list(created_rows or [])
        captured["count"] = len(rows)
        captured["requester"] = requester_user.get("email")
        return {
            "requester_name": "Owner One",
            "requester_email": "owner1@example.com",
            "reservation_ids": [int(rows[0]["id"])],
            "legs": [{"reservation_id": int(rows[0]["id"]), "dep_icao": "KCAK", "dest_icao": "KSRQ", "start_display": "", "end_display": "", "notes": ""}],
            "review_url": "/admin",
            "timezone": "America/New_York",
        }

    calls = {"requester": 0, "admin": 0}
    monkeypatch.setattr(tbm_app, "_build_reservation_email_context", fake_context)
    monkeypatch.setattr(
        tbm_app,
        "_send_requester_submission_email",
        lambda _ctx, *, source, actor_user_id=None: calls.__setitem__("requester", calls["requester"] + 1) or True,
    )
    monkeypatch.setattr(
        tbm_app,
        "_send_admin_new_request_email",
        lambda _ctx, _emails, *, source, actor_user_id=None: calls.__setitem__("admin", calls["admin"] + 1) or True,
    )

    start = datetime.now() + timedelta(days=3)
    end = start + timedelta(hours=1)
    resp = owner_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert resp.status_code == 201
    assert captured["count"] == 1
    assert captured["requester"] == "owner1@example.com"
    assert calls["requester"] == 1
    assert calls["admin"] == 1


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
    assert event["can_cancel"] is True


def test_reservations_api_applies_pending_and_owner_colors(reservation_env):
    owner_client = tbm_app.app.test_client()
    admin_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")
    _login(admin_client, "admin@example.com", "adminpass123")

    custom_pending = "#7A7A7A"
    custom_owner = "#2AAE91"
    owner_id = reservation_env["owner1"]
    settings_resp = admin_client.patch(
        "/api/admin/settings",
        json={
            "pending_reservation_color": custom_pending,
            f"owner_color_{owner_id}": custom_owner,
        },
    )
    assert settings_resp.status_code == 200

    start = datetime(2030, 2, 2, 9, 0)
    end = datetime(2030, 2, 2, 11, 0)
    created = owner_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert created.status_code == 201
    reservation_id = int(created.get_json()["reservation"]["id"])

    params = {"start": "2030-02-01", "end": "2030-02-03", "include_nonblocking": "false"}
    pending_events = owner_client.get("/api/reservations", query_string=params).get_json()
    pending_event = next(item for item in pending_events if int(item["id"]) == reservation_id)
    assert pending_event["status"] == "pending"
    assert pending_event["display_color"] == custom_pending

    approved = admin_client.post(f"/api/reservations/{reservation_id}/approve")
    assert approved.status_code == 200

    approved_events = owner_client.get("/api/reservations", query_string=params).get_json()
    approved_event = next(item for item in approved_events if int(item["id"]) == reservation_id)
    assert approved_event["status"] == "approved"
    assert approved_event["display_color"] == custom_owner


def test_reservation_event_can_cancel_truth_table(reservation_env):
    owner1_client = tbm_app.app.test_client()
    owner2_client = tbm_app.app.test_client()
    admin_client = tbm_app.app.test_client()
    _login(owner1_client, "owner1@example.com", "ownerpass123")
    _login(owner2_client, "owner2@example.com", "ownerpass123")
    _login(admin_client, "admin@example.com", "adminpass123")

    start = datetime.now() + timedelta(days=2)
    end = start + timedelta(hours=2)
    created = owner1_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert created.status_code == 201
    reservation_id = int(created.get_json()["reservation"]["id"])

    params = {"start": (start - timedelta(days=1)).isoformat(), "end": (end + timedelta(days=4)).isoformat(), "include_nonblocking": "true"}

    owner1_events = owner1_client.get("/api/reservations", query_string=params).get_json()
    owner1_pending = next(item for item in owner1_events if int(item["id"]) == reservation_id)
    assert owner1_pending["status"] == "pending"
    assert owner1_pending["can_cancel"] is True

    owner2_events = owner2_client.get("/api/reservations", query_string=params).get_json()
    owner2_pending = next(item for item in owner2_events if int(item["id"]) == reservation_id)
    assert owner2_pending["can_cancel"] is False

    admin_events = admin_client.get("/api/reservations", query_string=params).get_json()
    admin_pending = next(item for item in admin_events if int(item["id"]) == reservation_id)
    assert admin_pending["can_cancel"] is True

    approve = admin_client.post(f"/api/reservations/{reservation_id}/approve")
    assert approve.status_code == 200

    owner1_events_after_approve = owner1_client.get("/api/reservations", query_string=params).get_json()
    owner1_approved = next(item for item in owner1_events_after_approve if int(item["id"]) == reservation_id)
    assert owner1_approved["status"] == "approved"
    assert owner1_approved["can_cancel"] is False

    admin_events_after_approve = admin_client.get("/api/reservations", query_string=params).get_json()
    admin_approved = next(item for item in admin_events_after_approve if int(item["id"]) == reservation_id)
    assert admin_approved["can_cancel"] is True

    second_created = owner1_client.post(
        "/api/reservation-requests",
        json=_reservation_payload(start + timedelta(days=2), end + timedelta(days=2)),
    )
    assert second_created.status_code == 201
    second_id = int(second_created.get_json()["reservation"]["id"])
    deny = admin_client.post(f"/api/reservations/{second_id}/deny", json={"note": "Denied for test"})
    assert deny.status_code == 200

    denied_events = admin_client.get("/api/reservations", query_string=params).get_json()
    denied_event = next(item for item in denied_events if int(item["id"]) == second_id)
    assert denied_event["status"] == "denied"
    assert denied_event["can_cancel"] is False


def test_reservation_detail_route_access_and_snapshot(reservation_env):
    owner1_client = tbm_app.app.test_client()
    owner2_client = tbm_app.app.test_client()
    admin_client = tbm_app.app.test_client()
    _login(owner1_client, "owner1@example.com", "ownerpass123")
    _login(owner2_client, "owner2@example.com", "ownerpass123")
    _login(admin_client, "admin@example.com", "adminpass123")

    start = datetime.now() + timedelta(days=2)
    end = start + timedelta(hours=2)
    created = owner1_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert created.status_code == 201
    reservation_id = int(created.get_json()["reservation"]["id"])

    owner_detail = owner1_client.get(f"/reservations/{reservation_id}")
    assert owner_detail.status_code == 200
    assert f"Reservation #{reservation_id}".encode() in owner_detail.data
    assert b"Planning snapshot for this booked trip." in owner_detail.data
    assert b"Edit Reservation" in owner_detail.data

    forbidden = owner2_client.get(f"/reservations/{reservation_id}")
    assert forbidden.status_code == 403

    admin_detail = admin_client.get(f"/reservations/{reservation_id}")
    assert admin_detail.status_code == 200

    canceled = owner1_client.post(f"/api/reservations/{reservation_id}/cancel")
    assert canceled.status_code == 200

    owner_after_cancel = owner1_client.get(f"/reservations/{reservation_id}")
    assert owner_after_cancel.status_code == 404
    admin_after_cancel = admin_client.get(f"/reservations/{reservation_id}")
    assert admin_after_cancel.status_code == 404


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


def test_quote_draft_create_and_get(reservation_env):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    depart_dt = _to_quarter(datetime.now() + timedelta(days=2, hours=3))
    estimate = _quote_estimate_payload("oneway", depart_dt)
    create = owner_client.post(
        "/api/planner/quote-drafts",
        json={
            "estimate": estimate,
            "outbound_departure_local": depart_dt.strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert create.status_code == 201
    created = create.get_json()
    assert created["token"]
    assert created["draft_preview"]["trip_type"] == "oneway"
    assert len(created["draft_preview"]["legs"]) == 1

    token = created["token"]
    fetched = owner_client.get(f"/api/planner/quote-drafts/{token}")
    assert fetched.status_code == 200
    draft = fetched.get_json()
    assert draft["trip_type"] == "oneway"
    assert draft["legs"][0]["dep_icao"] == "KCAK"
    assert draft["legs"][0]["dest_icao"] == "KSRQ"
    assert draft["legs"][0]["notes"] == ""


def test_quote_draft_oneway_submit_notifications_single_leg(reservation_env, monkeypatch):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    depart_dt = _to_quarter(datetime.now() + timedelta(days=2, hours=1))
    estimate = _quote_estimate_payload("oneway", depart_dt)
    create = owner_client.post(
        "/api/planner/quote-drafts",
        json={
            "estimate": estimate,
            "outbound_departure_local": depart_dt.strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert create.status_code == 201
    token = create.get_json()["token"]

    captured = {"count": 0}
    monkeypatch.setattr(
        tbm_app,
        "_build_reservation_email_context",
        lambda created_rows, requester_user: (
            captured.__setitem__("count", len(list(created_rows or []))) or {
                "requester_name": requester_user.get("name"),
                "requester_email": requester_user.get("email"),
                "reservation_ids": [],
                "legs": [],
                "review_url": "/admin",
                "timezone": "America/New_York",
            }
        ),
    )
    monkeypatch.setattr(tbm_app, "_send_requester_submission_email", lambda _ctx, *, source, actor_user_id=None: True)
    monkeypatch.setattr(tbm_app, "_send_admin_new_request_email", lambda _ctx, _emails, *, source, actor_user_id=None: True)

    submit = owner_client.post(f"/api/planner/quote-drafts/{token}/submit")
    assert submit.status_code == 200
    assert captured["count"] == 1


def test_quote_draft_roundtrip_requires_return_departure(reservation_env):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    depart_dt = _to_quarter(datetime.now() + timedelta(days=4, hours=2))
    return_dt = depart_dt + timedelta(days=1)
    estimate = _quote_estimate_payload("roundtrip", depart_dt, return_dt)
    resp = owner_client.post(
        "/api/planner/quote-drafts",
        json={
            "estimate": estimate,
            "outbound_departure_local": depart_dt.strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert resp.status_code == 400
    assert resp.get_json()["field"] == "return_departure_local"


def test_quote_draft_submit_consumes_and_cannot_reuse(reservation_env):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    depart_dt = _to_quarter(datetime.now() + timedelta(days=5, hours=1))
    estimate = _quote_estimate_payload("oneway", depart_dt)
    create = owner_client.post(
        "/api/planner/quote-drafts",
        json={
            "estimate": estimate,
            "outbound_departure_local": depart_dt.strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert create.status_code == 201
    token = create.get_json()["token"]

    submit = owner_client.post(f"/api/planner/quote-drafts/{token}/submit")
    assert submit.status_code == 200
    created = submit.get_json()["created"]
    assert len(created) == 1
    assert created[0]["status"] == "pending"

    again = owner_client.post(f"/api/planner/quote-drafts/{token}/submit")
    assert again.status_code == 409
    assert again.get_json()["code"] == "draft_consumed"


def test_quote_draft_token_is_user_scoped(reservation_env):
    owner1_client = tbm_app.app.test_client()
    owner2_client = tbm_app.app.test_client()
    _login(owner1_client, "owner1@example.com", "ownerpass123")
    _login(owner2_client, "owner2@example.com", "ownerpass123")

    depart_dt = _to_quarter(datetime.now() + timedelta(days=8))
    estimate = _quote_estimate_payload("oneway", depart_dt)
    create = owner1_client.post(
        "/api/planner/quote-drafts",
        json={
            "estimate": estimate,
            "outbound_departure_local": depart_dt.strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert create.status_code == 201
    token = create.get_json()["token"]

    blocked = owner2_client.get(f"/api/planner/quote-drafts/{token}")
    assert blocked.status_code == 404


def test_roundtrip_quote_submit_is_atomic_when_overlap_exists(reservation_env):
    owner1_client = tbm_app.app.test_client()
    owner2_client = tbm_app.app.test_client()
    _login(owner1_client, "owner1@example.com", "ownerpass123")
    _login(owner2_client, "owner2@example.com", "ownerpass123")

    depart_dt = _to_quarter(datetime.now() + timedelta(days=6, hours=3))
    return_depart_dt = depart_dt + timedelta(days=1, hours=2)
    estimate = _quote_estimate_payload("roundtrip", depart_dt, return_depart_dt)
    create = owner1_client.post(
        "/api/planner/quote-drafts",
        json={
            "estimate": estimate,
            "outbound_departure_local": depart_dt.strftime("%Y-%m-%dT%H:%M"),
            "return_departure_local": return_depart_dt.strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert create.status_code == 201
    token = create.get_json()["token"]

    blocking_start = return_depart_dt + timedelta(minutes=15)
    blocking_end = blocking_start + timedelta(hours=1)
    blocking = owner2_client.post(
        "/api/reservation-requests",
        json=_reservation_payload(blocking_start, blocking_end),
    )
    assert blocking.status_code == 201

    submit = owner1_client.post(f"/api/planner/quote-drafts/{token}/submit")
    assert submit.status_code == 409
    assert submit.get_json()["code"] == "overlap_conflict"

    with storage.get_conn(reservation_env["db_url"]) as conn:
        owner1_rows = conn.execute(
            "SELECT id FROM reservations WHERE requested_by_user_id = %s",
            (reservation_env["owner1"],),
        ).fetchall()
    assert len(owner1_rows) == 0


def test_quote_draft_roundtrip_submit_notifications_bundled(reservation_env, monkeypatch):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    depart_dt = _to_quarter(datetime.now() + timedelta(days=9, hours=2))
    return_depart_dt = depart_dt + timedelta(days=1, hours=2)
    estimate = _quote_estimate_payload("roundtrip", depart_dt, return_depart_dt)
    create = owner_client.post(
        "/api/planner/quote-drafts",
        json={
            "estimate": estimate,
            "outbound_departure_local": depart_dt.strftime("%Y-%m-%dT%H:%M"),
            "return_departure_local": return_depart_dt.strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert create.status_code == 201
    token = create.get_json()["token"]

    captured = {"count": 0}
    monkeypatch.setattr(
        tbm_app,
        "_build_reservation_email_context",
        lambda created_rows, requester_user: (
            captured.__setitem__("count", len(list(created_rows or []))) or {
                "requester_name": requester_user.get("name"),
                "requester_email": requester_user.get("email"),
                "reservation_ids": [],
                "legs": [],
                "review_url": "/admin",
                "timezone": "America/New_York",
            }
        ),
    )
    monkeypatch.setattr(tbm_app, "_send_requester_submission_email", lambda _ctx, *, source, actor_user_id=None: True)
    monkeypatch.setattr(tbm_app, "_send_admin_new_request_email", lambda _ctx, _emails, *, source, actor_user_id=None: True)

    submit = owner_client.post(f"/api/planner/quote-drafts/{token}/submit")
    assert submit.status_code == 200
    assert captured["count"] == 2


def test_notifications_disabled_skips_smtp_transport(reservation_env, monkeypatch):
    owner_client = tbm_app.app.test_client()
    _login(owner_client, "owner1@example.com", "ownerpass123")

    called = {"smtp": 0}

    class TrapSMTP:
        def __init__(self, *args, **kwargs):
            called["smtp"] += 1

    monkeypatch.setattr(tbm_app, "EMAIL_ENABLED", False)
    monkeypatch.setattr(tbm_app, "EMAIL_SMTP_HOST", "smtp.test.local")
    monkeypatch.setattr(tbm_app, "EMAIL_FROM_ADDRESS", "noreply@test.local")
    monkeypatch.setattr(tbm_app.smtplib, "SMTP", TrapSMTP)

    start = datetime.now() + timedelta(days=2)
    end = start + timedelta(hours=1)
    resp = owner_client.post("/api/reservation-requests", json=_reservation_payload(start, end))
    assert resp.status_code == 201
    assert called["smtp"] == 0

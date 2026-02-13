from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

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
    if response.status_code == 200:
        _attach_csrf_headers(client)
    return response


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test_live_tracking.sqlite3"
    monkeypatch.setattr(tbm_app, "TBM_DB_PATH", str(db_path))
    monkeypatch.setattr(tbm_app, "TBM_HOME_TZ", "America/New_York")
    monkeypatch.setattr(tbm_app, "LIVE_TRACKING_TAIL", "N656W")
    monkeypatch.setattr(tbm_app, "FLIGHTAWARE_CACHE_TTL_SEC", 75)
    monkeypatch.setattr(tbm_app, "FLIGHTAWARE_TRACK_REFRESH_SEC", 300)
    storage.init_db(str(db_path))
    with storage.get_conn(str(db_path)) as conn:
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
    tbm_app._LIVE_TRACKING_CACHE.clear()
    tbm_app._LIVE_TRACKING_TRACK_CACHE.clear()
    return str(db_path)


def _sample_flight_payload():
    now = datetime.now(ZoneInfo("UTC"))
    return {
        "flights": [
            {
                "ident": "N656W",
                "fa_flight_id": "N656W-123",
                "status": "En Route",
                "origin": {"code_icao": "KCAK"},
                "destination": {"code_icao": "KSRQ"},
                "latitude": 39.98,
                "longitude": -82.89,
                "groundspeed": 302,
                "altitude": 24000,
                "heading": 166,
                "filed_departure_time": {"epoch": int((now - timedelta(minutes=50)).timestamp())},
                "actual_departure_time": {"epoch": int((now - timedelta(minutes=45)).timestamp())},
                "estimated_arrival_time": {"epoch": int((now + timedelta(minutes=95)).timestamp())},
                "last_position": {"epoch": int((now - timedelta(minutes=1)).timestamp())},
            }
        ]
    }


def _sample_track_payload():
    now = datetime.now(ZoneInfo("UTC"))
    return {
        "positions": [
            {
                "timestamp": int((now - timedelta(minutes=1)).timestamp()),
                "latitude": 39.98,
                "longitude": -82.89,
                "groundspeed": 302,
                "altitude": 24000,
            },
            {
                "timestamp": int((now - timedelta(minutes=3)).timestamp()),
                "latitude": 40.02,
                "longitude": -83.10,
                "groundspeed": 298,
                "altitude": 23500,
            },
        ]
    }


def _sample_ground_flight_payload(*, last_position_minutes_ago: int = 20):
    now = datetime.now(ZoneInfo("UTC"))
    return {
        "flights": [
            {
                "ident": "N656W",
                "fa_flight_id": "N656W-123",
                "status": "On Ground",
                "origin": {"code_icao": "KCAK"},
                "destination": {"code_icao": "KSRQ"},
                "latitude": 27.40,
                "longitude": -82.55,
                "groundspeed": 0,
                "altitude": 0,
                "heading": 0,
                "actual_arrival_time": {"epoch": int((now - timedelta(minutes=35)).timestamp())},
                "last_position": {"epoch": int((now - timedelta(minutes=last_position_minutes_ago)).timestamp())},
            }
        ]
    }


def test_live_tracking_page_requires_login(live_env):
    client = tbm_app.app.test_client()
    resp = client.get("/live-tracking")
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


def test_live_tracking_api_requires_login(live_env):
    client = tbm_app.app.test_client()
    resp = client.get("/api/live-tracking")
    assert resp.status_code == 401


def test_live_tracking_page_renders_for_logged_in_owner(live_env):
    client = tbm_app.app.test_client()
    assert _login(client, "owner@example.com", "ownerpass123").status_code == 200
    resp = client.get("/live-tracking")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Live Tracking: N656W" in body
    assert "Open in FlightAware" in body


def test_live_tracking_api_fallback_without_api_key(live_env, monkeypatch):
    monkeypatch.setattr(tbm_app, "FLIGHTAWARE_AEROAPI_KEY", "")
    tbm_app._LIVE_TRACKING_CACHE.clear()
    client = tbm_app.app.test_client()
    assert _login(client, "owner@example.com", "ownerpass123").status_code == 200

    resp = client.get("/api/live-tracking")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["provider_mode"] == "fallback"
    assert data["snapshot"]["tail_number"] == "N656W"
    assert data["flightaware_url"].endswith("/N656W")
    assert int(data["polling"]["active_ms"]) >= 10000
    assert int(data["polling"]["idle_ms"]) >= 30000


def test_live_tracking_api_uses_aeroapi_and_cache(live_env, monkeypatch):
    monkeypatch.setattr(tbm_app, "FLIGHTAWARE_AEROAPI_KEY", "test-key")
    tbm_app._LIVE_TRACKING_CACHE.clear()

    calls = {"count": 0}

    def fake_get(path, params=None):
        calls["count"] += 1
        if path.startswith("flights/N656W-123/track"):
            return _sample_track_payload()
        return _sample_flight_payload()

    monkeypatch.setattr(tbm_app, "_flightaware_get", fake_get)

    client = tbm_app.app.test_client()
    assert _login(client, "owner@example.com", "ownerpass123").status_code == 200

    first = client.get("/api/live-tracking")
    second = client.get("/api/live-tracking")
    assert first.status_code == 200
    assert second.status_code == 200
    data = first.get_json()
    assert data["provider_mode"] == "aeroapi"
    assert data["snapshot"]["status"] == "en_route"
    assert data["reservation_match"] in ("matched", "none")
    assert len(data["snapshot"]["track_points"]) >= 1
    # First call should fetch flights + track once. Second call should hit cache.
    assert calls["count"] == 2


def test_live_tracking_api_provider_failure_falls_back(live_env, monkeypatch):
    monkeypatch.setattr(tbm_app, "FLIGHTAWARE_AEROAPI_KEY", "test-key")
    tbm_app._LIVE_TRACKING_CACHE.clear()

    def fake_get(_path, params=None):  # noqa: ARG001
        raise RuntimeError("boom")

    monkeypatch.setattr(tbm_app, "_flightaware_get", fake_get)

    client = tbm_app.app.test_client()
    assert _login(client, "owner@example.com", "ownerpass123").status_code == 200
    resp = client.get("/api/live-tracking")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["provider_mode"] == "fallback"
    assert data["snapshot"]["status"] == "unknown"
    assert data["reservation_match"] == "none"
    assert data["ui_mode"] == "unknown"
    assert "ground_insights" in data["snapshot"]
    assert "ops_recommendations" in data
    assert "next_reservation" in data
    assert data["warnings"]
    assert "polling" in data


def test_live_tracking_track_call_is_throttled_by_track_refresh_window(live_env, monkeypatch):
    monkeypatch.setattr(tbm_app, "FLIGHTAWARE_AEROAPI_KEY", "test-key")
    monkeypatch.setattr(tbm_app, "FLIGHTAWARE_CACHE_TTL_SEC", 0)
    monkeypatch.setattr(tbm_app, "FLIGHTAWARE_TRACK_REFRESH_SEC", 300)
    tbm_app._LIVE_TRACKING_CACHE.clear()
    tbm_app._LIVE_TRACKING_TRACK_CACHE.clear()

    calls = {"flights": 0, "track": 0}

    def fake_get(path, params=None):  # noqa: ARG001
        if path.startswith("flights/N656W-123/track"):
            calls["track"] += 1
            return _sample_track_payload()
        calls["flights"] += 1
        return _sample_flight_payload()

    monkeypatch.setattr(tbm_app, "_flightaware_get", fake_get)

    client = tbm_app.app.test_client()
    assert _login(client, "owner@example.com", "ownerpass123").status_code == 200
    first = client.get("/api/live-tracking")
    second = client.get("/api/live-tracking")
    assert first.status_code == 200
    assert second.status_code == 200
    assert calls["flights"] == 2
    assert calls["track"] == 1


def test_live_tracking_api_matches_approved_reservation(live_env, monkeypatch):
    monkeypatch.setattr(tbm_app, "FLIGHTAWARE_AEROAPI_KEY", "test-key")
    tbm_app._LIVE_TRACKING_CACHE.clear()

    with storage.get_conn(tbm_app.TBM_DB_PATH) as conn:
        owner = storage.get_user_by_email(conn, "owner@example.com")
        now = datetime.now(ZoneInfo("UTC"))
        storage.create_reservation(
            conn,
            status="approved",
            start_utc=(now - timedelta(hours=1)).isoformat(),
            end_utc=(now + timedelta(hours=2)).isoformat(),
            dep_icao="KCAK",
            dest_icao="KSRQ",
            parked_icao="KSRQ",
            traveling_user_id=int(owner["id"]),
            requested_by_user_id=int(owner["id"]),
            notes="Live test",
        )

    def fake_get(path, params=None):  # noqa: ARG001
        if path.startswith("flights/N656W-123/track"):
            return _sample_track_payload()
        return _sample_flight_payload()

    monkeypatch.setattr(tbm_app, "_flightaware_get", fake_get)

    client = tbm_app.app.test_client()
    assert _login(client, "owner@example.com", "ownerpass123").status_code == 200
    resp = client.get("/api/live-tracking")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["reservation_match"] == "matched"
    assert data["reservation_context"]["dep_icao"] == "KCAK"
    assert data["reservation_context"]["dest_icao"] == "KSRQ"


def test_live_tracking_ground_mode_payload_shape(live_env, monkeypatch):
    monkeypatch.setattr(tbm_app, "FLIGHTAWARE_AEROAPI_KEY", "test-key")
    tbm_app._LIVE_TRACKING_CACHE.clear()

    def fake_get(path, params=None):  # noqa: ARG001
        if path.startswith("flights/N656W-123/track"):
            return _sample_track_payload()
        return _sample_ground_flight_payload()

    monkeypatch.setattr(tbm_app, "_flightaware_get", fake_get)
    client = tbm_app.app.test_client()
    assert _login(client, "owner@example.com", "ownerpass123").status_code == 200
    resp = client.get("/api/live-tracking")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ui_mode"] == "ground"
    assert data["reservation_match"] == "none"
    assert "ground_insights" in data["snapshot"]
    assert "current_airport" in data["snapshot"]["ground_insights"]
    assert isinstance(data["ops_recommendations"], list)


def test_live_tracking_includes_next_reservation_countdown(live_env, monkeypatch):
    monkeypatch.setattr(tbm_app, "FLIGHTAWARE_AEROAPI_KEY", "")
    tbm_app._LIVE_TRACKING_CACHE.clear()

    with storage.get_conn(tbm_app.TBM_DB_PATH) as conn:
        owner = storage.get_user_by_email(conn, "owner@example.com")
        start = datetime.now(ZoneInfo("UTC")) + timedelta(hours=2)
        end = start + timedelta(hours=2)
        storage.create_reservation(
            conn,
            status="approved",
            start_utc=start.isoformat(),
            end_utc=end.isoformat(),
            dep_icao="KSRQ",
            dest_icao="KCAK",
            parked_icao="KCAK",
            traveling_user_id=int(owner["id"]),
            requested_by_user_id=int(owner["id"]),
            notes="Upcoming",
        )

    client = tbm_app.app.test_client()
    assert _login(client, "owner@example.com", "ownerpass123").status_code == 200
    resp = client.get("/api/live-tracking")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["next_reservation"] is not None
    assert data["next_reservation"]["countdown_minutes"] <= 120
    assert data["next_reservation"]["countdown_minutes"] >= 0


def test_live_tracking_stale_telemetry_recommendation(live_env, monkeypatch):
    monkeypatch.setattr(tbm_app, "FLIGHTAWARE_AEROAPI_KEY", "test-key")
    monkeypatch.setattr(tbm_app, "FLIGHTAWARE_CACHE_TTL_SEC", 0)
    tbm_app._LIVE_TRACKING_CACHE.clear()

    def fake_get(path, params=None):  # noqa: ARG001
        if path.startswith("flights/N656W-123/track"):
            return _sample_track_payload()
        return _sample_ground_flight_payload(last_position_minutes_ago=800)

    monkeypatch.setattr(tbm_app, "_flightaware_get", fake_get)
    client = tbm_app.app.test_client()
    assert _login(client, "owner@example.com", "ownerpass123").status_code == 200
    resp = client.get("/api/live-tracking")
    assert resp.status_code == 200
    data = resp.get_json()
    assert any("Telemetry appears stale" in item for item in data["ops_recommendations"])


def test_live_tracking_nav_link_visible_for_logged_in_users(live_env):
    client = tbm_app.app.test_client()
    assert _login(client, "owner@example.com", "ownerpass123").status_code == 200
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert ">Live<" in body

import os
from datetime import datetime, timedelta

import pytest

import app as tbm_app
import db as storage
from auth import hash_password


def _setup_users(monkeypatch):
    db_url = os.environ.get("TEST_DATABASE_URL") or tbm_app.DATABASE_URL
    if not db_url:
        pytest.skip("Set TEST_DATABASE_URL (or DATABASE_URL) for Postgres-backed tests.")
    monkeypatch.setattr(tbm_app, "DATABASE_URL", db_url)
    storage.init_db(db_url)
    with storage.get_conn(db_url) as conn:
        storage.reset_for_tests(conn)
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


def test_response_has_request_id_header():
    client = tbm_app.app.test_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-Id")


def test_csrf_required_for_authenticated_mutation(tmp_path, monkeypatch):
    _setup_users(monkeypatch)
    monkeypatch.setattr(tbm_app, "ENABLE_RATE_LIMIT", False)
    tbm_app._RATE_LIMIT_BUCKETS.clear()
    client = tbm_app.app.test_client()
    login = client.post("/api/login", json={"email": "owner@example.com", "password": "ownerpass123"})
    assert login.status_code == 200

    start = datetime.now() + timedelta(days=2)
    end = start + timedelta(hours=1)
    payload = {
        "start_local": start.strftime("%Y-%m-%dT%H:%M"),
        "end_local": end.strftime("%Y-%m-%dT%H:%M"),
        "dep_icao": "KCAK",
        "dest_icao": "KSRQ",
    }
    resp = client.post("/api/reservation-requests", json=payload)
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["code"] == "csrf_failed"
    assert data.get("request_id")


def test_login_rate_limit(tmp_path, monkeypatch):
    _setup_users(monkeypatch)
    monkeypatch.setattr(tbm_app, "ENABLE_RATE_LIMIT", True)
    tbm_app._RATE_LIMIT_BUCKETS.clear()
    monkeypatch.setitem(tbm_app.RATE_LIMIT_RULES, "api_login", (2, 60))
    client = tbm_app.app.test_client()
    assert client.post("/api/login", json={"email": "owner@example.com", "password": "ownerpass123"}).status_code == 200
    assert client.post("/api/login", json={"email": "owner@example.com", "password": "ownerpass123"}).status_code == 200
    blocked = client.post("/api/login", json={"email": "owner@example.com", "password": "ownerpass123"})
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "rate_limited"


def test_session_cookie_security_baseline():
    assert tbm_app.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert tbm_app.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

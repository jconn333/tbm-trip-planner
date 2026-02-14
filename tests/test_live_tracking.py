import os
import pytest

import app as tbm_app
import db as storage
from auth import hash_password


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    db_url = os.environ.get("TEST_DATABASE_URL") or tbm_app.DATABASE_URL
    if not db_url:
        pytest.skip("Set TEST_DATABASE_URL (or DATABASE_URL) for Postgres-backed tests.")
    monkeypatch.setattr(tbm_app, "DATABASE_URL", db_url)
    monkeypatch.setattr(tbm_app, "TBM_HOME_TZ", "America/New_York")
    monkeypatch.setattr(tbm_app, "LIVE_TRACKING_TAIL", "N656W")
    storage.init_db(db_url)
    with storage.get_conn(db_url) as conn:
        storage.reset_for_tests(conn)
        storage.create_user(
            conn,
            email="owner@example.com",
            name="Owner",
            role="owner",
            password_hash=hash_password("ownerpass123"),
        )


def _login(client, email: str, password: str):
    return client.post("/api/login", json={"email": email, "password": password})


def test_live_tracking_page_requires_login(live_env):
    client = tbm_app.app.test_client()
    resp = client.get("/live-tracking")
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


def test_live_tracking_page_renders_flightaware_link(live_env):
    client = tbm_app.app.test_client()
    assert _login(client, "owner@example.com", "ownerpass123").status_code == 200

    resp = client.get("/live-tracking")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "N656W Live Profile" in body
    assert "Open on FlightAware" in body
    assert "https://www.flightaware.com/live/flight/N656W" in body


def test_live_tracking_api_endpoint_removed(live_env):
    client = tbm_app.app.test_client()
    assert _login(client, "owner@example.com", "ownerpass123").status_code == 200
    resp = client.get("/api/live-tracking")
    assert resp.status_code == 404


def test_live_tracking_nav_link_visible_for_logged_in_users(live_env):
    client = tbm_app.app.test_client()
    assert _login(client, "owner@example.com", "ownerpass123").status_code == 200
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert ">Live<" in body

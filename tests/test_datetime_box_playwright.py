from __future__ import annotations

from datetime import datetime, timedelta
from threading import Thread

import pytest
from werkzeug.serving import make_server

import app as tbm_app
import db as storage
from auth import hash_password

playwright = pytest.importorskip("playwright.sync_api")


class _ServerThread(Thread):
    def __init__(self, server):
        super().__init__(daemon=True)
        self.server = server

    def run(self):
        self.server.serve_forever()

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    db_path = tmp_path / "test_datetime_click.sqlite3"
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
        storage.create_reservation(
            conn,
            status="pending",
            start_utc=(datetime.now(tbm_app.ZoneInfo("UTC")) + timedelta(days=2)).isoformat(),
            end_utc=(datetime.now(tbm_app.ZoneInfo("UTC")) + timedelta(days=2, hours=2)).isoformat(),
            dep_icao="KCAK",
            dest_icao="KSRQ",
            parked_icao="KSRQ",
            traveling_user_id=int(owner["id"]),
            requested_by_user_id=int(owner["id"]),
            notes="Playwright test reservation",
        )
        storage.create_reservation(
            conn,
            status="approved",
            start_utc=(datetime.now(tbm_app.ZoneInfo("UTC")) + timedelta(days=4)).isoformat(),
            end_utc=(datetime.now(tbm_app.ZoneInfo("UTC")) + timedelta(days=4, hours=2)).isoformat(),
            dep_icao="KSRQ",
            dest_icao="KCAK",
            parked_icao="KCAK",
            traveling_user_id=int(owner["id"]),
            requested_by_user_id=int(owner["id"]),
            approved_by_user_id=int(admin["id"]),
            decision_at_utc=datetime.now(tbm_app.ZoneInfo("UTC")).isoformat(),
            notes="Playwright approved reservation",
        )
    tbm_app._invalidate_settings_cache()

    tbm_app.app.config["TESTING"] = True
    server = make_server("127.0.0.1", 0, tbm_app.app)
    port = server.server_port
    thread = _ServerThread(server)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url
    finally:
        thread.stop()
        thread.join(timeout=2)


def _login(context, base_url: str, email: str, password: str):
    resp = context.request.post(f"{base_url}/api/login", json={"email": email, "password": password})
    assert resp.ok


def test_calendar_datetime_box_click_focus(live_server):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        _login(context, live_server, "owner@example.com", "ownerpass123")
        page = context.new_page()
        page.goto(f"{live_server}/calendar")
        page.wait_for_selector('[data-dt-box="start"]')

        page.click('[data-dt-box="start"]', position={"x": 2, "y": 2})
        first_focus = page.evaluate("document.activeElement && document.activeElement.id")
        assert first_focus == "startDate"

        page.fill("#startDate", "2030-01-01")
        page.click('[data-dt-box="start"]', position={"x": 2, "y": 2})
        second_focus = page.evaluate("document.activeElement && document.activeElement.id")
        assert second_focus == "startTime"
        browser.close()


def test_admin_modal_datetime_box_click_focus(live_server):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        _login(context, live_server, "admin@example.com", "adminpass123")
        page = context.new_page()
        page.goto(f"{live_server}/admin/flights")
        page.wait_for_selector('[data-action="edit"]')
        page.click('[data-action="edit"]')
        page.wait_for_selector('[data-dt-box="edit-start"]')

        page.click('[data-dt-box="edit-start"]', position={"x": 2, "y": 2})
        first_focus = page.evaluate("document.activeElement && document.activeElement.id")
        assert first_focus == "editStartDate"

        page.fill("#editStartDate", "2030-01-02")
        page.click('[data-dt-box="edit-start"]', position={"x": 2, "y": 2})
        second_focus = page.evaluate("document.activeElement && document.activeElement.id")
        assert second_focus == "editStartTime"
        browser.close()


def test_my_flights_modal_datetime_box_click_focus(live_server):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        _login(context, live_server, "owner@example.com", "ownerpass123")
        page = context.new_page()
        page.goto(f"{live_server}/my-flights")
        page.wait_for_selector('[data-action="edit-pending"]')
        page.click('[data-action="edit-pending"]')
        page.wait_for_selector('[data-dt-box="modal-start"]')

        page.click('[data-dt-box="modal-start"]', position={"x": 2, "y": 2})
        first_focus = page.evaluate("document.activeElement && document.activeElement.id")
        assert first_focus == "modalStartDate"

        page.fill("#modalStartDate", "2030-01-03")
        page.click('[data-dt-box="modal-start"]', position={"x": 2, "y": 2})
        second_focus = page.evaluate("document.activeElement && document.activeElement.id")
        assert second_focus == "modalStartTime"
        browser.close()


def test_calendar_reservation_details_modal_and_view_detail(live_server):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        _login(context, live_server, "owner@example.com", "ownerpass123")
        page = context.new_page()
        page.goto(f"{live_server}/calendar")
        page.wait_for_selector(".fc-event")

        pending_event = page.locator(".fc-event", has_text="KCAK → KSRQ").first
        pending_event.click()
        page.wait_for_selector("#reservationDetailModal.open")

        assert page.locator("#detailViewBtn").is_visible()
        assert page.locator("#detailCancelBtn.is-hidden").count() == 0
        assert page.locator("#detailEditBtn.is-hidden").count() == 0

        page.click("#detailViewBtn")
        page.wait_for_url("**/reservations/*")
        page.wait_for_selector("text=Planning snapshot for this booked trip.")
        browser.close()


def test_my_flights_view_more_detail_buttons(live_server):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        _login(context, live_server, "owner@example.com", "ownerpass123")
        page = context.new_page()
        page.goto(f"{live_server}/my-flights")
        page.wait_for_selector('[data-action="view-detail"][data-list="pending"]')
        page.wait_for_selector('[data-action="view-detail"][data-list="approved"]')

        assert page.locator('[data-action="view-detail"][data-list="pending"]').count() >= 1
        assert page.locator('[data-action="view-detail"][data-list="approved"]').count() >= 1

        page.locator('[data-action="view-detail"][data-list="approved"]').first.click()
        page.wait_for_url("**/reservations/*")
        page.wait_for_selector("text=Reservation #")
        browser.close()

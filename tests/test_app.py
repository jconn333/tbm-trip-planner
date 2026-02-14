import os
from datetime import date, timedelta

import pytest

import app as tbm_app
import db as storage
from auth import hash_password


@pytest.fixture(autouse=True)
def geocode_test_defaults(monkeypatch):
    monkeypatch.setattr(tbm_app, "GOOGLE_MAPS_API_KEY", "")
    monkeypatch.setattr(tbm_app, "GEOCODE_PROVIDER_ORDER_RAW", "nominatim,census")
    with tbm_app._GEOCODE_CACHE_LOCK:
        tbm_app._GEOCODE_CACHE.clear()
    with tbm_app._GEOCODE_SUGGEST_CACHE_LOCK:
        tbm_app._GEOCODE_SUGGEST_CACHE.clear()


def test_haversine_nm_basic_range():
    # Approx JFK -> LAX great-circle distance should be around 2145 nm.
    nm = tbm_app.haversine_nm(40.6413, -73.7781, 33.9416, -118.4085)
    assert 2100 <= nm <= 2200


def test_parse_date_only_cases():
    today = date.today()

    assert tbm_app.parse_date_only("2026-02-10") == date(2026, 2, 10)
    assert tbm_app.parse_date_only("today") == today
    assert tbm_app.parse_date_only("tomorrow") == today + timedelta(days=1)
    assert tbm_app.parse_date_only("not-a-date") is None


def test_location_query_candidates_city_state():
    candidates = tbm_app._location_query_candidates("Trenton MO")
    assert "Trenton, MO" in candidates
    assert "Trenton, MO, USA" in candidates


def test_smtp_send_email_success(monkeypatch):
    calls = {"starttls": 0, "login": 0, "send": 0}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            assert host == "smtp.test.local"
            assert port == 587
            assert timeout == 8

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self, context=None):
            calls["starttls"] += 1

        def login(self, username, password):
            assert username == "user"
            assert password == "pass"
            calls["login"] += 1

        def send_message(self, msg):
            assert msg["Subject"] == "Test Subject"
            calls["send"] += 1

    monkeypatch.setattr(tbm_app, "EMAIL_ENABLED", True)
    monkeypatch.setattr(tbm_app, "EMAIL_SMTP_HOST", "smtp.test.local")
    monkeypatch.setattr(tbm_app, "EMAIL_SMTP_PORT", 587)
    monkeypatch.setattr(tbm_app, "EMAIL_SMTP_USERNAME", "user")
    monkeypatch.setattr(tbm_app, "EMAIL_SMTP_PASSWORD", "pass")
    monkeypatch.setattr(tbm_app, "EMAIL_SMTP_USE_TLS", True)
    monkeypatch.setattr(tbm_app, "EMAIL_SMTP_USE_SSL", False)
    monkeypatch.setattr(tbm_app, "EMAIL_FROM_ADDRESS", "noreply@test.local")
    monkeypatch.setattr(tbm_app, "EMAIL_FROM_NAME", "TBM Test")
    monkeypatch.setattr(tbm_app, "EMAIL_REPLY_TO", "")
    monkeypatch.setattr(tbm_app, "EMAIL_TIMEOUT_SEC", 8)
    monkeypatch.setattr(tbm_app.smtplib, "SMTP", FakeSMTP)

    ok = tbm_app._smtp_send_email(to_addrs=["owner@example.com"], subject="Test Subject", body_text="hello")
    assert ok is True
    assert calls["starttls"] == 1
    assert calls["login"] == 1
    assert calls["send"] == 1


def test_smtp_send_email_failure_returns_false(monkeypatch):
    class BoomSMTP:
        def __init__(self, host, port, timeout):
            raise RuntimeError("smtp down")

    monkeypatch.setattr(tbm_app, "EMAIL_ENABLED", True)
    monkeypatch.setattr(tbm_app, "EMAIL_SMTP_HOST", "smtp.test.local")
    monkeypatch.setattr(tbm_app, "EMAIL_FROM_ADDRESS", "noreply@test.local")
    monkeypatch.setattr(tbm_app, "EMAIL_SMTP_USE_SSL", False)
    monkeypatch.setattr(tbm_app.smtplib, "SMTP", BoomSMTP)

    ok = tbm_app._smtp_send_email(to_addrs=["owner@example.com"], subject="Test Subject", body_text="hello")
    assert ok is False


def test_build_reservation_email_context_single_and_multi():
    requester = {"name": "Owner One", "email": "owner1@example.com"}
    rows = [
        {
            "id": 101,
            "dep_icao": "KCAK",
            "dest_icao": "KSRQ",
            "start_utc": "2030-02-01T14:00:00+00:00",
            "end_utc": "2030-02-01T17:00:00+00:00",
            "notes": "First leg",
        },
        {
            "id": 102,
            "dep_icao": "KSRQ",
            "dest_icao": "KCAK",
            "start_utc": "2030-02-02T14:00:00+00:00",
            "end_utc": "2030-02-02T17:00:00+00:00",
            "notes": "",
        },
    ]
    context = tbm_app._build_reservation_email_context(rows, requester)
    assert context["requester_email"] == "owner1@example.com"
    assert context["reservation_ids"] == [101, 102]
    assert len(context["legs"]) == 2
    assert context["legs"][0]["dep_icao"] == "KCAK"


def test_send_requester_decision_email_builds_subject(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        tbm_app,
        "_smtp_send_email",
        lambda *, to_addrs, subject, body_text, **_kwargs: sent.update({"to": to_addrs, "subject": subject, "body": body_text}) or True,
    )
    monkeypatch.setattr(tbm_app, "EMAIL_SUBJECT_PREFIX", "[TBM]")
    context = {
        "requester_name": "Owner One",
        "requester_email": "owner1@example.com",
        "reservation_id": 101,
        "dep_icao": "KCAK",
        "dest_icao": "KSRQ",
        "start_display": "02-01-2030 09:00",
        "end_display": "02-01-2030 12:00",
        "timezone": "America/New_York",
    }
    ok = tbm_app._send_requester_decision_email(
        context,
        decision="approved",
        decision_note="",
        actor_name="Admin",
        source="reservation_approved",
    )
    assert ok is True
    assert sent["to"] == ["owner1@example.com"]
    assert sent["subject"] == "[TBM] Trip request approved"


@pytest.fixture
def no_winds(monkeypatch):
    monkeypatch.setattr(
        tbm_app,
        "wind_component_fl270_kts",
        lambda dep, dest, depart_dt_local: (0.0, "mock winds"),
    )


@pytest.fixture
def planner_auth_env(monkeypatch):
    db_url = os.environ.get("TEST_DATABASE_URL") or tbm_app.DATABASE_URL
    if not db_url:
        pytest.skip("Set TEST_DATABASE_URL (or DATABASE_URL) for Postgres-backed tests.")
    monkeypatch.setattr(tbm_app, "DATABASE_URL", db_url)
    monkeypatch.setattr(tbm_app, "TBM_HOME_TZ", "America/New_York")
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
    return db_url


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


def _login(client, email="owner@example.com", password="ownerpass123"):
    response = client.post("/api/login", json={"email": email, "password": password})
    if response.status_code == 200:
        _attach_csrf_headers(client)
    return response


def test_estimate_trip_structure(no_winds):
    depart_date = date.today() + timedelta(days=1)
    est = tbm_app.estimate_trip(
        dep="KCAK",
        dest="KSRQ",
        trip_type="oneway",
        depart_date=depart_date,
        return_date=None,
    )

    assert est["inputs"]["dep"] == "KCAK"
    assert est["inputs"]["dest"] == "KSRQ"
    assert len(est["legs"]) == 1
    assert "typical" in est["totals"]
    assert "conservative" in est["totals"]
    assert est["winds_outbound"]["details"] == "mock winds"


def test_api_estimate_response_shape(no_winds, planner_auth_env):
    client = tbm_app.app.test_client()
    assert _login(client).status_code == 200
    payload = {
        "dep": "KCAK",
        "dest": "KSRQ",
        "trip_type": "oneway",
        "depart_date": (date.today() + timedelta(days=1)).isoformat(),
    }

    resp = client.post("/api/estimate", json=payload)
    assert resp.status_code == 200

    data = resp.get_json()
    assert data["app"] == tbm_app.APP_NAME
    assert "assumptions" in data
    assert "legs" in data and isinstance(data["legs"], list)
    assert data["legs"][0]["from"] == "KCAK"
    assert data["legs"][0]["to"] == "KSRQ"
    assert "totals" in data
    assert "typical" in data["totals"]
    assert "conservative" in data["totals"]


def test_api_estimate_rejects_same_airport(planner_auth_env):
    client = tbm_app.app.test_client()
    assert _login(client).status_code == 200
    payload = {
        "dep": "KCAK",
        "dest": "KCAK",
        "trip_type": "oneway",
        "depart_date": (date.today() + timedelta(days=1)).isoformat(),
    }

    resp = client.post("/api/estimate", json=payload)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["code"] == "same_airport"


def test_api_estimate_accepts_lid_code(no_winds, planner_auth_env):
    client = tbm_app.app.test_client()
    assert _login(client).status_code == 200
    payload = {
        "dep": "10G",
        "dest": "KSRQ",
        "trip_type": "oneway",
        "depart_date": (date.today() + timedelta(days=1)).isoformat(),
    }

    resp = client.post("/api/estimate", json=payload)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["inputs"]["dep"] == "K10G"


def test_api_nearest_airports_lookup(monkeypatch):
    client = tbm_app.app.test_client()

    monkeypatch.setattr(
        tbm_app,
        "geocode_address",
        lambda address: {
            "query": address,
            "display_name": "Mocked Address, Akron, OH",
            "lat": 40.916,
            "lon": -81.442,
        },
    )

    resp = client.get("/api/nearest-airports?address=Akron+OH")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["resolved_address"] == "Mocked Address, Akron, OH"
    assert isinstance(data["airports"], list)
    assert len(data["airports"]) > 0
    assert "icao" in data["airports"][0]
    assert "distance_nm" in data["airports"][0]


def test_api_nearest_airports_filters(monkeypatch):
    client = tbm_app.app.test_client()

    monkeypatch.setattr(
        tbm_app,
        "geocode_address",
        lambda address: {
            "query": address,
            "display_name": "Mocked Address, Akron, OH",
            "lat": 40.916,
            "lon": -81.442,
        },
    )
    monkeypatch.setattr(
        tbm_app,
        "nearest_airports",
        lambda lat, lon, limit=8: [
            {"icao": "KAAA", "label": "AAA", "distance_nm": 12.0, "lat": 1.0, "lon": 1.0},
            {"icao": "KBBB", "label": "BBB", "distance_nm": 15.0, "lat": 2.0, "lon": 2.0},
            {"icao": "KCCC", "label": "CCC", "distance_nm": 20.0, "lat": 3.0, "lon": 3.0},
        ],
    )
    monkeypatch.setattr(
        tbm_app,
        "fetch_airport_ops_metadata",
        lambda icaos: {
            "KAAA": {"max_runway_ft": 3800, "runway_count": 1, "paved_runway": True, "jet_fuel": True, "towered": True},
            "KBBB": {"max_runway_ft": 5200, "runway_count": 2, "paved_runway": True, "jet_fuel": True, "towered": True},
            "KCCC": {"max_runway_ft": 6000, "runway_count": 2, "paved_runway": False, "jet_fuel": True, "towered": False},
        },
    )

    resp = client.get(
        "/api/nearest-airports?address=Akron+OH&min_runway_ft=5000&jet_fuel_only=true&paved_only=true&towered_only=true"
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["filters"]["min_runway_ft"] == 5000
    assert len(data["airports"]) == 1
    assert data["airports"][0]["icao"] == "KBBB"


def test_api_nearest_airports_widens_search_pool_for_towered(monkeypatch):
    client = tbm_app.app.test_client()

    monkeypatch.setattr(
        tbm_app,
        "geocode_address",
        lambda address: {
            "query": address,
            "display_name": "Mocked Address, Trenton, MO",
            "lat": 40.0891,
            "lon": -93.6040,
        },
    )

    def fake_nearest(_lat, _lon, limit=8):
        base = [
            {"icao": "KAAA", "label": "AAA", "distance_nm": 10.0, "lat": 1.0, "lon": 1.0},
            {"icao": "KBBB", "label": "BBB", "distance_nm": 15.0, "lat": 2.0, "lon": 2.0},
        ]
        far_towered = {"icao": "KTTT", "label": "TTT", "distance_nm": 120.0, "lat": 3.0, "lon": 3.0}
        return base if limit < 80 else base + [far_towered]

    monkeypatch.setattr(tbm_app, "nearest_airports", fake_nearest)
    monkeypatch.setattr(
        tbm_app,
        "fetch_airport_ops_metadata",
        lambda icaos: {
            "KAAA": {"max_runway_ft": 5000, "runway_count": 1, "paved_runway": True, "jet_fuel": True, "towered": False},
            "KBBB": {"max_runway_ft": 5200, "runway_count": 2, "paved_runway": True, "jet_fuel": True, "towered": False},
            "KTTT": {"max_runway_ft": 7000, "runway_count": 2, "paved_runway": True, "jet_fuel": True, "towered": True},
        },
    )

    resp = client.get("/api/nearest-airports?address=Trenton+MO&towered_only=true")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["airports"]) == 1
    assert data["airports"][0]["icao"] == "KTTT"
    assert data["filters"]["search_pool"] >= 80


def test_api_nearest_airports_accepts_selected_lat_lon(monkeypatch):
    client = tbm_app.app.test_client()

    monkeypatch.setattr(tbm_app, "geocode_address", lambda _address: None)
    monkeypatch.setattr(
        tbm_app,
        "nearest_airports",
        lambda lat, lon, limit=8: [
            {"icao": "KAAA", "label": "AAA", "distance_nm": 10.0, "lat": 1.0, "lon": 1.0},
            {"icao": "KBBB", "label": "BBB", "distance_nm": 15.0, "lat": 2.0, "lon": 2.0},
        ],
    )
    monkeypatch.setattr(
        tbm_app,
        "fetch_airport_ops_metadata",
        lambda icaos: {
            "KAAA": {"max_runway_ft": 6000, "runway_count": 1, "paved_runway": True, "jet_fuel": True, "towered": True},
            "KBBB": {"max_runway_ft": 4500, "runway_count": 1, "paved_runway": True, "jet_fuel": True, "towered": True},
        },
    )

    resp = client.get("/api/nearest-airports?address=Chosen+Suggestion&lat=40.1&lon=-81.2&limit=2")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["resolved_address"] == "Chosen Suggestion"
    assert len(data["airports"]) >= 1


def test_api_nearest_airports_accepts_google_place_id(monkeypatch):
    client = tbm_app.app.test_client()
    monkeypatch.setattr(
        tbm_app,
        "_google_place_details_geocode",
        lambda place_id, query=None: {
            "query": query or place_id,
            "display_name": "4365 STATE RTE 39, MILLERSBURG, OH, 44654",
            "lat": 40.5489,
            "lon": -81.7785,
            "place_id": place_id,
        },
    )
    monkeypatch.setattr(
        tbm_app,
        "nearest_airports",
        lambda lat, lon, limit=8: [
            {"icao": "KAAA", "label": "AAA", "distance_nm": 10.0, "lat": 1.0, "lon": 1.0},
        ],
    )
    monkeypatch.setattr(
        tbm_app,
        "fetch_airport_ops_metadata",
        lambda icaos: {"KAAA": {"max_runway_ft": 6000, "runway_count": 1, "paved_runway": True, "jet_fuel": True, "towered": True}},
    )

    resp = client.get("/api/nearest-airports?address=Chosen+Suggestion&place_id=abc123&limit=1")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["resolved_address"].startswith("4365 STATE RTE 39")
    assert len(data["airports"]) == 1


def test_geocode_address_prefers_specific_street_number(monkeypatch):
    query = "125 Main St Berlin OH unit-test"
    monkeypatch.setattr(tbm_app, "_location_query_candidates", lambda _q: [_q])
    monkeypatch.setattr(
        tbm_app,
        "_nominatim_search",
        lambda _q, limit=6: [
            {
                "display_name": "West Main Street, Berlin, Holmes County, Ohio, 44610, United States",
                "lat": "40.56",
                "lon": "-81.80",
                "importance": 0.9,
                "class": "highway",
                "type": "residential",
                "address": {"road": "West Main Street", "city": "Berlin", "state": "Ohio", "postcode": "44610"},
            },
            {
                "display_name": "125, East Main Street, Berlin Heights, Berlin Township, Ohio, 44814, United States",
                "lat": "40.57",
                "lon": "-81.79",
                "importance": 0.3,
                "class": "place",
                "type": "house",
                "address": {
                    "house_number": "125",
                    "road": "East Main Street",
                    "city": "Berlin Heights",
                    "state": "Ohio",
                    "postcode": "44814",
                },
            },
        ],
    )

    resolved = tbm_app.geocode_address(query)
    assert resolved is not None
    assert float(resolved["lat"]) == 40.57
    assert float(resolved["lon"]) == -81.79


def test_geocode_address_provider_order_prefers_google(monkeypatch):
    query = "4365 State Route 39 Millersburg OH 44654"
    monkeypatch.setattr(tbm_app, "GOOGLE_MAPS_API_KEY", "unit-test-key")
    monkeypatch.setattr(tbm_app, "GEOCODE_PROVIDER_ORDER_RAW", "google,nominatim,census")
    with tbm_app._GEOCODE_CACHE_LOCK:
        tbm_app._GEOCODE_CACHE.clear()

    monkeypatch.setattr(
        tbm_app,
        "_google_geocode_address",
        lambda _q: (
            {
                "query": _q,
                "display_name": "4365 STATE RTE 39, MILLERSBURG, OH, 44654",
                "lat": 40.5489,
                "lon": -81.7785,
            },
            True,
        ),
    )
    monkeypatch.setattr(tbm_app, "_nominatim_geocode_address", lambda _q: (None, False))
    monkeypatch.setattr(tbm_app, "_census_geocode_address", lambda _q: None)

    resolved = tbm_app.geocode_address(query)
    assert resolved is not None
    assert resolved["display_name"].startswith("4365 STATE RTE 39")


def test_geocode_address_provider_order_falls_through_on_google_miss(monkeypatch):
    query = "4365 State Route 39 Millersburg OH 44654"
    monkeypatch.setattr(tbm_app, "GOOGLE_MAPS_API_KEY", "unit-test-key")
    monkeypatch.setattr(tbm_app, "GEOCODE_PROVIDER_ORDER_RAW", "google,nominatim,census")
    with tbm_app._GEOCODE_CACHE_LOCK:
        tbm_app._GEOCODE_CACHE.clear()

    monkeypatch.setattr(tbm_app, "_google_geocode_address", lambda _q: (None, False))
    monkeypatch.setattr(
        tbm_app,
        "_nominatim_geocode_address",
        lambda _q: (
            {
                "query": _q,
                "display_name": "US 62;SR 39, Millersburg, Ohio, 44654",
                "lat": 40.5563,
                "lon": -81.8984,
            },
            True,
        ),
    )
    monkeypatch.setattr(tbm_app, "_census_geocode_address", lambda _q: None)

    resolved = tbm_app.geocode_address(query)
    assert resolved is not None
    assert resolved["display_name"].startswith("US 62;SR 39")


def test_location_query_candidates_expand_us_address_variants():
    candidates = tbm_app._location_query_candidates("4365 State Route 39 Millersburg OH 44654")
    assert "4365 State Route 39, Millersburg, OH 44654" in candidates
    assert "4365 State Route 39, Millersburg, OH 44654, USA" in candidates
    assert any("SR 39" in item for item in candidates)


def test_suggest_addresses_blocks_street_number_without_locality(monkeypatch):
    query = "5737 county road 203"
    monkeypatch.setattr(tbm_app, "_nominatim_search", lambda _q, limit=6: [{"display_name": "should-not-be-used"}])
    monkeypatch.setattr(
        tbm_app,
        "_google_geocode_address",
        lambda _q: (
            {"query": _q, "display_name": "should-not-be-used", "lat": 1.0, "lon": 1.0},
            True,
        ),
    )
    out = tbm_app.suggest_addresses(query, limit=5)
    assert out == []


def test_has_locality_hint_for_street_query():
    assert tbm_app._has_locality_hint_for_street_query("5737 county road 203, Millersburg")
    assert tbm_app._has_locality_hint_for_street_query("5737 county road 203 Millersburg OH")
    assert tbm_app._has_locality_hint_for_street_query("5737 county road 203 44654")
    assert not tbm_app._has_locality_hint_for_street_query("5737 county road 203")


def test_geocode_address_uses_census_fallback_for_low_confidence(monkeypatch):
    query = "4365 State Route 39 Millersburg OH 44654"
    monkeypatch.setattr(tbm_app, "_location_query_candidates", lambda _q: [_q])
    monkeypatch.setattr(
        tbm_app,
        "_nominatim_search",
        lambda _q, limit=6: [
            {
                "display_name": "State Route 39, Holmes County, Ohio, United States",
                "lat": "40.5655",
                "lon": "-81.8801",
                "importance": 0.5,
                "class": "highway",
                "type": "tertiary",
                "address": {"road": "State Route 39", "state": "Ohio"},
            }
        ],
    )
    monkeypatch.setattr(
        tbm_app,
        "_census_geocode_search",
        lambda _q: [
            {
                "matchedAddress": "4365 STATE ROUTE 39, MILLERSBURG, OH, 44654",
                "coordinates": {"x": -81.879, "y": 40.562},
            }
        ],
    )

    resolved = tbm_app.geocode_address(query)
    assert resolved is not None
    assert resolved["display_name"].startswith("4365 STATE ROUTE 39")
    assert float(resolved["lat"]) == 40.562
    assert float(resolved["lon"]) == -81.879


def test_suggest_addresses_uses_census_fallback_when_nominatim_empty(monkeypatch):
    query = "4365 State Route 39 Millersburg OH 44654"
    monkeypatch.setattr(tbm_app, "_location_query_candidates", lambda _q: [_q])
    monkeypatch.setattr(tbm_app, "_nominatim_search", lambda _q, limit=6: [])
    monkeypatch.setattr(
        tbm_app,
        "_census_geocode_search",
        lambda _q: [
            {
                "matchedAddress": "4365 STATE ROUTE 39, MILLERSBURG, OH, 44654",
                "coordinates": {"x": -81.879, "y": 40.562},
            }
        ],
    )

    out = tbm_app.suggest_addresses(query, limit=3)
    assert len(out) == 1
    assert "4365 STATE ROUTE 39" in out[0]["display_name"]
    assert float(out[0]["lat"]) == 40.562


def test_suggest_addresses_prefers_census_when_nominatim_top_is_not_specific(monkeypatch):
    query = "4365 State Route 39 Millersburg OH 44654"
    monkeypatch.setattr(tbm_app, "_location_query_candidates", lambda _q: [_q])
    monkeypatch.setattr(
        tbm_app,
        "_nominatim_search",
        lambda _q, limit=6: [
            {
                "display_name": "State Route 39, Holmes County, Ohio, United States",
                "lat": "40.5655",
                "lon": "-81.8801",
                "importance": 0.7,
                "class": "highway",
                "type": "tertiary",
                "address": {"road": "State Route 39", "state": "Ohio"},
            }
        ],
    )
    monkeypatch.setattr(
        tbm_app,
        "_census_geocode_search",
        lambda _q: [
            {
                "matchedAddress": "4365 STATE ROUTE 39, MILLERSBURG, OH, 44654",
                "coordinates": {"x": -81.879, "y": 40.562},
            }
        ],
    )

    out = tbm_app.suggest_addresses(query, limit=3)
    assert len(out) >= 1
    assert out[0]["display_name"].startswith("4365 STATE ROUTE 39")


def test_suggest_addresses_prepends_google_result(monkeypatch):
    query = "4365 State Route 39 Millersburg OH 44654"
    monkeypatch.setattr(tbm_app, "GOOGLE_MAPS_API_KEY", "unit-test-key")
    monkeypatch.setattr(tbm_app, "GEOCODE_PROVIDER_ORDER_RAW", "google,nominatim,census")
    with tbm_app._GEOCODE_SUGGEST_CACHE_LOCK:
        tbm_app._GEOCODE_SUGGEST_CACHE.clear()

    monkeypatch.setattr(
        tbm_app,
        "_google_places_autocomplete",
        lambda _q, limit=6: [
            {"display_name": "4365 STATE RTE 39, MILLERSBURG, OH, 44654", "place_id": "pid-1"},
        ],
    )
    monkeypatch.setattr(
        tbm_app,
        "_nominatim_search",
        lambda _q, limit=6: [
            {
                "display_name": "US 62;SR 39, Millersburg, Ohio, 44654",
                "lat": "40.5563",
                "lon": "-81.8984",
                "importance": 0.4,
                "class": "highway",
                "type": "tertiary",
                "address": {"road": "State Route 39", "city": "Millersburg", "state": "Ohio", "postcode": "44654"},
            }
        ],
    )
    monkeypatch.setattr(tbm_app, "_census_geocode_search", lambda _q: [])

    out = tbm_app.suggest_addresses(query, limit=3)
    assert len(out) >= 1
    assert out[0]["display_name"].startswith("4365 STATE RTE 39")
    assert out[0]["place_id"] == "pid-1"


def test_api_nearest_airports_rejects_short_address():
    client = tbm_app.app.test_client()
    resp = client.get("/api/nearest-airports?address=ab")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["code"] == "invalid_address"


def test_api_address_suggest(monkeypatch):
    client = tbm_app.app.test_client()
    monkeypatch.setattr(
        tbm_app,
        "suggest_addresses",
        lambda q, limit=None: [
            {"display_name": "2711 Oklahoma Avenue, Trenton, Missouri, 64683, United States", "lat": 40.0891, "lon": -93.6040}
        ],
    )

    resp = client.get("/api/address-suggest?q=2711+Oklahoma")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 1
    assert "display_name" in data[0]


def test_compact_address_suggestion_label_strips_county_and_country():
    raw = "2711 Oklahoma Avenue, Trenton, Grundy County, Missouri, 64683, United States"
    compact = tbm_app._compact_address_suggestion_label(raw)
    assert compact == "2711 Oklahoma Avenue, Trenton, Missouri, 64683"


@pytest.fixture
def chat_local_mode(monkeypatch):
    monkeypatch.setattr(tbm_app, "OpenAI", None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_api_chat_missing_fields_guardrail(chat_local_mode):
    client = tbm_app.app.test_client()
    resp = client.post("/api/chat", json={"message": "Can you estimate this trip for me?"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "I still need" in data["reply"]
    assert "parsed" in data


def test_api_chat_summary_mode(chat_local_mode, no_winds):
    client = tbm_app.app.test_client()
    resp = client.post("/api/chat", json={"message": "Give me a trip summary for KCAK to KSRQ tomorrow"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "Trip summary:" in data["reply"]
    assert "Typical:" in data["reply"]


def test_api_chat_email_mode(chat_local_mode, no_winds):
    client = tbm_app.app.test_client()
    resp = client.post("/api/chat", json={"message": "Draft an email for KCAK to KSRQ tomorrow"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "Subject:" in data["reply"]
    assert "Hi team," in data["reply"]


def test_planner_routes_require_login(planner_auth_env):
    anonymous = tbm_app.app.test_client()
    home = anonymous.get("/")
    assert home.status_code == 302
    assert "/login" in home.headers.get("Location", "")

    estimate = anonymous.post(
        "/estimate",
        data={
            "dep_icao": "KCAK",
            "dest_icao": "KSRQ",
            "trip_type": "oneway",
            "depart_date": (date.today() + timedelta(days=1)).isoformat(),
        },
    )
    assert estimate.status_code == 302
    assert "/login" in estimate.headers.get("Location", "")

    estimate_api = anonymous.post(
        "/api/estimate",
        json={
            "dep": "KCAK",
            "dest": "KSRQ",
            "trip_type": "oneway",
            "depart_date": (date.today() + timedelta(days=1)).isoformat(),
        },
    )
    assert estimate_api.status_code == 401


def test_account_page_requires_login(planner_auth_env):
    client = tbm_app.app.test_client()
    resp = client.get("/account")
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


def test_account_page_renders_profile(planner_auth_env):
    client = tbm_app.app.test_client()
    assert _login(client).status_code == 200
    resp = client.get("/account")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Profile" in body
    assert "owner@example.com" in body


def test_account_password_change_validations(planner_auth_env):
    client = tbm_app.app.test_client()
    assert _login(client).status_code == 200
    with client.session_transaction() as sess:
        csrf_token = sess.get("csrf_token")

    wrong_current = client.post(
        "/account/password",
        data={
            "csrf_token": csrf_token,
            "current_password": "wrong-password",
            "new_password": "newownerpass123",
            "confirm_password": "newownerpass123",
        },
    )
    assert wrong_current.status_code == 400
    assert "Current password is incorrect." in wrong_current.get_data(as_text=True)

    mismatch = client.post(
        "/account/password",
        data={
            "csrf_token": csrf_token,
            "current_password": "ownerpass123",
            "new_password": "newownerpass123",
            "confirm_password": "mismatch123",
        },
    )
    assert mismatch.status_code == 400
    assert "must match" in mismatch.get_data(as_text=True)

    weak = client.post(
        "/account/password",
        data={
            "csrf_token": csrf_token,
            "current_password": "ownerpass123",
            "new_password": "short",
            "confirm_password": "short",
        },
    )
    assert weak.status_code == 400
    assert "at least 8 characters" in weak.get_data(as_text=True)


def test_account_password_change_success_and_login_with_new_password(planner_auth_env):
    client = tbm_app.app.test_client()
    assert _login(client).status_code == 200
    with client.session_transaction() as sess:
        csrf_token = sess.get("csrf_token")

    changed = client.post(
        "/account/password",
        data={
            "csrf_token": csrf_token,
            "current_password": "ownerpass123",
            "new_password": "newownerpass123",
            "confirm_password": "newownerpass123",
        },
    )
    assert changed.status_code == 200
    assert "Password updated successfully." in changed.get_data(as_text=True)

    old_login_client = tbm_app.app.test_client()
    old_login = old_login_client.post("/api/login", json={"email": "owner@example.com", "password": "ownerpass123"})
    assert old_login.status_code == 401

    new_login_client = tbm_app.app.test_client()
    new_login = new_login_client.post("/api/login", json={"email": "owner@example.com", "password": "newownerpass123"})
    assert new_login.status_code == 200


def test_account_password_change_requires_csrf(planner_auth_env):
    client = tbm_app.app.test_client()
    login = client.post("/api/login", json={"email": "owner@example.com", "password": "ownerpass123"})
    assert login.status_code == 200
    resp = client.post(
        "/account/password",
        data={
            "current_password": "ownerpass123",
            "new_password": "newownerpass123",
            "confirm_password": "newownerpass123",
        },
    )
    assert resp.status_code == 403

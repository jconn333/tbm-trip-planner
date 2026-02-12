from datetime import date, timedelta

import pytest

import app as tbm_app
import db as storage
from auth import hash_password


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


@pytest.fixture
def no_winds(monkeypatch):
    monkeypatch.setattr(
        tbm_app,
        "wind_component_fl270_kts",
        lambda dep, dest, depart_dt_local: (0.0, "mock winds"),
    )


@pytest.fixture
def planner_auth_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test_planner_auth.sqlite3"
    monkeypatch.setattr(tbm_app, "TBM_DB_PATH", str(db_path))
    monkeypatch.setattr(tbm_app, "TBM_HOME_TZ", "America/New_York")
    storage.init_db(str(db_path))
    with storage.get_conn(str(db_path)) as conn:
        storage.create_user(
            conn,
            email="owner@example.com",
            name="Owner",
            role="owner",
            password_hash=hash_password("ownerpass123"),
        )
    return str(db_path)


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

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from math import asin, atan2, cos, degrees, radians, sin, sqrt
from threading import Lock

import airportsdata
import certifi
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

# Load .env from the same directory as this file (works no matter where you run from)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# IMPORTANT: templates folder is lowercase now
app = Flask(__name__, template_folder="templates")

APP_NAME = "TBM Trip Planner"


# ------------------ Logging ------------------
def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)



def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("tbm_trip_planner")


# ------------------ Config ------------------
ASSUMPTION_SPECS: dict[str, dict[str, float | str]] = {
    "tas_typical": {"env": "TBM_TAS_TYPICAL", "default": 290.0, "min": 120.0, "max": 450.0},
    "tas_conservative": {"env": "TBM_TAS_CONSERVATIVE", "default": 280.0, "min": 120.0, "max": 450.0},
    "routing_typical": {"env": "TBM_ROUTING_TYPICAL", "default": 1.03, "min": 1.0, "max": 1.5},
    "routing_conservative": {"env": "TBM_ROUTING_CONSERVATIVE", "default": 1.10, "min": 1.0, "max": 1.8},
    "overhead_typical_min": {"env": "TBM_OVERHEAD_TYPICAL_MIN", "default": 15.0, "min": 0.0, "max": 120.0},
    "overhead_conservative_min": {"env": "TBM_OVERHEAD_CONSERVATIVE_MIN", "default": 20.0, "min": 0.0, "max": 120.0},
    "mgmt_fee_per_hr": {"env": "TBM_MGMT_FEE_PER_HR", "default": 100.0, "min": 0.0, "max": 5000.0},
    "maint_reserve_per_hr": {"env": "TBM_MAINT_RESERVE_PER_HR", "default": 250.0, "min": 0.0, "max": 5000.0},
    "engine_reserve_per_hr": {"env": "TBM_ENGINE_RESERVE_PER_HR", "default": 215.0, "min": 0.0, "max": 5000.0},
    "fuel_price_per_gal": {"env": "TBM_FUEL_PRICE_PER_GAL", "default": 5.50, "min": 0.5, "max": 25.0},
    "fuel_burn_gph": {"env": "TBM_FUEL_BURN_GPH", "default": 60.0, "min": 10.0, "max": 250.0},
}

DEFAULT_ASSUMPTIONS = {
    key: _env_float(spec["env"], float(spec["default"])) for key, spec in ASSUMPTION_SPECS.items()
}

# Winds Aloft (FD)
WINDS_REGION = os.getenv("WINDS_REGION", "us")
WINDS_LEVEL = os.getenv("WINDS_LEVEL", "low")
WINDS_LAYOUT = os.getenv("WINDS_LAYOUT", "off")
WINDS_CACHE_TTL_SEC = _env_int("WINDS_CACHE_TTL_SEC", 900)
WINDS_CACHE_MAX_KEYS = _env_int("WINDS_CACHE_MAX_KEYS", 32)

GEOCODE_TIMEOUT_SEC = _env_int("GEOCODE_TIMEOUT_SEC", 8)
GEOCODE_CACHE_TTL_SEC = _env_int("GEOCODE_CACHE_TTL_SEC", 3600)
GEOCODE_CACHE_MAX_KEYS = _env_int("GEOCODE_CACHE_MAX_KEYS", 128)
GEOCODE_USER_AGENT = os.getenv("GEOCODE_USER_AGENT", "tbm-trip-planner/1.0 (nearest-airports)")
GEOCODE_SUGGEST_MIN_CHARS = _env_int("GEOCODE_SUGGEST_MIN_CHARS", 3)
GEOCODE_SUGGEST_LIMIT = _env_int("GEOCODE_SUGGEST_LIMIT", 6)
GEOCODE_COUNTRY_CODES = os.getenv("GEOCODE_COUNTRY_CODES", "us").strip()
AIRPORT_META_TIMEOUT_SEC = _env_int("AIRPORT_META_TIMEOUT_SEC", 8)
AIRPORT_META_CACHE_TTL_SEC = _env_int("AIRPORT_META_CACHE_TTL_SEC", 21600)
AIRPORT_META_CACHE_MAX_KEYS = _env_int("AIRPORT_META_CACHE_MAX_KEYS", 512)

ICAO_RE = re.compile(r"^[A-Z][A-Z0-9]{3}$")
AIRPORT_CODE_RE = re.compile(r"^[A-Z0-9]{2,4}$")

AIRPORTS = airportsdata.load("ICAO")
LID_TO_ICAOS: dict[str, list[str]] = {}
for _icao, _a in AIRPORTS.items():
    lid = (_a.get("lid") or "").strip().upper()
    if not lid:
        continue
    LID_TO_ICAOS.setdefault(lid, []).append(_icao)

_WINDTEMP_CACHE: dict[tuple[str, int, str, str, str], tuple[float, str]] = {}
_WINDTEMP_CACHE_LOCK = Lock()
_GEOCODE_CACHE: dict[str, tuple[float, dict]] = {}
_GEOCODE_CACHE_LOCK = Lock()
_GEOCODE_SUGGEST_CACHE: dict[str, tuple[float, list[dict]]] = {}
_GEOCODE_SUGGEST_CACHE_LOCK = Lock()
_AIRPORT_META_CACHE: dict[str, tuple[float, dict]] = {}
_AIRPORT_META_CACHE_LOCK = Lock()

AIRPORT_LATLON_INDEX: list[tuple[str, float, float]] = []
for _icao, _a in AIRPORTS.items():
    _lat = _a.get("lat")
    _lon = _a.get("lon")
    if _lat is None or _lon is None:
        continue
    try:
        AIRPORT_LATLON_INDEX.append((_icao, float(_lat), float(_lon)))
    except Exception:
        continue


# ------------------ General helpers ------------------
def _json_error(message: str, status: int = 400, code: str = "bad_request", field: str | None = None):
    payload = {"error": message, "code": code}
    if field:
        payload["field"] = field
    return jsonify(payload), status



def _normalize_trip_type(raw: str | None) -> str:
    trip_type = (raw or "oneway").strip().lower()
    return "roundtrip" if trip_type in ("roundtrip", "round_trip", "rt") else "oneway"


def _resolve_airport_code(raw_code: str | None) -> str | None:
    code = (raw_code or "").strip().upper()
    if not code:
        return None

    if code in AIRPORTS:
        return code

    lid_hits = LID_TO_ICAOS.get(code) or []
    if len(lid_hits) == 1:
        return lid_hits[0]

    # Common convenience for US FAA/local IDs, e.g. "10G" -> "K10G".
    if len(code) in (3, 4):
        prefixed = f"K{code}" if len(code) == 3 else code
        if prefixed in AIRPORTS:
            return prefixed

    return None


def _parse_assumption_overrides(raw: dict | None):
    if not raw:
        return {}, None

    overrides: dict[str, float] = {}
    for key, spec in ASSUMPTION_SPECS.items():
        if key not in raw:
            continue
        value = raw.get(key)
        if value is None or str(value).strip() == "":
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None, {
                "message": f"{key} must be numeric.",
                "code": "invalid_assumption",
                "field": key,
                "status": 400,
            }
        min_v = float(spec["min"])
        max_v = float(spec["max"])
        if not (min_v <= parsed <= max_v):
            return None, {
                "message": f"{key} must be between {min_v:g} and {max_v:g}.",
                "code": "invalid_assumption",
                "field": key,
                "status": 400,
            }
        overrides[key] = parsed

    return overrides, None



def _merged_assumptions(overrides: dict | None = None) -> dict[str, float]:
    merged = dict(DEFAULT_ASSUMPTIONS)
    if overrides:
        merged.update(overrides)
    return merged



def _validate_trip_inputs(
    dep_raw: str | None,
    dest_raw: str | None,
    trip_type_raw: str | None,
    depart_date_raw: str | None,
    return_date_raw: str | None,
):
    dep_in = (dep_raw or "").strip().upper()
    dest_in = (dest_raw or "").strip().upper()
    trip_type = _normalize_trip_type(trip_type_raw)

    if not dep_in:
        return None, {"message": "dep is required.", "code": "invalid_dep", "field": "dep", "status": 400}
    if not AIRPORT_CODE_RE.fullmatch(dep_in):
        return None, {
            "message": f"dep must be a valid airport code (got '{dep_in}').",
            "code": "invalid_dep",
            "field": "dep",
            "status": 400,
        }
    dep = _resolve_airport_code(dep_in)
    if not dep:
        return None, {
            "message": f"Unknown departure airport: {dep_in}",
            "code": "unknown_dep",
            "field": "dep",
            "status": 400,
        }

    if not dest_in:
        return None, {"message": "dest is required.", "code": "invalid_dest", "field": "dest", "status": 400}
    if not AIRPORT_CODE_RE.fullmatch(dest_in):
        return None, {
            "message": f"dest must be a valid airport code (got '{dest_in}').",
            "code": "invalid_dest",
            "field": "dest",
            "status": 400,
        }
    dest = _resolve_airport_code(dest_in)
    if not dest:
        return None, {
            "message": f"Unknown destination airport: {dest_in}",
            "code": "unknown_dest",
            "field": "dest",
            "status": 400,
        }

    if dep == dest:
        return None, {
            "message": "Departure and destination must be different airports.",
            "code": "same_airport",
            "field": "dest",
            "status": 400,
        }

    depart_date = parse_date_only(depart_date_raw)
    if not depart_date:
        return None, {
            "message": "depart_date required (YYYY-MM-DD, today, or tomorrow).",
            "code": "invalid_depart_date",
            "field": "depart_date",
            "status": 400,
        }

    return_date = parse_date_only(return_date_raw)
    if trip_type == "roundtrip" and return_date and return_date < depart_date:
        return None, {
            "message": "return_date cannot be before depart_date.",
            "code": "invalid_return_date",
            "field": "return_date",
            "status": 400,
        }

    return {
        "dep": dep,
        "dest": dest,
        "trip_type": trip_type,
        "depart_date": depart_date,
        "return_date": return_date,
    }, None



def _home_context(error: str | None = None, form_values: dict | None = None, assumption_values: dict | None = None):
    merged_assumptions = _merged_assumptions(assumption_values)
    return {
        "app_name": APP_NAME,
        "error": error,
        "form_values": form_values or {},
        "assumption_values": merged_assumptions,
    }


# ------------------ Math helpers ------------------
def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    r_km = 6371.0
    km_to_nm = 0.539957
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))
    return (r_km * c) * km_to_nm



def initial_bearing_deg(lat1, lon1, lat2, lon2) -> float:
    phi1 = radians(lat1)
    phi2 = radians(lat2)
    dlon = radians(lon2 - lon1)
    y = sin(dlon) * cos(phi2)
    x = cos(phi1) * sin(phi2) - sin(phi1) * cos(phi2) * cos(dlon)
    brg = (degrees(atan2(y, x)) + 360) % 360
    return brg



def format_hhmm(total_minutes: int) -> str:
    hh = total_minutes // 60
    mm = total_minutes % 60
    return f"{hh}:{mm:02d}"


# ------------------ Airport resolution ------------------
def airport_to_choice(icao: str, a: dict) -> dict:
    return {
        "icao": icao,
        "iata": (a.get("iata") or "").upper(),
        "name": a.get("name") or "",
        "city": a.get("city") or "",
        "country": a.get("country") or "",
        "lat": a.get("lat"),
        "lon": a.get("lon"),
        "region": a.get("region") or "",
    }



def pretty_airport(choice: dict) -> str:
    icao = choice["icao"]
    iata = choice["iata"]
    name = choice["name"]
    city = choice["city"]
    country = choice["country"]
    code = f"{icao}/{iata}" if iata else icao
    loc = f"{city}, {country}".strip().strip(",")
    return f"{name} ({code}) — {loc}"



def get_airport_by_icao(icao: str) -> dict:
    a = AIRPORTS[icao]
    return airport_to_choice(icao, a)



def find_airports(query: str, limit: int = 12) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []

    q_up = re.sub(r"\s+", " ", q.strip()).upper()

    # ICAO/LID exact
    resolved = _resolve_airport_code(q_up)
    if resolved:
        return [get_airport_by_icao(resolved)]

    # ICAO exact
    if len(q_up) == 4 and q_up in AIRPORTS:
        return [get_airport_by_icao(q_up)]

    # IATA exact
    if len(q_up) == 3:
        hits = []
        for icao, a in AIRPORTS.items():
            if (a.get("iata") or "").upper() == q_up:
                hits.append(airport_to_choice(icao, a))
        if hits:
            hits.sort(key=lambda c: (0 if c["iata"] else 1, c["icao"]))
            return hits[:limit]

    # fallback: US name/city contains
    q_low = q.lower().strip()
    found = []
    for icao, a in AIRPORTS.items():
        if (a.get("country") or "").upper() != "US":
            continue
        name = (a.get("name") or "").lower()
        city = (a.get("city") or "").lower()
        lid = (a.get("lid") or "").lower()
        if q_low and (q_low in name or q_low in city or q_low == lid):
            found.append(airport_to_choice(icao, a))

    found.sort(key=lambda c: (0 if c["iata"] else 1, c["icao"]))
    return found[:limit]


def geocode_address(address: str) -> dict | None:
    q = (address or "").strip()
    if not q:
        return None

    now = time.time()
    cache_key = q.lower()
    with _GEOCODE_CACHE_LOCK:
        cached = _GEOCODE_CACHE.get(cache_key)
        if cached and (now - cached[0]) <= GEOCODE_CACHE_TTL_SEC:
            return cached[1]

    result = None
    for candidate in _location_query_candidates(q):
        body = _nominatim_search(candidate, limit=1)
        if body is None:
            return None
        if not isinstance(body, list) or not body:
            continue
        result = _nominatim_item_to_geocode(body[0], q)
        if result:
            break
    if not result:
        return None

    with _GEOCODE_CACHE_LOCK:
        # Evict expired keys first.
        for key in list(_GEOCODE_CACHE.keys()):
            if (now - _GEOCODE_CACHE[key][0]) > GEOCODE_CACHE_TTL_SEC:
                _GEOCODE_CACHE.pop(key, None)
        _GEOCODE_CACHE[cache_key] = (now, result)
        if len(_GEOCODE_CACHE) > GEOCODE_CACHE_MAX_KEYS:
            oldest_key = min(_GEOCODE_CACHE.keys(), key=lambda k: _GEOCODE_CACHE[k][0])
            _GEOCODE_CACHE.pop(oldest_key, None)

    return result


def _location_query_candidates(query: str) -> list[str]:
    q = " ".join((query or "").strip().split())
    if not q:
        return []

    candidates: list[str] = [q]

    city_state_match = re.match(r"^([A-Za-z .'-]+),?\s+([A-Za-z]{2})$", q)
    if city_state_match:
        city = city_state_match.group(1).strip(" ,")
        st = city_state_match.group(2).upper()
        normalized = f"{city}, {st}"
        if normalized not in candidates:
            candidates.append(normalized)
        us_variant = f"{normalized}, USA"
        if us_variant not in candidates:
            candidates.append(us_variant)
    elif "usa" not in q.lower() and "united states" not in q.lower():
        # Encourage stable US geocoding for broad queries such as "Akron OH".
        us_variant = f"{q}, USA"
        if us_variant not in candidates:
            candidates.append(us_variant)

    return candidates


def _nominatim_search(query: str, limit: int = 1) -> list[dict] | None:
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": str(max(1, min(limit, 10))),
        "addressdetails": "0",
    }
    if GEOCODE_COUNTRY_CODES:
        params["countrycodes"] = GEOCODE_COUNTRY_CODES
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": GEOCODE_USER_AGENT})
    ctx = ssl.create_default_context(cafile=certifi.where())

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=GEOCODE_TIMEOUT_SEC, context=ctx) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.info("nominatim_ok latency_ms=%.1f limit=%s query=%s", elapsed_ms, limit, query[:120])
        return body
    except Exception:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.exception("nominatim_failed latency_ms=%.1f limit=%s query=%s", elapsed_ms, limit, query[:120])
        return None


def _nominatim_item_to_geocode(item: dict, query: str) -> dict | None:
    lat = item.get("lat")
    lon = item.get("lon")
    if lat is None or lon is None:
        return None

    try:
        return {
            "query": query,
            "display_name": item.get("display_name") or query,
            "lat": float(lat),
            "lon": float(lon),
        }
    except Exception:
        return None


def _compact_address_suggestion_label(display_name: str) -> str:
    raw = (display_name or "").strip()
    if not raw:
        return raw

    country_tokens = {
        "united states",
        "united states of america",
        "usa",
        "u.s.a.",
        "us",
        "u.s.",
    }

    parts = [p.strip() for p in raw.split(",") if p and p.strip()]
    filtered: list[str] = []
    for part in parts:
        low = part.lower()
        if "county" in low:
            continue
        if low in country_tokens:
            continue
        filtered.append(part)

    if not filtered:
        return raw

    deduped: list[str] = []
    seen = set()
    for part in filtered:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(part)

    return ", ".join(deduped) if deduped else raw


def suggest_addresses(query: str, limit: int | None = None) -> list[dict]:
    q = (query or "").strip()
    if len(q) < GEOCODE_SUGGEST_MIN_CHARS:
        return []

    limit_n = GEOCODE_SUGGEST_LIMIT if limit is None else max(1, min(int(limit), 10))
    now = time.time()
    cache_key = f"{q.lower()}::{limit_n}"
    with _GEOCODE_SUGGEST_CACHE_LOCK:
        cached = _GEOCODE_SUGGEST_CACHE.get(cache_key)
        if cached and (now - cached[0]) <= GEOCODE_CACHE_TTL_SEC:
            return cached[1]

    body = _nominatim_search(q, limit=limit_n)
    if not isinstance(body, list):
        return []

    out: list[dict] = []
    seen = set()
    for item in body:
        parsed = _nominatim_item_to_geocode(item, q)
        if not parsed:
            continue
        display_name = _compact_address_suggestion_label(parsed["display_name"])
        if display_name in seen:
            continue
        seen.add(display_name)
        out.append(
            {
                "display_name": display_name,
                "lat": parsed["lat"],
                "lon": parsed["lon"],
            }
        )

    with _GEOCODE_SUGGEST_CACHE_LOCK:
        for key in list(_GEOCODE_SUGGEST_CACHE.keys()):
            if (now - _GEOCODE_SUGGEST_CACHE[key][0]) > GEOCODE_CACHE_TTL_SEC:
                _GEOCODE_SUGGEST_CACHE.pop(key, None)
        _GEOCODE_SUGGEST_CACHE[cache_key] = (now, out)
        if len(_GEOCODE_SUGGEST_CACHE) > GEOCODE_CACHE_MAX_KEYS:
            oldest_key = min(_GEOCODE_SUGGEST_CACHE.keys(), key=lambda k: _GEOCODE_SUGGEST_CACHE[k][0])
            _GEOCODE_SUGGEST_CACHE.pop(oldest_key, None)

    return out


def nearest_airports(lat: float, lon: float, limit: int = 8) -> list[dict]:
    if limit <= 0:
        return []

    ranked = []
    for icao, alat, alon in AIRPORT_LATLON_INDEX:
        dist = haversine_nm(lat, lon, alat, alon)
        ranked.append((dist, icao))

    ranked.sort(key=lambda row: row[0])
    out = []
    for dist_nm, icao in ranked[:limit]:
        choice = get_airport_by_icao(icao)
        out.append(
            {
                "icao": icao,
                "label": pretty_airport(choice),
                "distance_nm": round(float(dist_nm), 1),
                "lat": choice["lat"],
                "lon": choice["lon"],
            }
        )
    return out


def _parse_runway_length_ft(dimension: str | None) -> int | None:
    raw = (dimension or "").strip()
    if not raw:
        return None
    match = re.match(r"^(\d{2,6})\s*x\s*\d{2,6}$", raw, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _is_paved_surface(surface: str | None) -> bool:
    code = (surface or "").strip().upper()
    return code in {"A", "C", "B"}


def _airport_ops_summary(raw: dict) -> dict:
    runways = raw.get("runways") if isinstance(raw.get("runways"), list) else []
    runway_lengths = []
    paved_any = False
    for rwy in runways:
        if not isinstance(rwy, dict):
            continue
        runway_len = _parse_runway_length_ft(rwy.get("dimension"))
        if runway_len:
            runway_lengths.append(runway_len)
        if _is_paved_surface(rwy.get("surface")):
            paved_any = True

    services_raw = (raw.get("services") or "").strip().upper()
    jet_fuel = services_raw not in {"", "-", "N", "NONE"}

    tower_raw = (raw.get("tower") or "").strip().upper()
    towered = tower_raw in {"T", "Y", "YES", "TRUE", "1"}

    return {
        "max_runway_ft": max(runway_lengths) if runway_lengths else None,
        "runway_count": len(runways),
        "paved_runway": paved_any if runways else None,
        "jet_fuel": jet_fuel,
        "towered": towered,
    }


def fetch_airport_ops_metadata(icaos: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not icaos:
        return out

    now = time.time()
    normalized = []
    for icao in icaos:
        code = (icao or "").strip().upper()
        if code and code not in normalized:
            normalized.append(code)

    missing: list[str] = []
    with _AIRPORT_META_CACHE_LOCK:
        for code in normalized:
            cached = _AIRPORT_META_CACHE.get(code)
            if cached and (now - cached[0]) <= AIRPORT_META_CACHE_TTL_SEC:
                out[code] = cached[1]
            else:
                missing.append(code)

    if not missing:
        return out

    params = {"ids": ",".join(missing), "format": "json"}
    url = "https://aviationweather.gov/api/data/airport?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "tbm-trip-planner/1.0"})
    ctx = ssl.create_default_context(cafile=certifi.where())
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=AIRPORT_META_TIMEOUT_SEC, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.info("airport_meta_ok latency_ms=%.1f count=%s", elapsed_ms, len(missing))
    except Exception:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.exception("airport_meta_failed latency_ms=%.1f count=%s", elapsed_ms, len(missing))
        return out

    fresh: dict[str, dict] = {}
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            code = (item.get("icaoId") or "").strip().upper()
            if not code:
                continue
            fresh[code] = _airport_ops_summary(item)

    with _AIRPORT_META_CACHE_LOCK:
        for code, summary in fresh.items():
            _AIRPORT_META_CACHE[code] = (now, summary)
            out[code] = summary
        for key in list(_AIRPORT_META_CACHE.keys()):
            if (now - _AIRPORT_META_CACHE[key][0]) > AIRPORT_META_CACHE_TTL_SEC:
                _AIRPORT_META_CACHE.pop(key, None)
        if len(_AIRPORT_META_CACHE) > AIRPORT_META_CACHE_MAX_KEYS:
            sorted_keys = sorted(_AIRPORT_META_CACHE.keys(), key=lambda k: _AIRPORT_META_CACHE[k][0])
            for key in sorted_keys[: len(_AIRPORT_META_CACHE) - AIRPORT_META_CACHE_MAX_KEYS]:
                _AIRPORT_META_CACHE.pop(key, None)

    return out


def _parse_bool_query_arg(name: str, default: bool = False) -> tuple[bool | None, dict | None]:
    raw = (request.args.get(name) or "").strip().lower()
    if raw == "":
        return default, None
    if raw in {"1", "true", "yes", "on"}:
        return True, None
    if raw in {"0", "false", "no", "off"}:
        return False, None
    return None, {
        "message": f"{name} must be true/false.",
        "code": "invalid_query_param",
        "field": name,
        "status": 400,
    }


def _parse_int_query_arg(name: str, default: int, min_v: int, max_v: int) -> tuple[int | None, dict | None]:
    raw = (request.args.get(name) or "").strip()
    if raw == "":
        return default, None
    try:
        value = int(raw)
    except ValueError:
        return None, {
            "message": f"{name} must be an integer.",
            "code": "invalid_query_param",
            "field": name,
            "status": 400,
        }
    if not (min_v <= value <= max_v):
        return None, {
            "message": f"{name} must be between {min_v} and {max_v}.",
            "code": "invalid_query_param",
            "field": name,
            "status": 400,
        }
    return value, None


def _apply_nearest_filters(
    items: list[dict],
    min_runway_ft: int,
    jet_fuel_only: bool,
    paved_only: bool,
    towered_only: bool,
    limit: int,
) -> list[dict]:
    filtered = []
    for merged in items:
        runway_len = merged.get("max_runway_ft")
        if min_runway_ft > 0 and (runway_len is None or runway_len < min_runway_ft):
            continue
        if jet_fuel_only and not merged.get("jet_fuel"):
            continue
        if paved_only and not merged.get("paved_runway"):
            continue
        if towered_only and not merged.get("towered"):
            continue
        filtered.append(merged)
        if len(filtered) >= limit:
            break
    return filtered


# ------------------ Winds aloft (FD) ------------------
def choose_fcst_hours(depart_dt_local: str | None) -> int:
    if not depart_dt_local:
        return 12
    try:
        dt = datetime.fromisoformat(depart_dt_local)
        now = datetime.now()
        hours_ahead = (dt - now).total_seconds() / 3600.0
        if hours_ahead <= 6:
            return 6
        if hours_ahead <= 12:
            return 12
        return 24
    except Exception:
        return 12



def _wind_cache_day_key(depart_dt_local: str | None) -> str:
    if not depart_dt_local:
        return date.today().isoformat()
    try:
        return datetime.fromisoformat(depart_dt_local).date().isoformat()
    except Exception:
        return date.today().isoformat()



def fetch_windtemp_text(fcst_hours: int, depart_dt_local: str | None = None) -> str:
    cache_key = (WINDS_REGION, fcst_hours, WINDS_LEVEL, WINDS_LAYOUT, _wind_cache_day_key(depart_dt_local))
    now = time.time()

    with _WINDTEMP_CACHE_LOCK:
        cached = _WINDTEMP_CACHE.get(cache_key)
        if cached and (now - cached[0]) <= WINDS_CACHE_TTL_SEC:
            return cached[1]

    params = {
        "region": WINDS_REGION,
        "fcst": str(fcst_hours),
        "level": WINDS_LEVEL,
        "layout": WINDS_LAYOUT,
    }
    url = "https://www.aviationweather.gov/api/data/windtemp?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "tbm-trip-planner/1.0"})
    ctx = ssl.create_default_context(cafile=certifi.where())

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.info("winds_fetch_ok fcst=%sh day=%s latency_ms=%.1f", fcst_hours, cache_key[-1], elapsed_ms)
    except Exception:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.exception("winds_fetch_failed fcst=%sh day=%s latency_ms=%.1f", fcst_hours, cache_key[-1], elapsed_ms)
        raise

    with _WINDTEMP_CACHE_LOCK:
        # Clean expired cache entries.
        for key in list(_WINDTEMP_CACHE.keys()):
            if (now - _WINDTEMP_CACHE[key][0]) > WINDS_CACHE_TTL_SEC:
                _WINDTEMP_CACHE.pop(key, None)

        _WINDTEMP_CACHE[cache_key] = (now, text)

        if len(_WINDTEMP_CACHE) > WINDS_CACHE_MAX_KEYS:
            oldest_key = min(_WINDTEMP_CACHE.keys(), key=lambda k: _WINDTEMP_CACHE[k][0])
            _WINDTEMP_CACHE.pop(oldest_key, None)

    return text



def build_windtemp_index(raw_text: str):
    lines = [ln.rstrip() for ln in raw_text.splitlines() if ln.strip()]
    ft_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("FT"):
            ft_idx = i
            break
    if ft_idx is None:
        raise RuntimeError("Could not find FT header in winds aloft data")

    ft_parts = lines[ft_idx].split()
    alts = []
    for p in ft_parts[1:]:
        try:
            alts.append(int(p))
        except ValueError:
            pass

    station_map = {}
    for ln in lines[ft_idx + 1 :]:
        parts = ln.split()
        if not parts:
            continue
        st = parts[0].upper()
        if len(st) != 3:
            continue
        groups = parts[1:]
        if len(groups) < len(alts):
            continue
        station_map[st] = groups[: len(alts)]
    return alts, station_map



def parse_fd_group(group: str):
    g = group.strip()
    if not g or "////" in g:
        return (None, None)

    if g.startswith("99"):  # light/variable
        return (None, 0.0)

    m = re.fullmatch(r"(\d{2})(\d{2})([+-]?\d{2,3})?", g)
    if not m:
        m2 = re.fullmatch(r"(\d{2})(\d{2})", g)
        if not m2:
            return (None, None)
        dd = int(m2.group(1))
        ff = int(m2.group(2))
    else:
        dd = int(m.group(1))
        ff = int(m.group(2))

    if dd >= 51:
        dd -= 50
        ff += 100

    return (dd * 10.0, float(ff))



def iata_to_latlon(iata: str):
    iata = (iata or "").upper()
    for _icao, a in AIRPORTS.items():
        if (a.get("country") or "").upper() != "US":
            continue
        if (a.get("iata") or "").upper() == iata:
            lat = a.get("lat")
            lon = a.get("lon")
            if lat is None or lon is None:
                return None
            return (float(lat), float(lon))
    return None



def wind_component_at_point_fl270(point_lat: float, point_lon: float, course_deg: float, alts: list[int], station_map: dict):
    if 24000 not in alts or 30000 not in alts:
        return (0.0, "N/A", None, None)

    i24 = alts.index(24000)
    i30 = alts.index(30000)

    stations_by_dist = []
    for cand in station_map.keys():
        ll = iata_to_latlon(cand)
        if not ll:
            continue
        d = haversine_nm(point_lat, point_lon, ll[0], ll[1])
        stations_by_dist.append((d, cand))
    stations_by_dist.sort()

    chosen = None
    d24 = s24 = d30 = s30 = None

    for _, cand in stations_by_dist[:20]:
        g24 = station_map[cand][i24]
        g30 = station_map[cand][i30]
        d24, s24 = parse_fd_group(g24)
        d30, s30 = parse_fd_group(g30)
        if d24 is not None and s24 is not None and d30 is not None and s30 is not None:
            chosen = cand
            break

    if not chosen:
        return (0.0, "N/A", None, None)

    def to_uv(dir_from_deg: float, speed: float):
        th = radians(dir_from_deg)
        u = -speed * sin(th)  # east (toward)
        v = -speed * cos(th)  # north (toward)
        return u, v

    u24, v24 = to_uv(d24, s24)
    u30, v30 = to_uv(d30, s30)

    u27 = (u24 + u30) / 2.0
    v27 = (v24 + v30) / 2.0

    wt_e = u27
    wt_n = v27

    br = radians(course_deg)
    crs_e = sin(br)
    crs_n = cos(br)

    component = wt_e * crs_e + wt_n * crs_n  # + tailwind, - headwind

    speed = sqrt(wt_e * wt_e + wt_n * wt_n)
    toward_deg = (degrees(atan2(wt_e, wt_n)) + 360) % 360
    from_deg = (toward_deg + 180) % 360

    return (component, chosen, from_deg, speed)



def wind_component_fl270_kts(dep: dict, dest: dict, depart_dt_local: str | None):
    lat1, lon1 = float(dep["lat"]), float(dep["lon"])
    lat2, lon2 = float(dest["lat"]), float(dest["lon"])

    fcst = choose_fcst_hours(depart_dt_local)
    try:
        raw = fetch_windtemp_text(fcst, depart_dt_local=depart_dt_local)
        alts, station_map = build_windtemp_index(raw)
    except Exception:
        return (0.0, f"Winds aloft lookup failed (fcst {fcst}h); using 0 kt.")

    if 24000 not in alts or 30000 not in alts:
        return (0.0, f"FD table missing 24000/30000 columns (fcst {fcst}h); using 0 kt.")

    course = initial_bearing_deg(lat1, lon1, lat2, lon2)

    dep_lat, dep_lon = lat1, lon1
    mid_lat, mid_lon = (lat1 + lat2) / 2.0, (lon1 + lon2) / 2.0
    dest_lat, dest_lon = lat2, lon2

    c1, s1, f1, sp1 = wind_component_at_point_fl270(dep_lat, dep_lon, course, alts, station_map)
    c2, s2, f2, sp2 = wind_component_at_point_fl270(mid_lat, mid_lon, course, alts, station_map)
    c3, s3, f3, sp3 = wind_component_at_point_fl270(dest_lat, dest_lon, course, alts, station_map)

    comps = [c for c in [c1, c2, c3] if c is not None]
    component = sum(comps) / len(comps) if comps else 0.0

    def fmt_station(st, fdeg, spd):
        if st == "N/A" or fdeg is None or spd is None:
            return "N/A"
        return f"{st} {fdeg:.0f}°/{spd:.0f}kt"

    details = (
        f"FD fcst {fcst}h. "
        f"Dep:{fmt_station(s1, f1, sp1)}  "
        f"Mid:{fmt_station(s2, f2, sp2)}  "
        f"Dest:{fmt_station(s3, f3, sp3)}"
    )

    return (component, details)


# ------------------ Core estimating ------------------
def money(x: float) -> str:
    return f"${x:,.0f}"



def costs_for_minutes(total_min: int, assumptions: dict[str, float]):
    hours = total_min / 60.0
    mgmt = assumptions["mgmt_fee_per_hr"] * hours
    maint = assumptions["maint_reserve_per_hr"] * hours
    eng = assumptions["engine_reserve_per_hr"] * hours
    fuel_gal = assumptions["fuel_burn_gph"] * hours
    fuel = fuel_gal * assumptions["fuel_price_per_gal"]
    total = mgmt + maint + eng + fuel
    return {
        "hours": hours,
        "mgmt": mgmt,
        "maint": maint,
        "engine": eng,
        "fuel_gal": fuel_gal,
        "fuel": fuel,
        "total": total,
    }



def estimate_leg(dep_icao: str, dest_icao: str, depart_dt_local: str | None, assumptions: dict[str, float]):
    dep = get_airport_by_icao(dep_icao)
    dest = get_airport_by_icao(dest_icao)

    dep_lat, dep_lon = float(dep["lat"]), float(dep["lon"])
    dest_lat, dest_lon = float(dest["lat"]), float(dest["lon"])

    dist_nm = haversine_nm(dep_lat, dep_lon, dest_lat, dest_lon)
    course = initial_bearing_deg(dep_lat, dep_lon, dest_lat, dest_lon)

    wind_component, wind_details = wind_component_fl270_kts(dep, dest, depart_dt_local)

    # Typical
    dist_typ = dist_nm * assumptions["routing_typical"]
    gs_typ = max(60, assumptions["tas_typical"] + wind_component)
    cruise_min_typ = int(round((dist_typ / gs_typ) * 60))
    total_min_typ = cruise_min_typ + int(round(assumptions["overhead_typical_min"]))

    # Conservative
    dist_con = dist_nm * assumptions["routing_conservative"]
    gs_con = max(60, assumptions["tas_conservative"] + wind_component)
    cruise_min_con = int(round((dist_con / gs_con) * 60))
    total_min_con = cruise_min_con + int(round(assumptions["overhead_conservative_min"]))

    return {
        "from": dep_icao,
        "to": dest_icao,
        "from_pretty": pretty_airport(dep),
        "to_pretty": pretty_airport(dest),
        "from_lat": dep_lat,
        "from_lon": dep_lon,
        "to_lat": dest_lat,
        "to_lon": dest_lon,
        "distance_nm": int(round(dist_nm)),
        "course_deg": int(round(course)),
        "winds": {
            "component_kt": float(wind_component),
            "details": wind_details,
        },
        "typical": {
            "tas_kt": int(round(assumptions["tas_typical"])),
            "gs_kt": int(round(gs_typ)),
            "minutes": int(total_min_typ),
            "block_time": format_hhmm(int(total_min_typ)),
            "costs": costs_for_minutes(int(total_min_typ), assumptions),
        },
        "conservative": {
            "tas_kt": int(round(assumptions["tas_conservative"])),
            "gs_kt": int(round(gs_con)),
            "minutes": int(total_min_con),
            "block_time": format_hhmm(int(total_min_con)),
            "costs": costs_for_minutes(int(total_min_con), assumptions),
        },
    }



def parse_date_only(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return date.fromisoformat(s)
        except Exception:
            return None

    low = s.lower()
    today = date.today()
    if "tomorrow" in low:
        return today + timedelta(days=1)
    if "today" in low:
        return today
    return None



def make_depart_dt_for_winds(d: date | None) -> str | None:
    if not d:
        return None
    return f"{d.isoformat()}T12:00"



def estimate_trip(
    dep: str,
    dest: str,
    trip_type: str,
    depart_date: date,
    return_date: date | None,
    assumptions: dict[str, float] | None = None,
):
    final_assumptions = _merged_assumptions(assumptions)

    depart_dt = make_depart_dt_for_winds(depart_date)
    legs = [estimate_leg(dep, dest, depart_dt, final_assumptions)]

    return_dt = None
    if trip_type == "roundtrip":
        if not return_date:
            return_date = depart_date + timedelta(days=1)
        return_dt = make_depart_dt_for_winds(return_date)
        legs.append(estimate_leg(dest, dep, return_dt, final_assumptions))

    def totals_for(which: str):
        minutes = sum(int(leg[which]["minutes"]) for leg in legs)
        costs = {
            "mgmt": sum(float(leg[which]["costs"]["mgmt"]) for leg in legs),
            "maint": sum(float(leg[which]["costs"]["maint"]) for leg in legs),
            "engine": sum(float(leg[which]["costs"]["engine"]) for leg in legs),
            "fuel_gal": sum(float(leg[which]["costs"]["fuel_gal"]) for leg in legs),
            "fuel": sum(float(leg[which]["costs"]["fuel"]) for leg in legs),
            "total": sum(float(leg[which]["costs"]["total"]) for leg in legs),
        }
        return {"minutes": minutes, "block_time": format_hhmm(minutes), "costs": costs}

    return {
        "app": APP_NAME,
        "assumptions": final_assumptions,
        "inputs": {
            "dep": dep,
            "dest": dest,
            "trip_type": trip_type,
            "depart_date": depart_date.isoformat(),
            "return_date": return_date.isoformat() if return_date else None,
            "depart_dt": depart_dt,
            "return_dt": return_dt,
        },
        "legs": legs,
        "totals": {
            "typical": totals_for("typical"),
            "conservative": totals_for("conservative"),
        },
        "winds_outbound": legs[0]["winds"],
    }


# ------------------ API: airports search ------------------
@app.get("/api/airports")
def api_airports():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    choices = find_airports(q, limit=12)
    payload = [{"icao": c["icao"], "label": pretty_airport(c)} for c in choices]
    return jsonify(payload)


@app.get("/api/address-suggest")
def api_address_suggest():
    q = (request.args.get("q") or "").strip()
    if len(q) < GEOCODE_SUGGEST_MIN_CHARS:
        return jsonify([])
    suggestions = suggest_addresses(q, limit=GEOCODE_SUGGEST_LIMIT)
    return jsonify(suggestions)


@app.get("/api/nearest-airports")
def api_nearest_airports():
    limit, err = _parse_int_query_arg("limit", 8, 1, 25)
    if err:
        return _json_error(err["message"], err["status"], err["code"], err.get("field"))

    min_runway_ft, err = _parse_int_query_arg("min_runway_ft", 4000, 0, 30000)
    if err:
        return _json_error(err["message"], err["status"], err["code"], err.get("field"))

    jet_fuel_only, err = _parse_bool_query_arg("jet_fuel_only", False)
    if err:
        return _json_error(err["message"], err["status"], err["code"], err.get("field"))

    paved_only, err = _parse_bool_query_arg("paved_only", False)
    if err:
        return _json_error(err["message"], err["status"], err["code"], err.get("field"))

    towered_only, err = _parse_bool_query_arg("towered_only", False)
    if err:
        return _json_error(err["message"], err["status"], err["code"], err.get("field"))

    address = (request.args.get("address") or "").strip()
    if len(address) < 3:
        return _json_error("address must be at least 3 characters.", 400, "invalid_address", "address")

    geocode = geocode_address(address)
    if not geocode:
        return _json_error("Could not geocode that address. Try adding city/state or ZIP.", 422, "geocode_failed", "address")

    # Start narrow for speed, then widen if filters are restrictive and no results appear.
    candidate_limits = [max(limit * 4, 24)]
    if any([jet_fuel_only, paved_only, towered_only, min_runway_ft > 4000]):
        candidate_limits.extend([80, 180, 500, len(AIRPORT_LATLON_INDEX)])

    filtered = []
    used_pool = candidate_limits[0]
    for candidate_limit in candidate_limits:
        pool = min(candidate_limit, len(AIRPORT_LATLON_INDEX))
        used_pool = pool
        nearest = nearest_airports(float(geocode["lat"]), float(geocode["lon"]), limit=pool)
        metadata = fetch_airport_ops_metadata([item["icao"] for item in nearest])

        enriched = []
        for item in nearest:
            ops = metadata.get(item["icao"], {})
            merged = dict(item)
            merged.update(
                {
                    "max_runway_ft": ops.get("max_runway_ft"),
                    "runway_count": ops.get("runway_count"),
                    "paved_runway": ops.get("paved_runway"),
                    "jet_fuel": ops.get("jet_fuel"),
                    "towered": ops.get("towered"),
                }
            )
            enriched.append(merged)

        filtered = _apply_nearest_filters(
            items=enriched,
            min_runway_ft=min_runway_ft,
            jet_fuel_only=jet_fuel_only,
            paved_only=paved_only,
            towered_only=towered_only,
            limit=limit,
        )
        if filtered:
            break

    return jsonify(
        {
            "query": address,
            "resolved_address": geocode["display_name"],
            "lat": geocode["lat"],
            "lon": geocode["lon"],
            "filters": {
                "limit": limit,
                "min_runway_ft": min_runway_ft,
                "jet_fuel_only": jet_fuel_only,
                "paved_only": paved_only,
                "towered_only": towered_only,
                "search_pool": used_pool,
            },
            "airports": filtered,
        }
    )


# ------------------ API: estimate (machine-readable) ------------------
@app.post("/api/estimate")
def api_estimate():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _json_error("Request body must be a JSON object.", 400, "invalid_json")

    trip_input, err = _validate_trip_inputs(
        dep_raw=data.get("dep"),
        dest_raw=data.get("dest"),
        trip_type_raw=data.get("trip_type"),
        depart_date_raw=data.get("depart_date"),
        return_date_raw=data.get("return_date"),
    )
    if err:
        return _json_error(err["message"], err["status"], err["code"], err.get("field"))

    raw_assumptions = data.get("assumptions") if isinstance(data.get("assumptions"), dict) else data
    assumption_overrides, assumption_err = _parse_assumption_overrides(raw_assumptions)
    if assumption_err:
        return _json_error(
            assumption_err["message"],
            assumption_err["status"],
            assumption_err["code"],
            assumption_err.get("field"),
        )

    est = estimate_trip(
        dep=trip_input["dep"],
        dest=trip_input["dest"],
        trip_type=trip_input["trip_type"],
        depart_date=trip_input["depart_date"],
        return_date=trip_input["return_date"],
        assumptions=assumption_overrides,
    )
    return jsonify(est)


# ------------------ UI: pages ------------------
@app.get("/")
def home():
    return render_template("home.html", **_home_context())


@app.post("/estimate")
def estimate_page():
    form_values = {
        "dep": (request.form.get("dep_icao") or "").strip().upper(),
        "dest": (request.form.get("dest_icao") or "").strip().upper(),
        "trip_type": _normalize_trip_type(request.form.get("trip_type")),
        "depart_date": (request.form.get("depart_date") or "").strip(),
        "return_date": (request.form.get("return_date") or "").strip(),
    }

    trip_input, err = _validate_trip_inputs(
        dep_raw=form_values["dep"],
        dest_raw=form_values["dest"],
        trip_type_raw=form_values["trip_type"],
        depart_date_raw=form_values["depart_date"],
        return_date_raw=form_values["return_date"],
    )

    raw_assumptions = {k: request.form.get(k) for k in ASSUMPTION_SPECS.keys()}
    assumption_overrides, assumption_err = _parse_assumption_overrides(raw_assumptions)

    if err:
        return render_template("home.html", **_home_context(error=err["message"], form_values=form_values, assumption_values=assumption_overrides)), 400

    if assumption_err:
        return (
            render_template(
                "home.html",
                **_home_context(
                    error=assumption_err["message"],
                    form_values=form_values,
                    assumption_values=assumption_overrides,
                ),
            ),
            400,
        )

    est = estimate_trip(
        dep=trip_input["dep"],
        dest=trip_input["dest"],
        trip_type=trip_input["trip_type"],
        depart_date=trip_input["depart_date"],
        return_date=trip_input["return_date"],
        assumptions=assumption_overrides,
    )
    legs = est["legs"]

    # Totals for the UI (nice formatted)
    total_time_typ = est["totals"]["typical"]["block_time"]
    total_cost_typ = money(float(est["totals"]["typical"]["costs"]["total"]))
    total_time_con = est["totals"]["conservative"]["block_time"]
    total_cost_con = money(float(est["totals"]["conservative"]["costs"]["total"]))

    # Plain-English “top summary” seed text for estimate.html
    wind = legs[0]["winds"]["component_kt"]
    wind_phrase = "a tailwind" if wind > 5 else ("a headwind" if wind < -5 else "light winds")
    wind_abs = abs(int(round(wind)))
    if wind_abs <= 5:
        wind_line = "Winds look light for the outbound leg."
    else:
        wind_line = f"Winds look like {wind_abs} kt of {wind_phrase} on the outbound leg."

    if trip_input["trip_type"] == "roundtrip":
        trip_line = f"Round trip from {trip_input['dep']} to {trip_input['dest']}."
    else:
        trip_line = f"One-way from {trip_input['dep']} to {trip_input['dest']}."

    summary_seed = f"{trip_line} Typical comes out around {total_time_typ} and {total_cost_typ}. Conservative is closer to {total_time_con} and {total_cost_con}. {wind_line}"

    assumptions = est["assumptions"]

    return render_template(
        "estimate.html",
        app_name=APP_NAME,
        trip_type=trip_input["trip_type"],
        depart_date=trip_input["depart_date"].isoformat(),
        return_date=(trip_input["return_date"].isoformat() if trip_input["return_date"] else None),
        dep_icao=trip_input["dep"],
        dest_icao=trip_input["dest"],
        dep_pretty=legs[0]["from_pretty"],
        dest_pretty=legs[0]["to_pretty"],
        legs=legs,
        estimation_json=json.dumps(est),
        total_time_typ=total_time_typ,
        total_cost_typ=total_cost_typ,
        total_time_con=total_time_con,
        total_cost_con=total_cost_con,
        fuel_burn_gph=assumptions["fuel_burn_gph"],
        fuel_price=f"{assumptions['fuel_price_per_gal']:.2f}",
        routing_typ_pct=int(round((assumptions["routing_typical"] - 1) * 100)),
        routing_con_pct=int(round((assumptions["routing_conservative"] - 1) * 100)),
        overhead_typ_min=int(round(assumptions["overhead_typical_min"])),
        overhead_con_min=int(round(assumptions["overhead_conservative_min"])),
        summary_seed=summary_seed,
    )


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "app": APP_NAME,
            "timestamp_utc": datetime.utcnow().isoformat() + "Z",
            "winds_cache_keys": len(_WINDTEMP_CACHE),
        }
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5050"))
    app.run(debug=True, port=port, use_reloader=True)

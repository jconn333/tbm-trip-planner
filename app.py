from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import ssl
import secrets
import time
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from math import asin, atan2, cos, degrees, radians, sin, sqrt
from threading import Lock
from zoneinfo import ZoneInfo

import airportsdata
import certifi
import db as storage
from auth import admin_required, hash_password, login_required, verify_password
from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from routes_auth import create_auth_blueprint
from routes_reservations import create_reservations_blueprint

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# Load .env from the same directory as this file (works no matter where you run from)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# IMPORTANT: templates folder is lowercase now
app = Flask(__name__, template_folder="templates")

APP_NAME = "TBM Trip Planner"
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-change-me")
APP_ENV = (os.getenv("APP_ENV") or os.getenv("FLASK_ENV") or "development").strip().lower()
IS_PRODUCTION = APP_ENV in ("production", "prod")
IS_DEVELOPMENT = APP_ENV in ("development", "dev", "")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)


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
LIVE_TRACKING_TAIL = (os.getenv("LIVE_TRACKING_TAIL") or "N656W").strip().upper()
TBM_DB_PATH = os.getenv("TBM_DB_PATH", os.path.join(BASE_DIR, "tbm.sqlite3"))
TBM_HOME_TZ = os.getenv("TBM_HOME_TZ", "America/New_York")
TBM_DEFAULT_PARKED_ICAO = (os.getenv("TBM_DEFAULT_PARKED_ICAO") or "").strip().upper()
TBM_BOOTSTRAP_ADMIN_EMAIL = (os.getenv("TBM_BOOTSTRAP_ADMIN_EMAIL") or "").strip()
TBM_BOOTSTRAP_ADMIN_NAME = (os.getenv("TBM_BOOTSTRAP_ADMIN_NAME") or "").strip()
TBM_BOOTSTRAP_ADMIN_PASSWORD = (os.getenv("TBM_BOOTSTRAP_ADMIN_PASSWORD") or "").strip()
RESERVATION_MIN_MINUTES = _env_int("TBM_RESERVATION_MIN_MINUTES", 15)
RESERVATION_MAX_DAYS = _env_int("TBM_RESERVATION_MAX_DAYS", 45)
PLANNER_DRAFT_TTL_SEC = _env_int("TBM_PLANNER_DRAFT_TTL_SEC", 7200)

ICAO_RE = re.compile(r"^[A-Z][A-Z0-9]{3}$")
AIRPORT_CODE_RE = re.compile(r"^[A-Z0-9]{2,4}$")
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

DEFAULT_PENDING_RESERVATION_COLOR = "#9AA2B4"
OWNER_COLOR_PALETTE = (
    "#60A5FA",
    "#34D399",
    "#FBBF24",
    "#F472B6",
    "#A78BFA",
    "#22D3EE",
    "#FB923C",
    "#86EFAC",
)

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
_SETTINGS_CACHE: dict[str, str] = {}
_SETTINGS_CACHE_AT = 0.0
_SETTINGS_CACHE_LOCK = Lock()
SETTINGS_CACHE_TTL_SEC = 30
CSRF_SAFE_ENDPOINTS = {
    "api_login",
}
RATE_LIMIT_DEFAULT_WINDOW_SEC = _env_int("TBM_RATE_LIMIT_WINDOW_SEC", 60)
ENABLE_RATE_LIMIT = (os.getenv("TBM_ENABLE_RATE_LIMIT") or ("true" if IS_PRODUCTION else "false")).strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
RATE_LIMIT_RULES = {
    "api_login": (_env_int("TBM_RATE_LIMIT_LOGIN_PER_WINDOW", 20), RATE_LIMIT_DEFAULT_WINDOW_SEC),
    "login_post": (_env_int("TBM_RATE_LIMIT_LOGIN_PER_WINDOW", 20), RATE_LIMIT_DEFAULT_WINDOW_SEC),
    "api_owner_reset_password": (_env_int("TBM_RATE_LIMIT_ADMIN_WRITES_PER_WINDOW", 60), RATE_LIMIT_DEFAULT_WINDOW_SEC),
    "api_create_owner": (_env_int("TBM_RATE_LIMIT_ADMIN_WRITES_PER_WINDOW", 60), RATE_LIMIT_DEFAULT_WINDOW_SEC),
    "api_admin_settings_patch": (_env_int("TBM_RATE_LIMIT_ADMIN_WRITES_PER_WINDOW", 60), RATE_LIMIT_DEFAULT_WINDOW_SEC),
    "api_approve_reservation": (_env_int("TBM_RATE_LIMIT_ADMIN_WRITES_PER_WINDOW", 60), RATE_LIMIT_DEFAULT_WINDOW_SEC),
    "api_deny_reservation": (_env_int("TBM_RATE_LIMIT_ADMIN_WRITES_PER_WINDOW", 60), RATE_LIMIT_DEFAULT_WINDOW_SEC),
    "api_cancel_reservation": (_env_int("TBM_RATE_LIMIT_ADMIN_WRITES_PER_WINDOW", 60), RATE_LIMIT_DEFAULT_WINDOW_SEC),
    "api_reopen_reservation": (_env_int("TBM_RATE_LIMIT_ADMIN_WRITES_PER_WINDOW", 60), RATE_LIMIT_DEFAULT_WINDOW_SEC),
}
_RATE_LIMIT_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LIMIT_LOCK = Lock()

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


# ------------------ Reservation/Auth helpers ------------------
def _default_runtime_settings() -> dict[str, str]:
    return {
        "home_timezone": TBM_HOME_TZ,
        "reservation_min_minutes": str(RESERVATION_MIN_MINUTES),
        "reservation_max_days": str(RESERVATION_MAX_DAYS),
        "admin_flights_default_scope": "future_only",
        "user_show_closed_default": "false",
        "pending_reservation_color": DEFAULT_PENDING_RESERVATION_COLOR,
    }


def _load_runtime_settings(force: bool = False) -> dict[str, str]:
    global _SETTINGS_CACHE_AT, _SETTINGS_CACHE
    now = time.time()
    with _SETTINGS_CACHE_LOCK:
        if not force and _SETTINGS_CACHE and (now - _SETTINGS_CACHE_AT) <= SETTINGS_CACHE_TTL_SEC:
            return dict(_SETTINGS_CACHE)

    defaults = _default_runtime_settings()
    db_values: dict[str, str] = {}
    try:
        with storage.get_conn(TBM_DB_PATH) as conn:
            db_values = storage.list_settings(conn)
    except Exception:
        logger.exception("settings_load_failed")
    merged = dict(defaults)
    merged.update(db_values)

    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE = dict(merged)
        _SETTINGS_CACHE_AT = now
    return merged


def _invalidate_settings_cache() -> None:
    global _SETTINGS_CACHE_AT, _SETTINGS_CACHE
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE = {}
        _SETTINGS_CACHE_AT = 0.0


def _effective_home_timezone_name() -> str:
    return _load_runtime_settings().get("home_timezone", TBM_HOME_TZ)


def _effective_reservation_min_minutes() -> int:
    raw = _load_runtime_settings().get("reservation_min_minutes", str(RESERVATION_MIN_MINUTES))
    try:
        return max(1, int(raw))
    except Exception:
        return RESERVATION_MIN_MINUTES


def _effective_reservation_max_days() -> int:
    raw = _load_runtime_settings().get("reservation_max_days", str(RESERVATION_MAX_DAYS))
    try:
        return max(1, int(raw))
    except Exception:
        return RESERVATION_MAX_DAYS


def _effective_admin_flights_default_scope() -> str:
    raw = (_load_runtime_settings().get("admin_flights_default_scope") or "").strip().lower()
    return "all" if raw == "all" else "future_only"


def _effective_user_show_closed_default() -> bool:
    raw = (_load_runtime_settings().get("user_show_closed_default") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _normalize_hex_color(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if not HEX_COLOR_RE.fullmatch(value):
        return None
    return value.upper()


def _default_owner_color(owner_user_id: int) -> str:
    return OWNER_COLOR_PALETTE[max(0, int(owner_user_id) - 1) % len(OWNER_COLOR_PALETTE)]


def _effective_pending_reservation_color() -> str:
    configured = _normalize_hex_color(_load_runtime_settings().get("pending_reservation_color"))
    return configured or DEFAULT_PENDING_RESERVATION_COLOR


def _effective_owner_color(owner_user_id: int) -> str:
    configured = _normalize_hex_color(_load_runtime_settings().get(f"owner_color_{int(owner_user_id)}"))
    return configured or _default_owner_color(owner_user_id)


def _home_zone() -> ZoneInfo:
    try:
        return ZoneInfo(_effective_home_timezone_name())
    except Exception:
        return ZoneInfo("UTC")


def _utc_now() -> datetime:
    return datetime.now(ZoneInfo("UTC"))


def _to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("UTC")).isoformat()


def _utc_iso_to_local_display(iso_value: str) -> str:
    dt = datetime.fromisoformat(iso_value)
    return dt.astimezone(_home_zone()).strftime("%m-%d-%Y %H:%M")


def _format_date_display(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        dt = value.astimezone(_home_zone()) if value.tzinfo else value.replace(tzinfo=_home_zone())
        return dt.strftime("%m-%d-%Y")
    if isinstance(value, date):
        return value.strftime("%m-%d-%Y")
    text = str(value).strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo:
            parsed = parsed.astimezone(_home_zone())
        return parsed.strftime("%m-%d-%Y")
    except Exception:
        pass
    try:
        parsed_date = date.fromisoformat(text)
        return parsed_date.strftime("%m-%d-%Y")
    except Exception:
        return text


def _utc_iso_to_local_iso(iso_value: str) -> str:
    dt = datetime.fromisoformat(iso_value)
    return dt.astimezone(_home_zone()).isoformat()


def _parse_local_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_home_zone())
    else:
        dt = dt.astimezone(_home_zone())
    return dt


def _extract_local_datetime_from_payload(
    payload: dict,
    *,
    prefix: str,
):
    local_key = f"{prefix}_local"
    date_key = f"{prefix}_date"
    time_key = f"{prefix}_time"

    local_raw = payload.get(local_key)
    date_raw = payload.get(date_key)
    time_raw = payload.get(time_key)

    if local_raw is not None and str(local_raw).strip():
        return str(local_raw).strip(), None

    has_date = date_raw is not None and str(date_raw).strip() != ""
    has_time = time_raw is not None and str(time_raw).strip() != ""

    if has_date or has_time:
        if not has_date or not has_time:
            return None, {
                "message": f"{date_key} and {time_key} are both required when either is provided.",
                "code": "invalid_datetime",
                "field": date_key if not has_date else time_key,
                "status": 400,
            }
        return f"{str(date_raw).strip()}T{str(time_raw).strip()}", None

    return None, None


def _db_conn():
    return storage.get_conn(TBM_DB_PATH)


def _request_id() -> str:
    rid = getattr(g, "request_id", None)
    if rid:
        return rid
    return "-"


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _get_or_create_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _verify_csrf_token() -> bool:
    expected = session.get("csrf_token")
    if not expected:
        return False
    provided = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
        or request.headers.get("X-XSRF-Token")
    )
    return bool(provided) and secrets.compare_digest(str(expected), str(provided))


def _enforce_rate_limit(endpoint: str) -> tuple[bool, int | None]:
    if not ENABLE_RATE_LIMIT:
        return True, None
    rule = RATE_LIMIT_RULES.get(endpoint.split(".")[-1])
    if not rule:
        return True, None
    limit, window_sec = rule
    if limit <= 0 or window_sec <= 0:
        return True, None
    now = time.time()
    bucket_key = f"{endpoint}:{_client_ip()}"
    with _RATE_LIMIT_LOCK:
        bucket = _RATE_LIMIT_BUCKETS[bucket_key]
        while bucket and (now - bucket[0]) > window_sec:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, int(window_sec - (now - bucket[0])))
            return False, retry_after
        bucket.append(now)
    return True, None


def _serialize_user(row) -> dict:
    return {
        "id": int(row["id"]),
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
    }


def _account_view_context(*, row, form_error: str | None = None, form_success: str | None = None) -> dict:
    account_user = _serialize_user(row)
    created_at_utc = row["created_at_utc"] if "created_at_utc" in row.keys() else None
    created_at_display = _utc_iso_to_local_display(created_at_utc) if created_at_utc else None
    return {
        "app_name": APP_NAME,
        "home_timezone": _effective_home_timezone_name(),
        "account_user": account_user,
        "account_created_at_display": created_at_display,
        "form_error": form_error,
        "form_success": form_success,
    }


def _can_edit_reservation(user, reservation) -> bool:
    if not user or not reservation:
        return False
    if user["role"] == "admin":
        return reservation["status"] not in ("denied", "canceled")
    return reservation["status"] == "pending" and int(reservation["requested_by_user_id"]) == int(user["id"])


def _can_request_change(user, reservation) -> bool:
    if not user or not reservation:
        return False
    if reservation["status"] != "approved":
        return False
    if datetime.fromisoformat(reservation["end_utc"]) < _utc_now():
        return False
    return user["role"] == "admin" or int(reservation["requested_by_user_id"]) == int(user["id"])


def _can_reopen_reservation(user, reservation) -> bool:
    if not user or not reservation:
        return False
    return user["role"] == "admin" and reservation["status"] in ("denied", "canceled")


def _can_cancel_reservation(user, reservation) -> bool:
    if not user or not reservation:
        return False
    if reservation["status"] in ("denied", "canceled"):
        return False
    if user["role"] == "admin":
        return True
    return reservation["status"] == "pending" and int(reservation["requested_by_user_id"]) == int(user["id"])


def _reservation_to_event_payload(row, user) -> dict:
    title = f"{row['traveling_owner_name']} — {row['dep_icao']} → {row['dest_icao']} (Parked: {row['parked_icao']})"
    status = row["status"]
    color = _effective_pending_reservation_color() if status == "pending" else _effective_owner_color(int(row["traveling_user_id"]))
    return {
        "id": int(row["id"]),
        "title": title,
        "start": _utc_iso_to_local_iso(row["start_utc"]),
        "end": _utc_iso_to_local_iso(row["end_utc"]),
        "status": status,
        "display_color": color,
        "dep_icao": row["dep_icao"],
        "dest_icao": row["dest_icao"],
        "parked_icao": row["parked_icao"],
        "traveling_owner": row["traveling_owner_name"],
        "notes": row["notes"] or "",
        "editable": _can_edit_reservation(user, row),
        "can_cancel": _can_cancel_reservation(user, row),
    }


def _reservation_payload(row) -> dict:
    return {
        "id": int(row["id"]),
        "status": row["status"],
        "start": _utc_iso_to_local_iso(row["start_utc"]),
        "end": _utc_iso_to_local_iso(row["end_utc"]),
        "start_display": _utc_iso_to_local_display(row["start_utc"]),
        "end_display": _utc_iso_to_local_display(row["end_utc"]),
        "dep_icao": row["dep_icao"],
        "dest_icao": row["dest_icao"],
        "parked_icao": row["parked_icao"],
        "traveling_owner": row["traveling_owner_name"],
        "requested_by": row["requested_by_name"],
        "approved_by": row["approved_by_name"] if "approved_by_name" in row.keys() else None,
        "notes": row["notes"] or "",
        "created_at": _utc_iso_to_local_iso(row["created_at_utc"]),
        "updated_at": _utc_iso_to_local_iso(row["updated_at_utc"]),
        "decision_at": _utc_iso_to_local_iso(row["decision_at_utc"]) if row["decision_at_utc"] else None,
    }


def _resolve_required_airport(raw_code: str | None, field: str):
    raw = (raw_code or "").strip().upper()
    if not raw:
        return None, {"message": f"{field} is required.", "code": f"invalid_{field}", "field": field, "status": 400}
    if not AIRPORT_CODE_RE.fullmatch(raw):
        return None, {"message": f"{field} must be a valid airport code.", "code": f"invalid_{field}", "field": field, "status": 400}
    resolved = _resolve_airport_code(raw)
    if not resolved:
        return None, {"message": f"Unknown airport code for {field}: {raw}.", "code": f"unknown_{field}", "field": field, "status": 400}
    return resolved, None


def _validate_reservation_fields(
    payload: dict,
    *,
    existing: dict | None = None,
    current_user: dict,
):
    start_local, start_err = _extract_local_datetime_from_payload(payload, prefix="start")
    if start_err:
        return None, start_err
    end_local, end_err = _extract_local_datetime_from_payload(payload, prefix="end")
    if end_err:
        return None, end_err

    if existing and start_local is None:
        start_dt_local = datetime.fromisoformat(existing["start"]).astimezone(_home_zone())
    else:
        start_dt_local = _parse_local_datetime(start_local)
    if existing and end_local is None:
        end_dt_local = datetime.fromisoformat(existing["end"]).astimezone(_home_zone())
    else:
        end_dt_local = _parse_local_datetime(end_local)

    if not start_dt_local or not end_dt_local:
        return None, {
            "message": "Provide start/end as start_local+end_local or as start_date+start_time and end_date+end_time.",
            "code": "invalid_datetime",
            "field": "start_date",
            "status": 400,
        }

    if (start_dt_local.minute % 15) != 0 or start_dt_local.second != 0:
        return None, {
            "message": "Departure time must be in 15-minute increments.",
            "code": "invalid_time_increment",
            "field": "start_time",
            "status": 400,
        }
    if (end_dt_local.minute % 15) != 0 or end_dt_local.second != 0:
        return None, {
            "message": "Arrival time must be in 15-minute increments.",
            "code": "invalid_time_increment",
            "field": "end_time",
            "status": 400,
        }

    duration_minutes = int((end_dt_local - start_dt_local).total_seconds() / 60)
    min_minutes = _effective_reservation_min_minutes()
    max_days = _effective_reservation_max_days()
    if duration_minutes < min_minutes:
        return None, {
            "message": f"Reservation duration must be at least {min_minutes} minutes.",
            "code": "invalid_duration",
            "field": "end_local",
            "status": 400,
        }
    if duration_minutes > (max_days * 24 * 60):
        return None, {
            "message": f"Reservation duration cannot exceed {max_days} days.",
            "code": "duration_too_long",
            "field": "end_local",
            "status": 400,
        }

    dep_value = payload.get("dep_icao", existing["dep_icao"] if existing else None)
    dest_value = payload.get("dest_icao", existing["dest_icao"] if existing else None)
    dep, err = _resolve_required_airport(dep_value, "dep_icao")
    if err:
        return None, err
    dest, err = _resolve_required_airport(dest_value, "dest_icao")
    if err:
        return None, err
    # Parked airport is derived from destination for this single-aircraft workflow.
    parked = dest

    if dep == dest:
        return None, {"message": "Departure and destination cannot be the same.", "code": "same_airport", "field": "dest_icao", "status": 400}

    traveling_user_id = payload.get("traveling_user_id")
    if current_user["role"] != "admin":
        traveling_user_id = current_user["id"]
    elif traveling_user_id is None and existing:
        traveling_user_id = existing["traveling_user_id"]
    elif traveling_user_id is None:
        traveling_user_id = current_user["id"]
    try:
        traveling_user_id = int(traveling_user_id)
    except Exception:
        return None, {"message": "traveling_user_id must be an integer.", "code": "invalid_traveling_owner", "field": "traveling_user_id", "status": 400}

    notes = (payload.get("notes") if "notes" in payload else (existing["notes"] if existing else "")) or ""
    start_utc = _to_utc_iso(start_dt_local)
    end_utc = _to_utc_iso(end_dt_local)

    return {
        "start_utc": start_utc,
        "end_utc": end_utc,
        "dep_icao": dep,
        "dest_icao": dest,
        "parked_icao": parked,
        "traveling_user_id": traveling_user_id,
        "notes": notes.strip(),
    }, None


def _quote_draft_token() -> str:
    return secrets.token_urlsafe(24)


def _iso_local_minute(dt_local: datetime) -> str:
    return dt_local.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def _ceil_to_quarter_hour(dt_local: datetime) -> datetime:
    base = dt_local.replace(second=0, microsecond=0)
    remainder = base.minute % 15
    if remainder == 0:
        return base
    return base + timedelta(minutes=(15 - remainder))


def _parse_required_local_departure(raw: object, *, field: str):
    dt_local = _parse_local_datetime(str(raw or "").strip())
    if not dt_local:
        return None, {
            "message": f"{field} must be a valid local datetime in YYYY-MM-DDTHH:MM format.",
            "code": "invalid_datetime",
            "field": field,
            "status": 400,
        }
    if (dt_local.minute % 15) != 0 or dt_local.second != 0:
        return None, {
            "message": f"{field} must be in 15-minute increments.",
            "code": "invalid_time_increment",
            "field": field,
            "status": 400,
        }
    return dt_local, None


def _validate_estimate_for_quote_draft(estimate_payload: object):
    if not isinstance(estimate_payload, dict):
        return None, {
            "message": "estimate is required and must be an object.",
            "code": "invalid_estimate",
            "field": "estimate",
            "status": 400,
        }

    inputs = estimate_payload.get("inputs")
    if not isinstance(inputs, dict):
        return None, {
            "message": "estimate.inputs is required.",
            "code": "invalid_estimate",
            "field": "estimate.inputs",
            "status": 400,
        }

    trip_input, err = _validate_trip_inputs(
        dep_raw=inputs.get("dep"),
        dest_raw=inputs.get("dest"),
        trip_type_raw=inputs.get("trip_type"),
        depart_date_raw=inputs.get("depart_date"),
        return_date_raw=inputs.get("return_date"),
    )
    if err:
        return None, {
            "message": f"estimate payload invalid: {err['message']}",
            "code": "invalid_estimate",
            "field": "estimate.inputs",
            "status": 400,
        }

    legs = estimate_payload.get("legs")
    if not isinstance(legs, list) or len(legs) < 1:
        return None, {
            "message": "estimate.legs must include at least one leg.",
            "code": "invalid_estimate",
            "field": "estimate.legs",
            "status": 400,
        }
    if trip_input["trip_type"] == "roundtrip" and len(legs) < 2:
        return None, {
            "message": "Roundtrip estimate must include outbound and return legs.",
            "code": "invalid_estimate",
            "field": "estimate.legs",
            "status": 400,
        }

    parsed_legs: list[dict] = []
    expected_routes = [(trip_input["dep"], trip_input["dest"])]
    if trip_input["trip_type"] == "roundtrip":
        expected_routes.append((trip_input["dest"], trip_input["dep"]))

    max_duration_minutes = _effective_reservation_max_days() * 24 * 60
    for index, (dep_expected, dest_expected) in enumerate(expected_routes):
        leg = legs[index] if index < len(legs) and isinstance(legs[index], dict) else {}
        typical = leg.get("typical") if isinstance(leg.get("typical"), dict) else {}
        try:
            duration_minutes = int(typical.get("minutes"))
        except Exception:
            duration_minutes = 0
        if duration_minutes < _effective_reservation_min_minutes():
            return None, {
                "message": f"Leg {index + 1} duration is below minimum reservation duration.",
                "code": "invalid_estimate",
                "field": "estimate.legs",
                "status": 400,
            }
        if duration_minutes > max_duration_minutes:
            return None, {
                "message": f"Leg {index + 1} duration exceeds maximum reservation duration.",
                "code": "invalid_estimate",
                "field": "estimate.legs",
                "status": 400,
            }
        parsed_legs.append(
            {
                "dep_icao": dep_expected,
                "dest_icao": dest_expected,
                "duration_minutes": duration_minutes,
            }
        )

    return {
        "trip_type": trip_input["trip_type"],
        "dep_icao": trip_input["dep"],
        "dest_icao": trip_input["dest"],
        "depart_date": trip_input["depart_date"].isoformat(),
        "return_date": trip_input["return_date"].isoformat() if trip_input["return_date"] else None,
        "legs": parsed_legs,
    }, None


def _build_quote_draft_payload(estimate_data: dict, outbound_departure_local: datetime, return_departure_local: datetime | None):
    legs: list[dict] = []
    outbound = estimate_data["legs"][0]
    outbound_end = _ceil_to_quarter_hour(outbound_departure_local + timedelta(minutes=int(outbound["duration_minutes"])))
    legs.append(
        {
            "dep_icao": outbound["dep_icao"],
            "dest_icao": outbound["dest_icao"],
            "start_local": _iso_local_minute(outbound_departure_local),
            "end_local": _iso_local_minute(outbound_end),
            "duration_minutes": int(outbound["duration_minutes"]),
            "notes": f"Quote-based {estimate_data['trip_type']} request ({outbound['dep_icao']} → {outbound['dest_icao']}).",
        }
    )
    if estimate_data["trip_type"] == "roundtrip":
        return_leg = estimate_data["legs"][1]
        return_start = return_departure_local
        return_end = _ceil_to_quarter_hour(return_start + timedelta(minutes=int(return_leg["duration_minutes"])))
        legs.append(
            {
                "dep_icao": return_leg["dep_icao"],
                "dest_icao": return_leg["dest_icao"],
                "start_local": _iso_local_minute(return_start),
                "end_local": _iso_local_minute(return_end),
                "duration_minutes": int(return_leg["duration_minutes"]),
                "notes": f"Quote-based {estimate_data['trip_type']} request ({return_leg['dep_icao']} → {return_leg['dest_icao']}).",
            }
        )
    return {
        "trip_type": estimate_data["trip_type"],
        "dep_icao": estimate_data["dep_icao"],
        "dest_icao": estimate_data["dest_icao"],
        "depart_date": estimate_data["depart_date"],
        "return_date": estimate_data["return_date"],
        "legs": legs,
    }


def _planner_quote_draft_payload(row) -> dict:
    draft_payload = json.loads(row["draft_json"])
    return {
        "token": row["token"],
        "status": row["status"],
        "trip_type": draft_payload.get("trip_type", "oneway"),
        "legs": draft_payload.get("legs", []),
        "dep_icao": draft_payload.get("dep_icao"),
        "dest_icao": draft_payload.get("dest_icao"),
        "expires_at": _utc_iso_to_local_iso(row["expires_at_utc"]),
    }


def _planner_quote_draft_row_or_error(conn, *, token: str, user_id: int):
    now_utc = _utc_now().isoformat()
    storage.expire_open_planner_quote_drafts(conn, now_utc=now_utc)
    row = storage.get_planner_quote_draft_by_token(conn, token)
    if not row:
        return None, {"message": "Draft not found.", "code": "not_found", "status": 404}
    if int(row["user_id"]) != int(user_id):
        return None, {"message": "Draft not found.", "code": "not_found", "status": 404}
    if row["status"] != "open":
        if row["status"] == "expired":
            return None, {"message": "This draft has expired.", "code": "draft_expired", "status": 410}
        return None, {"message": "This draft has already been submitted.", "code": "draft_consumed", "status": 409}
    if row["expires_at_utc"] <= now_utc:
        conn.execute(
            "UPDATE planner_quote_drafts SET status = 'expired' WHERE id = ? AND status = 'open'",
            (int(row["id"]),),
        )
        conn.commit()
        return None, {"message": "This draft has expired.", "code": "draft_expired", "status": 410}
    return row, None


def _bootstrap_admin_if_configured() -> None:
    if not TBM_BOOTSTRAP_ADMIN_EMAIL or not TBM_BOOTSTRAP_ADMIN_NAME or not TBM_BOOTSTRAP_ADMIN_PASSWORD:
        logger.info("bootstrap_admin_skipped reason=missing_env")
        return
    with _db_conn() as conn:
        created = storage.ensure_bootstrap_admin(
            conn,
            email=TBM_BOOTSTRAP_ADMIN_EMAIL,
            name=TBM_BOOTSTRAP_ADMIN_NAME,
            password_hash=hash_password(TBM_BOOTSTRAP_ADMIN_PASSWORD),
        )
        if created:
            logger.info("bootstrap_admin_created email=%s", TBM_BOOTSTRAP_ADMIN_EMAIL)


@app.before_request
def load_current_user():
    g.request_started_at = time.time()
    g.request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    g.current_user = None
    user_id = session.get("user_id")
    if not user_id:
        return
    try:
        uid = int(user_id)
    except Exception:
        session.pop("user_id", None)
        return
    with _db_conn() as conn:
        row = storage.get_user_by_id(conn, uid)
    if not row:
        session.pop("user_id", None)
        return
    g.current_user = _serialize_user(row)


@app.before_request
def apply_request_guards():
    endpoint = (request.endpoint or "")
    endpoint_key = endpoint.split(".")[-1]

    allowed, retry_after = _enforce_rate_limit(endpoint)
    if not allowed:
        response, status = _json_error("Rate limit exceeded. Please retry shortly.", 429, "rate_limited")
        if retry_after:
            response.headers["Retry-After"] = str(retry_after)
        return response, status

    is_mutating = request.method.upper() in ("POST", "PATCH", "PUT", "DELETE")
    if not is_mutating:
        return None

    if endpoint_key in CSRF_SAFE_ENDPOINTS:
        return None

    if endpoint_key == "logout_post":
        if not _verify_csrf_token():
            return redirect(url_for("login"))
        return None
    if endpoint_key == "login_post":
        if not _verify_csrf_token():
            return render_template("login.html", app_name=APP_NAME, error="Session expired. Please try logging in again."), 403
        return None
    if endpoint_key == "account_password_post":
        if getattr(g, "current_user", None) and not _verify_csrf_token():
            return "Forbidden.", 403

    if request.path.startswith("/api/") and getattr(g, "current_user", None):
        if not _verify_csrf_token():
            return _json_error("Missing or invalid CSRF token.", 403, "csrf_failed")
    return None


@app.after_request
def add_response_metadata(response):
    response.headers["X-Request-Id"] = _request_id()
    elapsed_ms = None
    started = getattr(g, "request_started_at", None)
    if isinstance(started, (int, float)):
        elapsed_ms = int((time.time() - started) * 1000)
    logger.info(
        "request_complete method=%s path=%s endpoint=%s status=%s elapsed_ms=%s request_id=%s",
        request.method,
        request.path,
        request.endpoint,
        response.status_code,
        elapsed_ms,
        _request_id(),
    )
    return response


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    from werkzeug.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        if request.path.startswith("/api/"):
            return _json_error(exc.description or "Request failed.", exc.code or 500, "http_error")
        return exc

    logger.exception("unhandled_exception request_id=%s", _request_id())
    if request.path.startswith("/api/"):
        return _json_error("Internal server error.", 500, "internal_error")
    return render_template("home.html", app_name=APP_NAME), 500


@app.context_processor
def inject_template_globals():
    return {
        "current_user": getattr(g, "current_user", None),
        "home_timezone": _effective_home_timezone_name(),
        "csrf_token": _get_or_create_csrf_token(),
        "format_date_display": _format_date_display,
    }


# ------------------ General helpers ------------------
def _json_error(message: str, status: int = 400, code: str = "bad_request", field: str | None = None):
    payload = {"error": message, "code": code, "request_id": _request_id()}
    if field:
        payload["field"] = field
    return jsonify(payload), status


def _flightaware_live_url(tail_number: str) -> str:
    return f"https://www.flightaware.com/live/flight/{urllib.parse.quote((tail_number or LIVE_TRACKING_TAIL).strip().upper())}"



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


# ------------------ UI/API: auth + reservations ------------------
app.register_blueprint(
    create_auth_blueprint(
        app_name=APP_NAME,
        db_conn=_db_conn,
        serialize_user=_serialize_user,
        json_error=_json_error,
    )
)
app.register_blueprint(
    create_reservations_blueprint(
        login_required=login_required,
        admin_required=admin_required,
        db_conn=_db_conn,
        json_error=_json_error,
        parse_local_datetime=_parse_local_datetime,
        home_zone=_home_zone,
        to_utc_iso=_to_utc_iso,
        utc_iso_to_local_iso=_utc_iso_to_local_iso,
        effective_home_timezone_name=_effective_home_timezone_name,
        effective_user_show_closed_default=_effective_user_show_closed_default,
        reservation_payload=_reservation_payload,
        reservation_to_event_payload=_reservation_to_event_payload,
        validate_reservation_fields=_validate_reservation_fields,
        can_edit_reservation=_can_edit_reservation,
        can_request_change=_can_request_change,
        can_reopen_reservation=_can_reopen_reservation,
        utc_now=_utc_now,
        default_parked_icao=TBM_DEFAULT_PARKED_ICAO,
    )
)


@app.get("/calendar")
@login_required
def calendar_page():
    return render_template(
        "calendar.html",
        app_name=APP_NAME,
        home_timezone=_effective_home_timezone_name(),
        reservation_min_minutes=_effective_reservation_min_minutes(),
    )


@app.get("/admin")
@admin_required
def admin_page():
    with _db_conn() as conn:
        owners = [_serialize_user(row) for row in storage.list_owner_users(conn)]
    return render_template("admin.html", app_name=APP_NAME, owners=owners, home_timezone=_effective_home_timezone_name())


@app.get("/admin/flights")
@admin_required
def admin_flights_page():
    return render_template("admin_flights.html", app_name=APP_NAME, home_timezone=_effective_home_timezone_name())


@app.get("/admin/settings")
@admin_required
def admin_settings_page():
    return render_template("admin_settings.html", app_name=APP_NAME, home_timezone=_effective_home_timezone_name())


@app.get("/my-flights")
@login_required
def my_flights_page():
    return render_template("my_flights.html", app_name=APP_NAME, home_timezone=_effective_home_timezone_name())


@app.get("/live-tracking")
@login_required
def live_tracking_page():
    return render_template(
        "live_tracking.html",
        app_name=APP_NAME,
        home_timezone=_effective_home_timezone_name(),
        live_tracking_tail=LIVE_TRACKING_TAIL,
        live_tracking_url=_flightaware_live_url(LIVE_TRACKING_TAIL),
    )


@app.get("/account")
@login_required
def account_page():
    with _db_conn() as conn:
        row = storage.get_user_by_id(conn, int(g.current_user["id"]))
    if not row:
        return ("Account not found.", 404)
    return render_template("account.html", **_account_view_context(row=row))


@app.post("/account/password")
@login_required
def account_password_post():
    current_password = str(request.form.get("current_password") or "")
    new_password = str(request.form.get("new_password") or "")
    confirm_password = str(request.form.get("confirm_password") or "")

    with _db_conn() as conn:
        row = storage.get_user_by_id(conn, int(g.current_user["id"]))
        if not row:
            return ("Account not found.", 404)

        if not current_password or not new_password or not confirm_password:
            return render_template(
                "account.html",
                **_account_view_context(row=row, form_error="Current password, new password, and confirm password are required."),
            ), 400
        if not verify_password(row["password_hash"], current_password):
            return render_template(
                "account.html",
                **_account_view_context(row=row, form_error="Current password is incorrect."),
            ), 400
        if len(new_password) < 8:
            return render_template(
                "account.html",
                **_account_view_context(row=row, form_error="New password must be at least 8 characters."),
            ), 400
        if new_password != confirm_password:
            return render_template(
                "account.html",
                **_account_view_context(row=row, form_error="New password and confirm password must match."),
            ), 400
        if verify_password(row["password_hash"], new_password):
            return render_template(
                "account.html",
                **_account_view_context(row=row, form_error="New password must be different from current password."),
            ), 400

        storage.set_user_password(conn, int(row["id"]), hash_password(new_password))
        refreshed = storage.get_user_by_id(conn, int(row["id"]))

    return render_template(
        "account.html",
        **_account_view_context(row=refreshed, form_success="Password updated successfully."),
    )


@app.get("/reservations/<int:reservation_id>")
@login_required
def reservation_detail_page(reservation_id: int):
    with _db_conn() as conn:
        row = storage.get_reservation_by_id(conn, reservation_id)
    if not row:
        return ("Reservation not found.", 404)

    if row["status"] not in ("pending", "approved"):
        return ("Reservation not found.", 404)

    current_user = g.current_user
    is_owner_scope = (
        int(row["requested_by_user_id"]) == int(current_user["id"])
        or int(row["traveling_user_id"]) == int(current_user["id"])
    )
    if current_user["role"] != "admin" and not is_owner_scope:
        return ("Forbidden.", 403)

    reservation = _reservation_payload(row)
    reservation["created_display"] = _utc_iso_to_local_display(row["created_at_utc"])
    reservation["updated_display"] = _utc_iso_to_local_display(row["updated_at_utc"])
    reservation["editable"] = _can_edit_reservation(current_user, row)
    reservation["can_cancel"] = _can_cancel_reservation(current_user, row)
    reservation["can_request_change"] = _can_request_change(current_user, row)

    start_local = datetime.fromisoformat(row["start_utc"]).astimezone(_home_zone())
    estimate = estimate_trip(
        dep=row["dep_icao"],
        dest=row["dest_icao"],
        trip_type="oneway",
        depart_date=start_local.date(),
        return_date=None,
        assumptions=None,
    )
    reservation["estimate"] = {
        "typical_block_time": estimate["totals"]["typical"]["block_time"],
        "typical_total_cost": money(float(estimate["totals"]["typical"]["costs"]["total"])),
        "conservative_block_time": estimate["totals"]["conservative"]["block_time"],
        "conservative_total_cost": money(float(estimate["totals"]["conservative"]["costs"]["total"])),
    }

    edit_link = url_for(
        "calendar_page",
        edit_reservation_id=reservation["id"],
        edit_start=reservation["start"],
        edit_end=reservation["end"],
    )

    return render_template(
        "reservation_detail.html",
        app_name=APP_NAME,
        home_timezone=_effective_home_timezone_name(),
        reservation=reservation,
        edit_link=edit_link,
    )


@app.get("/api/owners")
@login_required
def api_owners():
    with _db_conn() as conn:
        owners = []
        for row in storage.list_owner_users(conn):
            owner = _serialize_user(row)
            owner_id = int(owner["id"])
            owner["default_color"] = _default_owner_color(owner_id)
            owner["color"] = _effective_owner_color(owner_id)
            owners.append(owner)
    return jsonify(owners)


@app.post("/api/owners")
@admin_required
def api_create_owner():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    if not name or not email or not password:
        return _json_error("name, email, and password are required.", 400, "invalid_owner")
    if len(password) < 8:
        return _json_error("password must be at least 8 characters.", 400, "weak_password", "password")
    try:
        with _db_conn() as conn:
            owner = storage.create_user(
                conn,
                email=email,
                name=name,
                role="owner",
                password_hash=hash_password(password),
            )
    except sqlite3.IntegrityError:
        return _json_error("An account with this email already exists.", 409, "owner_exists", "email")
    return jsonify({"ok": True, "owner": _serialize_user(owner)}), 201


@app.post("/api/owners/<int:user_id>/reset-password")
@admin_required
def api_owner_reset_password(user_id: int):
    data = request.get_json(silent=True) or {}
    password = str(data.get("password") or "")
    if len(password) < 8:
        return _json_error("password must be at least 8 characters.", 400, "weak_password", "password")
    with _db_conn() as conn:
        target = storage.get_user_by_id(conn, user_id)
        if not target:
            return _json_error("Owner not found.", 404, "not_found")
        if target["role"] != "owner":
            return _json_error("Only owner passwords can be reset here.", 400, "invalid_owner")
        storage.set_user_password(conn, user_id, hash_password(password))
    return jsonify({"ok": True})


@app.get("/api/admin/settings")
@admin_required
def api_admin_settings():
    effective = _load_runtime_settings(force=True)
    return jsonify(
        {
            "settings": effective,
            "defaults": _default_runtime_settings(),
        }
    )


@app.patch("/api/admin/settings")
@admin_required
def api_admin_settings_patch():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _json_error("Request body must be a JSON object.", 400, "invalid_json")

    allowed = {
        "home_timezone",
        "reservation_min_minutes",
        "reservation_max_days",
        "admin_flights_default_scope",
        "user_show_closed_default",
        "pending_reservation_color",
    }
    updates: dict[str, str] = {}
    owner_color_owner_ids: set[int] = set()

    for key, raw_value in data.items():
        is_owner_color = key.startswith("owner_color_")
        if key not in allowed and not is_owner_color:
            return _json_error(f"Unsupported setting: {key}", 400, "invalid_setting", key)
        value = str(raw_value).strip()
        if is_owner_color:
            owner_id_raw = key.removeprefix("owner_color_")
            try:
                owner_id = int(owner_id_raw)
            except Exception:
                return _json_error("owner color key must be owner_color_<owner_id>.", 400, "invalid_setting", key)
            if owner_id < 1:
                return _json_error("owner color key must use a positive owner id.", 400, "invalid_setting", key)
            normalized = _normalize_hex_color(value)
            if not normalized:
                return _json_error("owner colors must be hex values like #A1B2C3.", 400, "invalid_setting", key)
            owner_color_owner_ids.add(owner_id)
            value = normalized
        elif key == "home_timezone":
            try:
                ZoneInfo(value)
            except Exception:
                return _json_error("home_timezone must be a valid IANA timezone.", 400, "invalid_setting", key)
        elif key == "reservation_min_minutes":
            try:
                parsed = int(value)
            except Exception:
                return _json_error("reservation_min_minutes must be an integer.", 400, "invalid_setting", key)
            if parsed < 1 or parsed > 1440:
                return _json_error("reservation_min_minutes must be between 1 and 1440.", 400, "invalid_setting", key)
            value = str(parsed)
        elif key == "reservation_max_days":
            try:
                parsed = int(value)
            except Exception:
                return _json_error("reservation_max_days must be an integer.", 400, "invalid_setting", key)
            if parsed < 1 or parsed > 365:
                return _json_error("reservation_max_days must be between 1 and 365.", 400, "invalid_setting", key)
            value = str(parsed)
        elif key == "admin_flights_default_scope":
            value = value.lower()
            if value not in ("future_only", "all"):
                return _json_error("admin_flights_default_scope must be 'future_only' or 'all'.", 400, "invalid_setting", key)
        elif key == "user_show_closed_default":
            value = value.lower()
            if value not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
                return _json_error("user_show_closed_default must be a boolean-like value.", 400, "invalid_setting", key)
            value = "true" if value in ("true", "1", "yes", "on") else "false"
        elif key == "pending_reservation_color":
            normalized = _normalize_hex_color(value)
            if not normalized:
                return _json_error("pending_reservation_color must be a hex value like #A1B2C3.", 400, "invalid_setting", key)
            value = normalized
        updates[key] = value

    if updates:
        current = _load_runtime_settings(force=True)
        merged = dict(current)
        merged.update(updates)
        try:
            min_minutes = int(merged["reservation_min_minutes"])
            max_days = int(merged["reservation_max_days"])
        except Exception:
            return _json_error("reservation_min_minutes and reservation_max_days must be integers.", 400, "invalid_setting")
        if min_minutes >= (max_days * 24 * 60):
            return _json_error(
                "reservation_min_minutes must be less than reservation_max_days * 24 * 60.",
                400,
                "invalid_setting",
                "reservation_min_minutes",
            )

    if not updates:
        return jsonify({"ok": True, "settings": _load_runtime_settings(force=True)})

    with _db_conn() as conn:
        for owner_id in owner_color_owner_ids:
            owner = storage.get_user_by_id(conn, owner_id)
            if not owner or owner["role"] != "owner":
                return _json_error("owner color keys must reference an existing owner user id.", 400, "invalid_setting")
        storage.upsert_settings_with_audit(conn, updates, updated_by_user_id=int(g.current_user["id"]))
    _invalidate_settings_cache()
    return jsonify({"ok": True, "settings": _load_runtime_settings(force=True)})


@app.get("/api/admin/settings/history")
@admin_required
def api_admin_settings_history():
    key = (request.args.get("key") or "").strip()
    page_raw = (request.args.get("page") or "1").strip()
    page_size_raw = (request.args.get("page_size") or "25").strip()
    try:
        page = max(1, int(page_raw))
    except Exception:
        return _json_error("page must be an integer.", 400, "invalid_page", "page")
    try:
        page_size = max(1, min(100, int(page_size_raw)))
    except Exception:
        return _json_error("page_size must be an integer.", 400, "invalid_page_size", "page_size")

    with _db_conn() as conn:
        result = storage.list_settings_audit_history(conn, key=key or None, page=page, page_size=page_size)

    rows = []
    for row in result["rows"]:
        rows.append(
            {
                "id": int(row["id"]),
                "key": row["key"],
                "old_value": row["old_value"],
                "new_value": row["new_value"],
                "changed_by_user_id": int(row["changed_by_user_id"]) if row["changed_by_user_id"] is not None else None,
                "changed_by": row["changed_by_name"] or row["changed_by_email"] or "Unknown",
                "changed_at": _utc_iso_to_local_iso(row["changed_at_utc"]),
            }
        )
    return jsonify({"items": rows, "total": int(result["total"]), "page": page, "page_size": page_size})


@app.get("/api/admin/flights")
@admin_required
def api_admin_flights():
    status = (request.args.get("status") or "all").strip().lower()
    owner_id_raw = (request.args.get("owner_id") or "").strip()
    requested_by_id_raw = (request.args.get("requested_by_id") or "").strip()
    query = (request.args.get("q") or "").strip()
    from_raw = (request.args.get("from") or "").strip()
    to_raw = (request.args.get("to") or "").strip()
    page_raw = (request.args.get("page") or "1").strip()
    page_size_raw = (request.args.get("page_size") or "25").strip()

    try:
        page = max(1, int(page_raw))
    except Exception:
        return _json_error("page must be an integer.", 400, "invalid_page", "page")
    try:
        page_size = int(page_size_raw)
    except Exception:
        return _json_error("page_size must be an integer.", 400, "invalid_page_size", "page_size")
    page_size = max(1, min(page_size, 100))

    owner_id = None
    if owner_id_raw:
        try:
            owner_id = int(owner_id_raw)
        except Exception:
            return _json_error("owner_id must be an integer.", 400, "invalid_owner", "owner_id")
    requested_by_id = None
    if requested_by_id_raw:
        try:
            requested_by_id = int(requested_by_id_raw)
        except Exception:
            return _json_error("requested_by_id must be an integer.", 400, "invalid_owner", "requested_by_id")

    from_utc = None
    to_utc = None
    if from_raw:
        parsed = _parse_local_datetime(from_raw)
        if not parsed:
            return _json_error("Invalid from value.", 400, "invalid_range", "from")
        from_utc = _to_utc_iso(parsed)
    if to_raw:
        parsed = _parse_local_datetime(to_raw)
        if not parsed:
            return _json_error("Invalid to value.", 400, "invalid_range", "to")
        to_utc = _to_utc_iso(parsed)
    if not from_utc and not from_raw and _effective_admin_flights_default_scope() == "future_only":
        from_utc = _to_utc_iso(datetime.now(_home_zone()))

    with _db_conn() as conn:
        result = storage.list_admin_flights(
            conn,
            status=status,
            from_utc=from_utc,
            to_utc=to_utc,
            owner_id=owner_id,
            requested_by_id=requested_by_id,
            query=query or None,
            page=page,
            page_size=page_size,
        )
    rows = [_reservation_payload(row) for row in result["rows"]]
    return jsonify({"items": rows, "total": int(result["total"]), "page": page, "page_size": page_size})




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
    min_runway_ft_explicit = (request.args.get("min_runway_ft") or "").strip() != ""

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
        metadata_coverage = 0
        for item in nearest:
            ops = metadata.get(item["icao"], {})
            if ops:
                metadata_coverage += 1
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

        # If ops metadata could not be fetched at all (network outage/proxy block),
        # keep default lookups functional by avoiding the default runway-length filter.
        # Explicit runway filters from the caller are still honored.
        effective_min_runway_ft = min_runway_ft
        if metadata_coverage == 0 and not min_runway_ft_explicit:
            effective_min_runway_ft = 0

        filtered = _apply_nearest_filters(
            items=enriched,
            min_runway_ft=effective_min_runway_ft,
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
@login_required
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


@app.post("/api/planner/quote-drafts")
@login_required
def api_create_planner_quote_draft():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _json_error("Request body must be a JSON object.", 400, "invalid_json")

    estimate_data, estimate_err = _validate_estimate_for_quote_draft(data.get("estimate"))
    if estimate_err:
        return _json_error(
            estimate_err["message"],
            estimate_err["status"],
            estimate_err["code"],
            estimate_err.get("field"),
        )

    outbound_departure_local, outbound_err = _parse_required_local_departure(
        data.get("outbound_departure_local"),
        field="outbound_departure_local",
    )
    if outbound_err:
        return _json_error(
            outbound_err["message"],
            outbound_err["status"],
            outbound_err["code"],
            outbound_err.get("field"),
        )

    return_departure_local = None
    if estimate_data["trip_type"] == "roundtrip":
        return_departure_local, return_err = _parse_required_local_departure(
            data.get("return_departure_local"),
            field="return_departure_local",
        )
        if return_err:
            return _json_error(
                return_err["message"],
                return_err["status"],
                return_err["code"],
                return_err.get("field"),
            )

    draft_payload = _build_quote_draft_payload(estimate_data, outbound_departure_local, return_departure_local)
    if draft_payload["trip_type"] == "roundtrip":
        outbound_end = _parse_local_datetime(draft_payload["legs"][0]["end_local"])
        return_start = _parse_local_datetime(draft_payload["legs"][1]["start_local"])
        if not outbound_end or not return_start or return_start <= outbound_end:
            return _json_error(
                "return_departure_local must be after outbound arrival time.",
                400,
                "invalid_datetime",
                "return_departure_local",
            )

    expires_at_utc = (_utc_now() + timedelta(seconds=PLANNER_DRAFT_TTL_SEC)).isoformat()
    token = _quote_draft_token()
    with _db_conn() as conn:
        created = storage.create_planner_quote_draft(
            conn,
            token=token,
            user_id=int(g.current_user["id"]),
            draft_json=json.dumps(draft_payload),
            expires_at_utc=expires_at_utc,
        )

    return jsonify(
        {
            "token": created["token"],
            "expires_at": _utc_iso_to_local_iso(created["expires_at_utc"]),
            "draft_preview": _planner_quote_draft_payload(created),
        }
    ), 201


@app.get("/api/planner/quote-drafts/<string:token>")
@login_required
def api_get_planner_quote_draft(token: str):
    with _db_conn() as conn:
        row, err = _planner_quote_draft_row_or_error(conn, token=token, user_id=int(g.current_user["id"]))
        if err:
            return _json_error(err["message"], err["status"], err["code"])
        return jsonify(_planner_quote_draft_payload(row))


@app.post("/api/planner/quote-drafts/<string:token>/submit")
@login_required
def api_submit_planner_quote_draft(token: str):
    with _db_conn() as conn:
        row, err = _planner_quote_draft_row_or_error(conn, token=token, user_id=int(g.current_user["id"]))
        if err:
            return _json_error(err["message"], err["status"], err["code"])

        payload = request.get_json(silent=True) or {}
        if payload and not isinstance(payload, dict):
            return _json_error("Request body must be a JSON object.", 400, "invalid_json")

        draft_payload = json.loads(row["draft_json"])
        legs = draft_payload.get("legs")
        if not isinstance(legs, list) or not legs:
            return _json_error("Draft does not contain any reservation legs.", 400, "invalid_draft")

        overrides = payload.get("legs")
        if overrides is not None:
            if not isinstance(overrides, list) or len(overrides) != len(legs):
                return _json_error("legs override must be an array matching draft leg count.", 400, "invalid_legs", "legs")
            for index, override in enumerate(overrides):
                if not isinstance(override, dict):
                    return _json_error("Each legs override item must be an object.", 400, "invalid_legs", "legs")
                leg = dict(legs[index])
                for key in ("start_local", "end_local", "dep_icao", "dest_icao", "notes"):
                    if key in override and str(override[key]).strip():
                        leg[key] = str(override[key]).strip()
                legs[index] = leg

        normalized_legs: list[dict] = []
        for index, leg in enumerate(legs):
            leg_payload = {
                "start_local": leg.get("start_local"),
                "end_local": leg.get("end_local"),
                "dep_icao": leg.get("dep_icao"),
                "dest_icao": leg.get("dest_icao"),
                "notes": leg.get("notes") or "",
                "traveling_user_id": int(g.current_user["id"]),
            }
            normalized, validation_err = _validate_reservation_fields(
                leg_payload,
                current_user=g.current_user,
            )
            if validation_err:
                return _json_error(
                    f"Leg {index + 1}: {validation_err['message']}",
                    validation_err["status"],
                    validation_err["code"],
                    validation_err.get("field"),
                )
            normalized_legs.append(normalized)

        if len(normalized_legs) >= 2:
            ordered = sorted(normalized_legs, key=lambda item: item["start_utc"])
            for idx in range(1, len(ordered)):
                if ordered[idx]["start_utc"] < ordered[idx - 1]["end_utc"]:
                    return _json_error(
                        "Draft legs overlap each other. Adjust departure times before submitting.",
                        409,
                        "overlap_conflict",
                    )

        for normalized in normalized_legs:
            if storage.overlap_exists(
                conn,
                start_utc=normalized["start_utc"],
                end_utc=normalized["end_utc"],
            ):
                return _json_error(
                    "One or more draft legs overlap an existing reservation.",
                    409,
                    "overlap_conflict",
                )

        now_utc = _utc_now().isoformat()
        try:
            created_rows = []
            for normalized in normalized_legs:
                cur = conn.execute(
                    """
                    INSERT INTO reservations (
                        status, start_utc, end_utc, dep_icao, dest_icao, parked_icao,
                        traveling_user_id, requested_by_user_id, notes, created_at_utc, updated_at_utc
                    ) VALUES ('pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized["start_utc"],
                        normalized["end_utc"],
                        normalized["dep_icao"],
                        normalized["dest_icao"],
                        normalized["parked_icao"],
                        int(g.current_user["id"]),
                        int(g.current_user["id"]),
                        normalized["notes"],
                        now_utc,
                        now_utc,
                    ),
                )
                created_rows.append(storage.get_reservation_by_id(conn, int(cur.lastrowid)))
            conn.execute(
                """
                UPDATE planner_quote_drafts
                SET status = 'consumed', consumed_at_utc = ?
                WHERE id = ?
                  AND status = 'open'
                """,
                (now_utc, int(row["id"])),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return jsonify(
        {
            "ok": True,
            "created": [_reservation_to_event_payload(item, g.current_user) for item in created_rows],
        }
    )


def _extract_trip_from_message(message: str):
    text = (message or "").strip()
    m = re.search(r"\b([A-Za-z0-9]{3,4})\s+to\s+([A-Za-z0-9]{3,4})\b", text, flags=re.IGNORECASE)
    if not m:
        return None
    dep_raw, dest_raw = m.group(1).upper(), m.group(2).upper()
    dep = _resolve_airport_code(dep_raw)
    dest = _resolve_airport_code(dest_raw)
    if not dep or not dest or dep == dest:
        return None
    when = parse_date_only(text) or (date.today() + timedelta(days=1))
    return dep, dest, when


@app.post("/api/chat")
def api_chat():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"reply": "Please send a message.", "parsed": {}})
    parsed = _extract_trip_from_message(message)
    lower = message.lower()
    if not parsed:
        return jsonify(
            {
                "reply": "I still need departure airport, destination airport, and a date (e.g. 'KCAK to KSRQ tomorrow').",
                "parsed": {},
            }
        )
    dep, dest, depart = parsed
    est = estimate_trip(dep=dep, dest=dest, trip_type="oneway", depart_date=depart, return_date=None, assumptions=None)
    leg = est["legs"][0]
    if "email" in lower or "draft" in lower:
        reply = (
            f"Subject: TBM trip estimate {dep} to {dest} on {depart.isoformat()}\\n\\n"
            "Hi team,\\n\\n"
            f"For {dep} to {dest}, typical block time is {leg['typical']['block_time']} with estimated total {money(float(leg['typical']['costs']['total']))}.\\n"
            f"Conservative block time is {leg['conservative']['block_time']} with estimated total {money(float(leg['conservative']['costs']['total']))}.\\n\\n"
            "Best,\\nTBM Planner"
        )
    else:
        reply = (
            "Trip summary:\\n"
            f"{dep} to {dest} on {depart.isoformat()}\\n"
            f"Typical: {leg['typical']['block_time']} and {money(float(leg['typical']['costs']['total']))}\\n"
            f"Conservative: {leg['conservative']['block_time']} and {money(float(leg['conservative']['costs']['total']))}"
        )
    return jsonify(
        {
            "reply": reply,
            "parsed": {"dep": dep, "dest": dest, "depart_date": depart.isoformat()},
        }
    )


# ------------------ UI: pages ------------------
@app.get("/")
@login_required
def home():
    return render_template("home.html", **_home_context())


@app.post("/estimate")
@login_required
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
            "timestamp_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
            "winds_cache_keys": len(_WINDTEMP_CACHE),
        }
    )


@app.get("/api/admin/system-metrics")
@admin_required
def api_admin_system_metrics():
    db_exists = os.path.exists(TBM_DB_PATH)
    db_size_bytes = os.path.getsize(TBM_DB_PATH) if db_exists else 0
    db_user_version = 0
    if db_exists:
        with storage.get_conn(TBM_DB_PATH) as conn:
            db_user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    return jsonify(
        {
            "app": APP_NAME,
            "env": APP_ENV,
            "request_id": _request_id(),
            "db": {
                "path": TBM_DB_PATH,
                "exists": db_exists,
                "size_bytes": db_size_bytes,
                "user_version": db_user_version,
            },
            "caches": {
                "settings_cache_keys": len(_SETTINGS_CACHE),
                "winds_cache_keys": len(_WINDTEMP_CACHE),
                "geocode_cache_keys": len(_GEOCODE_CACHE),
            },
            "uptime_sec": int(time.time() - START_TIME_EPOCH),
        }
    )


def _startup_safety_checks() -> None:
    secret = app.config.get("SECRET_KEY") or ""
    if IS_PRODUCTION:
        if not secret or secret == "dev-change-me":
            raise RuntimeError("FLASK_SECRET_KEY must be set to a strong value in production.")
        db_dir = os.path.dirname(TBM_DB_PATH) or "."
        if not os.path.isdir(db_dir):
            raise RuntimeError(f"TBM_DB_PATH directory does not exist: {db_dir}")
    elif secret == "dev-change-me":
        logger.warning("using_default_dev_secret_key env=%s", APP_ENV)


START_TIME_EPOCH = time.time()
_startup_safety_checks()
storage.init_db(TBM_DB_PATH)
with storage.get_conn(TBM_DB_PATH) as _conn:
    storage.seed_settings_defaults(_conn, _default_runtime_settings())
_invalidate_settings_cache()
_bootstrap_admin_if_configured()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5050"))
    debug_enabled = (os.getenv("FLASK_DEBUG") or "").strip().lower() in ("1", "true", "yes", "on")
    app.run(debug=debug_enabled, port=port, use_reloader=debug_enabled)

from __future__ import annotations

import os
import json
import re
import ssl
import certifi
import urllib.request
import urllib.parse

from math import radians, sin, cos, asin, sqrt, atan2, degrees
from datetime import datetime, date, timedelta

from flask import Flask, request, render_template, jsonify
import airportsdata

from dotenv import load_dotenv

# ------------------ Env (.env) ------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ------------------ OpenAI ------------------
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# IMPORTANT: your repo uses "Templates" (capital T)
app = Flask(__name__, template_folder="Templates")

APP_NAME = "TBM Trip Planner"

# --- Config ---
TAS_TYPICAL = 290  # kt
TAS_CONSERVATIVE = 280  # kt
ROUTING_TYPICAL = 1.03
ROUTING_CONSERVATIVE = 1.10
OVERHEAD_TYPICAL_MIN = 15
OVERHEAD_CONSERVATIVE_MIN = 20

MGMT_FEE_PER_HR = 100
MAINT_RESERVE_PER_HR = 250
ENGINE_RESERVE_PER_HR = 215
FUEL_PRICE_PER_GAL = 5.50
FUEL_BURN_GPH = 60

# Winds Aloft (FD)
WINDS_REGION = "us"
WINDS_LEVEL = "low"
WINDS_LAYOUT = "off"
ALT_TARGET_FT = 27000  # interpolate between 24000 and 30000

AIRPORTS = airportsdata.load("ICAO")


# ------------------ Math helpers ------------------
def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    R_km = 6371.0
    km_to_nm = 0.539957
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))
    return (R_km * c) * km_to_nm


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
        if q_low and (q_low in name or q_low in city):
            found.append(airport_to_choice(icao, a))

    found.sort(key=lambda c: (0 if c["iata"] else 1, c["icao"]))
    return found[:limit]


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


def fetch_windtemp_text(fcst_hours: int) -> str:
    params = {
        "region": WINDS_REGION,
        "fcst": str(fcst_hours),
        "level": WINDS_LEVEL,
        "layout": WINDS_LAYOUT,
    }
    url = "https://www.aviationweather.gov/api/data/windtemp?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "tbm-trip-planner/1.0"})
    ctx = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


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
    for ln in lines[ft_idx + 1:]:
        parts = ln.split()
        if not parts:
            continue
        st = parts[0].upper()
        if len(st) != 3:
            continue
        groups = parts[1:]
        if len(groups) < len(alts):
            continue
        station_map[st] = groups[:len(alts)]
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
    raw = fetch_windtemp_text(fcst)
    alts, station_map = build_windtemp_index(raw)

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


def costs_for_minutes(total_min: int):
    hours = total_min / 60.0
    mgmt = MGMT_FEE_PER_HR * hours
    maint = MAINT_RESERVE_PER_HR * hours
    eng = ENGINE_RESERVE_PER_HR * hours
    fuel_gal = FUEL_BURN_GPH * hours
    fuel = fuel_gal * FUEL_PRICE_PER_GAL
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


def estimate_leg(dep_icao: str, dest_icao: str, depart_dt_local: str | None):
    dep = get_airport_by_icao(dep_icao)
    dest = get_airport_by_icao(dest_icao)

    dist_nm = haversine_nm(float(dep["lat"]), float(dep["lon"]), float(dest["lat"]), float(dest["lon"]))
    course = initial_bearing_deg(float(dep["lat"]), float(dep["lon"]), float(dest["lat"]), float(dest["lon"]))

    wind_component, wind_details = wind_component_fl270_kts(dep, dest, depart_dt_local)

    # Typical
    dist_typ = dist_nm * ROUTING_TYPICAL
    gs_typ = max(60, TAS_TYPICAL + wind_component)
    cruise_min_typ = int(round((dist_typ / gs_typ) * 60))
    total_min_typ = cruise_min_typ + OVERHEAD_TYPICAL_MIN

    # Conservative
    dist_con = dist_nm * ROUTING_CONSERVATIVE
    gs_con = max(60, TAS_CONSERVATIVE + wind_component)
    cruise_min_con = int(round((dist_con / gs_con) * 60))
    total_min_con = cruise_min_con + OVERHEAD_CONSERVATIVE_MIN

    return {
        "from": dep_icao,
        "to": dest_icao,
        "from_pretty": pretty_airport(dep),
        "to_pretty": pretty_airport(dest),
        "distance_nm": int(round(dist_nm)),
        "course_deg": int(round(course)),
        "winds": {
            "component_kt": float(wind_component),
            "details": wind_details,
        },
        "typical": {
            "tas_kt": TAS_TYPICAL,
            "gs_kt": int(round(gs_typ)),
            "minutes": int(total_min_typ),
            "block_time": format_hhmm(int(total_min_typ)),
            "costs": costs_for_minutes(int(total_min_typ)),
        },
        "conservative": {
            "tas_kt": TAS_CONSERVATIVE,
            "gs_kt": int(round(gs_con)),
            "minutes": int(total_min_con),
            "block_time": format_hhmm(int(total_min_con)),
            "costs": costs_for_minutes(int(total_min_con)),
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


def estimate_trip(dep: str, dest: str, trip_type: str, depart_date: date, return_date: date | None):
    depart_dt = make_depart_dt_for_winds(depart_date)
    legs = [estimate_leg(dep, dest, depart_dt)]

    return_dt = None
    if trip_type == "roundtrip":
        if not return_date:
            return_date = depart_date + timedelta(days=1)
        return_dt = make_depart_dt_for_winds(return_date)
        legs.append(estimate_leg(dest, dep, return_dt))

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


# ------------------ API: estimate (machine-readable) ------------------
@app.post("/api/estimate")
def api_estimate():
    data = request.get_json(silent=True) or {}

    dep = (data.get("dep") or "").strip().upper()
    dest = (data.get("dest") or "").strip().upper()
    trip_type_raw = (data.get("trip_type") or "oneway").strip().lower()
    trip_type = "roundtrip" if trip_type_raw in ("roundtrip", "round_trip", "rt") else "oneway"

    depart_date = parse_date_only(data.get("depart_date"))
    return_date = parse_date_only(data.get("return_date"))

    if dep not in AIRPORTS:
        return jsonify({"error": f"Unknown departure airport: {dep}"}), 400
    if dest not in AIRPORTS:
        return jsonify({"error": f"Unknown destination airport: {dest}"}), 400
    if not depart_date:
        return jsonify({"error": "depart_date required (YYYY-MM-DD, today, or tomorrow)"}), 400

    est = estimate_trip(dep, dest, trip_type, depart_date, return_date)
    return jsonify(est)


# ------------------ UI: pages ------------------
@app.get("/")
def home():
    return render_template("home.html")


@app.get("/chat")
def chat_page():
    return render_template("chat.html")


@app.post("/estimate")
def estimate_page():
    dep_icao = (request.form.get("dep_icao") or "").strip().upper()
    dest_icao = (request.form.get("dest_icao") or "").strip().upper()
    trip_type_raw = (request.form.get("trip_type") or "oneway").strip().lower()
    trip_type = "roundtrip" if trip_type_raw == "roundtrip" else "oneway"

    depart_date = parse_date_only(request.form.get("depart_date"))
    return_date = parse_date_only(request.form.get("return_date"))

    if dep_icao not in AIRPORTS or dest_icao not in AIRPORTS or not depart_date:
        return render_template("home.html", error="Please pick valid airports and a departure date.")

    est = estimate_trip(dep_icao, dest_icao, trip_type, depart_date, return_date)
    legs = est["legs"]

    total_time_typ = est["totals"]["typical"]["block_time"]
    total_cost_typ = money(float(est["totals"]["typical"]["costs"]["total"]))
    total_time_con = est["totals"]["conservative"]["block_time"]
    total_cost_con = money(float(est["totals"]["conservative"]["costs"]["total"]))

    return render_template(
        "estimate.html",
        app_name=APP_NAME,
        trip_type=trip_type,
        depart_date=depart_date.isoformat(),
        return_date=(return_date.isoformat() if return_date else None),
        dep_pretty=legs[0]["from_pretty"],
        dest_pretty=legs[0]["to_pretty"],
        legs=legs,
        total_time_typ=total_time_typ,
        total_cost_typ=total_cost_typ,
        total_time_con=total_time_con,
        total_cost_con=total_cost_con,
        fuel_burn_gph=FUEL_BURN_GPH,
        fuel_price=f"{FUEL_PRICE_PER_GAL:.2f}",
        routing_typ_pct=int(round((ROUTING_TYPICAL - 1) * 100)),
        routing_con_pct=int(round((ROUTING_CONSERVATIVE - 1) * 100)),
        overhead_typ_min=OVERHEAD_TYPICAL_MIN,
        overhead_con_min=OVERHEAD_CONSERVATIVE_MIN,
    )


# ------------------ Chatbot (Option B: tool-calling, but conversational tone) ------------------
def _has_openai() -> bool:
    return OpenAI is not None and bool(os.getenv("OPENAI_API_KEY"))


def _tool_estimate_trip(dep: str, dest: str, trip_type: str, depart_date: str, return_date: str | None = None):
    dep = (dep or "").strip().upper()
    dest = (dest or "").strip().upper()
    trip_type = (trip_type or "oneway").strip().lower()
    trip_type = "roundtrip" if trip_type in ("roundtrip", "round_trip", "rt") else "oneway"

    d1 = parse_date_only(depart_date)
    d2 = parse_date_only(return_date) if return_date else None

    if dep not in AIRPORTS:
        return {"error": f"Unknown departure airport: {dep}"}
    if dest not in AIRPORTS:
        return {"error": f"Unknown destination airport: {dest}"}
    if not d1:
        return {"error": "depart_date must be YYYY-MM-DD or today/tomorrow"}

    return estimate_trip(dep, dest, trip_type, d1, d2)


def _tool_search_airports(query: str):
    choices = find_airports(query or "", limit=8)
    return [{"icao": c["icao"], "label": pretty_airport(c)} for c in choices]


def _clean_text(s: str) -> str:
    """Strip common markdown-ish formatting so it reads more like normal chat."""
    if not s:
        return ""
    s = s.replace("**", "").replace("__", "")
    # Remove leading bullet markers
    s = re.sub(r"(?m)^\s*[-*]\s+", "", s)
    # Collapse extra blank lines
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


@app.post("/api/chat")
def api_chat():
    data = request.get_json(silent=True) or {}
    msg = (data.get("message") or "").strip()

    if not msg:
        return jsonify({"reply": "Tell me something like: how much to fly from KCAK to KSRQ tomorrow?"}), 200

    # If key/library missing, still return something useful (and conversational)
    if not _has_openai():
        codes = re.findall(r"\bK[A-Z0-9]{3}\b", msg.upper())
        if len(codes) < 2:
            return jsonify({"reply": "Give me two airport codes like KCAK and KSRQ, plus a date (today/tomorrow works)."}), 200

        dep, dest = codes[0], codes[1]
        trip_type = "roundtrip" if "round" in msg.lower() else "oneway"
        d = parse_date_only(msg) or (date.today() + timedelta(days=1))

        est = estimate_trip(dep, dest, trip_type, d, None)
        typical_total = float(est["totals"]["typical"]["costs"]["total"])
        typical_time = est["totals"]["typical"]["block_time"]

        reply = f"For {dep} to {dest} on {d.isoformat()}, you’re roughly looking at {money(typical_total)} and about {typical_time} block time (typical case)."
        return jsonify({"reply": reply, "estimation": est}), 200

    # OpenAI tool-calling
    client = OpenAI()
    model = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")

    tools = [
        {
            "type": "function",
            "function": {
                "name": "estimate_trip",
                "description": "Estimate TBM trip costs/time given airports + dates.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dep": {"type": "string", "description": "Departure ICAO like KCAK"},
                        "dest": {"type": "string", "description": "Destination ICAO like KSRQ"},
                        "trip_type": {"type": "string", "enum": ["oneway", "roundtrip"]},
                        "depart_date": {"type": "string", "description": "YYYY-MM-DD or today/tomorrow"},
                        "return_date": {"type": ["string", "null"], "description": "YYYY-MM-DD or today/tomorrow or null"},
                    },
                    "required": ["dep", "dest", "trip_type", "depart_date"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_airports",
                "description": "Search airports by text (city/name/code). Returns a short list of options.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
    ]

    system = (
        f"You are the chat assistant inside {APP_NAME}.\n"
        "Talk like ChatGPT: natural, friendly, plain text.\n"
        "Avoid markdown, bullet lists, and heavy formatting.\n"
        "When the user asks about cost/time for a trip, call estimate_trip.\n"
        "If the user gives a city/name instead of ICAO codes, call search_airports and then ask which ICAO they mean.\n"
        "Keep answers short unless the user asks for details.\n"
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": msg},
    ]

    try:
        for _ in range(3):
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )

            m = completion.choices[0].message

            # Normal assistant response (no tool calls)
            if not getattr(m, "tool_calls", None):
                reply = _clean_text((m.content or "").strip())
                if not reply:
                    reply = "I can help—what are the two ICAO airports (like KCAK and KSRQ) and what day are you flying?"
                return jsonify({"reply": reply}), 200

            # Append the assistant tool call message
            messages.append(
                {
                    "role": "assistant",
                    "content": m.content or "",
                    "tool_calls": [tc.model_dump() for tc in m.tool_calls],
                }
            )

            # Execute tools
            for tc in m.tool_calls:
                name = tc.function.name
                args_str = tc.function.arguments or "{}"
                try:
                    args = json.loads(args_str)
                except Exception:
                    args = {}

                if name == "estimate_trip":
                    result = _tool_estimate_trip(
                        dep=args.get("dep", ""),
                        dest=args.get("dest", ""),
                        trip_type=args.get("trip_type", "oneway"),
                        depart_date=args.get("depart_date", ""),
                        return_date=args.get("return_date"),
                    )
                elif name == "search_airports":
                    result = _tool_search_airports(query=args.get("query", ""))
                else:
                    result = {"error": f"Unknown tool: {name}"}

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    }
                )

        return jsonify({"reply": "I got stuck—try again with two ICAO codes and a date (today/tomorrow works)."}), 200

    except Exception as e:
        # IMPORTANT: return JSON so your UI doesn't choke
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5050, use_reloader=False)
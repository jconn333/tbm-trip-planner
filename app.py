from __future__ import annotations

from flask import Flask, request, render_template, jsonify
import airportsdata
from math import radians, sin, cos, asin, sqrt, atan2, degrees
from datetime import datetime
import urllib.request
import urllib.parse
import re
import ssl
import certifi

app = Flask(__name__)

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
WINDS_LEVEL = "low"       # includes 24000/30000/etc in this API format
WINDS_LAYOUT = "off"      # raw text
ALT_TARGET_FT = 27000     # we interpolate between 24000 and 30000

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
def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).upper()

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

US_STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC"
}
US_STATE_CODES = set(US_STATE_NAME_TO_CODE.values())

def parse_city_region(raw: str):
    s = (raw or "").strip()
    if not s:
        return ("", None)
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) >= 2:
        city = parts[0]
        region = parts[1]
    else:
        tokens = s.split()
        if len(tokens) >= 2:
            city = " ".join(tokens[:-1])
            region = tokens[-1]
        else:
            return (s, None)

    r = region.strip().lower()
    if r in US_STATE_NAME_TO_CODE:
        return (city.strip(), US_STATE_NAME_TO_CODE[r])
    r2 = region.strip().upper()
    if len(r2) == 2 and r2 in US_STATE_CODES:
        return (city.strip(), r2)
    return (s, None)

def find_airports(query: str, limit: int = 12) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []

    q_up = normalize(q)

    # ICAO
    if len(q_up) == 4 and q_up in AIRPORTS:
        return [get_airport_by_icao(q_up)]

    # IATA exact
    if len(q_up) == 3:
        hits = []
        for icao, a in AIRPORTS.items():
            if (a.get("iata") or "").upper() == q_up:
                hits.append(airport_to_choice(icao, a))
        if hits:
            return hits[:limit]

    city_query, st = parse_city_region(q)
    q_low = city_query.lower().strip()

    found = []
    for icao, a in AIRPORTS.items():
        if (a.get("country") or "").upper() != "US":
            continue
        name = (a.get("name") or "").lower()
        city = (a.get("city") or "").lower()
        if q_low and (q_low in name or q_low in city):
            if st:
                region = (a.get("region") or "").upper()  # often US-XX
                if region.startswith("US-") and not region.endswith(st):
                    continue
            found.append(airport_to_choice(icao, a))

    found.sort(key=lambda c: (0 if c["iata"] else 1, c["icao"]))
    return found[:limit]

# ------------------ Winds aloft (FD) ------------------
def choose_fcst_hours(depart_dt_local: str | None) -> int:
    """
    Accepts either:
      - 'YYYY-MM-DD' (date-only)
      - 'YYYY-MM-DDTHH:MM' (datetime-local)
    For date-only, assume local noon so it's not treated as "already in the past".
    """
    if not depart_dt_local:
        return 12
    try:
        s = depart_dt_local.strip()

        # Date-only -> assume 12:00 local
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            dt = datetime.fromisoformat(s + "T12:00")
        else:
            dt = datetime.fromisoformat(s)

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
    req = urllib.request.Request(url, headers={"User-Agent": "tbm-trip-planner/1.1"})
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

    # 51-86 indicates 100+ kt
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
    """
    Returns (component_kt, station, from_deg, speed_kt)
    component_kt: + tailwind, - headwind along course_deg
    """
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

    # from-direction/speed -> "toward" u/v (east/north)
    def to_uv(dir_from_deg: float, speed: float):
        th = radians(dir_from_deg)
        u = -speed * sin(th)  # east (toward)
        v = -speed * cos(th)  # north (toward)
        return u, v

    u24, v24 = to_uv(d24, s24)
    u30, v30 = to_uv(d30, s30)

    # interpolate to FL270 (midway between 24k and 30k)
    u27 = (u24 + u30) / 2.0
    v27 = (v24 + v30) / 2.0

    wt_e = u27
    wt_n = v27

    br = radians(course_deg)
    crs_e = sin(br)
    crs_n = cos(br)

    component = wt_e * crs_e + wt_n * crs_n

    speed = sqrt(wt_e * wt_e + wt_n * wt_n)
    toward_deg = (degrees(atan2(wt_e, wt_n)) + 360) % 360
    from_deg = (toward_deg + 180) % 360

    return (component, chosen, from_deg, speed)

def wind_component_fl270_kts(dep: dict, dest: dict, depart_dt_local: str | None):
    """
    Returns (component_kt, details_str)
    component_kt positive = tailwind, negative = headwind
    """
    lat1, lon1 = float(dep["lat"]), float(dep["lon"])
    lat2, lon2 = float(dest["lat"]), float(dest["lon"])

    fcst = choose_fcst_hours(depart_dt_local)
    raw = fetch_windtemp_text(fcst)
    alts, station_map = build_windtemp_index(raw)

    if 24000 not in alts or 30000 not in alts:
        return (0.0, f"FD table missing 24000/30000 columns (fcst {fcst}h); using 0 kt.")

    course = initial_bearing_deg(lat1, lon1, lat2, lon2)

    # sample dep/mid/dest and average
    dep_lat, dep_lon = lat1, lon1
    mid_lat, mid_lon = (lat1 + lat2) / 2.0, (lon1 + lon2) / 2.0
    dest_lat, dest_lon = lat2, lon2

    c1, s1, f1, sp1 = wind_component_at_point_fl270(dep_lat, dep_lon, course, alts, station_map)
    c2, s2, f2, sp2 = wind_component_at_point_fl270(mid_lat, mid_lon, course, alts, station_map)
    c3, s3, f3, sp3 = wind_component_at_point_fl270(dest_lat, dest_lon, course, alts, station_map)

    comps = [c for c in (c1, c2, c3) if c is not None]
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

# ------------------ Costing helpers ------------------
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
    return hours, mgmt, maint, eng, fuel_gal, fuel, total

def compute_leg(dep: dict, dest: dict, depart_dt_local: str | None):
    dist_nm = haversine_nm(float(dep["lat"]), float(dep["lon"]), float(dest["lat"]), float(dest["lon"]))
    wind_component, wind_details = wind_component_fl270_kts(dep, dest, depart_dt_local)

    # Typical
    dist_typ = dist_nm * ROUTING_TYPICAL
    gs_typ = max(60, TAS_TYPICAL + wind_component)
    cruise_min_typ = int(round((dist_typ / gs_typ) * 60))
    total_min_typ = cruise_min_typ + OVERHEAD_TYPICAL_MIN
    _, mg_t, ma_t, en_t, _, fu_t, tot_t = costs_for_minutes(total_min_typ)

    # Conservative
    dist_con = dist_nm * ROUTING_CONSERVATIVE
    gs_con = max(60, TAS_CONSERVATIVE + wind_component)
    cruise_min_con = int(round((dist_con / gs_con) * 60))
    total_min_con = cruise_min_con + OVERHEAD_CONSERVATIVE_MIN
    _, mg_c, ma_c, en_c, _, fu_c, tot_c = costs_for_minutes(total_min_con)

    wind_sign = "+" if wind_component >= 0 else "−"
    wind_abs = abs(int(round(wind_component)))

    return {
        "from_pretty": pretty_airport(dep),
        "to_pretty": pretty_airport(dest),
        "dist_nm": int(round(dist_nm)),
        "wind_sign": wind_sign,
        "wind_abs": wind_abs,
        "wind_details": wind_details,
        "typ": {
            "gs": int(round(gs_typ)),
            "time": format_hhmm(total_min_typ),
            "minutes": total_min_typ,
            "cost_raw": float(tot_t),
            "cost": money(tot_t),
            "mgmt": money(mg_t),
            "maint": money(ma_t),
            "eng": money(en_t),
            "fuel": money(fu_t),
        },
        "con": {
            "gs": int(round(gs_con)),
            "time": format_hhmm(total_min_con),
            "minutes": total_min_con,
            "cost_raw": float(tot_c),
            "cost": money(tot_c),
            "mgmt": money(mg_c),
            "maint": money(ma_c),
            "eng": money(en_c),
            "fuel": money(fu_c),
        },
        "dep_lat": float(dep["lat"]),
        "dep_lon": float(dep["lon"]),
        "dest_lat": float(dest["lat"]),
        "dest_lon": float(dest["lon"]),
    }

# ------------------ API ------------------
@app.get("/api/airports")
def api_airports():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    choices = find_airports(q, limit=12)
    payload = [{"icao": c["icao"], "label": pretty_airport(c)} for c in choices]
    return jsonify(payload)

# ------------------ UI ------------------
@app.get("/")
def home():
    return render_template("home.html")

def _resolve_selected_airport(icao_from_hidden: str, fallback_query: str) -> dict | None:
    icao = (icao_from_hidden or "").strip().upper()
    if icao and len(icao) == 4 and icao in AIRPORTS:
        return get_airport_by_icao(icao)

    # fallback: try to resolve from text (ICAO/IATA/city)
    hits = find_airports(fallback_query or "", limit=1)
    if hits:
        return hits[0]
    return None

@app.post("/start")
@app.post("/estimate")  # optional alias
def start():
    trip_type = (request.form.get("trip_type") or "oneway").strip().lower()
    is_roundtrip = trip_type == "roundtrip"

    dep = _resolve_selected_airport(request.form.get("dep_icao", ""), request.form.get("dep_query", ""))
    dest = _resolve_selected_airport(request.form.get("dest_icao", ""), request.form.get("dest_query", ""))

    if not dep or not dest:
        return '<p>Could not resolve airports. <a href="/">Start over</a></p>'

    depart_dt = (request.form.get("depart_dt") or "").strip() or None
    return_dt = (request.form.get("return_dt") or "").strip() or None

    legs = []
    legs.append(compute_leg(dep, dest, depart_dt))

    if is_roundtrip:
        legs.append(compute_leg(dest, dep, return_dt))

    total_typ_minutes = sum(leg["typ"]["minutes"] for leg in legs)
    total_con_minutes = sum(leg["con"]["minutes"] for leg in legs)

    total_typ_cost_raw = sum(leg["typ"]["cost_raw"] for leg in legs)
    total_con_cost_raw = sum(leg["con"]["cost_raw"] for leg in legs)

    trip_label = "Round trip" if is_roundtrip else "One way"

    # Map: show the main city-pair (dep <-> dest)
    return render_template(
        "estimate.html",
        trip_label=trip_label,
        legs=legs,

        total_typ_time=format_hhmm(int(round(total_typ_minutes))),
        total_con_time=format_hhmm(int(round(total_con_minutes))),
        total_typ_cost=money(total_typ_cost_raw),
        total_con_cost=money(total_con_cost_raw),

        dep_lat=legs[0]["dep_lat"],
        dep_lon=legs[0]["dep_lon"],
        dest_lat=legs[0]["dest_lat"],
        dest_lon=legs[0]["dest_lon"],
    )

if __name__ == "__main__":
    app.run(debug=True, port=5050, use_reloader=False)
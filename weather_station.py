#!/usr/bin/env python3
"""
Weather Station - Stability Edition
"""

import pygame
import sys
import os
import json
import time
import threading
import math
import hashlib
import urllib.request
import urllib.error
import socket
import subprocess
import random
import re
import io
from datetime import datetime


from configparser import ConfigParser
from pathlib import Path

config_path = Path("~/ws-settings.ini").expanduser()

if not config_path.exists():
    raise FileNotFoundError(f"Config file not found at: {config_path}")


config = ConfigParser()
config.read(config_path)

LATITUDE = config.getfloat("LOCATION", "LATITUDE")
LONGITUDE = config.getfloat("LOCATION", "LONGITUDE")

MAP_LAT = config.getfloat("LOCATION", "MAP_LAT", fallback=LATITUDE)
MAP_LONG = config.getfloat("LOCATION", "MAP_LONG", fallback=LONGITUDE)

LOCATION_NAME = config.get("LOCATION", "NAME")

FEATURES = {
    "OSV_COUNT":       config.getboolean("FEATURES", "OSV_COUNT",       fallback=False),
    "OSV_TIDES":       config.getboolean("FEATURES", "OSV_TIDES",       fallback=False),
    "RAM_PRICE":       config.getboolean("FEATURES", "RAM_PRICE",       fallback=False),
    "HOURLY_FORECAST": config.getboolean("FEATURES", "HOURLY_FORECAST", fallback=True),
    "HOURLY_PRECIP":   config.getboolean(
        "FEATURES",
        "HOURLY_PRECIP",
        fallback=config.getboolean("FEATURES", "HOURLY_FORECAST", fallback=True),
    ),
    "SUNTIME":         config.getboolean("FEATURES", "SUNTIME",         fallback=True),
    "POTOMAC":         config.getboolean("FEATURES", "POTOMAC",         fallback=False),
    "NFLSTATS":        config.getboolean("FEATURES", "NFLSTATS",        fallback=False),
}

NFL_API_KEY = config["NFLSTATS"].get("API-KEY", "").strip() if config.has_section("NFLSTATS") else ""

SCREEN_W, SCREEN_H = 1920, 1080

MAP_ZOOM = 7  # zoom level for both basemap and radar tiles (RainViewer max = 7)
SELF_UPDATE_INTERVAL_S = config.getint("GENERAL", "SELF_UPDATE_INTERVAL_S", fallback=6 * 3600)
SELF_UPDATE_TIMEOUT_S = 120

def _loc_pixel_offset(lat, lon, zoom):
    """Sub-tile pixel offset of exact location from center of its tile at given zoom."""
    n = 2 ** zoom
    lat_rad = math.radians(lat)
    xt = int((lon + 180.0) / 360.0 * n)
    yt = int((1.0 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2.0 * n)
    fx = (lon + 180.0) / 360.0 * n - xt
    fy = (1.0 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2.0 * n - yt
    return int(fx * 256 - 128), int(fy * 256 - 128)

def _tile_xy(lat, lon, zoom):
    """Return the integer Web-Mercator tile x/y containing a location."""
    n = 2 ** zoom
    lat_rad = math.radians(lat)
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y

LOC_DOT_OFFSET = (0,0)  # computed dynamically

USER_AGENT = "FrederickWeatherStation/1.8 (RPi3B+; Dashboard)"
HEADERS = {"User-Agent": USER_AGENT}

_NWS_ENDPOINTS_TTL = 3600
_NWS_ENDPOINTS_LOCK = threading.Lock()
_NWS_ENDPOINTS_CACHE = {
    "forecast": None,
    "observations": None,
    "timezone": None,
    "expires_at": 0,
}
TILE_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "weather_station_tiles")
os.makedirs(TILE_CACHE_DIR, exist_ok=True)
RAM_HISTORY_FILE = os.path.join(TILE_CACHE_DIR, "ram_price_history.json")

# ── Colors ────────────────────────────────────────────────────────────────────
BG, PANEL = (10, 14, 26), (18, 24, 42)
ACCENT, GOLD, RAIN = (64, 196, 255), (255, 200, 80), (60, 140, 255)
TEMP_NEUTRAL = (200, 200, 210)
TEXT_DIM, TEXT_BRIGHT = (110, 130, 170), (255, 255, 255)
GREEN, RED = (60, 220, 100), (220, 70, 70)

# Icons: only codepoints confirmed in DejaVu Sans on Raspberry Pi OS
# U+2600 ☀  U+2601 ☁  U+2602 ☂  U+2603 ☃  — always present
WMO_ICON_TYPE = {
    0:"sun",       1:"sun",       2:"cloud",     3:"cloud",
    45:"cloud",    48:"cloud",
    51:"rain",     53:"rain",     55:"rain",
    61:"rain",     63:"rain",     65:"rain",
    71:"snow",     73:"snow",     75:"snow",     77:"snow",
    80:"rain",     81:"rain",     82:"rain",
    85:"snow",     86:"snow",
    95:"thunder",  96:"thunder",  99:"thunder",
}
WMO_DESC = {
    0:"Clear sky",       1:"Mainly clear",    2:"Partly cloudy",   3:"Overcast",
    45:"Fog",            48:"Icy fog",
    51:"Light drizzle",  53:"Drizzle",         55:"Heavy drizzle",
    61:"Light rain",     63:"Rain",            65:"Heavy rain",
    71:"Light snow",     73:"Snow",            75:"Heavy snow",     77:"Snow grains",
    80:"Showers",        81:"Showers",         82:"Heavy showers",
    85:"Snow showers",   86:"Heavy snow showers",
    95:"Thunderstorm",   96:"T-storm w/ hail", 99:"Heavy t-storm",
}

# ── Data Core ─────────────────────────────────────────────────────────────────
WIND_DIR = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]

def _wind_direction(deg):
    return WIND_DIR[round(deg / 22.5) % 16]

def _cloud_circles(size):
    """Return list of (ox, oy, r) puff definitions for a cloud of given size."""
    s = size
    return [
        ( 0.00*s, -0.08*s,  0.32*s),   # top center — tallest puff
        (-0.28*s,  0.04*s,  0.24*s),   # top left
        ( 0.28*s,  0.04*s,  0.24*s),   # top right
        (-0.42*s,  0.22*s,  0.18*s),   # bottom left edge
        ( 0.42*s,  0.22*s,  0.18*s),   # bottom right edge
        (-0.14*s,  0.18*s,  0.22*s),   # bottom left center
        ( 0.14*s,  0.18*s,  0.22*s),   # bottom right center
    ]

def _draw_cloud_on(surf, cx, cy, size, color):
    """Draw a smooth filled cloud onto surf at (cx,cy)."""
    for ox, oy, r in _cloud_circles(size):
        pygame.draw.circle(surf, color, (int(cx+ox), int(cy+oy)), int(r))

def draw_weather_icon(surf, code, cx, cy, size, color):
    """Draw a detailed weather icon as pygame primitives.
    cx, cy = center; size = nominal half-width of bounding box."""
    kind  = WMO_ICON_TYPE.get(code, "sun")
    lw    = max(2, size // 20)
    small = size < 60

    # Render onto a 2× SRCALPHA surface then scale down for smooth edges
    scale = 2
    S     = size * scale
    pad   = S + 10
    tmp   = pygame.Surface((pad*2, pad*2), pygame.SRCALPHA)
    tc    = (pad, pad)   # center on tmp surface

    if kind == "sun":
        core_r  = int(S * 0.30)
        inner_r = core_r + max(3, S // 12)
        outer_r = core_r + int(S * 0.32)
        hw      = max(3, S // 18)
        # Rays first so core covers ray bases
        for i in range(8):
            angle = math.radians(i * 45)
            perp  = angle + math.pi / 2
            bx = tc[0] + int(math.cos(angle) * inner_r)
            by = tc[1] + int(math.sin(angle) * inner_r)
            tx = tc[0] + int(math.cos(angle) * outer_r)
            ty = tc[1] + int(math.sin(angle) * outer_r)
            p1 = (bx + int(math.cos(perp)*hw), by + int(math.sin(perp)*hw))
            p2 = (bx - int(math.cos(perp)*hw), by - int(math.sin(perp)*hw))
            pygame.draw.polygon(tmp, color, [p1, p2, (tx, ty)])
        pygame.draw.circle(tmp, color, tc, core_r)

    elif kind == "cloud":
        _draw_cloud_on(tmp, tc[0], tc[1], S * 0.90, color)

    elif kind == "rain":
        cloud_cy = tc[1] - int(S * 0.10)
        _draw_cloud_on(tmp, tc[0], cloud_cy, S * 0.78, color)
        # Rain drops: staggered rows, angled
        cols     = 3 if small else 5
        spacingx = int(S * 0.22)
        start_x  = tc[0] - spacingx * (cols // 2)
        drop_top = tc[1] + int(S * 0.22)
        drop_len = int(S * 0.28)
        slant    = int(S * 0.08)
        droplw   = max(2, lw * scale - 1)
        for i in range(cols):
            x  = start_x + i * spacingx
            yo = int(S * 0.13) if i % 2 == 1 else 0
            pygame.draw.line(tmp, color,
                             (x,         drop_top + yo),
                             (x + slant, drop_top + yo + drop_len), droplw)

    elif kind == "snow":
        cloud_cy = tc[1] - int(S * 0.10)
        _draw_cloud_on(tmp, tc[0], cloud_cy, S * 0.78, color)
        # Proper snowflake
        fx, fy   = tc[0], tc[1] + int(S * 0.30)
        arm      = int(S * 0.22)
        tick     = int(S * 0.09)
        flw      = max(2, lw * scale - 1)
        for deg in range(0, 360, 60):
            rad  = math.radians(deg)
            ex   = fx + int(math.cos(rad) * arm)
            ey   = fy + int(math.sin(rad) * arm)
            pygame.draw.line(tmp, color, (fx, fy), (ex, ey), flw)
            if not small:
                for frac in (0.45, 0.75):
                    tx   = fx + int(math.cos(rad) * arm * frac)
                    ty   = fy + int(math.sin(rad) * arm * frac)
                    perp = rad + math.pi / 2
                    pygame.draw.line(tmp, color,
                        (int(tx - math.cos(perp)*tick), int(ty - math.sin(perp)*tick)),
                        (int(tx + math.cos(perp)*tick), int(ty + math.sin(perp)*tick)), flw)

    elif kind == "thunder":
        cloud_cy = tc[1] - int(S * 0.12)
        _draw_cloud_on(tmp, tc[0], cloud_cy, S * 0.76, color)
        # Lightning bolt
        bx, by = tc[0] + int(S*0.04), tc[1] + int(S*0.08)
        bolt = [
            (bx + int(S*0.12), by),
            (bx - int(S*0.02), by + int(S*0.24)),
            (bx + int(S*0.07), by + int(S*0.24)),
            (bx - int(S*0.14), by + int(S*0.52)),
            (bx + int(S*0.01), by + int(S*0.26)),
            (bx - int(S*0.07), by + int(S*0.26)),
        ]
        pygame.draw.polygon(tmp, color, bolt)

    # Scale down 2× → smooth anti-aliased result
    out = pygame.transform.smoothscale(tmp, (pad, pad))
    surf.blit(out, (cx - pad//2, cy - pad//2))

def _fetch_quote_pool():
    """Fetch a batch of inspirational quotes from zenquotes.io.
    Returns a list of 'quote — author' strings, or empty list on failure."""
    try:
        raw = safe_fetch("https://zenquotes.io/api/quotes", timeout=30)
        if raw and raw != "RATE_LIMITED":
            quotes = json.loads(raw.decode())
            result = [f"{q['q'].strip()} — {q['a']}" for q in quotes if q.get("q") and q.get("a")]
            if result:
                return result
    except Exception as e:
        print(f"Quote fetch error: {e}")
    return []


def _is_timeout_like_error(err):
    reason = getattr(err, "reason", err)
    if isinstance(reason, socket.timeout):
        return True
    msg = str(reason).lower()
    return "timed out" in msg or "timeout" in msg


def safe_fetch(url, timeout=15, headers=None, retries=0, retry_delay=1.5):
    print("fetching: ", url)
    attempts = max(1, int(retries) + 1)
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=headers or HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("!!! Rate limited (429). Cooling down.")
                return "RATE_LIMITED"
            print(f"Fetch error for {url}: {e}")
            return None
        except urllib.error.URLError as e:
            if attempt < attempts and _is_timeout_like_error(e):
                delay = retry_delay * attempt
                print(f"Fetch timeout for {url} (attempt {attempt}/{attempts}), retrying in {delay:.1f}s")
                time.sleep(delay)
                continue
            print(f"Fetch error for {url}: {e}")
            return None
        except Exception as e:
            if attempt < attempts and _is_timeout_like_error(e):
                delay = retry_delay * attempt
                print(f"Fetch timeout for {url} (attempt {attempt}/{attempts}), retrying in {delay:.1f}s")
                time.sleep(delay)
                continue
            print(f"Fetch error for {url}: {e}")
            return None
    return None


def _resolve_nws_endpoints():
    """Resolve NWS forecast + observations URLs + timezone for configured LAT/LON."""
    now = time.time()
    with _NWS_ENDPOINTS_LOCK:
        if (
            _NWS_ENDPOINTS_CACHE["expires_at"] > now
            and _NWS_ENDPOINTS_CACHE["forecast"]
            and _NWS_ENDPOINTS_CACHE["observations"]
            and _NWS_ENDPOINTS_CACHE["timezone"]
        ):
            return (
                _NWS_ENDPOINTS_CACHE["forecast"],
                _NWS_ENDPOINTS_CACHE["observations"],
                _NWS_ENDPOINTS_CACHE["timezone"],
            )

    points_url = f"https://api.weather.gov/points/{LATITUDE:.4f},{LONGITUDE:.4f}"
    raw = safe_fetch(points_url, timeout=15, retries=2)
    if not raw or raw == "RATE_LIMITED":
        raise RuntimeError("Failed to resolve NWS points data for configured coordinates")

    props = json.loads(raw.decode()).get("properties", {})
    forecast_url = props.get("forecast")
    timezone = props.get("timeZone")
    stations_url = props.get("observationStations")
    if not forecast_url or not timezone or not stations_url:
        raise RuntimeError("NWS points response missing forecast, timeZone, or observationStations")

    stations_raw = safe_fetch(stations_url, timeout=15, retries=2)
    if not stations_raw or stations_raw == "RATE_LIMITED":
        raise RuntimeError("Failed to resolve NWS observation station list")

    stations_data = json.loads(stations_raw.decode())
    features = stations_data.get("features") or []
    if not features:
        raise RuntimeError("NWS observation station list is empty")

    observations_url = None
    for feature in features:
        station_props = feature.get("properties", {})
        station_id = station_props.get("stationIdentifier")
        station_url = station_props.get("@id")
        if station_id:
            observations_url = f"https://api.weather.gov/stations/{station_id}/observations?limit=24"
            break
        if station_url:
            observations_url = f"{station_url}/observations?limit=24"
            break
    if not observations_url:
        raise RuntimeError("No usable NWS observation station endpoint found")

    with _NWS_ENDPOINTS_LOCK:
        _NWS_ENDPOINTS_CACHE["forecast"] = forecast_url
        _NWS_ENDPOINTS_CACHE["observations"] = observations_url
        _NWS_ENDPOINTS_CACHE["timezone"] = timezone
        _NWS_ENDPOINTS_CACHE["expires_at"] = time.time() + _NWS_ENDPOINTS_TTL
        return (
            _NWS_ENDPOINTS_CACHE["forecast"],
            _NWS_ENDPOINTS_CACHE["observations"],
            _NWS_ENDPOINTS_CACHE["timezone"],
        )


def _fetch_nws_hilo(forecast_url, stations_url):
    """Return (hilo_dict, current_obs_temp_f) from NWS forecast + observations."""
    hilo = {}
    current_obs_temp_f = None
    try:
        # NWS 7-day forecast for days 1+
        raw = safe_fetch(forecast_url, timeout=15, retries=2)
        if raw and raw != "RATE_LIMITED":
            periods = json.loads(raw.decode())["properties"]["periods"]
            hi_by_date = {}
            lo_by_date = {}
            for p in periods:
                date = p["startTime"][:10]
                temp = p["temperature"]  # already in °F (NWS default)
                if p["isDaytime"]:
                    hi_by_date[date] = temp
                else:
                    lo_by_date[date] = temp
            all_dates = set(list(hi_by_date.keys()) + list(lo_by_date.keys()))
            for date in all_dates:
                hi = hi_by_date.get(date)
                lo = lo_by_date.get(date)
                if hi is not None or lo is not None:
                    hilo[date] = (hi, lo)
    except Exception as e:
        print(f"NWS forecast fetch error: {e}")

    try:
        # Current observed temp from nearest NWS station.
        # Use /latest to avoid stale ordering/caching issues in the /observations list.
        latest_url = stations_url.split("?", 1)[0].rstrip("/") + "/latest?require_qc=true"
        latest_raw = safe_fetch(latest_url, timeout=15, retries=2)
        if latest_raw and latest_raw != "RATE_LIMITED":
            latest_props = json.loads(latest_raw.decode()).get("properties", {})
            t_obj = latest_props.get("temperature") or {}
            t_c = t_obj.get("value")
            if t_c is not None:
                current_obs_temp_f = t_c * 9 / 5 + 32
    except Exception as e:
        print(f"NWS latest observation fetch error: {e}")

    try:
        # Today's observed high from nearest NWS observation station
        raw = safe_fetch(stations_url, timeout=15, retries=2)
        if raw and raw != "RATE_LIMITED":
            obs = json.loads(raw.decode())["features"]
            today = datetime.now().strftime("%Y-%m-%d")
            today_temps = []
            newest_ts = None
            newest_temp_f = None
            for o in obs:
                props = o.get("properties", {})
                ts = props.get("timestamp")
                t_obj = props.get("temperature") or {}
                t_c = t_obj.get("value")
                if ts and t_c is not None:
                    t_f = t_c * 9 / 5 + 32
                    if ts[:10] == today:
                        today_temps.append(t_f)

                    # Fallback in case /latest fails.
                    try:
                        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except Exception:
                        ts_dt = None
                    if ts_dt is not None and (newest_ts is None or ts_dt > newest_ts):
                        newest_ts = ts_dt
                        newest_temp_f = t_f

            if current_obs_temp_f is None and newest_temp_f is not None:
                current_obs_temp_f = newest_temp_f

            if today_temps:
                obs_hi = round(max(today_temps))
                existing = hilo.get(today, (None, None))
                # Use the greater of observed high and forecast high; keep forecast low
                forecast_hi = existing[0]
                hi = max(obs_hi, forecast_hi) if forecast_hi is not None else obs_hi
                hilo[today] = (hi, existing[1])
    except Exception as e:
        print(f"NWS observation fetch error: {e}")

    return hilo, current_obs_temp_f


def _fetch_nws_hourly_pop(forecast_url):
    """Return ({hourly: {time: [], precipitation_probability: []}}, source_label) from NWS hourly."""
    hourly_url = forecast_url.rstrip("/") + "/hourly"
    raw = safe_fetch(hourly_url, timeout=15, retries=2)
    if not raw or raw == "RATE_LIMITED":
        return {"hourly": {"time": [], "precipitation_probability": []}}, "NWS"

    periods = json.loads(raw.decode()).get("properties", {}).get("periods", [])
    times, pops = [], []
    for p in periods[:48]:
        start = p.get("startTime")
        pop_obj = p.get("probabilityOfPrecipitation") or {}
        pop_val = pop_obj.get("value")
        if not start:
            continue
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            times.append(dt.strftime("%Y-%m-%dT%H:00"))
        except Exception:
            continue
        pops.append(0 if pop_val is None else max(0, min(100, int(round(float(pop_val))))))
    return {"hourly": {"time": times, "precipitation_probability": pops}}, "NWS"


def _fetch_nws_hourly_temp(forecast_url):
    """Return ({hourly: {time: [], temperature_2m: []}}, source_label) from NWS hourly."""
    hourly_url = forecast_url.rstrip("/") + "/hourly"
    raw = safe_fetch(hourly_url, timeout=15, retries=2)
    if not raw or raw == "RATE_LIMITED":
        return {"hourly": {"time": [], "temperature_2m": []}}, "NWS"

    periods = json.loads(raw.decode()).get("properties", {}).get("periods", [])
    times, temps = [], []
    for p in periods[:48]:
        start = p.get("startTime")
        temp_f = p.get("temperature")
        if not start or temp_f is None:
            continue
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            times.append(dt.strftime("%Y-%m-%dT%H:00"))
            temps.append(float(temp_f))
        except Exception:
            continue
    return {"hourly": {"time": times, "temperature_2m": temps}}, "NWS"


def _load_ram_history():
    try:
        with open(RAM_HISTORY_FILE, "r") as f:
            return json.load(f).get("history", [])
    except Exception:
        return []

def _save_ram_history(history):
    try:
        with open(RAM_HISTORY_FILE, "w") as f:
            json.dump({"history": history}, f)
    except Exception as e:
        print(f"RAM history save error: {e}")

def _fetch_ddr5_price():
    """Scrape DRAM Exchange for DDR5 UDIMM 16GB 4800/5600 spot price and % change.
    Returns (price_float, pct_change_float) or (None, None) on failure."""
    raw = safe_fetch("https://www.dramexchange.com/", timeout=15)
    if not raw or raw == "RATE_LIMITED":
        return None, None
    try:
        html = raw.decode("utf-8", errors="replace")
        # Find the DDR5 UDIMM 16GB row and grab the avg price and % change
        m = re.search(
            r'DDR5 UDIMM 16GB[^<]*</a>.*?'
            r'<td[^>]*>([\d.]+)</td>\s*<td[^>]*>([\d.]+)</td>\s*'
            r'<td[^>]*>([\d.]+)</td>\s*<td[^>]*>([\d.]+)</td>\s*'
            r'<td[^>]*>([\d.]+)</td>.*?'
            r'([-+]?[\d.]+)\s*%',
            html, re.DOTALL
        )
        if m:
            avg_price = float(m.group(5))
            pct = float(m.group(6))
            return avg_price, pct
    except Exception as e:
        print(f"DDR5 parse error: {e}")
    return None, None


class AppState:
    def __init__(self):
        self.started_at = time.time()
        self.weather_loaded_once = False
        self.weather = None
        self.nws_hilo = {}              # {date_str: (hi_f, lo_f)} from NWS observed+forecast
        self.obs_temp_f = None          # nearest NWS station current temp in Fahrenheit
        self.nws_hourly_precip = None   # NWS hourly POP data mapped to open-meteo style hourly keys
        self.nws_hourly_temp = None     # NWS hourly temp data mapped to open-meteo style hourly keys
        self.map_tiles = {}
        self.map_label_tiles = {}
        self.radar_tiles = {}
        self.last_weather_upd = 0
        self.last_map_upd = 0
        self.last_ram_upd = 0
        self.backoff_until = 0
        self.motd = "Loading..."
        self.motd_pool = []
        self.motd_index = 0
        self.motd_last_cycle = 0
        self._quotes_running = False
        # RAM price state
        self.ram_price = None           # current price
        self.ram_pct = None             # % change from DRAM Exchange
        self.ram_delta_24h = None       # dollar diff vs ~24h ago
        self.ram_delta_7d = None        # dollar diff vs ~7d ago
        # Tide state
        self.tides = []              # list of {"t": "HH:MM", "v": float, "type": "H"/"L"} for today+tomorrow
        self.last_tide_upd = 0
        self._tide_running = False
        # OSV state
        self.osv_count = None        # int
        self.osv_max = 145           # int
        self.osv_status = None       # str
        self.osv_reported_at = None  # datetime or None
        self.last_osv_upd = 0
        self._osv_running = False
        # Potomac River state
        self.potomac_level    = []   # list of (epoch_float, feet_float)
        self.potomac_temp     = []   # list of (epoch_float, celsius_float)
        self.last_potomac_upd = 0
        self._potomac_running = False
        # NFL standings state
        self.nfl_nfc_north    = []    # list of standings rows for NFC North
        self.nfl_season       = None
        self.last_nfl_upd     = 0
        self._nfl_running     = False
        self.lock = threading.Lock()
        self._weather_running = False
        self._map_running = False
        self._ram_running = False
        self.runtime_error_source = None
        self.runtime_error_message = None
        self.runtime_error_at = 0

    def set_runtime_error(self, source, message):
        msg = str(message).strip() or "Unknown error"
        if len(msg) > 240:
            msg = msg[:237] + "..."
        with self.lock:
            self.runtime_error_source = source
            self.runtime_error_message = msg
            self.runtime_error_at = time.time()

    def clear_runtime_error(self, source=None):
        with self.lock:
            if source is None or self.runtime_error_source == source:
                self.runtime_error_source = None
                self.runtime_error_message = None
                self.runtime_error_at = 0

    # ── Weather ───────────────────────────────────────────────────────────────
    def update_weather(self):
        if self._weather_running:
            return
        self._weather_running = True
        try:
            self._do_update_weather()
        finally:
            self._weather_running = False

    def _do_update_weather(self):
        if time.time() < self.backoff_until:
            return
        try:
            forecast_url, stations_url, timezone = _resolve_nws_endpoints()
        except Exception as e:
            self.set_runtime_error("WEATHER", f"NWS endpoint resolve failed: {e}")
            self.last_weather_upd = 0
            return
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}"
               f"&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,sunrise,sunset"
               f"&hourly=temperature_2m,relative_humidity_2m,precipitation_probability"
               f"&current=temperature_2m,weather_code,wind_speed_10m,wind_direction_10m"
               f"&temperature_unit=fahrenheit&timezone={timezone}&forecast_days=7")
        raw = safe_fetch(url, timeout=20, retries=3, retry_delay=2.0)
        if raw == "RATE_LIMITED":
            self.backoff_until = time.time() + 1800
            self.set_runtime_error("WEATHER", "Open-Meteo rate limited; retrying in 30 minutes")
            self.last_weather_upd = time.time()
            return
        if raw:
            try:
                data = json.loads(raw.decode())
                nws_hilo, obs_temp_f = _fetch_nws_hilo(forecast_url, stations_url)
                nws_hourly_precip, nws_hourly_source = _fetch_nws_hourly_pop(forecast_url)
                nws_hourly_temp, nws_hourly_temp_source = _fetch_nws_hourly_temp(forecast_url)
                with self.lock:
                    self.weather = data
                    self.nws_hilo = nws_hilo
                    self.obs_temp_f = obs_temp_f
                    self.nws_hourly_precip = nws_hourly_precip
                    self.nws_hourly_source = nws_hourly_source
                    self.nws_hourly_temp = nws_hourly_temp
                    self.nws_hourly_temp_source = nws_hourly_temp_source
                    self.weather_loaded_once = True
                    self.last_weather_upd = time.time()
                self.clear_runtime_error("WEATHER")
            except Exception as e:
                print(f"Weather parse error: {e}")
                self.set_runtime_error("WEATHER", f"Weather parse failed: {e}")
                self.last_weather_upd = time.time()
        else:
            self.set_runtime_error("WEATHER", "Open-Meteo weather fetch failed")
            self.last_weather_upd = time.time()

    # ── Map + Radar ───────────────────────────────────────────────────────────
    def update_map(self):
        if self._map_running:
            return
        self._map_running = True
        try:
            self._do_update_map()
        finally:
            self._map_running = False

    def _do_update_map(self):
        zoom = MAP_ZOOM  # 7
        lat_rad = math.radians(MAP_LAT)
        n = 2 ** zoom
        xt = int((MAP_LONG + 180.0) / 360.0 * n)
        yt = int((1.0 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2.0 * n)

        # 3×3 basemap tiles at zoom 7
        new_tiles = {}
        for dy in range(-1, 2):
            for dx in range(-1, 2):
                # ArcGIS World Imagery (Esri), using the standard XYZ tile scheme.
                url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{zoom}/{yt+dy}/{xt+dx}"
                fname = os.path.join(TILE_CACHE_DIR, hashlib.md5(url.encode()).hexdigest() + ".png")
                if os.path.exists(fname):
                    with open(fname, "rb") as f:
                        data = f.read()
                else:
                    data = safe_fetch(url, timeout=5)
                    if data and data != "RATE_LIMITED" and len(data) > 2000:
                        with open(fname, "wb") as f:
                            f.write(data)
                if data and data != "RATE_LIMITED":
                    new_tiles[(dx, dy)] = data

        # ArcGIS place-name/reference overlay. World Imagery intentionally has
        # no city labels, so draw Esri's transparent reference layer over it.
        # This supplies city/town names while leaving the aerial imagery visible.
        new_label_tiles = {}
        for dy in range(-1, 2):
            for dx in range(-1, 2):
                label_url = (
                    f"https://server.arcgisonline.com/ArcGIS/rest/services/"
                    f"Reference/World_Boundaries_and_Places/MapServer/tile/"
                    f"{zoom}/{yt+dy}/{xt+dx}"
                )
                label_fname = os.path.join(
                    TILE_CACHE_DIR, hashlib.md5(label_url.encode()).hexdigest() + ".png"
                )
                label_data = None
                if os.path.exists(label_fname):
                    try:
                        with open(label_fname, "rb") as f:
                            label_data = f.read()
                    except Exception:
                        label_data = None
                if not label_data:
                    label_data = safe_fetch(label_url, timeout=5)
                    if label_data and label_data != "RATE_LIMITED" and len(label_data) > 100:
                        try:
                            with open(label_fname, "wb") as f:
                                f.write(label_data)
                        except Exception:
                            pass
                if label_data and label_data != "RATE_LIMITED":
                    new_label_tiles[(dx, dy)] = label_data

        # Radar — fetch same 3×3 grid of zoom-7 tiles as basemap so all nearby
        # precipitation is visible, not just what falls on the single center tile.
        new_radar_tiles = {}
        meta = safe_fetch("https://api.rainviewer.com/public/weather-maps.json")
        if meta and meta != "RATE_LIMITED":
            try:
                rv = json.loads(meta.decode())
                path, host = rv["radar"]["past"][-1]["path"], rv["host"]
                for dy in range(-1, 2):
                    for dx in range(-1, 2):
                        radar_url = f"{host}{path}/256/{zoom}/{xt+dx}/{yt+dy}/6/1_1.png"
                        raw = safe_fetch(radar_url, timeout=10)
                        if raw and raw != "RATE_LIMITED":
                            if raw[:4] == b'\x89PNG':
                                new_radar_tiles[(dx, dy)] = raw
                            else:
                                print(f"Radar tile ({dx},{dy}) not PNG: {raw[:120]}")
                            # else: transparent/empty tile, skip silently
            except Exception as e:
                print(f"Radar fetch error: {e}")

        with self.lock:
            self.map_tiles = new_tiles
            self.map_label_tiles = new_label_tiles
            self.radar_tiles = new_radar_tiles
            self.last_map_upd = time.time()

    # ── RAM Price ─────────────────────────────────────────────────────────────
    def update_ram(self):
        if self._ram_running:
            return
        self._ram_running = True
        try:
            self._do_update_ram()
        finally:
            self._ram_running = False

    def _do_update_ram(self):
        price, pct = _fetch_ddr5_price()
        if price is None:
            print("RAM price fetch failed")
            return
        now = time.time()
        history = _load_ram_history()

        # Deduplicate by calendar date — keep only one entry per day (latest fetch)
        today_str = datetime.now().strftime("%Y-%m-%d")
        if history and datetime.fromtimestamp(history[-1]["ts"]).strftime("%Y-%m-%d") == today_str:
            history[-1] = {"ts": now, "price": price}
        else:
            history.append({"ts": now, "price": price})

        # Keep only last 10 days
        cutoff = now - 10 * 86400
        history = [e for e in history if e["ts"] >= cutoff]
        _save_ram_history(history)

        # Build date -> price map (latest entry per date wins)
        by_date = {}
        for e in history:
            d = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d")
            by_date[d] = e["price"]

        from datetime import date as _date, timedelta as _timedelta
        today = _date.today()
        yesterday = (today - _timedelta(days=1)).isoformat()
        week_ago  = (today - _timedelta(days=7)).isoformat()

        delta_24h = (price - by_date[yesterday]) if yesterday in by_date else None
        delta_7d  = (price - by_date[week_ago])  if week_ago  in by_date else None

        with self.lock:
            self.ram_price = price
            self.ram_pct = pct
            self.ram_delta_24h = delta_24h
            self.ram_delta_7d = delta_7d
            self.last_ram_upd = now

    # ── Quotes ────────────────────────────────────────────────────────────────
    def update_quotes(self):
        if self._quotes_running:
            return
        self._quotes_running = True
        try:
            import random
            pool = _fetch_quote_pool()
            if not pool:
                with self.lock:
                    self.motd_last_cycle = time.time()  # start retry clock even on failure
                return
            random.shuffle(pool)
            start = random.randrange(len(pool))
            with self.lock:
                self.motd_pool = pool
                self.motd_index = start
                self.motd = pool[start]
                self.motd_last_cycle = time.time()
        except Exception as e:
            print(f"Quote pool update error: {e}")
        finally:
            self._quotes_running = False

    # ── Tides ─────────────────────────────────────────────────────────────────
    def update_tides(self):
        if self._tide_running:
            return
        self._tide_running = True
        try:
            self._do_update_tides()
        finally:
            self._tide_running = False

    def _do_update_tides(self):
        from datetime import date as _date, timedelta as _td
        today    = _date.today().strftime("%Y%m%d")
        tomorrow = (_date.today() + _td(days=1)).strftime("%Y%m%d")
        url = (
            "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
            f"?station=8570283&product=predictions&datum=MLLW"
            f"&time_zone=lst_ldt&interval=hilo&units=english"
            f"&application=weather_station&format=json"
            f"&begin_date={today}&end_date={tomorrow}"
        )
        raw = safe_fetch(url, timeout=15)
        if not raw or raw == "RATE_LIMITED":
            return
        try:
            data = json.loads(raw.decode())
            preds = data.get("predictions", [])
            tides = []
            for p in preds:
                tides.append({"t": p["t"], "v": float(p["v"]), "type": p["type"]})
            with self.lock:
                self.tides = tides
                self.last_tide_upd = time.time()
        except Exception as e:
            print(f"Tide parse error: {e}")

    # ── OSV ───────────────────────────────────────────────────────────────────
    def update_osv(self):
        if self._osv_running:
            return
        self._osv_running = True
        try:
            self._do_update_osv()
        finally:
            self._osv_running = False

    def _do_update_osv(self):
        raw = safe_fetch("https://osvcount.com/", timeout=15)
        if not raw or raw == "RATE_LIMITED":
            return
        try:
            html = raw.decode("utf-8", errors="replace")
            m_count  = re.search(r'id="fn-expired"[^>]*>(\d+)<', html)
            m_max    = re.search(r'Vehicles on the beach[^/]*/(\d+)', html)
            m_status = re.search(r'(OSV (?:Open[^<]*|Closed))', html)
            m_utc    = re.search(r'data-utc="([^"]+)"', html)
            count  = int(m_count.group(1))  if m_count  else None
            maxv   = int(m_max.group(1))    if m_max    else 145
            status = m_status.group(1).strip() if m_status else None
            reported_at = None
            if m_utc:
                try:
                    from datetime import timezone as _tz
                    utc_dt = datetime.strptime(m_utc.group(1), "%Y-%m-%dT%H:%M:%SZ")
                    utc_dt = utc_dt.replace(tzinfo=_tz.utc)
                    reported_at = utc_dt.astimezone().replace(tzinfo=None)
                except Exception:
                    pass
            with self.lock:
                self.osv_count       = count
                self.osv_max         = maxv
                self.osv_status      = status
                self.osv_reported_at = reported_at
                self.last_osv_upd    = time.time()
        except Exception as e:
            print(f"OSV parse error: {e}")

    # ── Potomac River ────────────────────────────────────────────────────────
    def update_potomac(self):
        if self._potomac_running:
            return
        self._potomac_running = True
        try:
            self._do_update_potomac()
        finally:
            self._potomac_running = False

    def _do_update_potomac(self):
        url = (
            "https://waterservices.usgs.gov/nwis/iv/"
            "?sites=01646500&parameterCd=00065&period=P7D&format=json"
        )
        raw = safe_fetch(url, timeout=20)
        if not raw or raw == "RATE_LIMITED":
            return
        try:
            data    = json.loads(raw.decode())
            series  = data["value"]["timeSeries"]
            level_pts: list = []
            for ts in series:
                code     = ts["variable"]["variableCode"][0]["value"]
                no_data  = float(ts["values"][0].get("noDataValue", -999999))
                vals     = ts["values"][0]["value"]
                pts: list = []
                for v in vals:
                    raw_v = float(v["value"])
                    if raw_v == no_data:
                        continue
                    dt_str = re.sub(r"\.\d+", "", v["dateTime"])
                    try:
                        epoch = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S%z").timestamp()
                    except Exception:
                        continue
                    pts.append((epoch, raw_v))
                if code == "00065":
                    level_pts = pts
            with self.lock:
                self.potomac_level    = level_pts
                self.last_potomac_upd = time.time()
        except Exception as e:
            print(f"Potomac parse error: {e}")

    # ── NFL standings ────────────────────────────────────────────────────────
    def update_nflstats(self):
        if self._nfl_running:
            return
        self._nfl_running = True
        try:
            self._do_update_nflstats()
        finally:
            self._nfl_running = False

    def _do_update_nflstats(self):
        if not NFL_API_KEY:
            return
        now_dt = datetime.now()
        season = now_dt.year if now_dt.month >= 9 else now_dt.year - 1
        hdrs = {"Authorization": NFL_API_KEY, "User-Agent": USER_AGENT}
        teams_url = "https://api.balldontlie.io/nfl/v1/teams?conference=NFC&division=NORTH"
        all_games = []
        try:
            raw = safe_fetch(teams_url, timeout=20, headers=hdrs)
            if not raw or raw == "RATE_LIMITED":
                return
            teams_data = json.loads(raw.decode()).get("data", [])
            team_ids = [t.get("id") for t in teams_data if t.get("id")]
            if not team_ids:
                return

            team_qs = "&".join(f"team_ids[]={tid}" for tid in team_ids)
            url = f"https://api.balldontlie.io/nfl/v1/games?seasons[]={season}&postseason=false&per_page=100&{team_qs}"
            cursor = None
            while True:
                page_url = url if cursor is None else f"{url}&cursor={cursor}"
                raw = safe_fetch(page_url, timeout=20, headers=hdrs)
                if not raw or raw == "RATE_LIMITED":
                    break
                data = json.loads(raw.decode())
                all_games.extend(data.get("data", []))
                cursor = (data.get("meta") or {}).get("next_cursor")
                if not cursor:
                    break

            if not all_games:
                return

            teams = {}
            for game in all_games:
                if not str(game.get("status", "")).startswith("Final"):
                    continue
                home = game.get("home_team") or {}
                away = game.get("visitor_team") or {}
                for t in (home, away):
                    if t.get("conference") != "NFC" or t.get("division") != "NORTH":
                        continue
                    teams.setdefault(t.get("abbreviation"), {
                        "team": t,
                        "wins": 0,
                        "losses": 0,
                        "ties": 0,
                        "pf": 0,
                        "pa": 0,
                    })

                if home.get("conference") == "NFC" and home.get("division") == "NORTH" and away.get("conference") == "NFC" and away.get("division") == "NORTH":
                    home_score = int(game.get("home_team_score") or 0)
                    away_score = int(game.get("visitor_team_score") or 0)
                    for t, pf, pa in ((home, home_score, away_score), (away, away_score, home_score)):
                        row = teams[t.get("abbreviation")]
                        row["pf"] += pf
                        row["pa"] += pa
                    if home_score > away_score:
                        teams[home.get("abbreviation")]["wins"] += 1
                        teams[away.get("abbreviation")]["losses"] += 1
                    elif away_score > home_score:
                        teams[away.get("abbreviation")]["wins"] += 1
                        teams[home.get("abbreviation")]["losses"] += 1
                    else:
                        teams[home.get("abbreviation")]["ties"] += 1
                        teams[away.get("abbreviation")]["ties"] += 1
                else:
                    # Only count games against NFC North opponents.
                    for side, opp_side in ((home, away), (away, home)):
                        if side.get("conference") != "NFC" or side.get("division") != "NORTH":
                            continue
                        score = int(game.get("home_team_score") if side is home else game.get("visitor_team_score") or 0)
                        opp_score = int(game.get("visitor_team_score") if side is home else game.get("home_team_score") or 0)
                        row = teams[side.get("abbreviation")]
                        row["pf"] += score
                        row["pa"] += opp_score
                        if score > opp_score:
                            row["wins"] += 1
                        elif score < opp_score:
                            row["losses"] += 1
                        else:
                            row["ties"] += 1

            rows = []
            for row in teams.values():
                gp = row["wins"] + row["losses"] + row["ties"]
                row["overall_record"] = f"{row['wins']}-{row['losses']}-{row['ties']}"
                row["point_differential"] = row["pf"] - row["pa"]
                row["games_played"] = gp
                rows.append(row)

            rows.sort(key=lambda r: (-r["wins"], r["losses"], -r["point_differential"], r["team"]["full_name"]))
            with self.lock:
                self.nfl_nfc_north = rows
                self.nfl_season = season
                self.last_nfl_upd = time.time()
        except Exception as e:
            print(f"NFL standings parse error: {e}")

# ── UI Rendering ──────────────────────────────────────────────────────────────
def draw_text(surf, text, font, color, x, y, anchor="topleft"):
    img = font.render(str(text), True, color)
    surf.blit(img, img.get_rect(**{anchor: (x, y)}))


def draw_error_banner(screen, fonts, W, H, source, message):
    if not message:
        return
    label = f"{source}: {message}" if source else str(message)
    label = label.replace("\n", " ")
    if len(label) > 130:
        label = label[:127] + "..."

    box_h = 32
    box_w = min(W - 120, 980)
    box_x = (W - box_w) // 2
    box_y = H - 84
    rect = pygame.Rect(box_x, box_y, box_w, box_h)
    pygame.draw.rect(screen, (88, 20, 20), rect, border_radius=8)
    pygame.draw.rect(screen, RED, rect, 1, border_radius=8)
    draw_text(screen, label, fonts["tiny"], (255, 230, 230), box_x + 10, box_y + box_h // 2, anchor="midleft")


def _run_update_task(state, source, fn, clear_on_success=True):
    try:
        fn()
        if clear_on_success:
            state.clear_runtime_error(source)
    except Exception as e:
        print(f"{source} update crash: {e}")
        state.set_runtime_error(source, f"{type(e).__name__}: {e}")


def _git_head(repo_dir):
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return proc.stdout.strip()
    except Exception as e:
        print(f"Self-update: git rev-parse failed: {e}")
        return None


def _maybe_self_update_and_restart(repo_dir):
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return False

    before = _git_head(repo_dir)
    print("Self-update: running git pull")
    try:
        pull = subprocess.run(
            ["git", "pull"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=SELF_UPDATE_TIMEOUT_S,
        )
    except Exception as e:
        print(f"Self-update: git pull failed: {e}")
        return False

    stdout = (pull.stdout or "").strip()
    stderr = (pull.stderr or "").strip()
    if stdout:
        print(f"Self-update git pull stdout: {stdout}")
    if stderr:
        print(f"Self-update git pull stderr: {stderr}")
    if pull.returncode != 0:
        print(f"Self-update: git pull exited with status {pull.returncode}")
        return False

    after = _git_head(repo_dir)
    combined = f"{stdout}\n{stderr}"
    already_up_to_date = (
        "Already up to date." in combined
        or "Already up-to-date." in combined
    )
    if before and after:
        updated = before != after
    else:
        updated = not already_up_to_date

    if not updated:
        print("Self-update: no remote updates found")
        return False

    script_path = os.path.abspath(__file__)
    print("Self-update: updates found, launching fresh process")
    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        subprocess.Popen([sys.executable, "-u", script_path], cwd=repo_dir, env=env)
        return True
    except Exception as e:
        print(f"Self-update: failed to launch fresh process: {e}")
        return False


def draw_map(screen, state, fonts, mx, my, mw, mh):
    """Draw the map centered on MAP_LAT/MAP_LONG and the weather marker at LATITUDE/LONGITUDE."""
    if not state.map_tiles:
        return

    TILE = 256.0
    zoom = MAP_ZOOM
    n = 2 ** zoom

    # MAP_LAT/MAP_LONG define the exact geographic point at the center
    # of the displayed map.  They are used only by the map renderer.
    map_lat_rad = math.radians(MAP_LAT)
    map_world_x = (MAP_LONG + 180.0) / 360.0 * n * TILE
    map_world_y = (1.0 - math.log(
        math.tan(map_lat_rad) + 1 / math.cos(map_lat_rad)
    ) / math.pi) / 2.0 * n * TILE

    # The downloaded 3x3 grid is indexed relative to the tile containing
    # MAP_LAT/MAP_LONG.  Shift every tile by the fractional position of the
    # exact map center so that MAP_LAT/MAP_LONG is precisely at (384,384).
    center_xt = int(map_world_x // TILE)
    center_yt = int(map_world_y // TILE)

    tile_surf = pygame.Surface((768, 768), pygame.SRCALPHA)
    tile_ox = (mw - 768) // 2
    tile_oy = (mh - 768) // 2

    def tile_position(dx, dy):
        tx = center_xt + dx
        ty = center_yt + dy
        return (int(round(384.0 + tx * TILE - map_world_x)),
                int(round(384.0 + ty * TILE - map_world_y)))

    # Basemap: these tiles were selected using MAP_LAT/MAP_LONG.
    for (dx, dy), d in state.map_tiles.items():
        try:
            img = pygame.image.load(io.BytesIO(d)).convert_alpha()
            tile_surf.blit(img, tile_position(dx, dy))
        except Exception:
            pass

    # Radar uses the same geographic tile positions as the basemap.
    for (dx, dy), blob in state.radar_tiles.items():
        try:
            rd = pygame.image.load(io.BytesIO(blob)).convert_alpha()
            rd.set_alpha(180)
            tile_surf.blit(rd, tile_position(dx, dy))
        except Exception as e:
            print(f"Radar draw error: {e}")

    # ArcGIS city/place-name reference layer.
    for (dx, dy), blob in state.map_label_tiles.items():
        try:
            labels = pygame.image.load(io.BytesIO(blob)).convert_alpha()
            tile_surf.blit(labels, tile_position(dx, dy))
        except Exception as e:
            print(f"Map label draw error: {e}")

    # ------------------------------------------------------------------
    # WEATHER LOCATION -- LATITUDE/LONGITUDE ONLY
    # ------------------------------------------------------------------
    # Project the actual weather coordinates into the same world-pixel
    # coordinate system.  This does not use MAP_LAT or MAP_LONG.
    weather_lat_rad = math.radians(LATITUDE)
    weather_world_x = (LONGITUDE + 180.0) / 360.0 * n * TILE
    weather_world_y = (1.0 - math.log(
        math.tan(weather_lat_rad) + 1 / math.cos(weather_lat_rad)
    ) / math.pi) / 2.0 * n * TILE

    # Because MAP_LAT/MAP_LONG is the exact center of the map, the exact
    # weather coordinate is simply its world-pixel displacement from that
    # independent map center.
    dot_x = int(round(384.0 + weather_world_x - map_world_x))
    dot_y = int(round(384.0 + weather_world_y - map_world_y))

    pygame.draw.circle(tile_surf, (0, 0, 0), (dot_x, dot_y), 8)
    pygame.draw.circle(tile_surf, ACCENT, (dot_x, dot_y), 6)
    pygame.draw.circle(tile_surf, (255, 255, 255), (dot_x, dot_y), 2)

    # LOCATION_NAME is anchored to the exact LATITUDE/LONGITUDE marker.
    label_font = fonts["tiny"]
    label_img = label_font.render(LOCATION_NAME, True, TEXT_BRIGHT)
    label_pad_x, label_pad_y = 8, 4
    label_w, label_h = label_img.get_size()
    label_box_w = label_w + label_pad_x * 2
    label_box_h = label_h + label_pad_y * 2

    label_x = dot_x - label_box_w // 2
    label_y = dot_y - label_box_h - 12
    label_x = max(6, min(label_x, 768 - label_box_w - 6))
    label_y = max(6, min(label_y, 768 - label_box_h - 6))
    label_box = pygame.Rect(label_x, label_y, label_box_w, label_box_h)
    pygame.draw.rect(tile_surf, (12, 16, 28, 230), label_box, border_radius=6)
    pygame.draw.rect(tile_surf, ACCENT, label_box, 1, border_radius=6)
    tile_surf.blit(label_img, (label_box.x + label_pad_x, label_box.y + label_pad_y))

    # Rounded clipping mask.
    mask_surf = pygame.Surface((768, 768), pygame.SRCALPHA)
    mask_surf.fill((0, 0, 0, 0))
    pygame.draw.rect(mask_surf, (255, 255, 255, 255), (0, 0, 768, 768), border_radius=16)
    tile_surf.blit(mask_surf, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)

    screen.blit(tile_surf, (mx + tile_ox, my + tile_oy))


def draw_wind_widget(screen, state, fonts, cx, cy):
    """Compass rose with arrow pointing toward where wind is going.
    cx, cy = center of circle."""
    RADIUS = 52
    cur = state.weather["current"] if state.weather else None

    if cur is None:
        line1 = "Wind"
        line2 = None
        speed_mph = None
        direction = 0
    else:
        speed_kmh = cur.get("wind_speed_10m", 0)
        speed_mph = speed_kmh * 0.621371
        direction = cur.get("wind_direction_10m", 0)  # meteorological: wind coming FROM this bearing
        dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                "S","SSW","SW","WSW","W","WNW","NW","NNW"]
        compass = dirs[round(direction / 22.5) % 16]
        line1 = f"{round(speed_mph)} mph"
        line2 = f"from {compass}"

    line1_img = fonts["tiny"].render(line1, True, TEXT_BRIGHT if speed_mph is not None else TEXT_DIM)
    line2_img = fonts["tiny"].render(line2, True, TEXT_DIM) if line2 else None
    widget_w = max(
        RADIUS * 2 + 2,
        line1_img.get_width(),
        line2_img.get_width() if line2_img else 0,
    ) + 8
    if widget_w % 2:
        widget_w += 1
    widget_h = (RADIUS * 2) + 42

    ws = pygame.Surface((widget_w, widget_h), pygame.SRCALPHA)
    wcx = widget_w // 2
    wcy = RADIUS + 1

    pygame.draw.circle(ws, PANEL, (wcx, wcy), RADIUS)
    pygame.draw.circle(ws, TEXT_DIM, (wcx, wcy), RADIUS, 1)

    # Cardinal labels — placed just inside the rim
    for label, sx, sy in (("N", 0, -1), ("S", 0, 1), ("E", 1, 0), ("W", -1, 0)):
        lx = wcx + int((RADIUS - 12) * sx)
        ly = wcy + int((RADIUS - 12) * sy)
        draw_text(ws, label, fonts["tiny"], TEXT_DIM, lx, ly, anchor="center")

    if cur is not None:
        # Open-Meteo reports meteorological direction as the direction the wind
        # comes FROM.  The display arrow should point in the direction the wind
        # is going TO, exactly 180 degrees opposite.
        to_direction = (direction + 180.0) % 360.0
        from_rad = math.radians(to_direction)
        arrow_len = RADIUS - 22
        head_len  = 9

        tip_x  = wcx + int(arrow_len * math.sin(from_rad))
        tip_y  = wcy - int(arrow_len * math.cos(from_rad))
        tail_x = wcx - int(arrow_len * math.sin(from_rad))
        tail_y = wcy + int(arrow_len * math.cos(from_rad))

        pygame.draw.line(ws, ACCENT, (tail_x, tail_y), (tip_x, tip_y), 2)
        for side in (+1, -1):
            wing_rad = from_rad + side * math.radians(145)
            pygame.draw.line(ws, ACCENT, (tip_x, tip_y),
                             (tip_x + int(head_len * math.sin(wing_rad)),
                              tip_y - int(head_len * math.cos(wing_rad))), 2)

    ws.blit(line1_img, line1_img.get_rect(center=(wcx, wcy + RADIUS + 8)))
    if line2_img:
        ws.blit(line2_img, line2_img.get_rect(center=(wcx, wcy + RADIUS + 26)))

    screen.blit(ws, (cx - widget_w // 2, cy - wcy))


def draw_tide_widget(screen, state, fonts, x, y, w=480, h=230):
    """Tide sine-curve widget showing today's hi/lo predictions.
    x,y = top-left corner.  w×h = bounding box."""
    from datetime import date as _date
    title_y = y + 4
    draw_text(screen, "Ocean City Tides", fonts["tiny"], TEXT_DIM, x, title_y)

    tides = state.tides  # today + tomorrow from NOAA

    if not tides:
        draw_text(screen, "Loading...", fonts["small"], TEXT_DIM, x, y + 30)
        return

    graph_x = x
    graph_y = y + 26
    graph_w = w
    graph_h = h - 26

    # Background panel
    pygame.draw.rect(screen, PANEL, (graph_x, graph_y, graph_w, graph_h), border_radius=10)

    def _minutes(t_str):
        # t_str: "YYYY-MM-DD HH:MM"  — convert to minutes since today midnight
        from datetime import date as _d2, datetime as _dt2
        today = _d2.today()
        dt = _dt2.strptime(t_str, "%Y-%m-%d %H:%M")
        delta = dt - _dt2.combine(today, _dt2.min.time())
        return int(delta.total_seconds() / 60)

    sorted_tides = sorted(tides, key=lambda t: _minutes(t["t"]))

    # Value range — use all fetched tides for normalisation
    all_vals = [t["v"] for t in sorted_tides]
    vmin = min(all_vals) - 0.3
    vmax = max(all_vals) + 0.3
    vrange = max(vmax - vmin, 0.5)

    def _norm(v):
        return (v - vmin) / vrange  # 0=low, 1=high

    def _px(minutes, norm_v):
        px = graph_x + int(minutes / 1440 * graph_w)
        py = graph_y + graph_h - 20 - int(norm_v * (graph_h - 38))
        return px, py

    # Gridlines at 6-hour marks
    for hr in range(0, 25, 6):
        gx = graph_x + int(hr / 24 * graph_w)
        pygame.draw.line(screen, BG, (gx, graph_y + 4), (gx, graph_y + graph_h - 22), 1)
        label = f"{hr % 12 or 12}{'a' if hr < 12 else 'p'}"
        draw_text(screen, label, fonts["tiny"], TEXT_DIM, gx, graph_y + graph_h - 20, anchor="midtop")

    # Build smooth polyline via cosine interpolation between all tide events
    # (curve continues naturally into tomorrow, graph clips at graph_w)
    pts_data = [(_minutes(t["t"]), t["v"]) for t in sorted_tides]

    poly = []
    for i in range(len(pts_data) - 1):
        m0, v0 = pts_data[i]
        m1, v1 = pts_data[i + 1]
        steps = max(int((m1 - m0) / 5), 2)
        for s in range(steps):
            frac = s / steps
            frac_cos = (1 - math.cos(frac * math.pi)) / 2
            mv = m0 + (m1 - m0) * frac
            vv = v0 + (v1 - v0) * frac_cos
            ppx, ppy = _px(mv, _norm(vv))
            if ppx > graph_x + graph_w:
                break
            poly.append((ppx, ppy))

    if len(poly) >= 2:
        pygame.draw.lines(screen, ACCENT, False, poly, 2)

    # Mark only today's hi/lo events — one combined label each
    today_tides = [t for t in sorted_tides if 0 <= _minutes(t["t"]) < 1440]
    for t in today_tides:
        m = _minutes(t["t"])
        n = _norm(t["v"])
        px, py = _px(m, n)
        is_high = t["type"] == "H"
        color = TEXT_BRIGHT if is_high else RAIN
        pygame.draw.circle(screen, color, (px, py), 5)
        # e.g. "H 3.2ft 10:24a"
        t_hm   = t["t"].split(" ")[1]
        hh, mm = int(t_hm.split(":")[0]), int(t_hm.split(":")[1])
        ampm   = "a" if hh < 12 else "p"
        h12    = hh % 12 or 12
        t_fmt  = f"{h12}:{mm:02d}{ampm}"
        lbl    = f"{'H' if is_high else 'L'} {t['v']:.1f}ft {t_fmt}"
        offset_y = -14 if is_high else 8
        draw_text(screen, lbl, fonts["tiny"], color, px, py + offset_y, anchor="center")

    # Now-marker
    now_min = datetime.now().hour * 60 + datetime.now().minute
    nx = graph_x + int(now_min / 1440 * graph_w)
    pygame.draw.line(screen, GOLD, (nx, graph_y + 4), (nx, graph_y + graph_h - 22), 2)


def draw_osv_widget(screen, state, fonts, x, y, w=480):
    """Off-Street Vehicle count widget for Assateague Island beach.
    x,y = top-left corner."""
    draw_text(screen, "Assateague OSV Count", fonts["tiny"], TEXT_DIM, x, y + 4)

    osv_max    = state.osv_max or 145
    osv_count  = state.osv_count
    osv_status = state.osv_status
    reported   = state.osv_reported_at

    bar_x = x
    bar_y = y + 28
    bar_w = w
    bar_h = 28

    if osv_count is None:
        draw_text(screen, "Loading...", fonts["small"], TEXT_DIM, x, bar_y)
        return

    # Fill bar
    fill_frac = min(osv_count / osv_max, 1.0)
    fill_color = GREEN if fill_frac < 0.7 else (GOLD if fill_frac < 0.9 else RED)
    pygame.draw.rect(screen, PANEL, (bar_x, bar_y, bar_w, bar_h), border_radius=6)
    if fill_frac > 0:
        pygame.draw.rect(screen, fill_color, (bar_x, bar_y, int(bar_w * fill_frac), bar_h), border_radius=6)
    pygame.draw.rect(screen, TEXT_DIM, (bar_x, bar_y, bar_w, bar_h), 1, border_radius=6)

    # Count / max text — vertically centered in bar
    draw_text(screen, f"{osv_count} / {osv_max}", fonts["small"], TEXT_BRIGHT, x + bar_w // 2, bar_y + bar_h // 2, anchor="center")

    # Status line
    if osv_status:
        status_short = osv_status.replace("OSV ", "")
        status_col   = GREEN if "Open" in osv_status else RED
        draw_text(screen, status_short, fonts["small"], status_col, x, bar_y + bar_h + 6)

    # Reported-at timestamp
    if reported:
        ts_str = reported.strftime("%-I:%M %p")
        draw_text(screen, f"as of {ts_str}", fonts["tiny"], TEXT_DIM, x + w, bar_y + bar_h + 8, anchor="topright")


def draw_ram_widget(screen, state, fonts, x, y):
    """Draw the DDR5 RAM price ticker in the bottom-left area.
      y +  0 : "DDR5 16GB 4800/5600  (spot)"   [tiny, dim]
      y + 22 : "$218.50"                        [large, bright]
      y + 95 : "24h -$1.25    7d -$4.00"        [small, colored]
    """
    draw_text(screen, "DDR5 16GB spot price", fonts["tiny"], TEXT_DIM, x, y)

    if state.ram_price is None:
        draw_text(screen, "Loading...", fonts["small"], TEXT_DIM, x, y + 28)
        return

    draw_text(screen, f"${state.ram_price:.2f}", fonts["large"], TEXT_BRIGHT, x, y + 22)

    def delta_str(delta):
        if delta is None:
            return "--", TEXT_DIM
        col = GREEN if delta <= 0 else RED
        sign = "-" if delta < 0 else "+"
        return f"{sign}${abs(delta):.2f}", col

    d24_str, d24_col = delta_str(state.ram_delta_24h)
    d7d_str, d7d_col = delta_str(state.ram_delta_7d)

    draw_text(screen, "24h", fonts["tiny"],    TEXT_DIM, x,       y + 97)
    draw_text(screen, d24_str, fonts["small"], d24_col,  x + 38,  y + 93)
    draw_text(screen, "7d",  fonts["tiny"],    TEXT_DIM, x + 160, y + 97)
    draw_text(screen, d7d_str, fonts["small"], d7d_col,  x + 190, y + 93)


def draw_nflstats_widget(screen, state, fonts, x, y, w=440, h=200):
    """Compact NFC North standings widget."""
    TITLE_H = 24
    min_panel_h = 14 + (24 * 4) + 8
    panel_y = y + TITLE_H
    panel_h = max(h - TITLE_H, min_panel_h)
    pygame.draw.rect(screen, PANEL, (x, panel_y, w, panel_h), border_radius=10)

    rows = state.nfl_nfc_north
    if not rows:
        draw_text(screen, "Loading NFC North...", fonts["small"], TEXT_DIM,
                  x + w // 2, panel_y + panel_h // 2, anchor="center")
        return

    title = f"NFC North standings" + (f"  {state.nfl_season}" if state.nfl_season else "")
    draw_text(screen, title, fonts["tiny"], TEXT_DIM, x, y + 4)

    row_y = panel_y + 14
    row_step = 24
    for row in rows:
        team = row.get("team") or {}
        full_name = team.get("full_name", "")
        wl = row.get("overall_record", "0-0")
        is_gb = full_name == "Green Bay Packers"
        row_color = TEXT_BRIGHT if is_gb else TEXT_DIM
        if is_gb:
            pygame.draw.rect(screen, (30, 56, 34), (x + 6, row_y - 2, w - 12, 22), border_radius=6)
        draw_text(screen, full_name, fonts["tiny"], row_color, x + 14, row_y)
        draw_text(screen, wl, fonts["tiny"], row_color, x + w - 14, row_y, anchor="topright")
        row_y += row_step



# ── Left-column layout engine ─────────────────────────────────────────────────
# The zone below the current-temp / icon block is divided dynamically among
# whichever features are enabled.  "Growable" widgets (chart types) expand
# equally to absorb remaining space, capped at _COL_MAX_H each.

_COL_X        = 60    # x for text-only widgets (SUNTIME title row, RAM labels)
_COL_WIDGET_X = 80    # x for chart / bar widgets
_COL_W        = 440   # width for chart / bar widgets  (right edge at x = 520)
_COL_Y_TOP    = 350   # top of dynamic zone (just below current-temp / icon block)
_COL_Y_BOT    = 1000  # bottom of dynamic zone (above MOTD bar)
_COL_MAX_GAP  = 40    # maximum gap between adjacent widgets (actual gap is computed dynamically)
_COL_MAX_H    = 400   # maximum height a growable widget may reach

# Ordered catalog: (feature_key, min_h, growable)
# Growable widgets grow equally to fill leftover space (up to _COL_MAX_H).
# Non-growable widgets keep their fixed min_h.
_COL_CATALOG = [
    ("SUNTIME",          52,  False),
    ("OSV_TIDES",       120,  True),
    ("OSV_COUNT",        95,  False),
    ("HOURLY_FORECAST", 150,  True),
    ("HOURLY_PRECIP",   150,  True),
    ("POTOMAC",         120,  True),
    ("NFLSTATS",        168,  False),
    ("RAM_PRICE",       128,  False),
]


def _left_column_layout():
    """Return [(widget_key, y, h), ...] for all enabled left-column widgets.

    Inter-widget gap is computed dynamically so all remaining space is shared
    evenly, up to _COL_MAX_GAP.  After the gap is fixed, any leftover space
    goes to the growable chart widgets (up to _COL_MAX_H each).
    """
    active = [(key, mn, grow) for key, mn, grow in _COL_CATALOG if FEATURES.get(key)]
    if not active:
        return []

    n         = len(active)
    available = _COL_Y_BOT - _COL_Y_TOP
    sum_min   = sum(mn for _, mn, _ in active)

    # Target gap: evenly share the space left at minimum heights, capped at _COL_MAX_GAP
    target_gap = min(_COL_MAX_GAP, (available - sum_min) // (n - 1)) if n > 1 else 0
    target_gap = max(target_gap, 0)

    # Remainder after gaps goes to growable widgets
    grow_budget = max(0, available - sum_min - target_gap * (n - 1))
    heights     = [mn for _, mn, _ in active]
    grow_idxs   = [i for i, (_, _, grow) in enumerate(active) if grow]

    if grow_idxs and grow_budget > 0:
        share     = grow_budget // len(grow_idxs)
        remainder = grow_budget - share * len(grow_idxs)
        for i in grow_idxs:
            heights[i] = min(heights[i] + share, _COL_MAX_H)
        fi = grow_idxs[0]
        heights[fi] = min(heights[fi] + remainder, _COL_MAX_H)

    # Recompute the actual gap from the final heights so any unclaimed space
    # (e.g. charts hitting _COL_MAX_H) is still distributed evenly
    total_h    = sum(heights)
    actual_gap = min(_COL_MAX_GAP, (available - total_h) // (n - 1)) if n > 1 else 0
    actual_gap = max(actual_gap, 0)

    result, y = [], _COL_Y_TOP
    for (key, _, _), h in zip(active, heights):
        result.append((key, y, h))
        y += h + actual_gap
    return result


def draw_suntime(screen, state, fonts, x, y):
    """Sunrise / sunset two-line display.  x,y = top-left; occupies ~52 px tall."""
    daily    = (state.weather or {}).get("daily", {})
    sunrises = daily.get("sunrise", [])
    sunsets  = daily.get("sunset",  [])
    if not sunrises or not sunsets:
        return
    try:
        rise = datetime.strptime(sunrises[0], "%Y-%m-%dT%H:%M").strftime("%-I:%M %p")
        setr = datetime.strptime(sunsets[0],  "%Y-%m-%dT%H:%M").strftime("%-I:%M %p")
    except Exception:
        return
    draw_text(screen, "Sun up",            fonts["tiny"],  TEXT_DIM, x,       y + 2)
    draw_text(screen, f"\u2191 {rise}",    fonts["small"], GOLD,     x,       y + 20)
    draw_text(screen, "Sun down",          fonts["tiny"],  TEXT_DIM, x + 220, y + 2)
    draw_text(screen, f"\u2193 {setr}",    fonts["small"], TEXT_DIM, x + 220, y + 20)


def _hourly_series(hourly, key, n=8):
    times = hourly.get("time", [])
    vals = hourly.get(key, [])
    if not times or not vals:
        return [], []

    now_str = datetime.now().strftime("%Y-%m-%dT%H:00")
    if now_str in times:
        idx = times.index(now_str)
    else:
        idx = max((i for i, t in enumerate(times) if t <= now_str), default=0)

    s_times = times[idx: idx + n]
    s_vals = vals[idx: idx + n]
    m = min(len(s_times), len(s_vals))
    return s_times[:m], s_vals[:m]


def draw_hourly_widget(screen, state, fonts, x, y, w=440, h=200):
    """8-hour temperature chart with Open-Meteo (cyan) and NWS (gold) overlaid."""
    TITLE_H = 24
    PAD_L = 10
    PAD_R = 12
    PAD_TOP = 8
    PAD_BOT = 22

    panel_y = y + TITLE_H
    panel_h = h - TITLE_H
    pygame.draw.rect(screen, PANEL, (x, panel_y, w, panel_h), border_radius=10)
    draw_text(screen, "Next 8 Hours Temp", fonts["tiny"], TEXT_DIM, x, y + 4)

    hourly = (state.weather or {}).get("hourly", {})
    s_times, s_temps = _hourly_series(hourly, "temperature_2m")
    nws_hourly = (state.nws_hourly_temp or {}).get("hourly", {})
    nws_times, nws_temps = _hourly_series(nws_hourly, "temperature_2m")

    if len(s_times) < 2:
        draw_text(screen, "Loading...", fonts["small"], TEXT_DIM,
                  x + w // 2, panel_y + panel_h // 2, anchor="center")
        return

    try:
        s_temps = [float(v) for v in s_temps]
        nws_temps = [float(v) for v in nws_temps]
    except Exception:
        draw_text(screen, "No data", fonts["small"], TEXT_DIM,
                  x + w // 2, panel_y + panel_h // 2, anchor="center")
        return

    n = len(s_times)
    chart_x = x + PAD_L
    chart_y = panel_y + PAD_TOP
    chart_w = w - PAD_L - PAD_R
    chart_h = panel_h - PAD_TOP - PAD_BOT

    if chart_h < 40:
        draw_text(screen, "Panel too small", fonts["tiny"], TEXT_DIM,
                  x + w // 2, panel_y + panel_h // 2, anchor="center")
        return

    def _cx(i):
        return chart_x + int(i / max(1, n - 1) * chart_w)

    all_temps = list(s_temps) + list(nws_temps)
    t_lo = min(all_temps)
    t_hi = max(all_temps)
    pad = max(2.0, (t_hi - t_lo) * 0.20)
    t_lo -= pad
    t_hi += pad
    t_rng = max(t_hi - t_lo, 1.0)

    def _ty(v):
        return chart_y + chart_h - int((v - t_lo) / t_rng * chart_h)

    for i in range(n):
        pygame.draw.line(screen, BG, (_cx(i), chart_y), (_cx(i), chart_y + chart_h), 1)
    for frac in (0.0, 0.5, 1.0):
        gy = chart_y + int(chart_h * frac)
        pygame.draw.line(screen, BG, (chart_x, gy), (chart_x + chart_w, gy), 1)

    om_pts = [(_cx(i), _ty(s_temps[i])) for i in range(n)]
    if len(om_pts) >= 2:
        pygame.draw.lines(screen, ACCENT, False, om_pts, 2)

    nws_idx = {t: v for t, v in zip(nws_times, nws_temps)}
    if len(nws_times) >= 2:
        nws_pts = [(_cx(i), _ty(nws_idx[t])) for i, t in enumerate(s_times) if t in nws_idx]
        if len(nws_pts) >= 2:
            pygame.draw.lines(screen, GOLD, False, nws_pts, 2)

    for i, (px, om_py) in enumerate(om_pts):
        pygame.draw.circle(screen, ACCENT, (px, om_py), 4 if i == 0 else 3)

        has_nws = s_times[i] in nws_idx
        nws_py = _ty(nws_idx[s_times[i]]) if has_nws else None
        if has_nws:
            pygame.draw.circle(screen, GOLD, (px, nws_py), 4 if i == 0 else 3)

        om_lbl = f"{round(s_temps[i])}\u00b0"
        if has_nws:
            nws_lbl = f"{round(nws_idx[s_times[i]])}\u00b0"
            if abs(om_py - nws_py) < 14:
                if om_py <= nws_py:
                    draw_text(screen, om_lbl, fonts["tiny"], ACCENT, px, om_py - 4, anchor="midbottom")
                    draw_text(screen, nws_lbl, fonts["tiny"], GOLD, px, nws_py + 6, anchor="midtop")
                else:
                    draw_text(screen, om_lbl, fonts["tiny"], ACCENT, px, om_py + 6, anchor="midtop")
                    draw_text(screen, nws_lbl, fonts["tiny"], GOLD, px, nws_py - 4, anchor="midbottom")
            else:
                if om_py - chart_y < 14:
                    draw_text(screen, om_lbl, fonts["tiny"], ACCENT, px, om_py + 6, anchor="midtop")
                else:
                    draw_text(screen, om_lbl, fonts["tiny"], ACCENT, px, om_py - 4, anchor="midbottom")
                if nws_py - chart_y < 14:
                    draw_text(screen, nws_lbl, fonts["tiny"], GOLD, px, nws_py + 6, anchor="midtop")
                else:
                    draw_text(screen, nws_lbl, fonts["tiny"], GOLD, px, nws_py - 4, anchor="midbottom")
        else:
            if om_py - chart_y < 14:
                draw_text(screen, om_lbl, fonts["tiny"], ACCENT, px, om_py + 6, anchor="midtop")
            else:
                draw_text(screen, om_lbl, fonts["tiny"], ACCENT, px, om_py - 4, anchor="midbottom")

    hour_y = chart_y + chart_h + 1
    for i, t_str in enumerate(s_times):
        hr = int(t_str[11:13])
        ampm = "a" if hr < 12 else "p"
        h12 = hr % 12 or 12
        draw_text(screen, f"{h12}{ampm}", fonts["tiny"], TEXT_DIM, _cx(i), hour_y, anchor="midtop")

    legend_x = x + 300
    legend_y = y + 20
    legend_gap = 24
    pygame.draw.line(screen, ACCENT, (legend_x, legend_y + 8), (legend_x + 18, legend_y + 8), 3)
    draw_text(screen, "Open-Meteo", fonts["tiny"], TEXT_DIM, legend_x + 22, legend_y, anchor="topleft")
    pygame.draw.line(screen, GOLD, (legend_x, legend_y + 8 + legend_gap), (legend_x + 18, legend_y + 8 + legend_gap), 3)
    draw_text(screen, "NWS", fonts["tiny"], TEXT_DIM, legend_x + 22, legend_y + legend_gap, anchor="topleft")


def draw_hourly_precip_widget(screen, state, fonts, x, y, w=440, h=200):
    """8-hour precipitation chance chart with Open-Meteo (cyan) and NWS (gold) overlaid."""
    TITLE_H = 24
    PAD_L = 10
    PAD_R = 12
    PAD_TOP = 10
    PAD_BOT = 24

    panel_y = y + TITLE_H
    panel_h = h - TITLE_H
    pygame.draw.rect(screen, PANEL, (x, panel_y, w, panel_h), border_radius=10)
    draw_text(screen, "Rain Chance", fonts["tiny"], TEXT_DIM, x, y + 4)

    hourly = (state.weather or {}).get("hourly", {})
    s_times, s_pops = _hourly_series(hourly, "precipitation_probability")
    nws_hourly = (state.nws_hourly_precip or {}).get("hourly", {})
    nws_times, nws_pops = _hourly_series(nws_hourly, "precipitation_probability")

    if len(s_times) < 2:
        draw_text(screen, "Loading...", fonts["small"], TEXT_DIM,
                  x + w // 2, panel_y + panel_h // 2, anchor="center")
        return

    try:
        s_pops = [max(0.0, min(100.0, float(v or 0))) for v in s_pops]
        nws_pops = [max(0.0, min(100.0, float(v or 0))) for v in nws_pops]
    except Exception:
        draw_text(screen, "No data", fonts["small"], TEXT_DIM,
                  x + w // 2, panel_y + panel_h // 2, anchor="center")
        return

    n = len(s_times)
    chart_x = x + PAD_L
    chart_y = panel_y + PAD_TOP
    chart_w = w - PAD_L - PAD_R
    chart_h = panel_h - PAD_TOP - PAD_BOT

    if chart_h < 60:
        draw_text(screen, "Panel too small", fonts["tiny"], TEXT_DIM,
                  x + w // 2, panel_y + panel_h // 2, anchor="center")
        return

    def _py(p):
        return chart_y + chart_h - int((p / 100.0) * chart_h)

    for pct in (0, 25, 50, 75, 100):
        gy = _py(pct)
        pygame.draw.line(screen, BG, (chart_x, gy), (chart_x + chart_w, gy), 1)

    slot_w = chart_w / max(1, n)
    bar_w = max(8, min(18, int(slot_w * 0.36)))

    for i in range(1, n):
        gx = chart_x + int(i * slot_w)
        pygame.draw.line(screen, BG, (gx, chart_y), (gx, chart_y + chart_h), 1)

    nws_by_time = {t: p for t, p in zip(nws_times, nws_pops)}
    for i, pct in enumerate(s_pops):
        cx = chart_x + int((i + 0.5) * slot_w)
        om_py = _py(pct)
        om_h = max(1, chart_y + chart_h - om_py)
        om_x = cx - bar_w - 2
        pygame.draw.rect(screen, (56, 102, 160), (om_x, om_py, bar_w, om_h), border_radius=3)
        pygame.draw.rect(screen, ACCENT, (om_x, om_py, bar_w, om_h), 1, border_radius=3)
        om_lbl_y = max(chart_y + 10, om_py - 2)

        if s_times[i] in nws_by_time:
            n_pct = nws_by_time[s_times[i]]
            nws_py = _py(n_pct)
            nws_h = max(1, chart_y + chart_h - nws_py)
            nws_x = cx + 2
            pygame.draw.rect(screen, (128, 102, 34), (nws_x, nws_py, bar_w, nws_h), border_radius=3)
            pygame.draw.rect(screen, GOLD, (nws_x, nws_py, bar_w, nws_h), 1, border_radius=3)
            nws_lbl_y = max(chart_y + 10, nws_py - 2)

            if abs(om_lbl_y - nws_lbl_y) < 12:
                if om_py <= nws_py:
                    draw_text(screen, f"{int(round(pct))}%", fonts["tiny"], ACCENT, om_x + bar_w // 2, om_py - 2, anchor="midbottom")
                    draw_text(screen, f"{int(round(n_pct))}%", fonts["tiny"], GOLD, nws_x + bar_w // 2, nws_py + 6, anchor="midtop")
                else:
                    draw_text(screen, f"{int(round(pct))}%", fonts["tiny"], ACCENT, om_x + bar_w // 2, om_py + 6, anchor="midtop")
                    draw_text(screen, f"{int(round(n_pct))}%", fonts["tiny"], GOLD, nws_x + bar_w // 2, nws_py - 2, anchor="midbottom")
            else:
                draw_text(screen, f"{int(round(pct))}%", fonts["tiny"], ACCENT, om_x + bar_w // 2, om_lbl_y, anchor="midbottom")
                draw_text(screen, f"{int(round(n_pct))}%", fonts["tiny"], GOLD, nws_x + bar_w // 2, nws_lbl_y, anchor="midbottom")
        else:
            draw_text(screen, f"{int(round(pct))}%", fonts["tiny"], ACCENT, om_x + bar_w // 2, om_lbl_y, anchor="midbottom")

    hour_y = chart_y + chart_h + 2
    for i in range(1, n):
        hr = int(s_times[i][11:13])
        ampm = "a" if hr < 12 else "p"
        h12 = hr % 12 or 12
        draw_text(screen, f"{h12}{ampm}", fonts["tiny"], TEXT_DIM,
                  chart_x + int(i * slot_w), hour_y, anchor="midtop")

    legend_x = x + 300
    legend_y = y + 20
    legend_gap = 24
    pygame.draw.line(screen, ACCENT, (legend_x, legend_y + 8), (legend_x + 18, legend_y + 8), 3)
    draw_text(screen, "Open-Meteo", fonts["tiny"], TEXT_DIM, legend_x + 22, legend_y, anchor="topleft")
    pygame.draw.line(screen, GOLD, (legend_x, legend_y + 8 + legend_gap), (legend_x + 18, legend_y + 8 + legend_gap), 3)
    draw_text(screen, "NWS", fonts["tiny"], TEXT_DIM, legend_x + 22, legend_y + legend_gap, anchor="topleft")


def draw_potomac_widget(screen, state, fonts, x, y, w=440, h=200):
    """Potomac River gage height 7-day history chart (USGS Little Falls).
    x,y = top-left corner.  w×h = total bounding box (two-line title included).
    Y-axis labels (lo / mid / hi) with spine; no inline annotations.
    """
    from datetime import date as _date, timedelta as _td

    TITLE_H = 44   # two lines at tiny (20 px) with 4 px top margin
    PAD_L   = 48   # room for y-axis labels fully inside the panel
    PAD_R   = 8
    PAD_T   = 10
    PAD_BOT = 22

    # Two-line title
    draw_text(screen, "Potomac at Little Falls", fonts["tiny"], TEXT_DIM, x, y + 4)
    draw_text(screen, "gage height (ft) \u00b7 last 7 days", fonts["tiny"], TEXT_DIM, x, y + 22)

    panel_y = y + TITLE_H
    panel_h = h - TITLE_H
    pygame.draw.rect(screen, PANEL, (x, panel_y, w, panel_h), border_radius=10)

    lvl_pts = state.potomac_level   # [(epoch, feet), ...]

    if not lvl_pts:
        draw_text(screen, "Loading...", fonts["small"], TEXT_DIM,
                  x + w // 2, panel_y + panel_h // 2, anchor="center")
        return

    chart_x = x + PAD_L
    chart_y = panel_y + PAD_T
    chart_w = w - PAD_L - PAD_R
    chart_h = panel_h - PAD_T - PAD_BOT

    # Time axis: 7 days ago → now
    now_ep = time.time()
    t_min  = now_ep - 7 * 86400
    t_rng  = now_ep - t_min

    def _cx(ep):
        return chart_x + int((ep - t_min) / t_rng * chart_w)

    # Day gridlines + labels
    for d_off in range(-7, 1):
        midnight_ep = datetime.combine(
            _date.today() + _td(days=d_off),
            datetime.min.time()
        ).timestamp()
        if t_min <= midnight_ep <= now_ep:
            gx = _cx(midnight_ep)
            pygame.draw.line(screen, BG, (gx, chart_y), (gx, chart_y + chart_h), 1)
            day_lbl = (_date.today() + _td(days=d_off)).strftime("%a")
            draw_text(screen, day_lbl, fonts["tiny"], TEXT_DIM,
                      gx + 3, chart_y + chart_h + 2, anchor="midtop")

    vis_lv  = [(ep, v) for ep, v in lvl_pts if t_min <= ep <= now_ep]
    lv_vals = [v for _, v in vis_lv]
    if not lv_vals:
        return

    lv_lo  = min(lv_vals) - 0.2
    lv_hi  = max(lv_vals) + 0.2
    lv_rng = max(lv_hi - lv_lo, 0.1)

    def _ly(v, _lo=lv_lo, _rng=lv_rng):
        return chart_y + chart_h - int((v - _lo) / _rng * chart_h)

    # ── Y-axis: lo / mid / hi of the data, deduplicated after rounding ────────
    lo_v  = min(lv_vals)
    hi_v  = max(lv_vals)
    mid_v = (lo_v + hi_v) / 2
    seen, ticks = set(), []
    for tv in (lo_v, mid_v, hi_v):
        rv = round(tv, 1)
        if rv not in seen:
            seen.add(rv)
            ticks.append(rv)

    # Spine
    pygame.draw.line(screen, TEMP_NEUTRAL, (chart_x, chart_y), (chart_x, chart_y + chart_h), 1)
    for tv in ticks:
        ty = _ly(tv)
        pygame.draw.line(screen, BG, (chart_x, ty), (chart_x + chart_w, ty), 1)
        pygame.draw.line(screen, TEMP_NEUTRAL, (chart_x - 3, ty), (chart_x, ty), 1)  # tick mark
        draw_text(screen, f"{tv:.1f}", fonts["tiny"], TEMP_NEUTRAL,
                  chart_x - 6, ty, anchor="midright")

    # ── Gage height line ──────────────────────────────────────────────────────
    pts = [(_cx(ep), _ly(v)) for ep, v in vis_lv]
    if len(pts) >= 2:
        pygame.draw.lines(screen, TEMP_NEUTRAL, False, pts, 2)

    # Current value — top-right
    cur_lvl = lvl_pts[-1][1]
    draw_text(screen, f"Now {cur_lvl:.1f} ft", fonts["tiny"], TEMP_NEUTRAL,
              chart_x + chart_w - 2, chart_y + 2, anchor="topright")


def draw_screen(screen, state, fonts, tick):
    screen.fill(BG)
    W, H = screen.get_size()
    from datetime import timedelta as _td

    with state.lock:
        started_at = state.started_at
        weather_loaded_once = state.weather_loaded_once
        runtime_error_source = state.runtime_error_source
        runtime_error_message = state.runtime_error_message
        draw_text(screen, LOCATION_NAME, fonts["large"], TEXT_BRIGHT, 60, 40)
        draw_text(screen, datetime.now().strftime("%I:%M %p"), fonts["medium"], TEXT_DIM, 60, 100)

        cur = (state.weather or {}).get("current")
        obs_temp_f = state.obs_temp_f
        daily = (state.weather or {}).get("daily")
        has_current = bool(cur and "temperature_2m" in cur and "weather_code" in cur)
        has_daily = bool(
            daily
            and len(daily.get("time", [])) >= 7
            and len(daily.get("weather_code", [])) >= 7
            and len(daily.get("temperature_2m_max", [])) >= 7
            and len(daily.get("temperature_2m_min", [])) >= 7
            and len(daily.get("precipitation_sum", [])) >= 7
        )

        # Current temp + icon
        if has_current:
            shown_temp_f = obs_temp_f if obs_temp_f is not None else cur["temperature_2m"]
            temp_str = f"{round(shown_temp_f)}°"
            temp_surf = fonts["huge"].render(temp_str, True, TEXT_BRIGHT)
            screen.blit(temp_surf, (60, 140))
            icon_x = 60 + temp_surf.get_width() + 18 + 65
            draw_weather_icon(screen, cur["weather_code"], icon_x, 195, 130, GOLD)
        else:
            bootstrap_loading = (not weather_loaded_once) and (time.time() - started_at < 90)
            if bootstrap_loading:
                draw_text(screen, "Loading forecast...", fonts["small"], GOLD, 60, 172)
                draw_text(screen, "Waiting for first weather update", fonts["tiny"], TEXT_DIM, 60, 210)
            else:
                draw_text(screen, "Forecast unavailable", fonts["small"], GOLD, 60, 172)
                draw_text(screen, "Weather API offline", fonts["tiny"], TEXT_DIM, 60, 210)


        # Map box centered between left widget column and right 7-day panel.
        left_col_right = 520
        right_panel_left = W - 430
        mw, mh = 820, 720
        mx = left_col_right + (right_panel_left - left_col_right - mw) // 2
        my = 160
        draw_map(screen, state, fonts, mx, my, mw, mh)

        # Wind widget — center from the actual rendered 768px map surface.
        map_draw_x = mx + (mw - 768) // 2
        map_center_x = map_draw_x + 768 // 2
        draw_wind_widget(screen, state, fonts, map_center_x - 10, my + mh + 6)

        # 7-Day Forecast
        for i in range(7):
            ry = 140 + (i * 120)
            pygame.draw.rect(screen, PANEL, (W-430, ry, 390, 110), border_radius=15)
            if has_daily:
                dt = datetime.strptime(daily["time"][i], "%Y-%m-%d")
                code = daily["weather_code"][i]
                draw_weather_icon(screen, code, W-407, ry+55, 46, GOLD)
                draw_text(screen, dt.strftime("%a").upper(), fonts["small"], TEXT_BRIGHT, W-372, ry+12)
                desc = WMO_DESC.get(code, "")
                draw_text(screen, desc, fonts["small"], ACCENT, W-372, ry+50)
                hi_raw = round(daily["temperature_2m_max"][i])
                lo_raw = round(daily["temperature_2m_min"][i])
                nws = state.nws_hilo.get(daily["time"][i])
                hi = round(nws[0]) if nws and nws[0] is not None else hi_raw
                lo = round(nws[1]) if nws and nws[1] is not None else lo_raw
                draw_text(screen, f"{hi}°", fonts["medium"], TEXT_BRIGHT, W-178, ry+8)
                draw_text(screen, f"{lo}°", fonts["small"],  TEXT_DIM,    W-178, ry+52)
                precip_mm = daily["precipitation_sum"][i]
                precip_in = precip_mm / 25.4
                if precip_in > 0.01:
                    draw_text(screen, f"~{precip_in:.2f}\"", fonts["small"], RAIN, W-45, ry+78, anchor="topright")
                else:
                    draw_text(screen, "Dry", fonts["small"], TEXT_DIM, W-45, ry+78, anchor="topright")
            else:
                dt = datetime.now() + _td(days=i)
                draw_text(screen, dt.strftime("%a").upper(), fonts["small"], TEXT_BRIGHT, W-372, ry+12)
                draw_text(screen, "No forecast", fonts["small"], TEXT_DIM, W-372, ry+50)
                draw_text(screen, "--", fonts["medium"], TEXT_BRIGHT, W-178, ry+8)
                draw_text(screen, "--", fonts["small"], TEXT_DIM, W-178, ry+52)
                draw_text(screen, "N/A", fonts["small"], TEXT_DIM, W-45, ry+78, anchor="topright")

        # Left-column dynamic layout — positions and heights computed by _left_column_layout()
        for _wkey, _wy, _wh in _left_column_layout():
            if _wkey == "SUNTIME":
                draw_suntime(screen, state, fonts, _COL_X, _wy)
            elif _wkey == "OSV_TIDES":
                draw_tide_widget(screen, state, fonts, _COL_WIDGET_X, _wy, w=_COL_W, h=_wh)
            elif _wkey == "OSV_COUNT":
                draw_osv_widget(screen, state, fonts, _COL_WIDGET_X, _wy, w=_COL_W)
            elif _wkey == "HOURLY_FORECAST":
                draw_hourly_widget(screen, state, fonts, _COL_WIDGET_X, _wy, w=_COL_W, h=_wh)
            elif _wkey == "HOURLY_PRECIP":
                draw_hourly_precip_widget(screen, state, fonts, _COL_WIDGET_X, _wy, w=_COL_W, h=_wh)
            elif _wkey == "POTOMAC":
                draw_potomac_widget(screen, state, fonts, _COL_WIDGET_X, _wy, w=_COL_W, h=_wh)
            elif _wkey == "NFLSTATS":
                draw_nflstats_widget(screen, state, fonts, _COL_WIDGET_X, _wy, w=_COL_W, h=_wh)
            elif _wkey == "RAM_PRICE":
                draw_ram_widget(screen, state, fonts, _COL_X, _wy)

        show_banner = True
        if runtime_error_source == "WEATHER" and (not weather_loaded_once) and (time.time() - started_at < 90):
            show_banner = False
        if show_banner:
            draw_error_banner(screen, fonts, W, H, runtime_error_source, runtime_error_message)


    # MOTD — shrink font until it fits within the screen width with padding
    max_w = W - 80
    for font_key in ("small", "tiny"):
        motd_surf = fonts[font_key].render(state.motd, True, GOLD)
        if motd_surf.get_width() <= max_w:
            break
    screen.blit(motd_surf, motd_surf.get_rect(center=(W // 2, H - 40)))

    # Cycle MOTD every 30 seconds; retry fetch if pool is empty or exhausted
    now = time.time()
    if now - state.motd_last_cycle > 30:
        if not state.motd_pool or state.motd_index + 1 >= len(state.motd_pool):
            # Pool empty (fetch failed) or exhausted — request a fresh batch
            if not state._quotes_running:
                threading.Thread(target=state.update_quotes, daemon=True).start()
            state.motd_last_cycle = now  # throttle: don't re-trigger next frame
        else:
            state.motd_index += 1
            state.motd = state.motd_pool[state.motd_index]
            state.motd_last_cycle = now


def main():
    pygame.init()
    screen = pygame.display.set_mode((SCREEN_W, SCREEN_H), pygame.FULLSCREEN | pygame.DOUBLEBUF | pygame.HWSURFACE)
    pygame.mouse.set_visible(False)

    f_p = "dejavusans"
    fonts = {
        "huge":   pygame.font.SysFont(f_p, 160, True),
        "large":  pygame.font.SysFont(f_p, 65, True),
        "medium": pygame.font.SysFont(f_p, 40),
        "small":  pygame.font.SysFont(f_p, 30),
        "tiny":   pygame.font.SysFont(f_p, 20),
    }

    state = AppState()
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    next_self_update_check = time.time() + SELF_UPDATE_INTERVAL_S

    def _spawn(source, fn, clear_on_success=True):
        threading.Thread(
            target=_run_update_task,
            args=(state, source, fn, clear_on_success),
            daemon=True,
        ).start()

    def _spawn_delayed(delay_s, source, fn, clear_on_success=True):
        def _delayed():
            time.sleep(delay_s)
            _run_update_task(state, source, fn, clear_on_success)

        threading.Thread(target=_delayed, daemon=True).start()

    _spawn("WEATHER", state.update_weather, clear_on_success=False)
    _spawn("QUOTES", state.update_quotes)
    state.last_weather_upd = time.time()
    state.last_map_upd = time.time()
    state.last_ram_upd = time.time()

    _spawn_delayed(5, "MAP", state.update_map)
    if FEATURES["RAM_PRICE"]: _spawn_delayed(8, "RAM", state.update_ram)
    if FEATURES["OSV_TIDES"]: _spawn_delayed(3, "TIDES", state.update_tides)
    if FEATURES["OSV_COUNT"]: _spawn_delayed(6, "OSV", state.update_osv)
    if FEATURES["POTOMAC"]:   _spawn_delayed(7, "POTOMAC", state.update_potomac)
    if FEATURES["NFLSTATS"] and NFL_API_KEY: _spawn_delayed(9, "NFL", state.update_nflstats)

    clock, tick = pygame.time.Clock(), 0
    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_q):
                pygame.quit()
                sys.exit()

        now = time.time()
        if now >= next_self_update_check:
            next_self_update_check = now + SELF_UPDATE_INTERVAL_S
            if _maybe_self_update_and_restart(repo_dir):
                pygame.quit()
                sys.exit(0)
        if now - state.last_weather_upd > 60 and not state._weather_running:
            state.last_weather_upd = now
            _spawn("WEATHER", state.update_weather, clear_on_success=False)
        if now - state.last_map_upd > 60 and not state._map_running:
            state.last_map_upd = now
            _spawn("MAP", state.update_map)
        if now - state.last_ram_upd > 60 and not state._ram_running and FEATURES["RAM_PRICE"]:
            state.last_ram_upd = now
            _spawn("RAM", state.update_ram)
        if now - state.last_tide_upd > 300 and not state._tide_running and FEATURES["OSV_TIDES"]:
            state.last_tide_upd = now
            _spawn("TIDES", state.update_tides)
        if now - state.last_osv_upd > 60 and not state._osv_running and FEATURES["OSV_COUNT"]:
            state.last_osv_upd = now
            _spawn("OSV", state.update_osv)
        if now - state.last_potomac_upd > 300 and not state._potomac_running and FEATURES["POTOMAC"]:
            state.last_potomac_upd = now
            _spawn("POTOMAC", state.update_potomac)
        if now - state.last_nfl_upd > 300 and not state._nfl_running and FEATURES["NFLSTATS"] and NFL_API_KEY:
            state.last_nfl_upd = now
            _spawn("NFL", state.update_nflstats)

        draw_screen(screen, state, fonts, tick)
        pygame.display.flip()
        clock.tick(10)
        tick += 1


if __name__ == "__main__":
    main()

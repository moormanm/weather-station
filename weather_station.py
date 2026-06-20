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
LOCATION_NAME = config.get("LOCATION", "NAME")

FEATURES = {
    "OSV_COUNT":       config.getboolean("FEATURES", "OSV_COUNT",       fallback=False),
    "OSV_TIDES":       config.getboolean("FEATURES", "OSV_TIDES",       fallback=False),
    "RAM_PRICE":       config.getboolean("FEATURES", "RAM_PRICE",       fallback=False),
    "HOURLY_FORECAST": config.getboolean("FEATURES", "HOURLY_FORECAST", fallback=True),
    "SUNTIME":         config.getboolean("FEATURES", "SUNTIME",         fallback=True),
    "POTOMAC":         config.getboolean("FEATURES", "POTOMAC",         fallback=True),
    "NFLSTATS":        config.getboolean("FEATURES", "NFLSTATS",        fallback=False),
}

NFL_API_KEY = config["NFLSTATS"].get("API-KEY", "").strip() if config.has_section("NFLSTATS") else ""

TIMEZONE = "America/New_York"
SCREEN_W, SCREEN_H = 1920, 1080

MAP_ZOOM = 7  # zoom level for both basemap and radar tiles (RainViewer max = 7)

def _loc_pixel_offset(lat, lon, zoom):
    """Sub-tile pixel offset of exact location from center of its tile at given zoom."""
    n = 2 ** zoom
    lat_rad = math.radians(lat)
    xt = int((lon + 180.0) / 360.0 * n)
    yt = int((1.0 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2.0 * n)
    fx = (lon + 180.0) / 360.0 * n - xt
    fy = (1.0 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2.0 * n - yt
    return int(fx * 256 - 128), int(fy * 256 - 128)

LOC_DOT_OFFSET = _loc_pixel_offset(LATITUDE, LONGITUDE, MAP_ZOOM)

USER_AGENT = "FrederickWeatherStation/1.8 (RPi3B+; Dashboard)"
HEADERS = {"User-Agent": USER_AGENT}

NWS_FORECAST_URL = "https://api.weather.gov/gridpoints/LWX/80,95/forecast"
NWS_STATIONS_URL = "https://api.weather.gov/stations/KFDK/observations?limit=24"
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


def safe_fetch(url, timeout=15, headers=None):
    print("fetching: ", url)
    try:
        req = urllib.request.Request(url, headers=headers or HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("!!! Rate limited (429). Cooling down.")
            return "RATE_LIMITED"
        return None
    except Exception as e:
        print(f"Fetch error: {e}")
        return None


def _fetch_nws_hilo():
    """Return dict of {date_str: (hi_f, lo_f)} from NWS forecast + today's observed high."""
    hilo = {}
    try:
        # NWS 7-day forecast for days 1+
        raw = safe_fetch(NWS_FORECAST_URL)
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
        # Today's observed high from KFDK (Frederick Municipal Airport)
        raw = safe_fetch(NWS_STATIONS_URL)
        if raw and raw != "RATE_LIMITED":
            obs = json.loads(raw.decode())["features"]
            today = datetime.now().strftime("%Y-%m-%d")
            today_temps = []
            for o in obs:
                ts = o["properties"]["timestamp"]
                t_c = o["properties"]["temperature"]["value"]
                if ts[:10] == today and t_c is not None:
                    today_temps.append(t_c * 9/5 + 32)
            if today_temps:
                obs_hi = round(max(today_temps))
                existing = hilo.get(today, (None, None))
                # Use the greater of observed high and forecast high; keep forecast low
                forecast_hi = existing[0]
                hi = max(obs_hi, forecast_hi) if forecast_hi is not None else obs_hi
                hilo[today] = (hi, existing[1])
    except Exception as e:
        print(f"NWS observation fetch error: {e}")

    return hilo


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
        self.weather = None
        self.nws_hilo = {}              # {date_str: (hi_f, lo_f)} from NWS observed+forecast
        self.map_tiles = {}
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
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}"
               f"&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,sunrise,sunset"
               f"&hourly=temperature_2m,relativehumidity_2m"
               f"&current_weather=true&temperature_unit=fahrenheit&timezone={TIMEZONE}&forecast_days=7"
               f"&models=gfs_global")
        raw = safe_fetch(url)
        if raw == "RATE_LIMITED":
            self.backoff_until = time.time() + 1800
            return
        if raw:
            try:
                data = json.loads(raw.decode())
                nws_hilo = _fetch_nws_hilo()
                with self.lock:
                    self.weather = data
                    self.nws_hilo = nws_hilo
                    self.last_weather_upd = time.time()
            except Exception as e:
                print(f"Weather parse error: {e}")
                self.last_weather_upd = 0
        else:
            self.last_weather_upd = 0

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
        lat_rad = math.radians(LATITUDE)
        n = 2 ** zoom
        xt = int((LONGITUDE + 180.0) / 360.0 * n)
        yt = int((1.0 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2.0 * n)

        # 3×3 basemap tiles at zoom 7
        new_tiles = {}
        for dy in range(-1, 2):
            for dx in range(-1, 2):
                url = f"https://basemaps.cartocdn.com/light_all/{zoom}/{xt+dx}/{yt+dy}.png"
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


def draw_map(screen, state, fonts, mx, my, mw, mh):
    """Draw the basemap tiles + radar overlay + location dot into a rounded-clipped area."""
    if not state.map_tiles:
        return

    # Render tiles onto an intermediate surface the size of the tile grid (768×768)
    tile_surf = pygame.Surface((768, 768), pygame.SRCALPHA)
    tile_ox = (mw - 768) // 2
    tile_oy = (mh - 768) // 2

    for (dx, dy), d in state.map_tiles.items():
        try:
            img = pygame.image.load(io.BytesIO(d)).convert_alpha()
            tile_surf.blit(img, ((dx + 1) * 256, (dy + 1) * 256))
        except Exception:
            pass

    # Radar overlay: blit each tile at the same position as its basemap counterpart
    for (dx, dy), blob in state.radar_tiles.items():
        try:
            rd = pygame.image.load(io.BytesIO(blob)).convert_alpha()
            rd.set_alpha(180)
            tile_surf.blit(rd, ((dx + 1) * 256, (dy + 1) * 256))
        except Exception as e:
            print(f"Radar draw error: {e}")

    # Location dot on the tile surface
    dot_x = 256 + 128 + LOC_DOT_OFFSET[0]  # center of center tile + sub-tile offset
    dot_y = 256 + 128 + LOC_DOT_OFFSET[1]
    pygame.draw.circle(tile_surf, ACCENT, (dot_x, dot_y), 8, 2)

    # Build a rounded-rect mask and apply it
    mask_surf = pygame.Surface((768, 768), pygame.SRCALPHA)
    mask_surf.fill((0, 0, 0, 0))
    pygame.draw.rect(mask_surf, (255, 255, 255, 255), (0, 0, 768, 768), border_radius=16)
    # Multiply tile_surf alpha by mask
    tile_surf.blit(mask_surf, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)

    screen.blit(tile_surf, (mx + tile_ox, my + tile_oy))


def draw_wind_widget(screen, state, fonts, cx, cy):
    """Compass rose with arrow pointing toward where wind is going.
    cx, cy = center of circle."""
    RADIUS = 52

    pygame.draw.circle(screen, PANEL, (cx, cy), RADIUS)
    pygame.draw.circle(screen, TEXT_DIM, (cx, cy), RADIUS, 1)

    # Cardinal labels — placed just inside the rim
    for label, sx, sy in (("N", 0, -1), ("S", 0, 1), ("E", 1, 0), ("W", -1, 0)):
        lx = cx + int((RADIUS - 12) * sx)
        ly = cy + int((RADIUS - 12) * sy)
        draw_text(screen, label, fonts["tiny"], TEXT_DIM, lx, ly, anchor="center")

    cur = state.weather["current_weather"] if state.weather else None
    if cur is None:
        draw_text(screen, "Wind", fonts["tiny"], TEXT_DIM, cx, cy + RADIUS + 8, anchor="center")
        return

    speed_kmh = cur.get("windspeed", 0)
    speed_mph = speed_kmh * 0.621371
    direction = cur.get("winddirection", 0)  # meteorological: wind coming FROM this bearing

    # Arrow tip points FROM where wind originates (i.e. toward that compass direction)
    # e.g. SW wind (225°) -> arrow tip points toward SW
    from_rad = math.radians(direction)
    arrow_len = RADIUS - 22   # shorter, tighter
    head_len  = 9

    tip_x  = cx + int(arrow_len * math.sin(from_rad))
    tip_y  = cy - int(arrow_len * math.cos(from_rad))
    tail_x = cx - int(arrow_len * math.sin(from_rad))
    tail_y = cy + int(arrow_len * math.cos(from_rad))

    pygame.draw.line(screen, ACCENT, (tail_x, tail_y), (tip_x, tip_y), 2)
    for side in (+1, -1):
        wing_rad = from_rad + side * math.radians(145)
        pygame.draw.line(screen, ACCENT, (tip_x, tip_y),
                         (tip_x + int(head_len * math.sin(wing_rad)),
                          tip_y - int(head_len * math.cos(wing_rad))), 2)

    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    compass = dirs[round(direction / 22.5) % 16]

    draw_text(screen, f"{round(speed_mph)} mph", fonts["tiny"], TEXT_BRIGHT, cx, cy + RADIUS + 8,  anchor="center")
    draw_text(screen, compass,                   fonts["tiny"], TEXT_DIM,    cx, cy + RADIUS + 26, anchor="center")


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
    ("HOURLY_FORECAST", 120,  True),
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


def draw_hourly_widget(screen, state, fonts, x, y, w=440, h=200):
    """8-hour temperature + humidity dual-line chart.
    x,y = top-left corner.  w x h = total bounding box (title bar included)."""
    TITLE_H = 24   # header height above the panel rect
    PAD_L   = 10   # inside panel — left margin
    PAD_R   = 10   # inside panel — right margin
    PAD_T   = 18   # inside panel — top margin (temperature value labels)
    PAD_BOT = 22   # inside panel — bottom margin (hour labels)

    draw_text(screen, "Next 8 Hours Temperature", fonts["tiny"], TEXT_DIM, x, y + 4)

    panel_y = y + TITLE_H
    panel_h = h - TITLE_H
    pygame.draw.rect(screen, PANEL, (x, panel_y, w, panel_h), border_radius=10)

    hourly = (state.weather or {}).get("hourly", {})
    times  = hourly.get("time", [])
    temps  = hourly.get("temperature_2m", [])
    humids = hourly.get("relativehumidity_2m", [])

    if not times or not temps or not humids:
        draw_text(screen, "Loading...", fonts["small"], TEXT_DIM,
                  x + w // 2, panel_y + panel_h // 2, anchor="center")
        return

    # Locate current-hour slot; fall back to the nearest past hour
    now_str = datetime.now().strftime("%Y-%m-%dT%H:00")
    if now_str in times:
        idx = times.index(now_str)
    else:
        idx = max((i for i, t in enumerate(times) if t <= now_str), default=0)

    N       = 8
    s_times = times[idx: idx + N]
    s_temps = temps[idx: idx + N]
    s_humid = humids[idx: idx + N]
    n       = len(s_times)

    if n < 2:
        draw_text(screen, "No data", fonts["small"], TEXT_DIM,
                  x + w // 2, panel_y + panel_h // 2, anchor="center")
        return

    # Chart area inside the panel
    chart_x = x + PAD_L
    chart_y = panel_y + PAD_T
    chart_w = w - PAD_L - PAD_R
    chart_h = panel_h - PAD_T - PAD_BOT

    # X-coordinate for slot i, spread evenly across chart_w
    def _cx(i):
        return chart_x + int(i / (N - 1) * chart_w)

    # Temperature: auto-scale with ±4 ° padding so the line never hugs an edge
    t_lo  = min(s_temps) - 4
    t_hi  = max(s_temps) + 4
    t_rng = max(t_hi - t_lo, 1.0)

    def _ty(v):
        return chart_y + chart_h - int((v - t_lo) / t_rng * chart_h)

    # Humidity: fixed 0–100 % scale (kept for future use; not drawn)
    def _hy(v):
        return chart_y + chart_h - int(v / 100.0 * chart_h)

    # Subtle vertical grid lines at each hour slot
    for i in range(n):
        pygame.draw.line(screen, BG, (_cx(i), chart_y), (_cx(i), chart_y + chart_h), 1)

    # ── Temperature line (ACCENT) ──────────────────────────────────────────────
    t_pts = [(_cx(i), _ty(s_temps[i])) for i in range(n)]
    if len(t_pts) >= 2:
        pygame.draw.lines(screen, TEMP_NEUTRAL, False, t_pts, 2)
    for i, (px, py) in enumerate(t_pts):
        pygame.draw.circle(screen, GOLD if i == 0 else TEMP_NEUTRAL, (px, py), 5 if i == 0 else 3)

    # Temperature value labels — above each dot, flipped below when near the top
    for i, (px, py) in enumerate(t_pts):
        lbl = f"{round(s_temps[i])}\u00b0"
        if py - chart_y < 16:
            draw_text(screen, lbl, fonts["tiny"], TEMP_NEUTRAL, px, py + 6,  anchor="midtop")
        else:
            draw_text(screen, lbl, fonts["tiny"], TEMP_NEUTRAL, px, py - 4, anchor="midbottom")

    # ── Hour labels along the bottom ──────────────────────────────────────────
    label_y = panel_y + panel_h - PAD_BOT + 3
    for i, t_str in enumerate(s_times):
        hr   = int(t_str[11:13])
        ampm = "a" if hr < 12 else "p"
        h12  = hr % 12 or 12
        draw_text(screen, f"{h12}{ampm}", fonts["tiny"], TEXT_DIM, _cx(i), label_y, anchor="midtop")


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

    with state.lock:
        draw_text(screen, LOCATION_NAME, fonts["large"], TEXT_BRIGHT, 60, 40)
        draw_text(screen, datetime.now().strftime("%I:%M %p"), fonts["medium"], TEXT_DIM, 60, 100)


        if not state.weather:
            draw_text(screen, state.motd, fonts["small"], GOLD, W//2, H//2, anchor="center")
            return

        cur, daily = state.weather["current_weather"], state.weather["daily"]

        # Current temp + icon
        temp_str = f"{round(cur['temperature'])}°"
        temp_surf = fonts["huge"].render(temp_str, True, TEXT_BRIGHT)
        screen.blit(temp_surf, (60, 140))
        icon_x = 60 + temp_surf.get_width() + 18 + 65
        draw_weather_icon(screen, cur['weathercode'], icon_x, 195, 130, GOLD)


        # Map box
        mx, my, mw, mh = 520, 140, 820, 720
        draw_map(screen, state, fonts, mx, my, mw, mh)

        # Wind widget — centered below the map
        draw_wind_widget(screen, state, fonts, mx + mw // 2, my + mh + 60)

        # 7-Day Forecast
        for i in range(7):
            ry = 140 + (i * 120)
            pygame.draw.rect(screen, PANEL, (W-430, ry, 390, 110), border_radius=15)
            dt = datetime.strptime(daily["time"][i], "%Y-%m-%d")
            code = daily["weathercode"][i]
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
            # Precipitation on its own bottom row, right-aligned — no overlap with desc or lo temp
            precip_mm = daily["precipitation_sum"][i]
            precip_in = precip_mm / 25.4
            if precip_in > 0.01:
                draw_text(screen, f"~{precip_in:.2f}\"", fonts["small"], RAIN, W-45, ry+78, anchor="topright")
            else:
                draw_text(screen, "Dry", fonts["small"], TEXT_DIM, W-45, ry+78, anchor="topright")

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
            elif _wkey == "POTOMAC":
                draw_potomac_widget(screen, state, fonts, _COL_WIDGET_X, _wy, w=_COL_W, h=_wh)
            elif _wkey == "NFLSTATS":
                draw_nflstats_widget(screen, state, fonts, _COL_WIDGET_X, _wy, w=_COL_W, h=_wh)
            elif _wkey == "RAM_PRICE":
                draw_ram_widget(screen, state, fonts, _COL_X, _wy)


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

    threading.Thread(target=state.update_weather, daemon=True).start()
    threading.Thread(target=state.update_quotes,  daemon=True).start()
    state.last_weather_upd = time.time()
    state.last_map_upd = time.time()
    state.last_ram_upd = time.time()

    def _delayed_map_start():
        time.sleep(5)
        state.update_map()

    def _delayed_ram_start():
        time.sleep(8)
        state.update_ram()

    def _delayed_tide_start():
        time.sleep(3)
        state.update_tides()

    def _delayed_osv_start():
        time.sleep(6)
        state.update_osv()

    def _delayed_potomac_start():
        time.sleep(7)
        state.update_potomac()

    def _delayed_nfl_start():
        time.sleep(9)
        state.update_nflstats()

    threading.Thread(target=_delayed_map_start,     daemon=True).start()
    if FEATURES["RAM_PRICE"]: threading.Thread(target=_delayed_ram_start,     daemon=True).start()
    if FEATURES["OSV_TIDES"]: threading.Thread(target=_delayed_tide_start,    daemon=True).start()
    if FEATURES["OSV_COUNT"]: threading.Thread(target=_delayed_osv_start,     daemon=True).start()
    if FEATURES["POTOMAC"]:   threading.Thread(target=_delayed_potomac_start, daemon=True).start()
    if FEATURES["NFLSTATS"] and NFL_API_KEY: threading.Thread(target=_delayed_nfl_start, daemon=True).start()

    clock, tick = pygame.time.Clock(), 0
    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_q):
                pygame.quit()
                sys.exit()

        now = time.time()
        if now - state.last_weather_upd > 60 and not state._weather_running:
            state.last_weather_upd = now
            threading.Thread(target=state.update_weather, daemon=True).start()
        if now - state.last_map_upd > 60 and not state._map_running:
            state.last_map_upd = now
            threading.Thread(target=state.update_map, daemon=True).start()
        if now - state.last_ram_upd > 60 and not state._ram_running and FEATURES["RAM_PRICE"]:
            state.last_ram_upd = now
            threading.Thread(target=state.update_ram, daemon=True).start()
        if now - state.last_tide_upd > 300 and not state._tide_running and FEATURES["OSV_TIDES"]:
            state.last_tide_upd = now
            threading.Thread(target=state.update_tides, daemon=True).start()
        if now - state.last_osv_upd > 60 and not state._osv_running and FEATURES["OSV_COUNT"]:
            state.last_osv_upd = now
            threading.Thread(target=state.update_osv, daemon=True).start()
        if now - state.last_potomac_upd > 300 and not state._potomac_running and FEATURES["POTOMAC"]:
            state.last_potomac_upd = now
            threading.Thread(target=state.update_potomac, daemon=True).start()
        if now - state.last_nfl_upd > 300 and not state._nfl_running and FEATURES["NFLSTATS"] and NFL_API_KEY:
            state.last_nfl_upd = now
            threading.Thread(target=state.update_nflstats, daemon=True).start()

        draw_screen(screen, state, fonts, tick)
        pygame.display.flip()
        clock.tick(10)
        tick += 1


if __name__ == "__main__":
    main()

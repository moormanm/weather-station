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

# ── Configuration ─────────────────────────────────────────────────────────────
# LATITUDE, LONGITUDE = 38.395392,  -75.1557415
# LOCATION_NAME = "Ocean Pines, MD"

LATITUDE, LONGITUDE = 39.4662, -77.4068
LOCATION_NAME = "Frederick, MD"

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
        raw = safe_fetch("https://zenquotes.io/api/quotes", timeout=10)
        if raw and raw != "RATE_LIMITED":
            quotes = json.loads(raw.decode())
            result = [f"{q['q'].strip()} — {q['a']}" for q in quotes if q.get("q") and q.get("a")]
            if result:
                return result
    except Exception as e:
        print(f"Quote fetch error: {e}")
    return []


def safe_fetch(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
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


def draw_screen(screen, state, fonts, tick):
    screen.fill(BG)
    W, H = screen.get_size()

    with state.lock:
        draw_text(screen, LOCATION_NAME, fonts["large"], TEXT_BRIGHT, 60, 40)
        draw_text(screen, datetime.now().strftime("%I:%M %p"), fonts["medium"], TEXT_DIM, 60, 100)

        # Last-updated helper (used in multiple sections)
        def _fmt_upd(ts):
            if ts == 0:
                return "never"
            return datetime.fromtimestamp(ts).strftime("%-I:%M %p")

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

        # RAM price widget — bottom left
        draw_ram_widget(screen, state, fonts, 60, 870)

        # All "updated at" timestamps — top-right corner
        draw_text(screen, f"Forecast updated at:   {_fmt_upd(state.last_weather_upd)}", fonts["tiny"], TEXT_DIM, W-20, 20, anchor="topright")
        draw_text(screen, f"Radar updated at:      {_fmt_upd(state.last_map_upd)}",     fonts["tiny"], TEXT_DIM, W-20, 42, anchor="topright")
        draw_text(screen, f"RAM price updated at:  {_fmt_upd(state.last_ram_upd)}",     fonts["tiny"], TEXT_DIM, W-20, 64, anchor="topright")

    # MOTD — shrink font until it fits within the screen width with padding
    max_w = W - 80
    for font_key in ("small", "tiny"):
        motd_surf = fonts[font_key].render(state.motd, True, GOLD)
        if motd_surf.get_width() <= max_w:
            break
    screen.blit(motd_surf, motd_surf.get_rect(center=(W // 2, H - 40)))

    # Cycle MOTD every 30 seconds; refetch a new batch when pool is exhausted
    now = time.time()
    if state.motd_pool and now - state.motd_last_cycle > 30:
        next_index = state.motd_index + 1
        if next_index >= len(state.motd_pool):
            # Pool exhausted — fetch a fresh batch in the background
            threading.Thread(target=state.update_quotes, daemon=True).start()
        else:
            state.motd_index = next_index
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

    threading.Thread(target=_delayed_map_start, daemon=True).start()
    threading.Thread(target=_delayed_ram_start, daemon=True).start()

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
        if now - state.last_ram_upd > 60 and not state._ram_running:
            state.last_ram_upd = now
            threading.Thread(target=state.update_ram, daemon=True).start()

        draw_screen(screen, state, fonts, tick)
        pygame.display.flip()
        clock.tick(10)
        tick += 1


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Weather Station - Stability Edition
Added 429 Rate-Limit Backoff & GPU Memory Optimizations
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
import io
from datetime import datetime

# ── Configuration ─────────────────────────────────────────────────────────────
LATITUDE, LONGITUDE = 39.4662, -77.4068
LOCATION_NAME = "Frederick, MD"
TIMEZONE = "America/New_York"
SCREEN_W, SCREEN_H = 1920, 1080

# Sub-tile pixel offset of exact location within its zoom-10 tile.
# Used to position the location dot accurately on the map.
def _loc_pixel_offset(lat, lon, zoom=10):
    import math
    n = 2 ** zoom
    lat_rad = math.radians(lat)
    xt = int((lon + 180.0) / 360.0 * n)
    yt = int((1.0 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2.0 * n)
    fx = (lon + 180.0) / 360.0 * n - xt  # fractional position within tile [0,1)
    fy = (1.0 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2.0 * n - yt
    # Pixel offset from center of the 256px tile
    return int(fx * 256 - 128), int(fy * 256 - 128)

LOC_DOT_OFFSET = _loc_pixel_offset(LATITUDE, LONGITUDE)  # (dx_px, dy_px) from box center

USER_AGENT = "FrederickWeatherStation/1.7 (RPi3B+; Dashboard)"
HEADERS = {"User-Agent": USER_AGENT}

# ── Colors ────────────────────────────────────────────────────────────────────
BG, PANEL, PANEL2 = (10, 14, 26), (18, 24, 42), (22, 30, 52)
ACCENT, GOLD, RAIN = (64, 196, 255), (255, 200, 80), (60, 140, 255)
TEXT_DIM, TEXT_BRIGHT = (110, 130, 170), (255, 255, 255)
# Icons: only use codepoints confirmed present in DejaVu Sans on Raspberry Pi OS
# U+2600 ☀  U+2601 ☁  U+2602 ☂  U+2603 ☃  — core Misc Symbols, always present
# U+26C6 ⛆  — confirmed working in session
# Avoid ⛅ ☔ ❄ ⚡ — unreliable across DejaVu versions; use ☁ / ☂ / ☃ / ☂ instead
WMO_ICON = {
    0:"☀",  1:"☀",  2:"☁",  3:"☁",
    45:"☂", 48:"☂",
    51:"☂", 53:"☂", 55:"☂",
    61:"☂", 63:"☂", 65:"☂",
    71:"☃", 73:"☃", 75:"☃", 77:"☃",
    80:"☂", 81:"☂", 82:"☂",
    85:"☃", 86:"☃",
    95:"☂", 96:"☂", 99:"☂",
}
WMO_DESC = {
    0:"Clear sky",     1:"Mainly clear",   2:"Partly cloudy",  3:"Overcast",
    45:"Fog",          48:"Icy fog",
    51:"Lt drizzle",   53:"Drizzle",        55:"Hvy drizzle",
    61:"Lt rain",      63:"Rain",           65:"Hvy rain",
    71:"Lt snow",      73:"Snow",           75:"Hvy snow",      77:"Snow grains",
    80:"Showers",      81:"Showers",        82:"Hvy showers",
    85:"Snow showers", 86:"Hvy snow shwr",
    95:"Thunderstorm", 96:"T-storm/hail",   99:"Hvy t-storm",
}

TILE_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "weather_station_tiles")
os.makedirs(TILE_CACHE_DIR, exist_ok=True)

# ── Data Core ─────────────────────────────────────────────────────────────────
WIND_DIR = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]

def _wind_direction(deg):
    return WIND_DIR[round(deg / 22.5) % 16]

def _build_motd_pool(data):
    """Return a list of data-driven messages from the latest weather payload."""
    cur = data.get("current_weather", {})
    daily = data.get("daily", {})
    messages = []

    temp = cur.get("temperature")
    if temp is not None:
        messages.append(f"Currently {round(temp)}\u00b0F.")

    wind_speed = cur.get("windspeed")
    wind_dir = cur.get("winddirection")
    if wind_speed is not None and wind_dir is not None:
        messages.append(f"Winds {_wind_direction(wind_dir)} at {round(wind_speed)} mph.")

    try:
        hi = round(daily["temperature_2m_max"][0])
        lo = round(daily["temperature_2m_min"][0])
        messages.append(f"Today: high {hi}\u00b0F, low {lo}\u00b0F.")
    except (KeyError, IndexError, TypeError):
        pass

    try:
        precip_mm = daily["precipitation_sum"][0]
        precip_in = precip_mm / 25.4
        if precip_in > 0.01:
            messages.append(f"{precip_in:.2f}\" of rain expected today.")
        else:
            messages.append("No precipitation expected today.")
    except (KeyError, IndexError, TypeError):
        pass

    try:
        code = cur.get("weathercode")
        desc = WMO_DESC.get(code)
        if desc:
            messages.append(f"Conditions: {desc}.")
    except Exception:
        pass

    return messages if messages else ["Loading weather..."]


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

class AppState:
    def __init__(self):
        self.weather = None
        self.map_tiles = {}
        self.radar_tiles = {}
        self.radar_crop_data = None
        self.radar_crop_rect = None
        self.last_weather_upd = 0
        self.last_map_upd = 0
        self.backoff_until = 0
        self.motd = "Fetching weather..."
        self.motd_pool = []
        self.motd_index = 0
        self.motd_last_cycle = 0
        self.lock = threading.Lock()
        self._weather_running = False
        self._map_running = False

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
               f"&current_weather=true&temperature_unit=fahrenheit&timezone={TIMEZONE}&forecast_days=7")
        
        raw = safe_fetch(url)
        if raw == "RATE_LIMITED":
            self.backoff_until = time.time() + 1800 # Wait 30 mins
            self.motd = "Weather service busy. Retrying later..."
            return

        if raw:
            try:
                data = json.loads(raw.decode())
                with self.lock:
                    self.weather = data
                    self.last_weather_upd = time.time()
                    self.motd_pool = _build_motd_pool(data)
                    self.motd_index = 0
                    self.motd = self.motd_pool[0]
            except Exception as e:
                print(f"Weather parse error: {e}")
                self.last_weather_upd = 0  # Allow retry on next cycle
        else:
            self.last_weather_upd = 0  # Fetch failed — retry next cycle

    def update_map(self):
        if self._map_running:
            return
        self._map_running = True
        try:
            self._do_update_map()
        finally:
            self._map_running = False

    def _do_update_map(self):
        zoom = 10
        lat_rad = math.radians(LATITUDE)
        n = 2**zoom
        xt = int((LONGITUDE + 180.0) / 360.0 * n)
        yt = int((1.0 - math.log(math.tan(lat_rad) + (1 / math.cos(lat_rad))) / math.pi) / 2.0 * n)
        
        # Base Tiles — light_all for readable colors
        new_tiles = {}
        for dy in range(-1, 2):
            for dx in range(-1, 2):
                url = f"https://basemaps.cartocdn.com/light_all/{zoom}/{xt+dx}/{yt+dy}.png"
                fname = os.path.join(TILE_CACHE_DIR, hashlib.md5(url.encode()).hexdigest() + ".png")
                if os.path.exists(fname):
                    with open(fname, "rb") as f: data = f.read()
                else:
                    data = safe_fetch(url, timeout=5)
                    if data and data != "RATE_LIMITED" and len(data) > 2000:
                        with open(fname, "wb") as f: f.write(data)
                if data and data != "RATE_LIMITED": new_tiles[(dx, dy)] = data
        
        # Radar — RainViewer only supports zoom ≤ 7 for x/y tile endpoint.
        # Fetch a 2×2 grid of zoom-7 tiles to guarantee full coverage of the
        # 3×3 zoom-10 base tile area regardless of where the location falls
        # within a tile.  The 2×2 grid is stitched (512×512) and then scaled
        # 4× to 2048×2048 in draw_screen before cropping.
        # Color scheme 6 = RAINBOW@SELEX — vibrant, easy to read.
        new_radar_tiles = {}
        new_radar_crop_rect = None
        meta = safe_fetch("https://api.rainviewer.com/public/weather-maps.json")
        if meta and meta != "RATE_LIMITED":
            try:
                rv = json.loads(meta.decode())
                path, host = rv["radar"]["past"][-1]["path"], rv["host"]
                rz = 7  # max supported zoom
                scale = 2 ** (zoom - rz)  # = 8 (zoom-10 pixels per zoom-7 tile pixel)
                # Top-left zoom-7 tile that contains our zoom-10 center tile
                rx = xt >> (zoom - rz)
                ry_tile = yt >> (zoom - rz)
                # Fetch 2×2 grid: (rx,ry), (rx+1,ry), (rx,ry+1), (rx+1,ry+1)
                all_ok = True
                fetched = {}
                for tdx in range(2):
                    for tdy in range(2):
                        ru = f"{host}{path}/256/{rz}/{rx+tdx}/{ry_tile+tdy}/6/1_1.png"
                        raw = safe_fetch(ru, timeout=10)
                        if raw and raw != "RATE_LIMITED" and len(raw) > 2000:
                            fetched[(tdx, tdy)] = raw
                        else:
                            all_ok = False
                            print(f"Radar tile ({tdx},{tdy}) missing or invalid (len={len(raw) if raw and raw != 'RATE_LIMITED' else 0})")
                if fetched:
                    # The stitched 512×512 surface covers 2×2 zoom-7 tiles = 16×16 zoom-10 tiles.
                    # We scale it 8× to 4096×4096 so that 1 zoom-10 tile = 256px,
                    # exactly matching the basemap tile resolution.
                    # Pixel position of center zoom-10 tile (xt,yt) in the 4096px surface:
                    px = (xt - rx * scale) * 256   # scale=8, so offset in zoom-10 tiles × 256px
                    py = (yt - ry_tile * scale) * 256
                    # Crop 768×768 = 3×3 zoom-10 tiles starting 1 tile before center
                    crop_x = px - 256
                    crop_y = py - 256
                    new_radar_tiles = fetched
                    new_radar_crop_rect = (max(0, crop_x), max(0, crop_y), 768, 768)
            except Exception as e:
                print(f"Radar fetch error: {e}")

        with self.lock:
            self.map_tiles = new_tiles
            self.radar_tiles = new_radar_tiles
            self.radar_crop_data = None  # unused (single-tile approach replaced)
            self.radar_crop_rect = new_radar_crop_rect
            self.last_map_upd = time.time()

# ── UI Rendering ──────────────────────────────────────────────────────────────
def draw_text(surf, text, font, color, x, y, anchor="topleft"):
    img = font.render(str(text), True, color)
    surf.blit(img, img.get_rect(**{anchor: (x, y)}))

def draw_screen(screen, state, fonts, tick):
    screen.fill(BG)
    W, H = screen.get_size()

    with state.lock:
        draw_text(screen, LOCATION_NAME, fonts["large"], TEXT_BRIGHT, 60, 40)
        draw_text(screen, datetime.now().strftime("%I:%M %p"), fonts["medium"], TEXT_DIM, 60, 100)

        if not state.weather:
            draw_text(screen, state.motd, fonts["small"], GOLD, W//2, H//2, anchor="center")
            # MOTD already drawn above; skip the rest of the weather UI
            return

        cur, daily = state.weather["current_weather"], state.weather["daily"]
        # Measure temp width so the icon sits flush to the right of it without overlap
        temp_str = f"{round(cur['temperature'])}°"
        temp_surf = fonts["huge"].render(temp_str, True, TEXT_BRIGHT)
        screen.blit(temp_surf, (60, 180))
        icon_x = 60 + temp_surf.get_width() + 20  # 20px gap after the degrees symbol
        draw_text(screen, WMO_ICON.get(cur['weathercode'], "☀"), fonts["huge_icon"], GOLD, icon_x, 200)

        # Map Box — no background rect; tiles fill the area directly
        mx, my, mw, mh = (W//2)-440, 180, 800, 700
        
        if state.map_tiles:
            for (dx, dy), d in state.map_tiles.items():
                try:
                    img = pygame.image.load(io.BytesIO(d)).convert_alpha()
                    screen.blit(img, (mx + (mw-768)//2 + (dx+1)*256, my + (mh-768)//2 + (dy+1)*256))
                except: pass
            # Overlay radar: stitch 2×2 zoom-7 tiles → scale 4× to 2048px → crop 768×768
            if state.radar_tiles and state.radar_crop_rect:
                try:
                    stitched = pygame.Surface((512, 512), pygame.SRCALPHA)
                    for (tdx, tdy), blob in state.radar_tiles.items():
                        t = pygame.image.load(io.BytesIO(blob)).convert_alpha()
                        stitched.blit(t, (tdx * 256, tdy * 256))
                    # Scale 512×512 → 4096×4096 (8×) so 1 zoom-10 tile = 256px,
                    # matching the basemap tile resolution exactly.
                    rd = pygame.transform.scale(stitched, (4096, 4096))
                    cx, cy, cw, ch = state.radar_crop_rect
                    # Safety clamp to avoid subsurface out-of-bounds
                    cx = max(0, min(cx, 4096 - cw))
                    cy = max(0, min(cy, 4096 - ch))
                    crop = rd.subsurface(pygame.Rect(cx, cy, cw, ch)).copy()
                    crop.set_alpha(180)
                    screen.blit(crop, (mx + (mw-768)//2, my + (mh-768)//2))
                except Exception as e:
                    print(f"Radar draw error: {e}")
            pygame.draw.circle(screen, ACCENT, (mx+mw//2 + LOC_DOT_OFFSET[0], my+mh//2 + LOC_DOT_OFFSET[1]), 8, 2)
        
        # 7-Day Forecast
        # Box is 380×95 px starting at (W-420, ry).
        # Columns: icon(left) | day+desc(mid-left) | hi/lo(mid-right) | precip(right)
        for i in range(7):
            ry = 180 + (i * 105)
            pygame.draw.rect(screen, PANEL, (W-420, ry, 380, 95), border_radius=15)
            dt = datetime.strptime(daily["time"][i], "%Y-%m-%d")
            code = daily["weathercode"][i]
            # Weather icon — far left, vertically centered in box
            draw_text(screen, WMO_ICON.get(code, "☀"), fonts["icon"], GOLD, W-415, ry+24)
            # Day name + description stacked in second column
            draw_text(screen, dt.strftime("%a").upper(), fonts["small"], TEXT_BRIGHT, W-365, ry+10)
            desc = WMO_DESC.get(code, "")
            draw_text(screen, desc, fonts["tiny"], ACCENT, W-365, ry+48)
            # High / Low temps — third column
            hi = round(daily["temperature_2m_max"][i])
            lo = round(daily["temperature_2m_min"][i])
            draw_text(screen, f"{hi}°", fonts["medium"], TEXT_BRIGHT, W-185, ry+8)
            draw_text(screen, f"{lo}°", fonts["small"], TEXT_DIM,    W-185, ry+50)
            # Precipitation — rightmost column, right-aligned to box inner edge
            precip_mm = daily["precipitation_sum"][i]
            precip_in = precip_mm / 25.4
            if precip_in > 0.01:
                draw_text(screen, f"~{precip_in:.2f}\"", fonts["tiny"], RAIN, W-48, ry+36, anchor="topright")
            else:
                draw_text(screen, "Dry", fonts["tiny"], TEXT_DIM, W-48, ry+36, anchor="topright")

    draw_text(screen, state.motd, fonts["small"], GOLD, W//2, H-80, anchor="center")

    # Cycle MOTD every 30 seconds through the pool
    now = time.time()
    if state.motd_pool and now - state.motd_last_cycle > 30:
        state.motd_index = (state.motd_index + 1) % len(state.motd_pool)
        state.motd = state.motd_pool[state.motd_index]
        state.motd_last_cycle = now
def main():
    pygame.init()
    # Explicitly using the framebuffer or X11 surface
    screen = pygame.display.set_mode((1920, 1080), pygame.FULLSCREEN | pygame.DOUBLEBUF | pygame.HWSURFACE)
    pygame.mouse.set_visible(False)

    f_p = "dejavusans"
    fonts = {
        "huge": pygame.font.SysFont(f_p, 200, True),
        "huge_icon": pygame.font.SysFont(f_p, 150),
        "large": pygame.font.SysFont(f_p, 65, True),
        "medium": pygame.font.SysFont(f_p, 40),
        "small": pygame.font.SysFont(f_p, 30),
        "tiny": pygame.font.SysFont(f_p, 20),
        "icon": pygame.font.SysFont(f_p, 46)
    }

    state = AppState()
    # Start weather fetch immediately; delay map fetch by 5 s without blocking the main thread
    threading.Thread(target=state.update_weather, daemon=True).start()
    state.last_weather_upd = time.time()  # Prevent main loop from re-triggering before first fetch lands
    state.last_map_upd = time.time()      # Same for map — first update fires after 10 min or on flag clear

    def _delayed_map_start():
        time.sleep(5)
        state.update_map()  # Uses _map_running guard properly

    threading.Thread(target=_delayed_map_start, daemon=True).start()

    clock, tick = pygame.time.Clock(), 0
    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_q):
                pygame.quit(); sys.exit()
        
        now = time.time()
        if now - state.last_weather_upd > 900 and not state._weather_running:
            state.last_weather_upd = now  # Prevent re-trigger until this fetch completes
            threading.Thread(target=state.update_weather, daemon=True).start()
        if now - state.last_map_upd > 600 and not state._map_running:
            state.last_map_upd = now
            threading.Thread(target=state.update_map, daemon=True).start()

        draw_screen(screen, state, fonts, tick)
        pygame.display.flip()
        clock.tick(10) # 10 FPS is plenty and saves GPU
        tick += 1

if __name__ == "__main__": main()

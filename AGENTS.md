# AGENTS.md

Single-file Python/pygame weather-dashboard kiosk. All code lives in `weather_station.py` (≈1 250 lines).

## Prerequisites — must do before running

```bash
cp ws-settings.ini.example ~/ws-settings.ini
# Edit ~/ws-settings.ini: set LATITUDE, LONGITUDE, NAME under [LOCATION]
```

`~/ws-settings.ini` is read at **module import time**, before `main()`. The app crashes immediately if the file is absent. It lives in `$HOME`, not the project directory.

## Developer commands

```bash
# Activate venv (either works — both have only pygame==2.6.1)
source .venv/bin/activate   # preferred: subdirectory venv
# OR: source bin/activate   # root-level venv (same Python, same packages)

# Run (requires a display — $DISPLAY or Wayland)
python weather_station.py

# Format (Black is not installed by default)
pip install black
black weather_station.py
```

**No build, no tests, no linter, no typecheck, no CI.**

## Architecture notes

- `AppState` (lines 339–618): single state object with a `threading.Lock()`. Each data source has a `_running` flag (prevents overlapping threads) and a `last_X_upd` timestamp (polling interval). Drawing holds `state.lock` for the entire frame.
- All HTTP uses stdlib `urllib.request` — no `requests`/`httpx`.
- Tile cache: `~/.cache/weather_station_tiles/<md5_of_url>.png`. Basemap tiles are cached indefinitely; radar tiles are always re-fetched.
- Exit: press `Q`.

## Left-column layout engine

The left column (x=60–520, y=350–1000) is managed by `_left_column_layout()`. Do **not** hardcode y-coordinates for left-column widgets.

- `_COL_CATALOG` (module level, ~line 906) defines widget order, minimum heights, and which widgets are "growable" (charts). Edit this list to reorder or add widgets.
- Growable widgets (`OSV_TIDES`, `HOURLY_FORECAST`, `POTOMAC`) expand equally to fill available space, capped at `_COL_MAX_H = 400 px`.
- Fixed widgets (`SUNTIME`, `OSV_COUNT`, `RAM_PRICE`) keep their catalog `min_h`.
- The loop in `draw_screen()` dispatches to each draw function with the computed `(y, h)`.

## Features / widgets

All features are off by default. Enable in `~/ws-settings.ini` under `[FEATURES]`:

| Key | Widget | Growable? | Notes |
|---|---|---|---|
| `SUNTIME` | Today's sunrise/sunset times | No | Data already in Open-Meteo response — no extra API call |
| `OSV_TIDES` | Ocean City tide chart | **Yes** | Fetches NOAA station 8570283 |
| `OSV_COUNT` | Assateague OSV count bar | No | Scrapes osvcount.com |
| `HOURLY_FORECAST` | 8-hour temp + humidity chart | **Yes** | Uses `hourly` data from Open-Meteo |
| `POTOMAC` | Potomac River 7-day gage height chart | **Yes** | USGS station 01646500 (Little Falls, MD); `period=P7D`; polls every 3600 s |
| `NFLSTATS` | NFC North standings | No | balldontlie NFL standings; requires `~/ws-settings.ini` `[NFLSTATS] API-KEY=` |
| `RAM_PRICE` | DDR5 spot price ticker | No | Scrapes dramexchange.com |

`HOURLY_FORECAST` requires `hourly=temperature_2m,relativehumidity_2m` in the Open-Meteo URL — this is already present in the code.

## Hardcoded constants — must update together when changing location

If `LATITUDE`/`LONGITUDE` in `~/ws-settings.ini` are changed to a different NWS grid area, **also** update in `weather_station.py`:

```python
NWS_FORECAST_URL = "https://api.weather.gov/gridpoints/LWX/80,95/forecast"  # grid office + cell
NWS_STATIONS_URL = "https://api.weather.gov/stations/KFDK/observations?..."  # ASOS station
```

Mismatched coordinates + hardcoded NWS constants produce silently wrong observed hi/lo temperatures.

Also hardcoded (not in settings): `TIMEZONE = "America/New_York"`, `SCREEN_W, SCREEN_H = 1920, 1080`, NOAA tide station `8570283`, font `"dejavusans"`.

## Production (Raspberry Pi) — do not change carelessly

- `launch.sh` runs `git pull origin main` on every launch — **any push to `main` is auto-deployed at next RPi start**.
- Production uses `/usr/bin/python3` (system Python), not the venv.
- `./install.sh` installs the autostart desktop entry (`~/.config/autostart/`).

## Repo quirks

- The root directory is also a venv (`bin/`, `include/`, `lib/`, `pyvenv.cfg` at root are venv artefacts, gitignored). `.venv/` is the cleaner copy — use that for development.
- `.idea/` PyCharm config is **committed** (not gitignored). Black is configured there as the IDE formatter but is not in the venv.
- RAM price history persists in `~/.cache/weather_station_tiles/ram_price_history.json`.

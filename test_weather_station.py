import importlib.util
import json
import math
import os
import pathlib
import runpy
import subprocess
import sys
import tempfile
import unittest
from configparser import ConfigParser
from unittest.mock import MagicMock, patch


def _read_test_config(config, path):
    config.read_dict({
        "LOCATION": {"LATITUDE": "39.46694", "LONGITUDE": "-77.40687", "NAME": "Frederick, MD"},
        "FEATURES": {},
    })


with (
    patch("pathlib.Path.exists", return_value=True),
    patch("configparser.ConfigParser.read", _read_test_config),
    patch("os.makedirs"),
):
    import weather_station as ws


class DeploymentSettingsTests(unittest.TestCase):
    def setUp(self):
        self.config = ConfigParser()
        _read_test_config(self.config, None)
        self.config.read_dict({
            "GENERAL": {"DEPLOYMENT_ID": "test-station", "SELF_UPDATE_INTERVAL_S": "21600"},
            "FEATURES": {"HOURLY_FORECAST": "false", "POTOMAC": "true"},
        })

    def apply_remote(self, data):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = data
        with patch.object(ws.urllib.request, "urlopen", return_value=response) as fetch:
            merged = ws._apply_deployment_settings(self.config)
        fetch.assert_called_once_with(
            "https://raw.githubusercontent.com/moormanm/weather-station/main/"
            "deployments/test-station.ini",
            timeout=5,
        )
        return merged

    def test_partial_overrides_keep_local_values_and_deployment_id(self):
        merged = self.apply_remote(
            b"[LOCATION]\nNAME=Remote station\n"
            b"[FEATURES]\nHOURLY_FORECAST=true\n"
            b"[GENERAL]\nDEPLOYMENT_ID=other-station\nSELF_UPDATE_INTERVAL_S=3600\n"
        )
        self.assertEqual(merged.get("LOCATION", "NAME"), "Remote station")
        self.assertEqual(merged.getfloat("LOCATION", "LATITUDE"), 39.46694)
        self.assertTrue(merged.getboolean("FEATURES", "HOURLY_FORECAST"))
        self.assertTrue(merged.getboolean("FEATURES", "POTOMAC"))
        self.assertEqual(merged.get("GENERAL", "DEPLOYMENT_ID"), "test-station")
        self.assertEqual(merged.getint("GENERAL", "SELF_UPDATE_INTERVAL_S"), 3600)
        self.assertEqual(self.config.get("LOCATION", "NAME"), "Frederick, MD")
        self.assertFalse(self.config.getboolean("FEATURES", "HOURLY_FORECAST"))

    def test_disabled_or_invalid_id_does_not_make_a_request(self):
        for deployment_id in ("", "../other", "a/b", "a?b", "a" * 129):
            with self.subTest(deployment_id=deployment_id):
                self.config.set("GENERAL", "DEPLOYMENT_ID", deployment_id)
                with patch.object(ws.urllib.request, "urlopen") as fetch:
                    self.assertIs(ws._apply_deployment_settings(self.config), self.config)
                fetch.assert_not_called()

    def test_network_errors_use_local_settings(self):
        for error in (
            TimeoutError("Connection timed out"),
            ws.urllib.error.URLError("Offline"),
            ws.urllib.error.HTTPError("url", 404, "Not found", {}, None),
        ):
            if isinstance(error, ws.urllib.error.HTTPError):
                self.addCleanup(error.close)
            with self.subTest(error=error), patch.object(
                ws.urllib.request, "urlopen", side_effect=error
            ):
                self.assertIs(ws._apply_deployment_settings(self.config), self.config)

    def test_invalid_remote_settings_are_not_partially_applied(self):
        for data in (
            b"not an INI file",
            b"\xff",
            b"x" * 65537,
            b"[LOCATION]\nNAME=Remote\nLATITUDE=nan\n",
            b"[LOCATION]\nLONGITUDE=181\n",
            b"[LOCATION]\nMAP_LAT=91\n",
            b"[LOCATION]\nNAME=\n",
            b"[FEATURES]\nSUNTIME=invalid\n",
            b"[GENERAL]\nSELF_UPDATE_INTERVAL_S=0\n",
        ):
            with self.subTest(data=data[:80]):
                self.assertIs(self.apply_remote(data), self.config)

    def test_remote_settings_are_applied_before_app_constants(self):
        def read_config(config, path):
            config.read_dict(self.config)

        response = MagicMock()
        response.__enter__.return_value.read.return_value = (
            b"[LOCATION]\nLATITUDE=40\nNAME=Remote station\n"
            b"[FEATURES]\nHOURLY_FORECAST=true\n"
        )
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("configparser.ConfigParser.read", read_config),
            patch("os.makedirs"),
            patch("urllib.request.urlopen", return_value=response),
        ):
            app = runpy.run_path(ws.__file__)
        self.assertEqual(app["LATITUDE"], 40)
        self.assertEqual(app["LOCATION_NAME"], "Remote station")
        self.assertTrue(app["FEATURES"]["HOURLY_FORECAST"])


class DisplayScalingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ws.pygame.font.init()
        cls.font_path = os.path.join(os.path.dirname(ws.__file__), "assets", "LiberationSans-Regular.ttf")

    def test_dashboard_fits_common_display_sizes_without_stretching(self):
        cases = [
            ((1280, 720), (0, 0, 1280, 720)),
            ((1920, 1080), (0, 0, 1920, 1080)),
            ((1512, 982), (0, 66, 1512, 850)),
            ((3456, 2234), (0, 145, 3456, 1944)),
            ((2560, 1440), (0, 0, 2560, 1440)),
            ((3840, 2160), (0, 0, 3840, 2160)),
            ((3440, 1440), (440, 0, 2560, 1440)),
            ((1280, 1024), (0, 152, 1280, 720)),
            ((1080, 1920), (0, 657, 1080, 607)),
        ]
        for size, bounds in cases:
            with self.subTest(size=size):
                display = ws.pygame.Surface(size)
                display.fill(ws.RED)
                screen = ws._display_surface(display)
                screen.fill(ws.ACCENT)
                ws._Draw.rect(screen, ws.GOLD, (960, 0, 960, 1080))
                viewport = ws.pygame.Rect(bounds)
                for point, color in (
                    (viewport.topleft, ws.ACCENT),
                    ((viewport.left, viewport.bottom - 1), ws.ACCENT),
                    ((viewport.right - 1, viewport.bottom - 1), ws.GOLD),
                ):
                    self.assertEqual(display.get_at(point)[:3], color)
                if viewport.left:
                    self.assertEqual(display.get_at((viewport.left - 1, viewport.centery))[:3], ws.BG)
                    self.assertEqual(display.get_at((viewport.right, viewport.centery))[:3], ws.BG)
                if viewport.top:
                    self.assertEqual(display.get_at((viewport.centerx, viewport.top - 1))[:3], ws.BG)
                    self.assertEqual(display.get_at((viewport.centerx, viewport.bottom))[:3], ws.BG)

    def test_full_hd_keeps_original_pixels_without_resampling(self):
        display = ws.pygame.Surface((1920, 1080))
        screen = ws._display_surface(display)
        with patch.object(ws.pygame.transform, "smoothscale") as scale:
            ws.draw_text(screen, "Forecast", ws._RenderFont(self.font_path, 30, 1), ws.GOLD, 123, 456)
        scale.assert_not_called()

    def test_text_is_rendered_at_native_size_without_bitmap_scaling(self):
        for size in ((1280, 720), (2560, 1440), (3840, 2160)):
            with self.subTest(size=size):
                display = ws.pygame.Surface(size)
                screen = ws._display_surface(display)
                font = ws._RenderFont(self.font_path, 30, screen.scale)
                expected = ws.pygame.font.Font(self.font_path, round(30 * screen.scale)).render(
                    "Forecast 67°", True, ws.GOLD
                )
                with patch.object(ws.pygame.transform, "smoothscale") as scale:
                    image = font.render("Forecast 67°", True, ws.GOLD)
                    ws.draw_text(screen, "Forecast 67°", font, ws.GOLD, 60, 100)
                scale.assert_not_called()
                self.assertEqual(image.surface.get_size(), expected.get_size())
                self.assertEqual(
                    ws.pygame.image.tobytes(image.surface, "RGBA"),
                    ws.pygame.image.tobytes(expected, "RGBA"),
                )
                reference = ws.pygame.Surface(size)
                reference.fill(ws.BG)
                reference.blit(expected, screen.point((60, 100)))
                self.assertEqual(
                    ws.pygame.image.tobytes(display, "RGB"),
                    ws.pygame.image.tobytes(reference, "RGB"),
                )

    def test_chart_lines_use_native_coordinates_and_widths(self):
        display = ws.pygame.Surface((3840, 2160))
        screen = ws._display_surface(display)
        reference = display.copy()
        ws.pygame.draw.line(reference, ws.GOLD, (120, 200), (240, 400), 4)
        ws.pygame.draw.circle(reference, ws.ACCENT, (600, 800), 10, 2)
        with patch.object(ws.pygame.transform, "smoothscale") as scale:
            ws._Draw.line(screen, ws.GOLD, (60, 100), (120, 200), 2)
            ws._Draw.circle(screen, ws.ACCENT, (300, 400), 5, 1)
        scale.assert_not_called()
        self.assertEqual(
            ws.pygame.image.tobytes(display, "RGB"),
            ws.pygame.image.tobytes(reference, "RGB"),
        )

    def test_widget_surfaces_keep_native_text_and_transparency(self):
        display = ws.pygame.Surface((2560, 1440))
        screen = ws._display_surface(display)
        child = screen.child((120, 100))
        font = ws._RenderFont(self.font_path, 20, screen.scale)
        with patch.object(ws.pygame.transform, "smoothscale") as scale:
            ws.draw_text(child, "Wind", font, ws.GOLD, 10, 10)
            screen.blit(child, (60, 100))
        scale.assert_not_called()
        self.assertEqual(child.surface.get_size(), (160, 133))
        self.assertEqual(display.get_at(screen.point((60, 100)))[:3], ws.BG)

    def test_map_and_wind_are_centered_on_macbook_and_hd_displays(self):
        state = ws.AppState()
        state.motd_last_cycle = ws.time.time()
        for size in ((1512, 982), (1920, 1080), (2560, 1440), (3440, 1440)):
            with self.subTest(size=size):
                display = ws.pygame.Surface(size)
                screen = ws._display_surface(display)
                fonts = {
                    key: ws._RenderFont(self.font_path, size, screen.scale)
                    for key, size in (("huge", 160), ("large", 65), ("medium", 40), ("small", 30), ("tiny", 20))
                }
                with patch.object(ws, "draw_map") as draw_map, patch.object(ws, "draw_wind_widget") as wind:
                    ws.draw_screen(screen, state, fonts, 0)
                mx, _, mw, _ = draw_map.call_args.args[3:]
                map_center = mx + mw / 2
                self.assertEqual(map_center, 960)
                self.assertEqual(wind.call_args.args[3], map_center)
                viewport = screen.surface.get_abs_offset()
                self.assertLessEqual(abs(viewport[0] + map_center * screen.scale - size[0] / 2), 1)

    def test_map_marker_and_rounded_mask_use_native_positions(self):
        state = ws.AppState()
        state.map_tiles = {(x, y): b"tile" for x in (-1, 0, 1) for y in (-1, 0, 1)}
        tile = ws.pygame.Surface((256, 256), ws.pygame.SRCALPHA)
        tile.fill(ws.PANEL)
        loaded = MagicMock()
        loaded.convert_alpha.return_value = tile
        for size in ((1512, 982), (1920, 1080), (3456, 2234)):
            with self.subTest(size=size):
                display = ws.pygame.Surface(size)
                screen = ws._display_surface(display)
                fonts = {"tiny": ws._RenderFont(self.font_path, 20, screen.scale)}
                with (
                    patch.object(ws.pygame.image, "load", return_value=loaded),
                    patch.object(ws, "MAP_LAT", ws.LATITUDE),
                    patch.object(ws, "MAP_LONG", ws.LONGITUDE),
                ):
                    ws.draw_map(screen, state, fonts, 550, 160, 820, 720)
                offset_x, offset_y = screen.surface.get_abs_offset()
                for point, color in (((960, 520), ws.TEXT_BRIGHT), ((576, 136), ws.BG)):
                    x, y = screen.point(point)
                    self.assertEqual(display.get_at((x + offset_x, y + offset_y))[:3], color)
                x, y = screen.point((600, 160))
                for actual, expected in zip(display.get_at((x + offset_x, y + offset_y))[:3], ws.PANEL):
                    self.assertLessEqual(abs(actual - expected), 3)

    def test_map_coverage_and_compass_center_match_in_rendered_pixels(self):
        n = 2 ** ws.MAP_ZOOM
        tile = ws.pygame.Surface((256, 256), ws.pygame.SRCALPHA)
        tile.fill(ws.PANEL)
        loaded = MagicMock()
        loaded.convert_alpha.return_value = tile
        for fraction in (0.1, 0.9):
            tile_x, tile_y = 40 + fraction, 50 + fraction
            longitude = tile_x / n * 360 - 180
            latitude = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * tile_y / n))))
            state = ws.AppState()
            metadata = json.dumps({"host": "https://radar.test", "radar": {"past": [{"path": "/frame"}]}}).encode()
            with (
                patch.object(ws, "MAP_LAT", latitude),
                patch.object(ws, "MAP_LONG", longitude),
                patch.object(ws.os.path, "exists", return_value=False),
                patch.object(ws, "safe_fetch", side_effect=lambda url, **kwargs: metadata if url.endswith("weather-maps.json") else b"\x89PNGtile"),
            ):
                state._do_update_map()
                self.assertEqual(len(state.map_tiles), 16)
                self.assertEqual(set(state.map_tiles), set(state.map_label_tiles))
                self.assertEqual(set(state.map_tiles), set(state.radar_tiles))
                state.map_label_tiles = {}
                state.radar_tiles = {}
                state.weather = {"current": {"temperature_2m": 67, "weather_code": 0, "wind_speed_10m": 18, "wind_direction_10m": 225}}
                state.motd_last_cycle = ws.time.time()
                for size in ((1512, 982), (1728, 1117), (1920, 1080), (3456, 2234)):
                    with self.subTest(fraction=fraction, size=size):
                        display = ws.pygame.Surface(size)
                        screen = ws._display_surface(display)
                        fonts = {
                            key: ws._RenderFont(self.font_path, font_size, screen.scale)
                            for key, font_size in (("huge", 160), ("large", 65), ("medium", 40), ("small", 30), ("tiny", 20))
                        }
                        with patch.object(ws.pygame.image, "load", return_value=loaded):
                            ws.draw_screen(screen, state, fonts, 0)
                        pixels = screen.surface
                        map_y = screen.point((0, 300))[1]
                        map_edges = [x for x in range(round(550 * screen.scale), round(1400 * screen.scale)) if pixels.get_at((x, map_y))[:3] != ws.BG]
                        compass_y = screen.point((0, 886))[1]
                        compass_edges = [x for x in range(round(550 * screen.scale), round(1400 * screen.scale)) if pixels.get_at((x, compass_y))[:3] == ws.TEXT_DIM]
                        expected_width = round(768 * screen.scale)
                        self.assertLessEqual(abs(map_edges[-1] - map_edges[0] + 1 - expected_width), 1)
                        self.assertLessEqual(abs(sum((map_edges[0], map_edges[-1])) / 2 - sum((compass_edges[0], compass_edges[-1])) / 2), 0.5)

    def test_mac_fullscreen_uses_retina_pixels_and_releases_display_mode(self):
        core = MagicMock()
        core.CGDisplayCopyDisplayMode.return_value = 123
        core.CGDisplayModeGetPixelWidth.return_value = 3456
        core.CGDisplayModeGetPixelHeight.return_value = 2234
        with (
            patch.object(ws.sys, "platform", "darwin"),
            patch.object(ws.pygame.display, "get_driver", return_value="cocoa"),
            patch("ctypes.CDLL", return_value=core),
        ):
            self.assertEqual(ws._fullscreen_size(), (3456, 2234))
        core.CGDisplayModeRelease.assert_called_once_with(123)

    def test_non_mac_displays_keep_sdl_resolution_detection(self):
        for platform, driver in (("linux", "x11"), ("win32", "windows"), ("darwin", "dummy")):
            with (
                self.subTest(platform=platform, driver=driver),
                patch.object(ws.sys, "platform", platform),
                patch.object(ws.pygame.display, "get_driver", return_value=driver),
                patch("ctypes.CDLL") as load,
            ):
                self.assertEqual(ws._fullscreen_size(), (0, 0))
                load.assert_not_called()


class ScreenInvalidationTests(unittest.TestCase):
    def test_redraw_is_skipped_until_state_or_display_time_changes(self):
        state = ws.AppState()
        state.started_at = 0
        state.weather_loaded_once = True
        state.motd_last_cycle = 100
        state.screen_dirty = False
        minute = "2026-11-07 15:00"

        redraw, loading = ws._screen_redraw_due(state, 120, minute, minute, False)
        self.assertFalse(redraw)
        self.assertFalse(loading)

        state.mark_screen_dirty()
        redraw, _ = ws._screen_redraw_due(state, 120, minute, minute, False)
        self.assertTrue(redraw)
        redraw, _ = ws._screen_redraw_due(state, 120, minute, minute, False)
        self.assertFalse(redraw)

        redraw, _ = ws._screen_redraw_due(state, 120, "2026-11-07 15:01", minute, False)
        self.assertTrue(redraw)
        redraw, _ = ws._screen_redraw_due(state, 131, minute, minute, False)
        self.assertTrue(redraw)

    def test_update_completion_marks_screen_dirty(self):
        state = ws.AppState()
        state.screen_dirty = False

        ws._run_update_task(state, "TEST", lambda: None)

        self.assertTrue(state.screen_dirty)

    def test_loading_window_start_and_end_trigger_redraws(self):
        state = ws.AppState()
        state.started_at = 100
        state.motd_last_cycle = 180
        state.screen_dirty = False

        redraw, loading = ws._screen_redraw_due(state, 170, "minute", "minute", False)
        self.assertTrue(redraw)
        self.assertTrue(loading)

        redraw, loading = ws._screen_redraw_due(state, 191, "minute", "minute", True)
        self.assertTrue(redraw)
        self.assertFalse(loading)


class ImageLocationConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = pathlib.Path(__file__).resolve().parent
        spec = importlib.util.spec_from_file_location(
            "configure_location", root / "image" / "configure-location.py"
        )
        cls.location = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.location)

    def test_address_lookup_uses_census_and_returns_latitude_then_longitude(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "result": {"addressMatches": [{
                "matchedAddress": "4600 SILVER HILL RD, WASHINGTON, DC, 20233",
                "coordinates": {"x": -76.927, "y": 38.846},
            }]},
        }).encode()
        with patch.object(self.location.urllib.request, "urlopen", return_value=response) as fetch:
            results = self.location.lookup_address("4600 Silver Hill Rd, Washington, DC 20233")
        url = fetch.call_args.args[0]
        self.assertTrue(url.startswith("https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?"))
        params = self.location.urllib.parse.parse_qs(self.location.urllib.parse.urlsplit(url).query)
        self.assertEqual(params["address"], ["4600 Silver Hill Rd, Washington, DC 20233"])
        self.assertEqual(params["benchmark"], ["Public_AR_Current"])
        self.assertEqual(fetch.call_args.kwargs["timeout"], 10)
        self.assertEqual(results, [("4600 SILVER HILL RD, WASHINGTON, DC, 20233", 38.846, -76.927)])

    def test_single_address_match_does_not_confirm_coordinates(self):
        with (
            patch.object(self.location, "dialog", return_value="address") as dialog,
            patch.object(self.location, "lookup_address", return_value=[("Matched", 38.8, -76.9)]) as lookup,
        ):
            result = self.location.choose_location()
        lookup.assert_called_once_with("address")
        self.assertEqual(result, (38.8, -76.9))
        dialog.assert_called_once()

    def test_address_prompt_explains_where_the_address_is_sent(self):
        with (
            patch.object(self.location, "dialog", return_value="address") as dialog,
            patch.object(self.location, "lookup_address", return_value=[("Matched", 39, -77)]),
        ):
            self.assertEqual(self.location.choose_location(), (39, -77))
        prompt = dialog.call_args_list[0].args[1]
        self.assertIn("U.S. Census Bureau", prompt)

    def test_dialog_can_be_cancelled(self):
        result = subprocess.CompletedProcess(["whiptail"], 1, stderr="")
        with patch.object(self.location.subprocess, "run", return_value=result):
            with self.assertRaises(self.location.SetupCancelled):
                self.location.dialog("--inputbox", "Address", "10", "74")

    def test_dialog_error_is_not_reported_as_cancellation(self):
        result = subprocess.CompletedProcess(["whiptail"], 255, stderr="terminal too small")
        with patch.object(self.location.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(OSError, "terminal too small"):
                self.location.dialog("--inputbox", "Address", "10", "74")

    def test_lookup_failure_and_no_match_allow_retry(self):
        for outcome in (OSError("Offline"), []):
            with (
                self.subTest(outcome=outcome),
                patch.object(self.location, "dialog", side_effect=["address", "", "retry"]),
                patch.object(self.location, "lookup_address", side_effect=[outcome, [("Matched", 39, -77)]]),
            ):
                self.assertEqual(self.location.choose_location(), (39, -77))

    def test_confirmed_address_saves_selected_coordinates(self):
        root = pathlib.Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            settings = pathlib.Path(directory) / "ws-settings.ini"
            settings.write_text((root / "ws-settings.ini.example").read_text())
            with (
                patch.object(sys, "argv", ["configure-location.py", "--interactive", str(settings)]),
                patch.object(self.location, "dialog", side_effect=["Station", "address", "2"]),
                patch.object(self.location, "lookup_address", return_value=[
                    ("First", 1, 2), ("Second", 38.846, -76.927)
                ]),
            ):
                self.location.main()
            config = ConfigParser()
            config.read(settings)
            self.assertEqual(config.get("LOCATION", "NAME"), "Station")
            self.assertEqual(config.getfloat("LOCATION", "LATITUDE"), 38.846)
            self.assertEqual(config.getfloat("LOCATION", "LONGITUDE"), -76.927)

    def test_location_is_saved_and_other_settings_are_kept(self):
        root = pathlib.Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            settings = pathlib.Path(directory) / "ws-settings.ini"
            settings.write_text((root / "ws-settings.ini.example").read_text())
            subprocess.run(
                [
                    sys.executable,
                    str(root / "image" / "configure-location.py"),
                    str(settings),
                    "Test Station",
                    "39.5",
                    "-77.4",
                ],
                check=True,
            )
            subprocess.run(
                [sys.executable, str(root / "image" / "validate-settings.py"), str(settings)],
                check=True,
            )
            config = ConfigParser()
            config.read(settings)
            self.assertEqual(config.get("LOCATION", "NAME"), "Test Station")
            self.assertEqual(config.getfloat("LOCATION", "LATITUDE"), 39.5)
            self.assertEqual(config.getfloat("LOCATION", "LONGITUDE"), -77.4)
            self.assertFalse(config.getboolean("FEATURES", "SUNTIME"))
            self.assertIn("# Use the nearest NOAA tide prediction station.", settings.read_text())

    def test_invalid_location_does_not_change_settings(self):
        root = pathlib.Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            settings = pathlib.Path(directory) / "ws-settings.ini"
            settings.write_text((root / "ws-settings.ini.example").read_text())
            original = settings.read_text()
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "image" / "configure-location.py"),
                    str(settings),
                    "Test Station",
                    "91",
                    "-77.4",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(settings.read_text(), original)

    def test_setup_preserves_map_and_osv_settings_and_sets_timezone(self):
        root = pathlib.Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            settings = pathlib.Path(directory) / "ws-settings.ini"
            settings.write_text(
                "[LOCATION]\nNAME=Old\nLATITUDE=39\nLONGITUDE=-77\nMAP_LAT=40\n"
                "[FEATURES]\nSUNTIME=false\nOSV_COUNT=true\nOSV_TIDES=true\n[GENERAL]\nDEPLOYMENT_ID=old\n"
                "SELF_UPDATE_INTERVAL_S=21600\n[OTHER]\nVALUE=keep\n"
            )
            answers = [self.location.SetupCancelled(), "SUNTIME\nALT_PREDICTIONS", "site-1", "3600"]
            with (
                patch.object(self.location, "dialog", side_effect=answers),
                patch.object(self.location, "detect_timezone", return_value="America/New_York") as detect,
                patch.object(self.location, "set_timezone") as set_zone,
            ):
                self.location.interactive_setup(str(settings))
            detect.assert_called_once_with(39, -77)
            set_zone.assert_called_once_with("America/New_York")
            config = ConfigParser()
            config.read(settings)
            self.assertEqual(config.get("LOCATION", "NAME"), "Old")
            self.assertEqual(config.getfloat("LOCATION", "MAP_LAT"), 40)
            self.assertFalse(config.has_option("LOCATION", "MAP_LONG"))
            self.assertTrue(config.getboolean("FEATURES", "OSV_COUNT"))
            self.assertTrue(config.getboolean("FEATURES", "OSV_TIDES"))
            self.assertTrue(config.getboolean("FEATURES", "SUNTIME"))
            self.assertTrue(config.getboolean("FEATURES", "ALT_PREDICTIONS"))
            self.assertFalse(config.getboolean("FEATURES", "POTOMAC"))
            self.assertEqual(config.get("GENERAL", "DEPLOYMENT_ID"), "site-1")
            self.assertEqual(config.getint("GENERAL", "SELF_UPDATE_INTERVAL_S"), 3600)
            self.assertEqual(config.get("OTHER", "VALUE"), "keep")

    def test_cancelled_setup_does_not_save_location(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = pathlib.Path(directory) / "ws-settings.ini"
            original = "[LOCATION]\n[FEATURES]\n[GENERAL]\n"
            settings.write_text(original)
            with (
                patch.object(self.location, "interactive_location", return_value=("New", 39, -77)),
                patch.object(self.location, "dialog", side_effect=self.location.SetupCancelled),
                self.assertRaises(self.location.SetupCancelled),
            ):
                self.location.interactive_setup(str(settings))
            self.assertEqual(settings.read_text(), original)

    def test_timezone_failure_does_not_save_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = pathlib.Path(directory) / "ws-settings.ini"
            original = "[LOCATION]\nNAME=Old\nLATITUDE=39\nLONGITUDE=-77\n"
            settings.write_text(original)
            with (
                patch.object(self.location, "dialog", side_effect=[
                    self.location.SetupCancelled(), "", "", "21600",
                ]),
                patch.object(self.location, "detect_timezone", side_effect=OSError("Offline")),
                self.assertRaisesRegex(OSError, "Time zone setup failed"),
            ):
                self.location.interactive_setup(str(settings))
            self.assertEqual(settings.read_text(), original)

    def test_timezone_lookup_and_passwordless_command(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"properties":{"timeZone":"America/New_York"}}'
        with patch.object(self.location.urllib.request, "urlopen", return_value=response):
            timezone = self.location.detect_timezone(39.1, -77.2)
        with patch.object(self.location.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.location.set_timezone(timezone)
        self.assertEqual(run.call_args.args[0], ["timedatectl", "--no-ask-password", "set-timezone", "America/New_York"])

    def test_new_location_sets_its_timezone(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = pathlib.Path(directory) / "ws-settings.ini"
            settings.write_text("[LOCATION]\n[FEATURES]\n[GENERAL]\n")
            with (
                patch.object(self.location, "interactive_location", return_value=("New", 47.6, -122.3)),
                patch.object(self.location, "dialog", side_effect=["", "", "21600"]),
                patch.object(self.location, "detect_timezone", return_value="America/Los_Angeles") as detect,
                patch.object(self.location, "set_timezone") as set_zone,
            ):
                self.location.interactive_setup(str(settings))
            detect.assert_called_once_with(47.6, -122.3)
            set_zone.assert_called_once_with("America/Los_Angeles")
            config = ConfigParser()
            config.read(settings)
            self.assertEqual(config.getfloat("LOCATION", "LONGITUDE"), -122.3)

class ImageSetupTests(unittest.TestCase):
    def test_first_boot_session_focuses_setup_before_starting_desktop(self):
        root = pathlib.Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            commands = {
                "xset": "exit 0",
                "openbox": "exit 0",
                "lxpolkit": "exit 0",
                "xterm": 'while [ ! -f "$HOME/focused" ]; do sleep 0.05; done\n'
                         'touch "$HOME/.weather-station-setup-complete"',
                "xdotool": 'case "$1" in search) echo 123 ;; '
                           'windowfocus) touch "$HOME/focused" ;; esac',
                "pcmanfm": 'test -f "$HOME/focused" && touch "$HOME/desktop-started"',
                "timeout": 'shift\nexec "$@"',
            }
            for name, body in commands.items():
                command = home / name
                command.write_text(f"#!/bin/sh\n{body}\n")
                command.chmod(0o755)
            session = (root / "image" / "weather-station-session").read_text().replace(
                "/usr/local/bin/weather-station-launch", "true"
            )
            script = home / "session"
            script.write_text(session)
            env = dict(os.environ, HOME=directory, PATH=f"{directory}:{os.environ['PATH']}")
            result = subprocess.run(
                ["bash", str(script)], env=env, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((home / "focused").exists())
            self.assertTrue((home / "desktop-started").exists())

    def test_completed_first_boot_does_not_run_again(self):
        root = pathlib.Path(__file__).resolve().parent
        setup = root / "image" / "weather-station-first-boot"
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            (home / ".weather-station-setup-complete").touch()
            completed = subprocess.run(
                ["bash", str(setup)], env=dict(os.environ, HOME=directory),
                input="", text=True, capture_output=True,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "")
            self.assertTrue((home / ".weather-station-setup-complete").exists())

    def test_setup_requires_connected_wifi_and_opens_all_settings(self):
        root = pathlib.Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            settings = home / "ws-settings.ini"
            original = "[LOCATION]\nNAME=Station\nLATITUDE=39\nLONGITUDE=-77\n[FEATURES]\nSUNTIME=false\n"
            settings.write_text(original)
            nmcli = home / "nmcli"
            whiptail = home / "whiptail"
            whiptail.write_text(
                '#!/bin/sh\ncase "$*" in *--yesno*) exit 1 ;; '
                '*"Self-update interval"*) echo 3600 >&2 ;; '
                '*--checklist*) echo SUNTIME >&2 ;; esac\nexit 0\n'
            )
            whiptail.chmod(0o755)
            (home / "configure-location.py").write_text(
                (root / "image" / "configure-location.py").read_text().replace(
                    "timezone = detect_timezone(lat, lon)", 'timezone = "America/New_York"'
                )
            )
            timedatectl = home / "timedatectl"
            timedatectl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$HOME/timezone-command"\n')
            timedatectl.chmod(0o755)
            setup = home / "weather-station-setup"
            setup.write_text((root / "image" / "weather-station-setup").read_text().replace(
                "SHARE=/usr/local/share/weather-station", f'SHARE="{home}"'
            ))
            env = dict(os.environ, HOME=directory, PATH=f"{directory}:{os.environ['PATH']}")
            nmcli.write_text("#!/bin/sh\nprintf 'ethernet:connected\\nwifi:disconnected\\n'\n")
            nmcli.chmod(0o755)
            result = subprocess.run(
                ["bash", str(setup)], env=env, input="", text=True, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Connect to Wi-Fi", result.stderr)
            self.assertEqual(settings.read_text(), original)
            nmcli.write_text("#!/bin/sh\nexit 1\n")
            result = subprocess.run(
                ["bash", str(setup)], env=env, input="", text=True, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Cannot check Wi-Fi", result.stderr)
            self.assertEqual(settings.read_text(), original)
            nmcli.write_text("#!/bin/sh\nprintf 'wifi:connected\\n'\n")
            result = subprocess.run(
                ["bash", str(setup)], env=env, input="n\n\n\n", text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            config = ConfigParser()
            config.read(settings)
            self.assertTrue(config.getboolean("FEATURES", "SUNTIME"))
            self.assertEqual(config.getint("GENERAL", "SELF_UPDATE_INTERVAL_S"), 3600)
            self.assertEqual((home / "timezone-command").read_text().strip(),
                             "--no-ask-password set-timezone America/New_York")


class CurrentTemperatureTests(unittest.TestCase):
    def test_display_uses_open_meteo_temperature(self):
        class StopDrawing(Exception):
            pass

        state = ws.AppState()
        state.weather = {"current": {"temperature_2m": 66.8, "weather_code": 0}}
        state.obs_temp_f = 37.4
        screen = MagicMock()
        screen.get_size.return_value = (1920, 1080)
        fonts = {key: MagicMock() for key in ("medium", "large", "small", "huge")}
        fonts["huge"].render.return_value.get_height.return_value = 160
        with (
            patch.object(ws, "draw_text") as text,
            patch.object(ws, "draw_weather_icon") as icon,
            patch.object(ws, "draw_map", side_effect=StopDrawing),
            self.assertRaises(StopDrawing),
        ):
            ws.draw_screen(screen, state, fonts, 0)
        self.assertEqual(fonts["huge"].render.call_args.args[0], "67°")
        screen.blit.assert_called_once_with(fonts["huge"].render.return_value, (60, 100))
        clock = text.call_args_list[0]
        self.assertEqual(clock.args[4:], (1880, 40))
        self.assertEqual(clock.kwargs, {"anchor": "topright"})
        self.assertEqual(icon.call_args.args[3], 180)

    def test_left_widgets_use_the_space_below_the_raised_temperature(self):
        state = ws.AppState()
        state.motd_last_cycle = ws.time.time()
        screen = MagicMock()
        screen.get_size.return_value = (1920, 1080)
        fonts = {key: MagicMock() for key in ("medium", "large", "small", "huge", "tiny")}
        fonts["small"].render.return_value.get_width.return_value = 100
        with (
            patch.dict(ws.FEATURES, {key: key in ("SUNTIME", "HOURLY_FORECAST") for key in ws.FEATURES}),
            patch.object(ws, "draw_text"),
            patch.object(ws, "draw_map"),
            patch.object(ws, "draw_wind_widget"),
            patch.object(ws._Draw, "rect"),
            patch.object(ws, "draw_suntime") as suntime,
            patch.object(ws, "draw_hourly_widget") as hourly,
        ):
            ws.draw_screen(screen, state, fonts, 0)
        self.assertEqual(suntime.call_args.args[4], 310)
        self.assertEqual(hourly.call_args.args[4], 402)

    def test_update_keeps_nws_highs_and_lows(self):
        state = ws.AppState()
        hilo = {"2026-10-22": (70, 45)}
        with (
            patch.object(ws, "_resolve_nws_endpoints", return_value=("forecast", "station", "America/New_York")),
            patch.object(ws, "_cached_fetch", return_value=b'{"current": {"temperature_2m": 67}}'),
            patch.object(ws, "_fetch_nws_hilo", return_value=hilo),
            patch.dict(ws.FEATURES, {"ALT_PREDICTIONS": False}),
        ):
            state._do_update_weather()
        self.assertEqual(state.nws_hilo, hilo)
        self.assertEqual(state.weather["current"]["temperature_2m"], 67)


class RemovedFeatureTests(unittest.TestCase):
    def test_old_settings_do_not_change_the_remaining_widgets(self):
        def read_legacy_config(config, path):
            _read_test_config(config, path)
            config.read_dict({
                "FEATURES": {"RAM_PRICE": "unused", "NFLSTATS": "unused"},
                "NFLSTATS": {"API-KEY": "unused-key"},
            })

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("configparser.ConfigParser.read", read_legacy_config),
            patch("os.makedirs"),
        ):
            legacy = runpy.run_path(ws.__file__)
        self.assertEqual(legacy["_left_column_layout"](), ws._left_column_layout())

    def test_removed_widgets_do_not_reserve_column_space(self):
        features = {key: False for key in ws.FEATURES}
        features.update({"RAM_PRICE": True, "NFLSTATS": True})
        with patch.dict(ws.FEATURES, features, clear=True):
            self.assertEqual(ws._left_column_layout(), [])


class CoordinateTideTests(unittest.TestCase):
    def test_selects_nearest_station_by_great_circle_distance(self):
        cases = [
            (38.5003345, -76.5038636, (38.465, -76.4733), (38.6867, -76.537)),
            (0, 179.9, (0, -179.9), (0, 179)),
            (80, 0, (80, 10), (78, 0)),
        ]
        for lat, lon, near, far in cases:
            with self.subTest(location=(lat, lon)):
                stations = [
                    {"id": "far", "lat": far[0], "lng": far[1]},
                    {"id": "near", "lat": near[0], "lng": near[1]},
                ]
                with (
                    patch.object(ws, "LATITUDE", lat),
                    patch.object(ws, "LONGITUDE", lon),
                    patch.object(ws, "safe_fetch", return_value=json.dumps({"stations": stations}).encode()),
                ):
                    self.assertEqual(ws._resolve_tide_station()["id"], "near")

    def test_coordinate_tides_take_priority_and_reuse_station(self):
        station = {"id": "8577004", "name": "Long Beach", "lat": 38.465, "lng": -76.4733}
        prediction = {"t": "2026-10-25 02:26", "v": "1.279", "type": "H"}
        for osv_enabled in (False, True):
            with self.subTest(osv_enabled=osv_enabled):
                state = ws.AppState()
                with (
                    patch.dict(ws.FEATURES, {"COORDINATE_TIDES": True, "OSV_TIDES": osv_enabled}),
                    patch.object(ws, "safe_fetch", side_effect=[
                        json.dumps({"stations": [station]}).encode(),
                        json.dumps({"predictions": [prediction]}).encode(),
                        json.dumps({"predictions": [prediction]}).encode(),
                    ]) as fetch,
                ):
                    state.update_tides()
                    state.update_tides()
                self.assertEqual(fetch.call_count, 3)
                for call in fetch.call_args_list[1:]:
                    self.assertIn("station=8577004&", call.args[0])
                self.assertEqual(state.tide_station, station)
                self.assertEqual(state.tides, [{**prediction, "v": 1.279}])

    def test_osv_tides_keep_fixed_station(self):
        with (
            patch.dict(ws.FEATURES, {"COORDINATE_TIDES": False, "OSV_TIDES": True}),
            patch.object(ws, "safe_fetch", return_value=b'{"predictions": []}') as fetch,
        ):
            ws.AppState().update_tides()
        fetch.assert_called_once()
        self.assertIn("station=8570283&", fetch.call_args.args[0])

    def test_station_failure_does_not_fall_back_to_osv(self):
        for raw in (None, "RATE_LIMITED", b'{"stations": []}', b'invalid json'):
            with self.subTest(raw=raw):
                state = ws.AppState()
                with (
                    patch.dict(ws.FEATURES, {"COORDINATE_TIDES": True, "OSV_TIDES": True}),
                    patch.object(ws, "safe_fetch", return_value=raw) as fetch,
                    self.assertRaises(ValueError),
                ):
                    state.update_tides()
                fetch.assert_called_once()
                self.assertIsNone(state.tide_station)
                self.assertFalse(state._tide_running)

    def test_layout_has_one_chart_when_either_feature_is_enabled(self):
        for coordinate, osv in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(coordinate=coordinate, osv=osv), patch.dict(
                ws.FEATURES, {"COORDINATE_TIDES": coordinate, "OSV_TIDES": osv}
            ):
                keys = [key for key, _, _ in ws._left_column_layout()]
                self.assertEqual(keys.count("OSV_TIDES"), int(coordinate or osv))

    def test_chart_title_uses_selected_station(self):
        state = ws.AppState()
        state.tide_station = {"id": "8577004", "name": "Long Beach"}
        fonts = {"tiny": MagicMock(), "small": MagicMock()}
        with (
            patch.dict(ws.FEATURES, {"COORDINATE_TIDES": True}),
            patch.object(ws, "draw_text") as draw,
        ):
            ws.draw_tide_widget(MagicMock(), state, fonts, 60, 350)
        self.assertEqual(draw.call_args_list[0].args[1], "Tides: Long Beach")


class DailyTemperatureTests(unittest.TestCase):
    def setUp(self):
        self.daily = {
            "time": ["2026-10-22", "2026-10-23"],
            "temperature_2m_max": [68.2, 65.4],
            "temperature_2m_min": [42.7, 44.6],
        }

    def test_today_uses_full_day_instead_of_partial_nws_values(self):
        for nws in ((39, 43), (39, None), (50, 43)):
            with self.subTest(nws=nws):
                self.assertEqual(
                    ws._daily_temperatures(self.daily, 0, {"2026-10-22": nws}),
                    (68, 43),
                )

    def test_future_uses_valid_nws_pair(self):
        self.assertEqual(
            ws._daily_temperatures(self.daily, 1, {"2026-10-23": (63, 41)}),
            (63, 41),
        )

    def test_future_rejects_inverted_or_incomplete_nws_pairs(self):
        for nws in ((39, 43), (39, None), (None, 70), (None, None)):
            with self.subTest(nws=nws):
                self.assertEqual(
                    ws._daily_temperatures(self.daily, 1, {"2026-10-23": nws}),
                    (65, 45),
                )
        self.assertEqual(ws._daily_temperatures(self.daily, 1, {}), (65, 45))


if __name__ == "__main__":
    unittest.main()

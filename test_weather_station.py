import json
import unittest
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
        with (
            patch.object(ws, "draw_text"),
            patch.object(ws, "draw_weather_icon"),
            patch.object(ws, "draw_map", side_effect=StopDrawing),
            self.assertRaises(StopDrawing),
        ):
            ws.draw_screen(screen, state, fonts, 0)
        self.assertEqual(fonts["huge"].render.call_args.args[0], "67°")

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

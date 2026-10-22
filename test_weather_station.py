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

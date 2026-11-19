import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

with tempfile.TemporaryDirectory() as home:
    with open(os.path.join(home, "ws-settings.ini"), "w") as settings:
        settings.write("[LOCATION]\nLATITUDE=39.46\nLONGITUDE=-77.40\nNAME=Test\n")
    with patch.dict(os.environ, {"HOME": home}):
        import weather_station as ws


class WeatherDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ws.pygame.init()
        cls.screen = ws.pygame.display.set_mode((ws.SCREEN_W, ws.SCREEN_H))
        font = ws.pygame.font.Font(None, 20)
        cls.fonts = {key: font for key in ("huge", "large", "medium", "small", "tiny", "compact", "compact_bold")}

    @classmethod
    def tearDownClass(cls):
        ws.pygame.quit()

    def test_weather_request_includes_fahrenheit_apparent_temperature(self):
        state = ws.AppState()
        data = {"current": {"temperature_2m": 70, "apparent_temperature": 65, "weather_code": 0}}
        with patch.object(ws, "_resolve_nws_endpoints", return_value=("forecast", "stations", "America/New_York")), \
                patch.object(ws, "_cached_fetch", return_value=json.dumps(data).encode()) as fetch, \
                patch.object(ws, "_fetch_nws_hilo", return_value={}):
            state.update_weather()
        url = fetch.call_args.args[0]
        self.assertIn("apparent_temperature", url)
        self.assertIn("temperature_unit=fahrenheit", url)
        self.assertEqual(state.weather["current"]["apparent_temperature"], 65)

    def test_feels_like_value_and_missing_value(self):
        state = ws.AppState()
        state.motd_last_cycle = time.time()
        ui = {"targets": [], "selected": None, "scroll": 0}
        for value, expected in ((65.7, "Feels like 66°"), (0, "Feels like 0°"), (None, "Feels like --")):
            with self.subTest(value=value):
                state.weather = {"current": {"temperature_2m": 70, "weather_code": 0, "apparent_temperature": value}}
                with patch.object(ws, "draw_text", wraps=ws.draw_text) as draw:
                    ws.draw_screen(self.screen, state, self.fonts, 0, ui)
                self.assertIn(expected, [call.args[1] for call in draw.call_args_list])


if __name__ == "__main__":
    unittest.main()

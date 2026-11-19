import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

with tempfile.TemporaryDirectory() as home:
    with open(os.path.join(home, "ws-settings.ini"), "w") as settings:
        settings.write("[LOCATION]\nLATITUDE=39.46\nLONGITUDE=-77.40\nNAME=Test\n")
    with patch.dict(os.environ, {"HOME": home}):
        import weather_station as ws


class TrainInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ws.pygame.init()
        cls.screen = ws.pygame.display.set_mode((ws.SCREEN_W, ws.SCREEN_H))
        font = ws.pygame.font.Font(None, 20)
        cls.fonts = {key: font for key in ("small", "tiny", "compact", "compact_bold")}

    @classmethod
    def tearDownClass(cls):
        ws.pygame.quit()

    def setUp(self):
        self.state = ws.AppState()
        self.raw = {
            "train_id": "2026-01-01_AMTRAK_10", "railroad": "AMTRAK",
            "number": 10, "name": "Test train", "origin": "AAA",
            "destination": "BBB", "location": {
                "latitude": ws.MAP_LAT, "longitude": ws.MAP_LONG,
                "heading": 90, "speed": 42,
            },
            "stops": [{"code": "BBB", "extra": "x" * 150} for _ in range(20)],
        }
        with patch.object(ws, "_fetch_transitdocs_json", side_effect=[
            [self.raw], [{"code": "BBB", "city": "End", "latitude": ws.MAP_LAT,
                          "longitude": ws.MAP_LONG}],
        ]):
            self.state.update_trains()
        self.train = self.state.train_positions[0]
        self.ui = {"targets": [], "selected": None, "scroll": 0}

    def click(self, pos):
        ws._handle_train_event(ws.pygame.event.Event(
            ws.pygame.MOUSEBUTTONDOWN, button=1, pos=pos), self.ui)

    def test_map_and_list_select_the_displayed_train(self):
        self.state.map_tiles = {(0, 0): b""}
        ws.draw_map(self.screen, self.state, self.fonts, 560, 130, 900, 780, self.ui)
        map_rect, train = self.ui["targets"][0]
        self.assertEqual(map_rect.center, (1010, 520))
        self.click(map_rect.center)
        self.assertIs(self.ui["selected"], train)
        ws._handle_train_event(ws.pygame.event.Event(
            ws.pygame.KEYDOWN, key=ws.pygame.K_ESCAPE), self.ui)
        self.ui["targets"] = []
        ws.draw_train_panel(self.screen, self.state, self.fonts, self.ui)
        self.click(self.ui["targets"][0][0].center)
        self.assertIs(self.ui["selected"], self.train)

    def test_details_scroll_refresh_and_missing_train(self):
        self.ui["targets"] = [(ws.pygame.Rect(0, 0, 20, 20), self.train)]
        self.click((5, 5))
        ws.draw_train_details(self.screen, self.state, self.fonts, self.ui)
        lines = self.ui["lines"]
        self.assertIn("Train type: Not provided", lines)
        self.assertIn("Heritage train: Not provided", lines)
        self.assertIn("Next stop: End", lines)
        self.assertIn("x" * 150, "".join(lines))
        ws._handle_train_event(ws.pygame.event.Event(
            ws.pygame.MOUSEWHEEL, y=-10000, x=0), self.ui)
        ws.draw_train_details(self.screen, self.state, self.fonts, self.ui)
        self.assertGreater(self.ui["scroll"], 0)
        self.assertLess(self.ui["scroll"], len(lines))

        updated = dict(self.train, raw=dict(self.raw, heritage=False, train_type="Diesel"))
        self.state.train_positions = [updated]
        ws.draw_train_details(self.screen, self.state, self.fonts, self.ui)
        self.assertIs(self.ui["selected"], updated)
        self.assertIn("Heritage train: No", self.ui["lines"])
        self.assertIn("Train type: Diesel", self.ui["lines"])
        self.state.train_positions = []
        ws.draw_train_details(self.screen, self.state, self.fonts, self.ui)
        self.assertIs(self.ui["selected"], updated)
        self.click(self.ui["close_rect"].center)
        self.assertIsNone(self.ui["selected"])

    def test_open_then_close_before_next_frame(self):
        self.ui["targets"] = [(ws.pygame.Rect(0, 0, 20, 20), self.train)]
        self.click((5, 5))
        self.click((0, 0))
        self.assertIsNone(self.ui["selected"])


if __name__ == "__main__":
    unittest.main()

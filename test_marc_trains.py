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

from google.transit import gtfs_realtime_pb2


class MarcTrainTests(unittest.TestCase):
    def feed(self):
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.timestamp = 1000
        vehicle = feed.entity.add(id="vehicle-1").vehicle
        vehicle.trip.trip_id = "Train455"
        vehicle.trip.start_date = "20261116"
        vehicle.timestamp = 990
        vehicle.position.latitude = 39.0
        vehicle.position.longitude = -77.0
        return feed

    def parse(self, feed):
        with patch.object(ws, "safe_fetch", return_value=feed.SerializeToString()), \
                patch.object(ws.time, "time", return_value=1000):
            return ws._fetch_marc_trains()

    def test_positions_units_missing_values_and_stale_vehicle(self):
        feed = self.feed()
        position = feed.entity[0].vehicle.position
        position.speed = 10
        position.bearing = 90
        old = feed.entity.add(id="old").vehicle
        old.timestamp = 800
        old.position.latitude = 39
        old.position.longitude = -77
        trains = self.parse(feed)
        self.assertEqual(len(trains), 1)
        self.assertEqual(trains[0]["id"], "MARC:Train455:20261116")
        self.assertEqual(trains[0]["number"], "455")
        self.assertEqual(trains[0]["heading"], "E")
        self.assertAlmostEqual(trains[0]["speed"], 22.369, places=2)
        position.ClearField("speed")
        position.ClearField("bearing")
        train = self.parse(feed)[0]
        self.assertIsNone(train["speed"])
        self.assertIsNone(train["bearing"])

    def test_stale_feed_and_empty_feed(self):
        feed = self.feed()
        feed.header.timestamp = 800
        with self.assertRaises(ValueError):
            self.parse(feed)
        feed.header.timestamp = 1000
        del feed.entity[:]
        self.assertEqual(self.parse(feed), [])

    def test_feeds_fail_independently(self):
        state = ws.AppState()
        marc = {"id": "MARC:test"}
        with patch.object(state, "_fetch_transitdocs_trains", side_effect=ValueError("offline")), \
                patch.object(ws, "_fetch_marc_trains", return_value=[marc]):
            state.update_trains()
        self.assertEqual(state.train_positions, [marc])
        self.assertTrue(state.train_data_loaded)
        self.assertIn("TransitDocs", state.train_error)
        with patch.object(state, "_fetch_transitdocs_trains", return_value=([{"id": "amtrak"}], [], [])), \
                patch.object(ws, "_fetch_marc_trains", side_effect=ValueError("offline")):
            state.update_trains()
        self.assertEqual(state.train_positions, [{"id": "amtrak"}])
        self.assertTrue(state.train_data_loaded)
        self.assertIn("MARC", state.train_error)
        with patch.object(state, "_fetch_transitdocs_trains", side_effect=ValueError("offline")), \
                patch.object(ws, "_fetch_marc_trains", side_effect=ValueError("offline")):
            state.update_trains()
        self.assertEqual(state.train_positions, [{"id": "amtrak"}])
        self.assertTrue(state.train_data_loaded)
        self.assertTrue(state.train_stale)

    def test_draw_train_without_speed_or_heading(self):
        train = self.parse(self.feed())[0]
        ws.pygame.init()
        try:
            screen = ws.pygame.display.set_mode((ws.SCREEN_W, ws.SCREEN_H))
            font = ws.pygame.font.Font(None, 20)
            fonts = {key: font for key in ("small", "tiny", "compact", "compact_bold")}
            state = ws.AppState()
            state.train_positions = [train]
            state.train_data_loaded = True
            state.map_tiles = {(0, 0): b""}
            ui = {"targets": [], "selected": None, "scroll": 0}
            ws.draw_map(screen, state, fonts, 560, 130, 900, 780, ui)
            ws.draw_train_panel(screen, state, fonts, ui)
            lines = ws._train_detail_lines(train, font, 1000)
            self.assertIn("Speed: Not provided   Heading: Not provided", lines)
            self.assertIn("Maryland MTA GTFS Realtime source data (timestamps are Unix seconds):", lines)
        finally:
            ws.pygame.quit()


if __name__ == "__main__":
    unittest.main()

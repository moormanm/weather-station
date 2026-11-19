import io
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


class MapZoomTests(unittest.TestCase):
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
        self.ui = {"targets": [], "selected": None, "scroll": 0}
        ws.draw_map(self.screen, self.state, self.fonts, 560, 130, 900, 780, self.ui)
        ws._draw_map_zoom_controls(self.screen, self.state, self.fonts, self.ui, 560, 130, 900)

    def key(self, key):
        return ws._handle_map_zoom_event(
            ws.pygame.event.Event(ws.pygame.KEYDOWN, key=key), self.state, self.ui)

    def test_controls_limits_and_modal(self):
        self.assertEqual(self.state.map_zoom - ws.MAP_MIN_ZOOM + 1, 2)
        minus, plus = self.ui["zoom_controls"]
        event = ws.pygame.event.Event(ws.pygame.MOUSEBUTTONDOWN, button=1, pos=minus.center)
        self.assertTrue(ws._handle_map_zoom_event(event, self.state, self.ui))
        self.key(ws.pygame.K_MINUS)
        self.assertEqual(self.state.map_target_zoom, ws.MAP_MIN_ZOOM)
        event = ws.pygame.event.Event(ws.pygame.MOUSEBUTTONDOWN, button=1, pos=plus.center)
        for _ in range(6):
            self.assertTrue(ws._handle_map_zoom_event(event, self.state, self.ui))
        self.assertEqual(self.state.map_target_zoom, ws.MAP_MAX_ZOOM)
        self.assertEqual(self.state.map_zoom, ws.MAP_ZOOM)
        self.ui["selected"] = {"id": "test"}
        self.assertFalse(self.key(ws.pygame.K_MINUS))
        self.assertEqual(self.state.map_target_zoom, ws.MAP_MAX_ZOOM)

    def test_obsolete_fetch_and_failed_fetch_keep_current_map(self):
        self.state.map_tiles = {(0, 0): b"old"}
        self.state.map_target_zoom = ws.MAP_ZOOM + 1

        def change_target(url, **kwargs):
            self.state.map_target_zoom = ws.MAP_ZOOM + 2
            return b"tile"

        with tempfile.TemporaryDirectory() as cache, \
                patch.object(ws, "TILE_CACHE_DIR", cache), \
                patch.object(ws, "_map_tile_offsets", return_value=[(0, 0)]), \
                patch.object(ws, "safe_fetch", side_effect=change_target):
            self.state._do_update_map()
            self.assertEqual(self.state.map_tiles, {(0, 0): b"old"})
            self.assertEqual(self.state.map_zoom, ws.MAP_ZOOM)
            with patch.object(ws, "safe_fetch", return_value=None):
                self.state._do_update_map()
            self.assertEqual(self.state.map_tiles, {(0, 0): b"old"})
            with patch.object(ws, "safe_fetch", return_value=b"tile") as fetch:
                self.state._do_update_map()
            self.assertIn(f"/tile/{ws.MAP_ZOOM + 2}/", fetch.call_args_list[0].args[0])
            self.assertEqual(self.state.map_zoom, ws.MAP_ZOOM + 2)

    def test_map_clicks_keep_the_clicked_location_fixed(self):
        rect = self.ui["map_rect"]
        pos = (rect.centerx + 250, rect.centery - 160)
        dx, dy = pos[0] - rect.centerx, pos[1] - rect.centery
        x, y = ws._world_px(*self.state.map_center, self.state.map_zoom)
        original = (x + dx, y + dy)
        click = ws.pygame.event.Event(ws.pygame.MOUSEBUTTONDOWN, button=1, pos=pos)
        self.assertTrue(ws._handle_map_zoom_event(click, self.state, self.ui))
        zoom = self.state.map_target_zoom
        x, y = ws._world_px(*self.state.map_target_center, zoom)
        self.assertAlmostEqual(x + dx, original[0] * 2, places=6)
        self.assertAlmostEqual(y + dy, original[1] * 2, places=6)

        self.state.map_center = self.state.map_target_center
        self.state.map_zoom = zoom
        click.button = 3
        self.assertTrue(ws._handle_map_zoom_event(click, self.state, self.ui))
        x, y = ws._world_px(*self.state.map_target_center, self.state.map_target_zoom)
        self.assertAlmostEqual(x + dx, original[0], places=6)
        self.assertAlmostEqual(y + dy, original[1], places=6)

    def test_wheel_zoom_uses_pointer_and_preserves_detail_scrolling(self):
        rect = self.ui["map_rect"]
        pos = (rect.centerx + 200, rect.centery - 100)
        dx, dy = pos[0] - rect.centerx, pos[1] - rect.centery
        x, y = ws._world_px(*self.state.map_center, self.state.map_zoom)
        wheel = ws.pygame.event.Event(ws.pygame.MOUSEWHEEL, x=0, y=2)
        with patch.object(ws.pygame.mouse, "get_pos", return_value=pos):
            self.assertTrue(ws._handle_map_zoom_event(wheel, self.state, self.ui))
            self.assertEqual(self.state.map_target_zoom, ws.MAP_ZOOM + 2)
            tx, ty = ws._world_px(*self.state.map_target_center, self.state.map_target_zoom)
            self.assertAlmostEqual(tx + dx, (x + dx) * 4, places=6)
            self.assertAlmostEqual(ty + dy, (y + dy) * 4, places=6)
            wheel.y = -10
            self.assertTrue(ws._handle_map_zoom_event(wheel, self.state, self.ui))
            self.assertEqual(self.state.map_target_zoom, ws.MAP_MIN_ZOOM)
            self.ui["selected"] = {"id": "test"}
            self.assertFalse(ws._handle_map_zoom_event(wheel, self.state, self.ui))
            ws._handle_train_event(wheel, self.ui)
            self.assertEqual(self.ui["scroll"], 30)
        self.ui["selected"] = None
        with patch.object(ws.pygame.mouse, "get_pos", return_value=(0, 0)):
            self.assertFalse(ws._handle_map_zoom_event(wheel, self.state, self.ui))
        self.assertEqual(self.state.map_target_zoom, ws.MAP_MIN_ZOOM)

    def test_click_priority_and_pending_zoom(self):
        rect = self.ui["map_rect"]
        pos = (rect.left + 100, rect.top + 100)
        train = {"id": "test"}
        self.ui["targets"] = [(ws.pygame.Rect(pos[0] - 10, pos[1] - 10, 20, 20), train)]
        click = ws.pygame.event.Event(ws.pygame.MOUSEBUTTONDOWN, button=1, pos=pos)
        self.assertFalse(ws._handle_map_zoom_event(click, self.state, self.ui))
        ws._handle_train_event(click, self.ui)
        self.assertIs(self.ui["selected"], train)
        self.ui["selected"] = None
        self.ui["targets"] = []
        self.assertTrue(ws._handle_map_zoom_event(click, self.state, self.ui))
        self.assertTrue(ws._handle_map_zoom_event(click, self.state, self.ui))
        dx, dy = pos[0] - rect.centerx, pos[1] - rect.centery
        x, y = ws._world_px(*self.state.map_center, self.state.map_zoom)
        target_x, target_y = ws._world_px(*self.state.map_target_center, self.state.map_target_zoom)
        self.assertAlmostEqual(target_x + dx, (x + dx) * 4, places=6)
        self.assertAlmostEqual(target_y + dy, (y + dy) * 4, places=6)
        outside = ws.pygame.event.Event(ws.pygame.MOUSEBUTTONDOWN, button=3, pos=(0, 0))
        self.assertFalse(ws._handle_map_zoom_event(outside, self.state, self.ui))

    def test_train_stays_at_anchor_after_zoom(self):
        rect = self.ui["map_rect"]
        pos = (rect.centerx - 250, rect.centery + 140)
        dx, dy = pos[0] - rect.centerx, pos[1] - rect.centery
        x, y = ws._world_px(*self.state.map_center, self.state.map_zoom)
        lat, lon = ws._world_center(x + dx, y + dy, self.state.map_zoom)
        self.state.train_positions = [{
            "lat": lat, "lon": lon, "bearing": 0,
            "number": "10", "color": (0, 255, 0),
        }]
        click = ws.pygame.event.Event(ws.pygame.MOUSEBUTTONDOWN, button=1, pos=pos)
        self.assertTrue(ws._handle_map_zoom_event(click, self.state, self.ui))
        self.state.map_zoom = self.state.map_target_zoom
        self.state.map_center = self.state.map_target_center
        self.state.map_tiles = {(0, 0): b""}
        ws.draw_map(self.screen, self.state, self.fonts, 560, 130, 900, 780, self.ui)
        self.assertEqual(self.ui["targets"][0][0].center, pos)

    def test_recenter_discards_obsolete_tiles_and_refreshes_radar(self):
        self.state.map_tiles = {(0, 0): b"old"}
        self.state.map_target_center = (ws.MAP_LAT, ws.MAP_LONG + 4)
        new_center = (ws.MAP_LAT, ws.MAP_LONG + 5)

        def change_center(url, **kwargs):
            self.state.map_target_center = new_center
            return b"tile"

        with tempfile.TemporaryDirectory() as cache, \
                patch.object(ws, "TILE_CACHE_DIR", cache), \
                patch.object(ws, "_map_tile_offsets", return_value=[(0, 0)]), \
                patch.object(self.state, "_fetch_radar_tiles", return_value={}) as radar, \
                patch.object(ws, "safe_fetch", side_effect=change_center):
            self.state._do_update_map()
            self.assertEqual(self.state.map_center, (ws.MAP_LAT, ws.MAP_LONG))
            self.assertEqual(self.state.map_tiles, {(0, 0): b"old"})
            with patch.object(ws, "safe_fetch", return_value=b"tile") as fetch:
                self.state._do_update_map()
            self.assertEqual(self.state.map_center, new_center)
            tile_x, tile_y = ws._tile_xy(*new_center, self.state.map_zoom)
            self.assertIn(f"/tile/{self.state.map_zoom}/{tile_y}/{tile_x}", fetch.call_args_list[0].args[0])
            radar.assert_called_with(*new_center)

    def test_radar_and_train_align_at_each_zoom(self):
        surface = ws.pygame.Surface((256, 256))
        surface.fill((255, 0, 0))
        blob = io.BytesIO()
        ws.pygame.image.save(surface, blob, "tile.png")
        radar = blob.getvalue()
        self.state.map_tiles = {(0, 0): b""}
        self.state.radar_tiles = {(x, y): radar for x in range(-1, 2) for y in range(-1, 2)}
        self.state.train_positions = [{
            "lat": ws.MAP_LAT, "lon": ws.MAP_LONG, "bearing": 0,
            "number": "10", "color": (0, 255, 0),
        }]
        original_scale = ws.pygame.transform.smoothscale
        sizes = []

        def scale(image, size):
            sizes.append(size)
            return original_scale(image, size)

        with patch.object(ws.pygame.transform, "smoothscale", side_effect=scale):
            radar_color = None
            for zoom in range(ws.MAP_MIN_ZOOM, ws.MAP_MAX_ZOOM + 1):
                self.state.map_zoom = zoom
                self.ui["targets"] = []
                ws.draw_map(self.screen, self.state, self.fonts, 560, 130, 900, 780, self.ui)
                self.assertEqual(self.ui["targets"][0][0].center, (1010, 520))
                color = self.screen.get_at((1000, 420))[:3]
                self.assertGreater(color[0], 150)
                self.assertLess(color[1], 10)
                if radar_color is not None:
                    self.assertEqual(color, radar_color)
                radar_color = color
                self.assertEqual(self.state._radar_render_key[1], zoom)
        self.assertTrue(sizes)
        self.assertLessEqual(max(max(size) for size in sizes), 964)


if __name__ == "__main__":
    unittest.main()

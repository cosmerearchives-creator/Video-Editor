import math
import unittest

from live2d_movement import step_motion


def character(**settings):
    return {'movement': {'enabled': True, **settings},
            '_movement_bounds': (.25, .2, .75, .8)}


class MovementTests(unittest.TestCase):
    def test_release_stops_translation_while_head_recenters(self):
        layer = character()
        for _ in range(30):
            step_motion(layer, {'d'}, 1 / 60)
        position = layer['x'], layer['y']
        turn = layer['_movement_turn']
        for _ in range(60):
            step_motion(layer, set(), 1 / 60)
        self.assertEqual(position, (layer['x'], layer['y']))
        self.assertLess(abs(layer['_movement_turn']), abs(turn) / 100)

    def test_long_stall_never_becomes_a_large_jump(self):
        layer = character()
        step_motion(layer, {'d', 's'}, 10)
        self.assertLessEqual(abs(layer['x']), 30 * .033)
        self.assertLessEqual(abs(layer['y']), 20 * .033)

    def test_vertical_release_stops_without_coasting(self):
        layer = character()
        for _ in range(20):
            step_motion(layer, {'w'}, 1 / 60)
        position = layer['x'], layer['y']
        for _ in range(60):
            step_motion(layer, set(), 1 / 60)
        self.assertEqual(position, (layer['x'], layer['y']))

    def test_opposite_keys_cancel_translation(self):
        layer = character()
        layer['_movement_turn'] = 1
        layer['_movement_vertical'] = 1
        step_motion(layer, {'w', 'a', 's', 'd'}, .016)
        self.assertEqual((layer['x'], layer['y']), (0, 0))

    def test_speed_is_consistent_at_30_and_60_hz(self):
        positions = []
        for fps in (30, 60):
            layer = character(horizontal_speed=5)
            for _ in range(fps):
                step_motion(layer, {'d'}, 1 / fps)
            positions.append(layer['x'])
        self.assertAlmostEqual(*positions, delta=.1)

    def test_diagonal_cannot_increase_normalized_speed(self):
        layer = character(horizontal_speed=5, vertical_speed=5)
        layer['_movement_turn'] = 1
        layer['_movement_vertical'] = 1
        step_motion(layer, {'d', 's'}, .016)
        self.assertLessEqual(math.hypot(layer['x'], layer['y']), 5 * .016)

    def test_proposed_position_is_clamped_to_scaled_geometry(self):
        for keys in ({'d', 's'}, {'a', 'w'}):
            layer = character(horizontal_speed=200, vertical_speed=200)
            layer['scale'] = 1.2
            for _ in range(600):
                step_motion(layer, keys, 1 / 60)
                y = layer['y']
                scale = 1.2 * (.4 + .6 * (y + 35) / 70)
                left, top, right, bottom = layer['_movement_bounds']
                self.assertGreaterEqual(.5 + layer['x'] / 100 + (left - .5) * scale, -1e-9)
                self.assertLessEqual(.5 + layer['x'] / 100 + (right - .5) * scale, 1 + 1e-9)
                self.assertGreaterEqual(.5 + y / 100 + (top - .5) * scale, -1e-9)
                self.assertLessEqual(.5 + y / 100 + (bottom - .5) * scale, 1 + 1e-9)

    def test_oversized_geometry_does_not_flip_bounds(self):
        layer = character(far_scale=200, near_scale=200)
        layer['scale'] = 5
        for _ in range(10):
            step_motion(layer, {'d', 's'}, .016)
        self.assertTrue(math.isfinite(layer['x']) and math.isfinite(layer['y']))


if __name__ == '__main__':
    unittest.main()

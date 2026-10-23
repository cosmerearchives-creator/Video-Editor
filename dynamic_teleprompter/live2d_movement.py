"""Frame-rate-independent camera-free Live2D movement and easing.

Runtime state keys start with '_' and are intentionally not saved in project files.
"""
import math

DEFAULT_MOVEMENT = {
    'enabled': False,
    'horizontal_speed': 30,
    'vertical_speed': 20,
    'turn_seconds': 350,
    'vertical_seconds': 350,
    'head_strength': 65,
    'far_y': -35,
    'near_y': 35,
    'far_scale': 40,
    'near_scale': 100,
}


def _approach(value, target, dt, duration):
    """Exponential interpolation stable across varying frame rates."""
    alpha = 1.0 - math.exp(-4.0 * dt / max(0.05, duration))
    return value + (target - value) * alpha


def _ease(value):
    value = max(0., min(1., abs(value)))
    return value * value * (3 - 2 * value)


def perspective_scale(layer, y):
    settings = {**DEFAULT_MOVEMENT, **layer.get('movement', {})}
    far, near = sorted((float(settings['far_y']), float(settings['near_y'])))
    fraction = max(0., min(1., (y - far) / max(1., near - far)))
    depth = (float(settings['far_scale']) +
             (float(settings['near_scale']) - float(settings['far_scale'])) * fraction)
    return max(.01, float(layer.get('scale', 1))) * depth / 100.


def _clamp_position(layer, settings, x, y):
    # Bounds describe the visible mesh in normalized, untransformed image space.
    # Until the worker returns geometry, the full image is a conservative bound.
    left, top, right, bottom = layer.get('_movement_bounds', (0., 0., 1., 1.))
    far, near = sorted((float(settings['far_y']), float(settings['near_y'])))
    low, high = far, near
    base = max(.01, float(layer.get('scale', 1)))
    slope = base * (float(settings['near_scale']) - float(settings['far_scale'])) / (100 * max(1., near - far))
    intercept = base * float(settings['far_scale']) / 100 - slope * far
    # Perspective scale is linear within [far, near]. Solve both edge
    # inequalities analytically, including when the depth scale is reversed.
    for a, b in ((.01 + (top - .5) * slope, .5 + (top - .5) * intercept),
                 (-.01 - (bottom - .5) * slope, .5 - (bottom - .5) * intercept)):
        if abs(a) < 1e-12:
            if b < 0:
                low, high = 1., 0.
                break
        elif a > 0:
            low = max(low, -b / a)
        else:
            high = min(high, -b / a)
    if low <= high:
        y = max(low, min(high, y))
    else:
        # An oversized model cannot fit. Center its visible geometry in the
        # permitted depth range rather than alternating invalid min/max bounds.
        center = (top + bottom) / 2 - .5
        denominator = .01 + center * slope
        centered = -center * intercept / denominator if abs(denominator) > 1e-12 else 0.
        y = max(far, min(near, centered))
    scale = perspective_scale(layer, y)
    low_x = 100 * (-.5 - (left - .5) * scale)
    high_x = 100 * (.5 - (right - .5) * scale)
    x = max(low_x, min(high_x, x)) if low_x <= high_x else (low_x + high_x) / 2
    return x, y


def step_motion(layer, keys, dt):
    """Update movement for the active character; return True if visual state changed.

    X movement speed follows a virtual head-turn signal, independent of raster
    completion. Release stops translation while the head smoothly recenters.
    """
    settings = {**DEFAULT_MOVEMENT, **layer.get('movement', {})}
    if not settings['enabled'] or not math.isfinite(dt) or dt <= 0:
        return False
    dt = min(.033, dt)
    x_direction = int('d' in keys) - int('a' in keys)
    y_direction = int('s' in keys) - int('w' in keys)
    turn = float(layer.get('_movement_turn', 0.))
    vertical = float(layer.get('_movement_vertical', 0.))
    new_turn = _approach(turn, x_direction, dt, float(settings['turn_seconds']) / 1000.)
    new_vertical = _approach(vertical, y_direction, dt, float(settings['vertical_seconds']) / 1000.)
    # Gate translation on held input. During reversal, wait for the virtual
    # angle to cross center instead of drifting against the requested direction.
    vx = math.copysign(_ease(new_turn), new_turn) if x_direction * new_turn > 0 else 0.
    vy = math.copysign(_ease(new_vertical), new_vertical) if y_direction * new_vertical > 0 else 0.
    divisor = max(1., math.hypot(vx, vy))
    velocity_x = vx / divisor * float(settings['horizontal_speed'])
    velocity_y = vy / divisor * float(settings['vertical_speed'])
    old_x, old_y = float(layer.get('x', 0)), float(layer.get('y', 0))
    new_x, new_y = old_x, old_y
    if x_direction or y_direction:
        new_x, new_y = _clamp_position(layer, settings, old_x + velocity_x * dt, old_y + velocity_y * dt)
    layer['_movement_turn'] = new_turn
    layer['_movement_vertical'] = new_vertical
    layer['x'] = new_x
    layer['y'] = new_y
    return abs(new_turn - turn) > 1e-5 or abs(new_vertical - vertical) > 1e-5 or abs(new_x - old_x) > 1e-5 or abs(new_y - old_y) > 1e-5

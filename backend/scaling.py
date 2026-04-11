import math

from shared.constants import SCREENSHOT_MAX_LONG_EDGE, SCREENSHOT_MAX_PIXELS


def get_scale_factor(width: int, height: int) -> float:
    long_edge = max(width, height)
    total_pixels = width * height
    long_edge_scale = SCREENSHOT_MAX_LONG_EDGE / long_edge
    total_pixels_scale = math.sqrt(SCREENSHOT_MAX_PIXELS / total_pixels)
    return min(1.0, long_edge_scale, total_pixels_scale)


def scale_coordinates_to_screen(x: int, y: int, scale_factor: float, dpr: float) -> tuple[float, float]:
    """Scale coordinates from API space back to logical screen pixels."""
    screen_x = x / scale_factor / dpr
    screen_y = y / scale_factor / dpr
    return screen_x, screen_y


def scale_dimensions_to_screen(w: int, h: int, scale_factor: float, dpr: float) -> tuple[float, float]:
    """Scale bounding box dimensions from API space back to logical screen pixels."""
    screen_w = w / scale_factor / dpr
    screen_h = h / scale_factor / dpr
    return screen_w, screen_h

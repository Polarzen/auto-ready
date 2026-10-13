import numpy as np
import pytest

from auto_ready.models import (
    Frame,
    Geometry,
    Identity,
    Rect,
    Region,
    Settings,
    Template,
    preview_to_pixel,
)
from auto_ready.recognition import match, validate_template


@pytest.mark.parametrize(
    "image",
    [
        np.zeros((24, 80, 3), np.uint8),
        np.full((24, 80, 3), [255, 0, 0], np.uint8),
        np.zeros((10, 10, 3), np.uint8),
        np.zeros((2000, 2000, 3), np.uint8),
        np.zeros((50, 50), np.uint8),
    ],
)
def test_invalid_templates(image):
    with pytest.raises(ValueError):
        validate_template(image)


def test_coordinate_conversion_letterbox_and_negative_screen():
    display = Rect(10, 30, 640, 480)
    assert preview_to_pixel(330, 270, display, (1280, 960)) == (640, 480)
    assert preview_to_pixel(0, 0, display, (1280, 960)) is None
    assert Geometry(-1000, -500, 1280, 960, 144).screen_point((640, 480)) == (-360, -20)
    rect = Rect(20, 30, 80, 40)
    assert Region.from_rect(rect, 320, 240).pixels(320, 240) == rect


def test_wrong_size_and_low_score_are_diagnostics():
    template = np.random.default_rng(1).integers(0, 256, (30, 60, 3), dtype=np.uint8)
    image = np.random.default_rng(2).integers(0, 256, (240, 320, 3), dtype=np.uint8)
    frame = Frame(image, Geometry(0, 0, 320, 240, 96), Identity(1, 2, 3, "a"), 1, "s", 1)
    assert match(frame, Template(template, (320, 240), 96), Region(), Settings()).unique is None
    assert "DPI" in match(frame, Template(template, (640, 480), 96), Region(), Settings()).reason


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.7, 1.1, True])
def test_setting_bounds(value):
    with pytest.raises(ValueError):
        Settings(threshold=value).validate()


def test_small_button_on_large_uniform_client_remains_detectable():
    template = np.random.default_rng(1).integers(0, 256, (24, 60, 3), dtype=np.uint8)
    image = np.full((1080, 1920, 3), 128, dtype=np.uint8)
    image[600:624, 1200:1260] = template
    frame = Frame(image, Geometry(0, 0, 1920, 1080, 96), Identity(1, 2, 3, "a"), 1, "s", 1)
    detection = match(frame, Template(template, (1920, 1080), 96), Region(), Settings())
    assert detection.unique is not None
    assert detection.unique.rect == Rect(1200, 600, 60, 24)


def test_many_shortlist_peaks_cannot_claim_uniqueness():
    template = np.random.default_rng(1).integers(0, 256, (24, 60, 3), dtype=np.uint8)
    image = np.full((400, 400, 3), 128, dtype=np.uint8)
    for row in range(3):
        for col in range(3):
            image[20 + row * 90 : 44 + row * 90, 20 + col * 100 : 80 + col * 100] = template
    frame = Frame(image, Geometry(0, 0, 400, 400, 96), Identity(1, 2, 3, "a"), 1, "s", 1)
    detection = match(frame, Template(template, (400, 400), 96), Region(), Settings())
    assert detection.unique is None
    assert "候选" in detection.reason

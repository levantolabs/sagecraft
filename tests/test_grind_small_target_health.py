"""Real calibrated pixels reach the death veto through the production HUD reader."""
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from sage_wow.agent.grind_perception import read_current_bars
from sage_wow.agent.grind_resources import hud_resources
from sage_wow.models import Frame


def fixture(tmp_path, columns=0, *, offset=0, rows=23):
    layout = {'version': 1, 'id': 'small-health', 'provenance': 'synthetic calibrated bars',
              'image_width': 300, 'image_height': 150, 'regions': {
                  'player_frame': [0, 0, 250, 50], 'player_health': [10, 10, 208, 33],
                  'target_frame': [0, 70, 250, 125], 'target_health': [10, 80, 208, 103]}}
    image = Image.new('RGB', (300, 150), '#101010')
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 207, 32), fill='#20d020')
    if columns:
        draw.rectangle((10+offset, 80, 10+offset+columns-1, 80+rows-1), fill='#20d020')
    path = tmp_path/'bars.png'
    image.save(path)
    frame = Frame.create('offline-window', 300, 150, image_path=str(path))
    controller = SimpleNamespace(profile=SimpleNamespace(values={'calibration': {'ui_layout': layout}}), config={})
    return frame, controller, layout


@pytest.mark.parametrize('columns,expected', [(2, .01), (5, .025), (6, .03), (7, .035)])
def test_current_confident_positive_health_survives_production_reader(tmp_path, columns, expected):
    frame, controller, layout = fixture(tmp_path, columns)
    hud = hud_resources(controller, frame)
    assert hud['frame_id'] == frame.frame_id
    assert hud['target_health'] == expected
    assert hud['target_health_confidence'] == 1
    scene = read_current_bars(frame, ui_layout=layout)
    assert scene.target_alive is (expected > .03)
    assert scene.health == 1


@pytest.mark.parametrize('columns,offset,rows', [(0, 0, 23), (2, 80, 23), (2, 0, 6)])
def test_blank_or_sparse_green_pixels_do_not_create_positive_target_health(tmp_path, columns, offset, rows):
    frame, controller, _ = fixture(tmp_path, columns, offset=offset, rows=rows)
    hud = hud_resources(controller, frame)
    assert hud['target_health'] is None


def test_small_bar_tolerates_limited_text_occlusion_but_keeps_confidence(tmp_path):
    frame, controller, _ = fixture(tmp_path, 2, rows=20)
    hud = hud_resources(controller, frame)
    assert hud['target_health'] == .01
    assert .8 <= hud['target_health_confidence'] < 1

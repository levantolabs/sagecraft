"""Moved target HUD uses explicit pixels; player-level evidence stays separate."""

import pytest
from PIL import Image, ImageDraw

from sage_wow.agent.ui_layout import UILayoutError, validate
from sage_wow.perception.ocr import TextObservation
from test_ui_layout import image_frame, layout


def target_layout():
    configured = layout()
    configured['regions'].update(target_frame=[20, 430, 220, 500], target_health=[50, 470, 200, 480])
    return configured


def moved_target(tmp_path):
    frame = image_frame(tmp_path)
    with Image.open(frame.image_path) as source:
        image = source.convert('RGB')
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 430, 219, 499), fill='#b76a22')
    draw.rectangle((50, 470, 199, 479), fill='#202020')
    draw.rectangle((50, 470, 139, 479), fill='#00dd00')
    image.save(frame.image_path)
    return frame


def token(text, x, y, width=180, height=20):
    return TextObservation(text, 1., {'x': x, 'y': y, 'width': width, 'height': height})


@pytest.mark.parametrize('mutation', [
    lambda x: x['regions'].pop('target_frame'),
    lambda x: x['regions'].pop('target_health'),
    lambda x: x['regions'].update(target_health=[0, 0, 20, 20]),
    lambda x: x['regions'].update(target_frame=[20, 430, 1001, 500]),
])
def test_target_geometry_group_and_containment_fail_closed(mutation):
    configured = target_layout()
    mutation(configured)
    with pytest.raises(UILayoutError): validate(configured)



"""Dispatch-time guard for a grounded on-screen control."""
import asyncio
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw

from sage_wow.agent import grounded_ui as g
from sage_wow.agent.dialog_focus import DialogPanelBounds
from sage_wow.models import Frame
from sage_wow.perception.ocr import TextObservation


def panel(path, *, x=130, y=30, title='Notice', animation=False, dim=False):
    image = Image.new('RGB', (400, 300), '#182838')
    draw = ImageDraw.Draw(image)
    draw.rectangle((x, y, x + 250, min(299, y + 250)), fill='#503e2a')
    rows = []

    def label(text, px, py, color='white'):
        box = draw.textbbox((px, py), text)
        draw.text((px, py), text, fill=color)
        rows.append(TextObservation(text, .99, {'x': box[0], 'y': box[1],
                                                'width': box[2] - box[0], 'height': box[3] - box[1]}))
    label(title, x + 15, y + 15)
    label('Okay', x + 20, y + 220, '#666666' if dim else 'white')
    if animation:
        draw.rectangle((x + 200, y + 40, x + 245, y + 90), fill='#ff40dd')
    image.save(path)
    return rows


@pytest.mark.parametrize('mutation', ['animation', 'title', 'dim', 'moved', 'missing', 'stale', 'pixels', 'hover'])
def test_guard_accepts_decoration_but_rejects_control_changes(tmp_path, mutation):
    async def run():
        source = tmp_path / 'source.png'
        fresh = tmp_path / 'fresh.png'
        rows = panel(source)
        new_rows = panel(fresh, animation=mutation == 'animation',
                         title='Other Notice' if mutation == 'title' else 'Notice',
                         x=140 if mutation == 'moved' else 130, dim=mutation == 'dim')
        if mutation == 'hover':
            # Brighten the button background without moving or changing its text.
            with Image.open(fresh) as im:
                im = im.convert('RGB')
                for y in range(248, 267):
                    for x in range(147, 228):
                        if im.getpixel((x, y)) == (80, 62, 42):
                            im.putpixel((x, y), (110, 92, 72))
                im.save(fresh)
        frame = Frame.create('offline', 400, 300, image_path=str(source))
        bounds = DialogPanelBounds.parse({'left': 0., 'top': 0., 'right': 1., 'bottom': 1.})
        title, okay = rows
        box = g.box_of(okay)
        control = g.GroundedControl('acknowledge_notice', 'Acknowledge the visible notice',
                                    ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2),
                                    (g.Anchor.from_row(title), g.Anchor.from_row(okay)))

        def capture():
            result = Frame.create('offline', 400, 300, image_path=str(fresh))
            return replace(result, captured_at=frame.captured_at, frame_id=frame.frame_id) if mutation == 'stale' else result
        guard = g.make_guard(capture, frame, bounds, control, ocr=lambda _: [] if mutation == 'missing' else new_rows)
        if mutation == 'pixels':
            panel(source, title='Modified Source')
        result = await guard(frame)
        assert result.approved is (mutation in {'animation', 'hover'}), result
    asyncio.run(run())


def test_control_outside_bounds_is_rejected(tmp_path):
    source = tmp_path / 'source.png'
    rows = panel(source)
    frame = Frame.create('offline', 400, 300, image_path=str(source))
    bounds = DialogPanelBounds.parse({'left': 0., 'top': 0., 'right': .3, 'bottom': .3})
    control = g.GroundedControl('acknowledge_notice', 'Acknowledge', (200, 260), (g.Anchor.from_row(rows[1]),))
    guard = g.make_guard(lambda: frame, frame, bounds, control, ocr=lambda _: rows)
    result = asyncio.run(guard(frame))
    assert not result.approved and result.detail == 'dialog_control_outside_confirmed_geometry'

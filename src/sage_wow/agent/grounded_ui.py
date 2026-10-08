"""Grounded on-screen controls and dispatch-time guards.

OCR proposes text and rectangles; it never chooses what to click. A click needs
a Sage choice, fresh source evidence and a dispatch-time check that the control
and its anchors are still where they were.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
from pathlib import Path
import time

from PIL import Image, ImageChops, ImageStat

from sage_wow.agent.cycle import DispatchValidation
from sage_wow.perception.ocr import recognize_text


CONTROL_LABELS = {'continue': 'dialog_continue', 'complete quest': 'dialog_complete_quest',
                  'goodbye': 'dialog_goodbye', 'accept': 'dialog_accept'}


def text_key(text):
    return ' '.join(str(text).casefold().split())


def box_of(obs):
    b = obs.bounds
    return (b['x'], b['y'], b['x'] + b['width'], b['y'] + b['height'])


def inside(box, region):
    return region[0] <= box[0] < box[2] <= region[2] and region[1] <= box[1] < box[3] <= region[3]


def usable(observations, width, height):
    return [o for o in observations if o.confidence >= .7
            and inside(box_of(o), (0, 0, width, height)) and text_key(o.text)]


@dataclass(frozen=True)
class Anchor:
    text: str
    box: tuple[int, int, int, int]

    @classmethod
    def from_row(cls, row):
        return cls(row.text, box_of(row))


@dataclass(frozen=True)
class GroundedControl:
    option: str
    description: str
    point: tuple[int, int]
    anchors: tuple[Anchor, ...]
    patch: tuple[int, int, int, int] | None = None
    reward_name: str | None = None


def _glyphs(image):
    """High-contrast glyph shape, independent of uniform hover illumination."""
    gray = image.convert('L')
    hist = gray.histogram()
    count = sum(hist); total = sum(i*n for i,n in enumerate(hist))
    weight = accum = 0; best = -1; threshold = 127
    for i,n in enumerate(hist):
        weight += n; accum += i*n
        if not weight or weight == count:
            continue
        score = weight*(count-weight)*(accum/weight - (total-accum)/(count-weight))**2
        if score > best:
            best, threshold = score, i
    mask = gray.point(lambda p: 255 if p > threshold else 0)
    if sum(mask.get_flattened_data()) > count*127.5:
        mask = ImageChops.invert(mask)
    ink = ImageStat.Stat(gray, mask).mean[0]
    return mask, ink


def make_guard(capture, frame, bounds, control, ocr=recognize_text):
    """Freeze critical text/point pixels; ignore unrelated panel animation."""
    with Image.open(frame.image_path) as image:
        image = image.convert('RGB')
        snapshots = [(a, image.crop(a.box).copy()) for a in control.anchors]
        patch = image.crop(control.patch).copy() if control.patch else None
        source_hash = hashlib.sha256(image.tobytes()).hexdigest()
    async def guard(source):
        x,y=control.point
        left,top,right,bottom=bounds.pixels(frame.width,frame.height)
        if not (type(x) is int and type(y) is int and left<=x<right and top<=y<bottom
                and control.anchors and all(inside(a.box,(left,top,right,bottom)) for a in control.anchors)):
            return DispatchValidation(False,detail='dialog_control_outside_confirmed_geometry')
        if not callable(capture):
            return DispatchValidation(False, detail='dialog_continuity_capture_unavailable')
        try:
            with Image.open(source.image_path) as original:
                if hashlib.sha256(original.convert('RGB').tobytes()).hexdigest() != source_hash:
                    return DispatchValidation(False, detail='dialog_source_pixels_changed_after_sage_request')
            fresh = await asyncio.to_thread(capture)
            if (fresh.width,fresh.height) != (source.width,source.height):
                return DispatchValidation(False, detail='dialog_geometry_changed')
            if (fresh.frame_id == source.frame_id or fresh.captured_at <= source.captured_at
                    or not 0 <= time.time()-datetime.fromisoformat(fresh.captured_at).timestamp() <= 10):
                return DispatchValidation(False, detail='dialog_dispatch_frame_not_fresh')
            observed = await asyncio.to_thread(ocr, Path(fresh.image_path))
            rows = usable(observed, fresh.width, fresh.height)
            with Image.open(fresh.image_path) as image:
                image = image.convert('RGB')
                if image.size != (fresh.width,fresh.height):
                    return DispatchValidation(False, detail='dialog_geometry_changed')
                for anchor, before in snapshots:
                    matches = [o for o in rows if text_key(o.text) == text_key(anchor.text)
                               and max(abs(a-b) for a,b in zip(box_of(o),anchor.box)) <= 2]
                    if len(matches) != 1:
                        return DispatchValidation(False, detail='dialog_identity_or_control_changed',
                            evidence={'expected_text':anchor.text,'box':anchor.box})
                    after = image.crop(anchor.box)
                    a,old_ink = _glyphs(before); b,new_ink = _glyphs(after)
                    fraction = sum(p > 0 for p in ImageChops.difference(a,b).get_flattened_data())/(a.width*a.height)
                    # Exact OCR/position plus glyph continuity. Dimming a control
                    # is not treated as harmless illumination or proof enabled.
                    if fraction > .025 or old_ink-new_ink > 25:
                        return DispatchValidation(False, detail='dialog_critical_text_changed',
                            evidence={'expected_text':anchor.text,'glyph_change_fraction':fraction,
                                      'ink_luminance_before':old_ink,'ink_luminance_after':new_ink})
                if patch and ImageChops.difference(patch,image.crop(control.patch)).getbbox():
                    return DispatchValidation(False, detail='dialog_reward_point_changed')
            return DispatchValidation(True, dispatch_frame=fresh,
                detail='fresh title/control OCR, geometry and glyph continuity; decorations excluded',
                evidence={'method':'dialog-critical-anchors-v1','anchors':[asdict(a) for a,_ in snapshots],
                          'source_frame_id':source.frame_id,'fresh_frame_id':fresh.frame_id,
                          'limitations':'fallible OCR/pixels; Sage verifies quest identity and enabled state'})
        except Exception as exc:
            return DispatchValidation(False, detail='dialog_continuity_failed', evidence={'error_type':type(exc).__name__})
    return guard

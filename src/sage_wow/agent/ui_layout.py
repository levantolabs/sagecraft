"""Operator-specified screenshot rectangles; geometry, never gameplay facts."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from PIL import Image


class UILayoutError(ValueError):
    pass


REGIONS = {'player_health', 'player_frame', 'target_health', 'target_frame', 'target_overlay', 'coordinate_primary', 'coordinate_alternate',
           'coordinate_magnification', 'minimap', 'local_caption', 'quest_tracker'}
GROUPS = ({'player_health', 'player_frame'},
          {'target_health', 'target_frame'},
          {'coordinate_primary', 'coordinate_alternate', 'coordinate_magnification', 'minimap', 'local_caption'})


def validate(layout):
    if layout is None:
        return None
    if not isinstance(layout, dict) or set(layout) != {'version', 'id', 'provenance', 'image_width', 'image_height', 'regions'}:
        raise UILayoutError('UI layout requires version, id, provenance, image_width, image_height and regions; recalibrate calibration.ui_layout.')
    if type(layout['version']) is not int or layout['version'] != 1:
        raise UILayoutError('UI layout version must be 1; recalibrate calibration.ui_layout.')
    if any(not isinstance(layout[k], str) or not layout[k].strip() or len(layout[k]) > limit
           for k, limit in [('id', 100), ('provenance', 500)]):
        raise UILayoutError('UI layout id/provenance must identify the operator calibration.')
    w, h = layout['image_width'], layout['image_height']
    if type(w) is not int or type(h) is not int or w <= 0 or h <= 0:
        raise UILayoutError('UI layout image dimensions must be positive integers.')
    regions = layout['regions']
    if not isinstance(regions, dict) or not regions or set(regions)-REGIONS:
        raise UILayoutError('UI layout regions must use supported named rectangles.')
    for name, box in regions.items():
        if (not isinstance(box, (list, tuple)) or len(box) != 4 or any(type(v) is not int for v in box)
                or not 0 <= box[0] < box[2] <= w or not 0 <= box[1] < box[3] <= h):
            raise UILayoutError(f'UI layout {name} must be an in-image pixel rectangle [left, top, right, bottom].')
    for group in GROUPS:
        if set(regions) & group and not group <= set(regions):
            raise UILayoutError(f'UI layout must calibrate related regions together: {", ".join(sorted(group))}.')
    for inner, outer in [('player_health', 'player_frame'), ('target_health', 'target_frame'), ('target_frame', 'target_overlay'), ('coordinate_primary', 'coordinate_alternate'),
                         ('coordinate_primary', 'coordinate_magnification')]:
        if inner in regions and outer in regions:
            a, b = regions[inner], regions[outer]
            if not (b[0] <= a[0] < a[2] <= b[2] and b[1] <= a[1] < a[3] <= b[3]):
                raise UILayoutError(f'UI layout {outer} must contain {inner}.')
    return deepcopy(layout)


def from_profile(profile):
    return validate(profile.values.get('calibration', {}).get('ui_layout'))


def kwargs(profile):
    layout = from_profile(profile)
    return {'ui_layout': layout} if layout is not None else {}


def fingerprint(layout):
    return hashlib.sha256(json.dumps(layout, sort_keys=True).encode()).hexdigest()[:16] if layout is not None else None


class UIRegions:
    def __init__(self, frame, image_size, layout=None):
        self.layout = validate(layout)
        self.width, self.height = image_size
        self.used = {}
        if self.layout is not None:
            expected = (self.layout['image_width'], self.layout['image_height'])
            if image_size != expected or (frame.width, frame.height) != expected:
                raise UILayoutError(f'UI layout {self.layout["id"]!r} expects {expected[0]}x{expected[1]} screenshot pixels; '
                    f'got image {self.width}x{self.height}, frame {frame.width}x{frame.height}. '
                    'Recalibrate calibration.ui_layout for the current window/UI before continuing; no automatic scaling.')

    def pixels(self, name, default_relative):
        configured = (self.layout or {}).get('regions', {}).get(name)
        box = tuple(configured) if configured is not None else tuple(round(v*s) for v, s in
            zip(default_relative, (self.width, self.height, self.width, self.height)))
        self.used[name] = {'pixels': list(box), 'source': 'operator layout' if configured is not None else 'legacy default'}
        return box

    def relative(self, name, default_relative):
        box = self.pixels(name, default_relative)
        if name not in (self.layout or {}).get('regions', {}):
            return default_relative
        return tuple(v/s for v, s in zip(box, (self.width, self.height, self.width, self.height)))

    def evidence(self):
        if self.layout is None:
            return None
        return {'id': self.layout['id'], 'fingerprint': fingerprint(self.layout),
            'provenance': self.layout['provenance'], 'image_width': self.width, 'image_height': self.height,
            'regions': deepcopy(self.used), 'source': 'operator-specified UI geometry; no game-state or semantic inference'}


def validate_frame(frame, profile):
    layout = from_profile(profile)
    if layout is not None:
        with Image.open(frame.image_path) as image:
            regions=UIRegions(frame, image.size, layout)
            for name in layout['regions']:
                regions.pixels(name,(0,0,1,1))
            return regions.evidence()

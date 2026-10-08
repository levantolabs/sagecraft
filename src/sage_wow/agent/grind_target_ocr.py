"""One calibrated reread of missing target text; never a gameplay decision."""
from dataclasses import asdict
import hashlib
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from PIL import Image

from sage_wow.control.target_names import plain_target_name
from sage_wow.perception.ocr import TextObservation


def reread(profile, config, frame, rows, raw, ocr):
    """Keep usable full-frame facts; recover only from fresh source pixels.

    Cropping changes the OCR observation, not the observed string. Unsupported
    characters are never stripped, and competing valid readings stay unknown.
    The caller caches this once per source frame, not once per failed choice.
    """
    from sage_wow.agent.grind_only import key, region_rows

    fields = []
    if not plain_target_name(raw.get('name')) and config.get('target_name_box'):
        fields.append(('name', config['target_name_box']))
    if not raw.get('levels'):
        fields.append(('level', config['target_level_box']))
    if not fields:
        return raw
    path = Path(frame.image_path)
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    proof = {'frame_id': frame.frame_id, 'source_sha256': source_hash,
             'raw_name': raw.get('name'), 'raw_levels': list(raw.get('levels', [])),
             'scale': 3, 'fields': {}}
    result = dict(raw)
    with Image.open(path) as original, TemporaryDirectory(prefix='sage-target-ocr-') as directory:
        if original.size != (frame.width, frame.height):
            raise ValueError('Target OCR source geometry changed')
        for field, box in fields:
            if (len(box) != 4 or any(type(v) is not int for v in box)
                    or not 0 <= box[0] < box[2] <= frame.width
                    or not 0 <= box[1] < box[3] <= frame.height):
                raise ValueError('Target OCR requires a contained calibrated region')
            crop = original.convert('RGB').crop(box)
            crop = crop.resize((crop.width * 3, crop.height * 3))
            image_path = Path(directory) / (field + '.png')
            crop.save(image_path)
            observed = ocr(image_path)
            candidates = [row for row in observed if row.confidence >= .85
                          and row.bounds['width'] > 0 and row.bounds['height'] > 0
                          and 0 <= row.bounds['x'] < row.bounds['x'] + row.bounds['width'] <= crop.width
                          and 0 <= row.bounds['y'] < row.bounds['y'] + row.bounds['height'] <= crop.height]
            fact = {'box': list(box), 'image_sha256': hashlib.sha256(image_path.read_bytes()).hexdigest(),
                    'rows': [asdict(row) for row in observed], 'accepted': False}
            proof['fields'][field] = fact
            # Multiple nonempty observations are ambiguity, even if one looks
            # like the name/number we hoped to find.
            if len(candidates) != 1 or len([row for row in observed if row.text.strip()]) != 1:
                continue
            row = candidates[0]
            if field == 'name':
                if not plain_target_name(row.text):
                    continue
                competing = [item.text for item in region_rows(rows, box)
                             if plain_target_name(item.text) and item.text.casefold() != row.text.casefold()]
                if competing:
                    fact['conflicting_full_frame_names'] = competing
                    continue
                b = row.bounds
                left, top = box[0] + b['x'] // 3, box[1] + b['y'] // 3
                right = box[0] + (b['x'] + b['width'] + 2) // 3
                bottom = box[1] + (b['y'] + b['height'] + 2) // 3
                result['name'] = row.text
                result['name_row'] = TextObservation(row.text, row.confidence,
                    {'x': left, 'y': top, 'width': right - left, 'height': bottom - top},
                    source='calibrated_target_crop_ocr')
                character = profile.values['character']
                own = ' '.join(str(character.get(k) or '') for k in ('name', 'surname')).strip()
                result['self_target'] = raw['self_target'] or key(row.text) in {key(own), key(character['name'])}
            else:
                if not re.fullmatch(r'[1-9][0-9]?', row.text):
                    continue
                result['levels'] = [int(row.text)]
            fact['accepted'] = True
    if hashlib.sha256(path.read_bytes()).hexdigest() != source_hash:
        raise ValueError('Immutable target OCR source changed during reread')
    result['focused_ocr'] = proof
    return result

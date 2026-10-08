"""Task-specific presentation of immutable movement evidence, never authority."""
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw


def motion_context(pending, target):
    action = pending['action']
    duration = (pending['receipt'].get('selected_binding') or {}).get('hold_seconds')
    purpose = pending.get('purpose')
    subject = target.get('name') or (pending.get('target') or {}).get('name')
    if purpose == 'standing':
        task = 'Compare our stance: did the movement stand our player up?'
    elif purpose in {'approach', 'cast_correction'}:
        task = (f'Compare our position and facing relative to the selected {subject}. '
            'Look at that creature in the WORLD views: is it closer, better faced, or approached from a clearer angle? '
            'A useful partial approach counts even if another step is needed. '
            'You are not being asked to prove that a spell is already in range or that it caused damage.')
    else:
        task = 'Compare the WORLD views: did this movement reveal a useful, visibly safe changed search sector?'
    return (f'Assess one completed movement: {action}, duration {duration} seconds, purpose {purpose}. '
        'The upper panel is BEFORE this movement; the next panel is CURRENT, after it. '
        'Both world views use the same scale. Current HUD crops are below the world comparison. '
        + task + ' Judge the visible change, not just the keypress or map-coordinate change. '
        'An unchanged health bar is normal during movement. A useful movement is not damage or kill credit. '
        'If the change cannot be seen, leave its effect unknown. Passive chat and normal HUD do not block the world.')


def motion_evidence_image(frame, box, path, *, history=(), player_hud=None, player_badge=None,
                          alternate=False, crop_label='', omit_target=False):
    """Same-scale chronological world pair, followed by modest current HUD crops."""
    from sage_wow.agent.grind_only import RetainedGrindSourceChanged
    try:
        if len(history) != 1:
            raise ValueError('motion evidence needs one linked source')
        before_path, expected_hash, _, _ = history[0]
        if not expected_hash or hashlib.sha256(Path(before_path).read_bytes()).hexdigest() != expected_hash:
            raise RetainedGrindSourceChanged('retained_grind_source_changed')
        with Image.open(before_path) as old, Image.open(frame.image_path) as new:
            if old.size != new.size:
                raise RetainedGrindSourceChanged('retained_grind_source_dimensions_changed')
            before, current = old.convert('RGB'), new.convert('RGB')
        scale = min(1, 1100 / current.width)
        size = (round(current.width * scale), round(current.height * scale))
        before = before.resize(size); after = current.resize(size)
        crops = []
        if not omit_target:
            crops.append(('CURRENT SELECTED TARGET', current.crop(box)))
        if player_hud or player_badge:
            crops.append(('CURRENT PLAYER', current.crop(player_hud or player_badge)))
        for _, crop in crops:
            crop.thumbnail((size[0] // 2 - 16, 160))
        header = 28
        bottom = max((crop.height for _, crop in crops), default=0)
        canvas = Image.new('RGB', (size[0], 2 * (size[1] + header) + (bottom + header if crops else 0)), '#151b23')
        draw = ImageDraw.Draw(canvas)
        for i, (label, world) in enumerate((('BEFORE MOVEMENT', before), ('CURRENT - AFTER MOVEMENT', after))):
            y = i * (size[1] + header)
            draw.text((8, y + 8), label, fill='white'); canvas.paste(world, (0, y + header))
        y = 2 * (size[1] + header)
        for i, (label, crop) in enumerate(crops):
            x = i * (size[0] // 2)
            draw.text((x + 8, y + 8), label, fill='white'); canvas.paste(crop, (x, y + header))
        canvas.save(path)
        return path
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise

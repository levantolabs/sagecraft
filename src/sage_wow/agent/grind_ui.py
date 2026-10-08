"""Current blocking-UI choices, separate from historical combat outcomes."""
import re

from PIL import Image

from sage_wow.agent.cycle import ActionCandidate, DispatchValidation
from sage_wow.agent.dialog_focus import DialogPanelBounds
from sage_wow.agent.grounded_ui import Anchor, GroundedControl, make_guard
from sage_wow.agent.ui_reset import panel_cues


def active_text_entry(frame, rows):
    """Current chat-entry label plus its enclosing field, never passive chat."""
    explicit = [row for row in rows if row.confidence >= .75 and row.text.strip().casefold()
                in {'chat input', 'type a message', 'active text entry'}]
    if explicit:
        return {'frame_id': frame.frame_id, 'label': explicit[0].text,
                'bounds': dict(explicit[0].bounds), 'kind': 'explicit_entry_label'}
    # OCR may merge the channel label with all typed content, or omit the
    # colon on an otherwise empty field. A recipient-bearing Tell label still
    # needs its colon; ordinary "tell ..." prose is not a channel label.
    channel = r'(?:say(?::.*)?|tell(?::.*)?|tell\s+[^:\r\n]{1,64}:.*)'
    labels = [row for row in rows if row.confidence >= .75
              and re.fullmatch(channel, row.text.strip().casefold())]
    if not labels:
        return None
    with Image.open(frame.image_path) as source:
        pixels = source.convert('L'); width, height = pixels.size
        if (width, height) != (frame.width, frame.height):
            return None
        for row in labels:
            b = row.bounds; x, y, w, h = (round(b[k]) for k in ('x', 'y', 'width', 'height'))
            if not (0 < w and x+w <= width*.5 and 4 <= h <= height*.06 and 0 <= x <= width*.15
                    and height*.65 <= y and y+h <= height*.97):
                continue
            # This is a bounded geometry probe, not an invented OCR rectangle.
            # A merged occupied row must not demand a border twice the width of
            # its entire command. Original row text/bounds remain in evidence.
            probe = min(w, h*3)
            start = min(width-1, x+probe+max(2, h//3))
            end = min(width, round(width*.5), x+max(probe*12, round(width*.25)))
            minimum = max(24, round(width*.07), probe*2)
            if end-start < minimum:
                continue
            for top in range(max(0, y-h), min(height-2, y+3)):
                for bottom in range(max(top+6, y+h//2), min(height-1, y+h*2)):
                    if not .5*h <= bottom-top <= 2.5*h:
                        continue
                    middle = (top+bottom)//2
                    run = 0; right = None
                    for xx in range(start, end):
                        upper, lower, inside = pixels.getpixel((xx, top)), pixels.getpixel((xx, bottom)), pixels.getpixel((xx, middle))
                        if min(upper, lower) >= 95 and inside <= 85 and min(upper, lower)-inside >= 35:
                            run += 1
                            if run >= minimum:
                                right = xx; break
                        else:
                            run = 0
                    if right is None:
                        continue
                    left = next((xx for xx in range(max(0, x-h), min(width, x+h//2+1))
                        if sum(pixels.getpixel((xx, yy)) >= 95 for yy in range(top+1, bottom))
                           >= .65*max(1, bottom-top-1)), None)
                    if left is not None:
                        return {'frame_id': frame.frame_id, 'label': row.text,
                            'bounds': dict(b), 'kind': 'bordered_chat_entry',
                            'field_evidence': [left, top, right, bottom]}
    return None


def decline_control(frame, rows):
    labels = {'join guild', 'decline invitation'}
    if not labels.issubset(panel_cues(frame, rows)):
        return None
    matches = {label: [] for label in labels}
    for row in rows:
        label = ' '.join(row.text.casefold().split()); b = row.bounds
        if (label in labels and row.confidence >= .75
            and b.get('width', 0) > 0 and b.get('height', 0) > 0
            and frame.width*.25 <= b.get('x', 0) < frame.width*.75
            and frame.height*.10 <= b.get('y', 0) < frame.height*.65):
            matches[label].append(row)
    if any(len(value) != 1 for value in matches.values()):
        return None
    join, decline = matches['join guild'][0], matches['decline invitation'][0]
    b = decline.bounds
    return GroundedControl('decline_invitation',
        'Click the visible enabled Decline Invitation button to dismiss this guild invitation. '
        'This declines only; never click Join Guild. Verify the dialog closed on the next view.',
        (round(b['x'] + b['width']/2), round(b['y'] + b['height']/2)),
        (Anchor.from_row(join), Anchor.from_row(decline)))


async def decline_candidate(c, frame, rows, valid):
    control = decline_control(frame, rows)
    if control is None:
        return None
    generation, epoch = c.cycle.input_generation, c.cycle.session_epoch
    bounds = DialogPanelBounds.parse({'left': 0., 'top': 0., 'right': 1., 'bottom': 1.})
    guarded = make_guard(c.capture, frame, bounds, control, c.ocr)
    def current():
        return (valid() and c.current() and generation == c.cycle.input_generation
            and epoch == c.cycle.session_epoch and not c.hunt.input_effect_unverified)
    async def check(source):
        if not current():
            return DispatchValidation(False, detail='Invitation source/input authority changed')
        result = await guarded(source)
        if not result.approved:
            return result
        fresh = result.dispatch_frame
        actual = decline_control(fresh, await c.rows(fresh))
        if (not current() or fresh.source != frame.source or c.cycle._scope_error(fresh)
            or actual is None or actual.point != control.point):
            return DispatchValidation(False, detail='Invitation pair or scope changed before dismissal')
        return result
    return ActionCandidate(control.option, control.description,
        {'type': 'click', 'image_x': control.point[0], 'image_y': control.point[1]},
        precondition=current, dispatch_guard=check)


def context(ui):
    if not ui['positive']:
        return ('Current task: verify that the current world is clear on this fresh view. '
            'Confirm clear world only if the blocking dialog is gone and no typing field or menu is active. '
            'Any completed closure input alone proves no outcome. Prior combat and loot facts remain unchanged.')
    return ('Current task: close the visible blocking UI so hunting can resume. '
        f'Current UI cues: {ui["cues"]}. '
        'Choose a supported dismissal of the CURRENT dialog. Closing UI establishes no combat or loot outcome. '
        'A guild invitation is unsolicited; dismiss it without joining. '
        'After closure input, assess a new screenshot before any world input.')

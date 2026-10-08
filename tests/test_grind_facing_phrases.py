"""Actual live-run OCR phrases must reach existing guarded combat corrections.

These are offline synthetic screenshots/OCR and scripted Sage choices. The
strings were retained at 14:03:11, 14:03:18 and 14:03:26 UTC on 2026-10-02.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from sage_wow.agent.grind_perception import read_hud_scene
from test_grind_product_spec import Rig


RECORDED_FACING_ERRORS = [
    'Target needs to berin front of you.',
    'You are facing the wrong way!',
    'Target needs to be in front of you.',
]


@pytest.mark.parametrize('text', RECORDED_FACING_ERRORS)
@pytest.mark.parametrize('source', ['current', 'early_post_cast'])
def test_recorded_error_reaches_guarded_turn_without_another_cast(tmp_path, text, source):
    async def exercise():
        rig = Rig(tmp_path)
        try:
            await rig.start()
            rig.c.config.update(committed_combat=True, turn_seconds=.05)
            await rig.choose('attack_mob_level_1')
            original_cast = rig.c.hunt.pending['receipt']['receipt_id']
            rig.world.error = text
            if source == 'early_post_cast':
                early = rig.world.capture()
                layout = rig.c.profile.values['calibration']['ui_layout']
                target_box = layout['regions']['target_frame']
                error_y = (target_box[3]-target_box[1])*3+40
                def row(value, y):
                    return SimpleNamespace(text=value, confidence=.99,
                        bounds={'x': 0, 'y': y, 'width': 250, 'height': 15})
                # The production focused OCR parser, not a hand-typed facing
                # cue, builds the retained early-postcast observation.
                scene = read_hud_scene(early, ui_layout=layout,
                    ocr=lambda _: [row('Young Wolf', 0), row(text, error_y)])
                rig.c.hunt.pending['receipt']['execution']['post_cast_observations'] = [{
                    'phase': 'early_post_cast', 'visible_error_ocr': scene.error,
                    'observed_error_cues': scene.error_cues, 'frame_id': early.frame_id,
                    'image_path': early.image_path, 'captured_at': early.captured_at}]
                rig.world.error = ''  # It faded before the next Sage request.
            await rig.choose('turn_left')
            options = rig.sage.calls[-1]['options']
            assert {'turn_left', 'turn_right', 'position_error'} <= options.keys()
            assert 'forward' not in options
            assert not any(name.startswith('attack_mob') for name in options)
            assert rig.c.hunt.cast_error['kind'] == 'facing'
            assert rig.c.hunt.pending['purpose'] == 'cast_correction'
            assert rig.controls['turn_left']['keycode'] in rig.physical_keys()
            assert sum(event[1].startswith('/cast ') for event in rig.casts()) == 1
            assert not rig.c.hunt.credited_kills
            guards = [json.loads(row[0]) for row in rig.store.connection.execute(
                "SELECT payload_json FROM events WHERE event_type='dispatch_guard_checked'")]
            turn_guards = [guard for guard in guards if guard['chosen'] == 'turn_left']
            assert len(turn_guards) == 1 and turn_guards[0]['approved']
            assert turn_guards[0]['evidence']['selected_target_required']
            assert all(turn_guards[0]['evidence']['predicates'].values())
            assert any(outcome['receipt_id'] == original_cast for outcome in rig.c.hunt.outcomes)
        finally:
            await rig.close()
    asyncio.run(exercise())

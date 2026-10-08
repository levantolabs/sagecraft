"""Offline regressions from a live run; retained pixels are HUD-only crops."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from sage_wow.agent.cycle import ActionCandidate
from sage_wow.agent.grind_loot import _corpse_choices, _corpse_continuity
from sage_wow.agent.grind_only import same_patch
from sage_wow.models import Frame
from test_grind_loot_integration import LootRig


FIXTURES = Path(__file__).parent / 'fixtures' / 'loot_translucent_hud'


def cropped_controller():
    badge = [280, 77, 322, 119]
    layout = {'version': 1, 'id': 'retained-corpse', 'provenance': 'retained HUD crop',
        'image_width': 335, 'image_height': 128, 'regions': {
            'target_frame': [3, 9, 215, 74], 'target_health': [13, 45, 211, 68],
            'target_overlay': [0, 0, 335, 128]}}
    return SimpleNamespace(profile=SimpleNamespace(values={'calibration': {'ui_layout': layout}}),
        config={'target_name_box': [10, 18, 209, 42], 'target_level_box': badge,
                'target_badge_continuity': {'badge_box': badge, 'image_width': 335,
                    'image_height': 128, 'inset_pixels': 5}})


@pytest.mark.parametrize('before,after', [('before', 'occluded'), ('occluded', 'after')])
def test_retained_translucent_dead_hud_ignores_background_creature(before, after):
    provenance = json.loads((FIXTURES / 'provenance.json').read_text())
    for row in provenance['records']:
        assert hashlib.sha256((FIXTURES / row['fixture']).read_bytes()).hexdigest() == row['fixture_sha256']
    a, b = (FIXTURES / (label+'.png') for label in (before, after))
    assert not same_patch(a, b, [0, 0, 335, 128])
    assert not same_patch(a, b, [13, 45, 211, 68])
    assert all(_corpse_continuity(cropped_controller(), a, b, {'box': [0, 0, 335, 128]}).values())


@pytest.mark.parametrize('box', [[10, 18, 209, 42], [13, 45, 211, 68], [280, 77, 322, 119]])
def test_changed_or_absent_corpse_identity_glyphs_fail(tmp_path, box):
    before = FIXTURES / 'before.png'
    with Image.open(before) as image:
        image = image.convert('RGB')
    ImageDraw.Draw(image).rectangle(tuple(box), fill='black')
    changed = tmp_path/'changed.png'
    image.save(changed)
    assert not all(_corpse_continuity(cropped_controller(), before, changed, {'box': [0, 0, 335, 128]}).values())


def test_corpse_proposals_exclude_observed_target_hud_click(tmp_path, monkeypatch):
    from sage_wow.agent import grind_loot
    path = tmp_path/'scene.png'
    Image.new('RGB', (1496, 967)).save(path)
    frame = Frame.create('offline', 1496, 967, image_path=str(path))
    points = [ActionCandidate(name, 'offline point', {'type': 'click', 'image_x': x, 'image_y': y})
              for name, x, y in [('hud', 415, 560), ('corpse', 671, 475)]]
    monkeypatch.setattr(grind_loot, 'corpse_proposals', lambda *args, **kwargs: points)
    layout = {'regions': {'target_overlay': [145, 500, 480, 628], 'player_frame': [52, 348, 446, 496]}}
    offered = {choice.option for choice in _corpse_choices(frame, layout)}
    assert 'hud' not in offered and 'corpse' in offered


@pytest.mark.parametrize('health,death_available', [('retained_full', False), ('black', True), ('#263746', True)])
def test_loot_player_death_choice_respects_current_own_health(tmp_path, health, death_available):
    async def exercise():
        rig = LootRig(tmp_path)
        try:
            await rig.start()
            rig.death()
            if health == 'retained_full':
                regions = rig.c.profile.values['calibration']['ui_layout']['regions']
                regions['player_health'] = [40, 30, 285, 58]
                regions['player_frame'] = [0, 0, 300, 90]
                original = rig.world.capture
                def capture():
                    frame = original()
                    with Image.open(frame.image_path) as source:
                        image = source.convert('RGB')
                    with Image.open(FIXTURES/'player_alive.png') as health_bar:
                        image.paste(health_bar, (40, 30))
                    image.save(frame.image_path)
                    return frame
                rig.world.capture = rig.c.capture = capture
            else:
                rig.world.player_health = health
            await rig.loot(None)
            options = rig.sage.calls[-1]['options']
            assert ('dead_or_unrecoverable' in options) is death_available
            assert 'recover_now' in options
            assert not rig.c.stopped and not rig.physical_keys()
            if death_available:
                await rig.loot('dead_or_unrecoverable')
                assert rig.c.stopped
        finally:
            await rig.close()
    asyncio.run(exercise())


def ground_choice(monkeypatch):
    from sage_wow.agent import grind_loot
    monkeypatch.setattr(grind_loot, '_corpse_choices', lambda *args: [ActionCandidate(
        'corpse_select_0', 'offline corpse', {'type': 'click', 'image_x': 100, 'image_y': 250})])


async def click_corpse(rig):
    rig.backend.mouse_button = lambda *args: rig.backend.events.append(('mouse', *args))
    await rig.loot('corpse_select_0')
    assert rig.c.loot.data['attempts'] == 0 and rig.c.loot.data['confirmations'] == 0
    assert rig.c.loot.pending


def test_bound_corpse_click_opens_two_view_positive_verification_without_f(tmp_path, monkeypatch):
    ground_choice(monkeypatch)
    async def exercise():
        rig = LootRig(tmp_path)
        try:
            await rig.start()
            rig.death()
            await click_corpse(rig)
            rig.world.name = ''  # Target auto-cleared; this alone is no success.
            await rig.loot('loot_verified')  # Explicit scripted positive image finding.
            assert 'BEFORE BOUND CORPSE CLICK' in rig.sage.calls[-1]['instructions']
            assert rig.c.loot.pending and rig.c.loot.data['confirmations'] == 1
            assert rig.c.loot.data['attempts'] == 0
            await rig.loot('loot_verified')
            assert not rig.c.loot.pending
            assert rig.c.loot.data['outcome'] == 'verified_looted'
            assert rig.physical_keys() == [] and not rig.c.hunt.credited_kills
        finally:
            await rig.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('mismatch', ['epoch', 'generation', 'source', 'source_hash'])
def test_click_loot_authority_cannot_cross_scope_or_obligation(tmp_path, monkeypatch, mismatch):
    ground_choice(monkeypatch)
    async def exercise():
        rig = LootRig(tmp_path)
        try:
            await rig.start()
            rig.death()
            await click_corpse(rig)
            await rig.loot('loot_verified')
            authority = rig.c.loot.data['click_loot_authority']
            if mismatch == 'epoch':
                authority['session_epoch'] = 'old-run'
            elif mismatch == 'generation':
                authority['input_generation'] -= 1
            elif mismatch == 'source':
                authority['sources'] = ['unrelated-corpse']
            else:
                authority['sha256'] = 'changed'
            await rig.loot(None)
            assert 'loot_verified' not in rig.sage.calls[-1]['options']
            assert rig.c.loot.pending and rig.c.loot.data['confirmations'] == 0
        finally:
            await rig.close()
    asyncio.run(exercise())


def test_partial_corpse_click_never_opens_verification(tmp_path, monkeypatch):
    ground_choice(monkeypatch)
    async def exercise():
        rig = LootRig(tmp_path)
        try:
            await rig.start()
            rig.death()
            def failure(*args):
                raise RuntimeError('offline uncertain mouse delivery')
            rig.backend.mouse_button = failure
            async def no_change():
                pass
            rig.sage.hook = no_change
            result = await rig.loot('corpse_select_0')
            assert result.receipt['dispatch_unknown'] or result.receipt['error']
            assert rig.c.loot.data.get('click_loot_authority') is None
            assert rig.c.loot.data['confirmations'] == 0 and rig.c.loot.pending
        finally:
            await rig.close()
    asyncio.run(exercise())

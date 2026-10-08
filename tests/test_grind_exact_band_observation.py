"""Live-run regression: an attack label cannot fill missing level OCR."""
import asyncio
import json

import pytest

from sage_wow.agent.grind_only import settings
from test_grind_level3_recovery import RecoveryRig, target_observer


async def start_band(r, own=3):
    prior = r.world.directory / 'prior.json'
    prior.write_text(json.dumps({'character': 'Test Player', 'verified_level': own,
        'level_observation_frame_id': 'offline-prior', 'source_session_id': 'offline'}))
    r.p.values['grind_only'].update(goal_level=5,
        target_bands_by_player_level={3: [2, 4], 4: [2, 4]}, target_names=[],
        campaign_continuation={'character': 'Test Player', 'verified_level': own,
            'evidence_path': str(prior)})
    r.c.config.update(settings(r.p))
    r.c.hunt.target_bands_by_player_level = r.c.config['target_bands_by_player_level'].copy()
    await r.start(own)
    assert r.c.baseline and not r.c.stopped


@pytest.mark.parametrize('own', [3, 4])
def test_missing_numeric_ocr_cannot_be_supplied_by_attack_choice(tmp_path, own):
    async def run():
        r = RecoveryRig(tmp_path/'missing')
        try:
            await start_band(r, own)
            assert r.world.target_level is None  # Image badge1, OCR has name but no number.
            await r.choose(None)
            options = r.sage.calls[-1]['options']
            assert not any(name.startswith('attack_mob_level_') for name in options)
            assert not (r.c.config.get('selected_target_observer') or {}).get('enabled')
            assert 'reinspect_selected_frame' not in options
            assert 'change_search_strategy' in options
            assert not r.physical()
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('observed_level', [1, 2, 4, None])
def test_missing_ocr_runs_separate_observer_before_eligibility(tmp_path, observed_level):
    async def run():
        calls = []
        async def observer(path, **kw):
            calls.append(kw)
            result = await target_observer()(path, **kw)
            result['observation']['level'] = observed_level
            return result
        r = RecoveryRig(tmp_path/'observer', observer=observer)
        try:
            await start_band(r)
            # Keep the drawn badge realistic while explicitly dropping its OCR row.
            r.world.target_level = observed_level
            original_ocr = r.c.ocr
            r.c.ocr = lambda path: [row for row in original_ocr(path)
                                   if row.bounds['x'] != 425]
            before = len(r.sage.calls)
            assert (await r.choose()).status == 'grind_reobserve'
            assert len(calls) == 1 and len(r.sage.calls) == before and not r.physical()
            eligible = observed_level in (2, 4)
            result = await r.choose(f'attack_mob_level_{observed_level}' if eligible else None)
            attacks = {name for name in r.sage.calls[-1]['options'] if name.startswith('attack_mob_level_')}
            assert attacks == ({f'attack_mob_level_{observed_level}'} if eligible else set())
            assert bool(r.physical()) == eligible
            if eligible:
                assert result.status == 'dispatched'
                assert ('text', '/cast [harm,nodead] Smite') in r.backend.events
                assert r.events('dispatch_guard_checked')[-1]['approved']
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('flaw', ['stale', 'scope', 'failure'])
def test_unusable_numeric_observation_leaves_attack_withheld(tmp_path, flaw):
    async def run():
        async def observer(path, **kw):
            if flaw == 'failure': raise RuntimeError('offline provider unavailable')
            result = await target_observer(flaw=flaw)(path, **kw)
            result['observation']['level'] = 2
            return result
        r = RecoveryRig(tmp_path/flaw, observer=observer)
        try:
            await start_band(r)
            assert (await r.choose()).status == 'grind_reobserve'
            assert r.c.target_observation is None
            await r.choose(None)
            assert not any(name.startswith('attack_mob_level_') for name in r.sage.calls[-1]['options'])
            assert not r.physical()
        finally: await r.close()
    asyncio.run(run())

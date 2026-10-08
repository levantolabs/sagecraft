"""Requested progression policy and local scouting, with only explicit fakes."""
import asyncio
from copy import deepcopy
import json

import pytest
from PIL import Image, ImageDraw

from sage_wow.agent.grind_only import settings
from sage_wow.agent.grind_search import HuntState
from sage_wow.agent.selected_target_vision import eligibility
from test_grind_only import profile
from test_grind_travel_acceptance import TravelRig


@pytest.mark.parametrize('value', [None, [], {0: [2, 4]}, {True: [2, 4]}, {'three': [2, 4]},
    {3: [4, 2]}, {3: [0, 4]}, {3: [2, 11]}, {3: [2, 4.0]}, {3: [True, 4]}, {3: [2]},
    {3: [2, 4], '3': [2, 4]}])
def test_invalid_override_policy_is_rejected(tmp_path, value):
    p = profile(tmp_path); p.values['grind_only']['target_bands_by_player_level'] = value
    with pytest.raises(ValueError, match='target_bands_by_player_level'): settings(p)


def test_explicit_overrides_share_one_band_and_preserve_other_level_defaults(tmp_path):
    p = profile(tmp_path)
    p.values['grind_only'].update(target_level_delta=1, target_bands_by_player_level={'3': [2, 4], 4: [2, 4]})
    cfg = settings(p); h = HuntState({}, 'offline')
    h.target_level_delta = cfg['target_level_delta']; h.target_bands_by_player_level = cfg['target_bands_by_player_level']
    assert [h.target_band(n) for n in range(1, 6)] == [(1, 2), (1, 3), (2, 4), (2, 4), (3, 6)]
    p.values['grind_only'].pop('target_bands_by_player_level')
    assert settings(p)['target_bands_by_player_level'] == {}


class PolicyRig(TravelRig):
    async def start_policy(self, own=3, *, soft=False):
        prior = self.world.directory / 'prior.json'
        prior.write_text(json.dumps({'character': 'Test Player', 'verified_level': own,
            'level_observation_frame_id': 'offline-prior-level', 'source_session_id': 'offline-prior-session'}))
        self.c.profile.values['grind_only'].update(goal_level=5, committed_combat=True,
            target_bands_by_player_level={3: [2, 4], 4: [2, 4]}, target_names=[],
            campaign_continuation={'character': 'Test Player', 'verified_level': own, 'evidence_path': str(prior)})
        if soft:
            self.c.profile.values['grind_only'].update(target_bands_by_player_level={3:[1,4],4:[1,4]},
                preferred_target_bands_by_player_level={3:[2,4],4:[2,4]})
        self.c.config.update(settings(self.c.profile))
        self.c.hunt.target_bands_by_player_level = deepcopy(self.c.config['target_bands_by_player_level'])
        self.c.hunt.preferred_target_bands_by_player_level = deepcopy(self.c.config['preferred_target_bands_by_player_level'])
        self.world.name = 'Rockjaw Trogg'; self.world.target_level = 1
        self.kind = 'creature'; self.life = 'alive'
        capture = self.world.capture
        def current_frame():
            frame = capture()
            if self.hud:
                with Image.open(frame.image_path) as image:
                    draw = ImageDraw.Draw(image); draw.rectangle((25, 55, 44, 79), fill='#263746')
                    draw.text((28, 60), str(own), fill='white'); image.save(frame.image_path)
            return frame
        self.world.capture = self.c.capture = current_frame
        original = self.c.target_proposal
        async def observed(frame):
            target = await original(frame)
            value = {'selected_hud': 'present' if target['name'] else 'absent', 'name': target['name'],
                'level': target['levels'][0] if len(target['levels']) == 1 else None,
                'target_kind': self.kind if target['name'] else 'unknown',
                'life_state': self.life if target['name'] else 'unknown'}
            low, high = self.c.hunt.target_band(self.c.level.last_confirmed_level or own)
            return {**target, 'visual_observation': value,
                'visual_provenance': {'frame_id': frame.frame_id, 'actual_model': 'explicit-offline-fake'},
                'eligibility': eligibility(value, min_level=low, max_level=high, allowed_names=tuple(self.c.config['target_names']))}
        self.c.target_proposal = observed
        await self.choose('world_normal_confirmed'); await self.choose(f'player_level_{own}')
        assert self.c.baseline and self.c.level.last_confirmed_level == own and not self.c.stopped
        self.sage.calls.clear(); self.backend.events.clear()


@pytest.mark.parametrize('own', [3, 4])
@pytest.mark.parametrize('mob', [1, 2, 3, 4, 5])
def test_actual_attack_menu_and_guard_follow_requested_band_for_generic_creatures(tmp_path, own, mob):
    async def run():
        r = PolicyRig(tmp_path)
        try:
            await r.start_policy(own); r.world.target_level = mob
            await r.choose(f'attack_mob_level_{mob}' if 2 <= mob <= 4 else None)
            options = r.sage.calls[-1]['options']
            attacks = {name for name in options if name.startswith('attack_mob_level_')}
            assert attacks == ({f'attack_mob_level_{mob}'} if 2 <= mob <= 4 else set())
            assert 'Allowed creature levels: 2 to 4' in r.sage.calls[-1]['prompt']
            assert bool(r.casts()) == (2 <= mob <= 4)
            assert not r.c.hunt.credited_kills
        finally: await r.close()
    asyncio.run(run())


def seed_area(r):
    h = r.c.hunt
    area = {'id': 'mixed_patch', 'label': 'Unverified mixed patch hypothesis', 'coordinate': [10., 10.],
        'zone_reference': r.zone, 'arrival_radius': .6, 'expected_level_range': [1, 4],
        'source': {'kind': 'offline_fixture', 'claim': 'Mixed range hypothesis; current population unknown'}}
    h.catalog = {area['id']: area}; h.choose(area, r.world.capture(), 'offline-area', 3)
    h.phase = 'search'; h.planning_requested = False
    h.action_failures['retained-combat-method'] = 2
    h.learned['retained_patch'] = {**area, 'id': 'retained_patch'}
    return area


@pytest.mark.parametrize('clear_result', ['target_cleared', 'clear_failed'])
def test_out_of_band_clear_feedback_precedes_planning_and_observed_scouting_travel(tmp_path, clear_result):
    async def run():
        r = PolicyRig(tmp_path)
        try:
            await r.start_policy(); area = seed_area(r); h = r.c.hunt
            learned = deepcopy(h.learned); before = h.encounter
            rejected = await r.choose('reject_selected_target')
            assert h.pending['family'] == 'clear' and h.strategy_required['reason'] == 'out_of_band_scout'
            assert h.phase == 'choose_area' and h.planning_requested
            assert h.encounter == before and not h.credited_kills and not r.casts()
            fact = h.scouting_observations[-1]
            assert fact['level'] == 1 and fact['band'] == [2, 4] and fact['position'] == [10., 10.]
            assert fact['clear_receipt_id'] == rejected.receipt['receipt_id'] and fact['source_hash']
            assert fact['plan_hypothesis_id'] == area['id']
            assert not h.result()['observed_levels']
            assert area in h.candidates(3)  # A single low-level sighting cannot exclude a mixed patch.
            if clear_result == 'target_cleared': r.world.name = ''; r.world.target_level = None
            await r.choose(clear_result)
            assert 'explore_visible' not in r.sage.calls[-1]['options']
            assert h.phase == 'choose_area' and h.planning_requested and h.strategy_required
            outcomes = deepcopy(h.outcomes)
            await r.choose('explore_visible')
            assert 'search_here' not in r.sage.calls[-1]['options']
            assert h.plan['area']['coordinate'] is None and h.plan['area_id'] != area['id']
            assert h.learned == learned and h.action_failures['retained-combat-method'] == 2
            assert h.outcomes == outcomes and h.scouting_observations == [fact]
            await r.choose(None)
            assert 'begin_hunt' not in r.sage.calls[-1]['options']
            assert 'eligible levels: 2–4' in r.sage.calls[-1]['prompt']
            assert 'destination: unavailable' in r.sage.calls[-1]['prompt']
            assert 'same-zone geometry unavailable; no arrival claim' in r.sage.calls[-1]['prompt']
            await r.choose('detour_backward'); r.position = [10.5, 10.]
            await r.choose('begin_hunt')
            assert h.phase == 'search' and h.search_revision == 1 and h.strategy_required is None
            assert h.action_failures['retained-combat-method'] == 2 and h.scouting_observations == [fact]
        finally: await r.close()
    asyncio.run(run())


def test_fresh_eligible_creature_in_mixed_patch_remains_available_after_scouting(tmp_path):
    async def run():
        r = PolicyRig(tmp_path)
        try:
            await r.start_policy(); seed_area(r); h = r.c.hunt
            await r.choose('reject_selected_target'); await r.choose('clear_failed')
            r.world.name = 'Burly Rockjaw Trogg'; r.world.target_level = 3
            await r.choose('attack_mob_level_3')
            assert r.casts() and h.strategy_required is None and not h.planning_requested
            assert h.scouting_observations[-1]['level'] == 1
            assert h.action_failures['retained-combat-method'] == 2 and not h.credited_kills
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind,life,level', [('player', 'alive', 1), ('friendly_or_self', 'alive', 1),
    ('creature', 'dead', 1), ('creature', 'unknown', 1), ('creature', 'alive', None)])
def test_non_living_or_uncertain_target_reject_does_not_invent_scouting(tmp_path, kind, life, level):
    async def run():
        r = PolicyRig(tmp_path)
        try:
            await r.start_policy(); r.kind = kind; r.life = life; r.world.target_level = level
            await r.choose('reject_selected_target')
            assert not r.c.hunt.scouting_observations and r.c.hunt.strategy_required is None
            assert not r.casts()
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['different_target', 'policy_changed', 'partial_input'])
def test_failed_or_stale_rejection_cannot_create_scouting_intent(tmp_path, failure):
    async def run():
        r = PolicyRig(tmp_path)
        try:
            await r.start_policy()
            if failure == 'partial_input':
                key = r.backend.key
                def partial(code, down):
                    key(code, down)
                    if code == r.controls['escape']['keycode'] and down: raise RuntimeError('offline uncertain key')
                r.backend.key = partial
            else:
                async def change():
                    if failure == 'different_target': r.world.name = 'Other Creature'
                    else: r.c.hunt.target_bands_by_player_level[3] = [1, 4]
                r.sage.hook = change
            r.sage.answers.append('reject_selected_target')
            result = await r.c.process(r.world.capture())
            assert not r.c.hunt.scouting_observations and r.c.hunt.strategy_required is None
            if failure == 'different_target': assert result.status == 'dispatch_guard_rejected'
            if failure == 'partial_input': assert r.c.stopped and r.c.reason == 'partial_or_unknown_grind_input'
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('own',[3,4])
@pytest.mark.parametrize('mob',[1,2,3,4,5])
def test_soft_preference_preserves_permitted_attacks_and_does_not_force_scouting(tmp_path,own,mob):
    async def run():
        r=PolicyRig(tmp_path)
        try:
            await r.start_policy(own,soft=True);seed_area(r)
            r.world.name='Ragged Young Wolf' if mob==1 else 'Rockjaw Trogg'
            r.world.target_level=mob
            await r.choose(f'attack_mob_level_{mob}' if mob<=4 else None)
            attacks={key for key in r.sage.calls[-1]['options'] if key.startswith('attack_mob_level_')}
            assert attacks==({f'attack_mob_level_{mob}'} if mob<=4 else set())
            assert bool(r.casts())==(mob<=4)
            assert not r.c.hunt.strategy_required and not r.c.hunt.scouting_observations
            assert not r.c.hunt.planning_requested
            prompt=r.sage.calls[-1]['prompt']
            assert 'Allowed creature levels: 1 to 4' in prompt
            assert 'Preferred creature levels: 2 to 4' in prompt and 'preference only' in prompt
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('preferred',[None,[],{3:[0,4]},{3:[1,5]},{3:[2,3.0]},{3:[4,2]}])
def test_invalid_or_outside_preferred_bands_are_rejected(tmp_path,preferred):
    p=profile(tmp_path)
    p.values['grind_only'].update(target_bands_by_player_level={3:[1,4]},
        preferred_target_bands_by_player_level=preferred)
    with pytest.raises(ValueError,match='preferred_target_bands_by_player_level'):settings(p)


def test_profile_template_keeps_level1_permitted():
    from pathlib import Path
    import yaml
    cfg=yaml.safe_load(Path('profiles/template.yaml').read_text())['grind_only']
    assert cfg['target_bands_by_player_level']=={3:[1,4],4:[1,4]}
    assert cfg['preferred_target_bands_by_player_level']=={3:[2,4],4:[2,4]}

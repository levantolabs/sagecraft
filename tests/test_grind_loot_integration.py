"""Offline loot transitions with real Sage decision cycle and guarded executor."""
import asyncio
from datetime import datetime
import time
from PIL import Image, ImageDraw
from sage_wow.agent.looting import corpse_proposals
from sage_wow.agent.grind_loot import loot_route_pending, process_loot, _combat_cues
from sage_wow.agent.looting import LootSweep
from sage_wow.agent.grind_search import measure
from test_grind_product_spec import Rig


class LootRig(Rig):
    def __init__(self, path):
        super().__init__(path)
        self.controls['interact_target'] = {'keycode': 3, 'verified_from': 'offline F binding'}
        # The fake portrait/badge ends at y=200. Do not miscalibrate blank
        # ground at y=219 as HUD when testing a legitimate corpse click.
        regions = self.c.profile.values['calibration']['ui_layout']['regions']
        regions['target_frame'][3] = regions['target_overlay'][3] = 205

    def death(self, identity='death-1'):
        self.world.health = 'black'
        self.c.hunt.target_dead_observed = True
        self.c.hunt.encounter_ended = True
        self.c.hunt.loot_request = {'frame_id': identity, 'receipt_id': 'cast-' + identity,
                                    'target': {'name': 'Young Wolf', 'levels': [1]}}

    async def loot(self, answer):
        self.c.wait_until = 0
        self.sage.answers.append(answer)
        frame = self.world.capture()
        rows = await self.c.rows(frame)
        target = await self.c.target_proposal(frame)
        measurement = measure(self.c.profile, frame, rows, self.c.ocr)
        result = await process_loot(self.c, frame, target, measurement, rows)
        if answer is not None and not self.sage.hook:
            assert result.status == 'dispatched', (answer, result.status, result.detail)
        return result


def combat_portrait(r):
    original = r.world.capture
    def capture():
        frame = original()
        with Image.open(frame.image_path) as source:
            image = source.convert('RGB')
        ImageDraw.Draw(image).ellipse((3, 3, 87, 87), outline=(230, 20, 20), width=5)
        image.save(frame.image_path)
        return frame
    r.world.capture = capture
    r.c.capture = capture


def tiny_selected_health(r):
    original = r.world.capture
    def capture():
        frame = original()
        with Image.open(frame.image_path) as source:
            image = source.convert('RGB')
        ImageDraw.Draw(image).rectangle((240, 125, 241, 134), fill=(0, 200, 0))
        image.save(frame.image_path)
        return frame
    r.world.capture = capture
    r.c.capture = capture


def scenario(fn):
    def run(tmp_path):
        async def exercise():
            rig = LootRig(tmp_path)
            try:
                await rig.start()
                rig.death()
                await fn(rig)
            finally:
                await rig.close()
        asyncio.run(exercise())
    run.__name__ = fn.__name__
    return run


@scenario
async def test_loot_requires_selected_dead_confirmation_then_two_fresh_positive_views(r):
    assert loot_route_pending(r.c)
    await r.loot('loot_remaining')
    assert 'loot_verified' not in r.sage.calls[-1]['options']
    await r.loot('loot_selected_corpse')
    assert 'interact_target' not in r.sage.calls[-1]['options']
    assert 'CURRENT selected target' in r.sage.calls[-1]['instructions']
    await r.loot('interact_target')
    assert 'press F now' in r.sage.calls[-1]['instructions']
    assert r.c.loot.pending and r.c.loot.data['attempts'] == 1
    assert r.c.loot.data['death_sources'][0]['receipt_id'] == 'cast-death-1'
    await r.loot('loot_verified')
    assert 'Inspect the result after F' in r.sage.calls[-1]['instructions']
    assert r.c.loot.pending
    await r.loot('loot_verified')
    assert not r.c.loot.pending
    assert r.c.loot.data['outcome'] == 'verified_looted'
    assert not r.c.hunt.credited_kills
    assert r.store.load_checkpoint('grind_loot')['loot_sweep']['outcome'] == 'verified_looted'


@scenario
async def test_live_selection_after_corpse_confirmation_never_offers_interact(r):
    await r.loot('loot_remaining')
    await r.loot('loot_selected_corpse')
    r.world.health = 'green'
    await r.loot('loot_inspect')
    assert 'interact_target' not in r.sage.calls[-1]['options']
    assert 3 not in r.physical_keys()
    assert r.c.loot.pending


@scenario
async def test_attributed_absent_hud_vetoes_stale_name_corpse_confirmation(r):
    await r.loot('loot_remaining')
    await r.loot('loot_selected_corpse')
    original = r.c.target_proposal
    async def absent(frame):
        return {**await original(frame), 'visual_observation': {'selected_hud': 'absent'}}
    r.c.target_proposal = absent
    await r.loot('loot_inspect')
    assert not {'interact_target', 'loot_selected_corpse'} & r.sage.calls[-1]['options'].keys()


@scenario
async def test_combat_interrupt_preserves_loot_sources_and_routes_attacker(r):
    combat_portrait(r)
    await r.loot('under_attack')
    assert r.c.loot.pending and not loot_route_pending(r.c)
    assert r.c.hunt.compact_stage == 'inspect'
    assert not r.c.hunt.target_dead_observed
    r.c.hunt.encounter_ended = True
    assert loot_route_pending(r.c)
    await r.loot('loot_remaining')
    assert not r.c.loot.data['interrupted']
    r.death('death-2')
    await r.loot('loot_remaining')
    assert len(r.c.loot.data['death_sources']) == 2
    assert len(r.c.loot.data['suspected_kills']) == 2


@scenario
async def test_unavailable_needs_full_guarded_search_and_never_counts_as_looted(r):
    await r.loot(None)
    await r.loot(None)
    assert 'loot_unavailable' not in r.sage.calls[-1]['options']
    for _ in range(4):
        await r.loot('loot_search')
    assert r.c.loot.pending and r.c.loot.data['search_steps'] == 4
    await r.loot('loot_unavailable')
    assert not r.c.loot.pending
    assert r.c.loot.data['outcome'] == 'unavailable_after_search_not_verified_looted'
    assert not r.c.hunt.credited_kills


@scenario
async def test_eight_interactions_exhaust_actions_then_require_bounded_sweep(r):
    await r.loot('loot_remaining')
    r.c.loot.data['attempts'] = 8
    for _ in range(4):
        await r.loot('loot_search')
        options = r.sage.calls[-1]['options']
        assert not {'loot_remaining', 'interact_target', 'loot_selected_corpse', 'forward'} & options.keys()
        assert not any(name.startswith('corpse_select_') for name in options)
        assert 'loot_unavailable' not in options
    assert r.c.loot.pending
    await r.loot('loot_unavailable')
    assert not r.c.loot.pending
    assert r.c.loot.data['outcome'] == 'unavailable_after_search_not_verified_looted'


@scenario
async def test_dispatch_guard_rejects_corpse_selection_changed_during_sage_call(r):
    await r.loot('loot_remaining')
    await r.loot('loot_selected_corpse')
    async def change():
        r.world.health = 'green'
    r.sage.hook = change
    result = await r.loot('interact_target')
    assert result.status == 'dispatch_guard_rejected'
    assert r.c.loot.data['attempts'] == 0
    assert not r.physical_keys()
    assert r.c.loot.pending


@scenario
async def test_pending_corpse_and_positive_attempt_survive_checkpoint_reload(r):
    await r.loot('loot_remaining')
    await r.loot('loot_selected_corpse')
    await r.loot('interact_target')
    await r.loot('loot_verified')
    del r.c.loot
    del r.c.loot_state
    await r.loot('loot_verified')
    assert not r.c.loot.pending
    assert r.c.loot.data['outcome'] == 'verified_looted'


@scenario
async def test_null_answer_retains_bound_corpse_without_input(r):
    result = await r.loot(None)
    assert result.status == 'needs_more_evidence'
    assert r.c.loot.pending and not r.physical_keys()
    assert r.c.loot.data['death_sources'][0]['frame_id'] == 'death-1'


def test_suspected_unselected_corpse_can_select_and_loot_without_kill_claim(tmp_path):
    async def run():
        r = LootRig(tmp_path)
        try:
            await r.start()
            r.world.name = ''
            r.c.hunt.loot_request = {'frame_id': 'postcast-absent', 'receipt_id': 'completed-cast',
                'target': {'name': 'Young Wolf', 'levels': [1]}, 'suspected': True,
                'death_observed': False, 'credit_known': False}
            r.backend.mouse_button = lambda *args: r.backend.events.append(('mouse', *args))
            await r.loot('corpse_ground_1')
            options = r.sage.calls[-1]['options']
            assert 'interact_target' not in options and 'loot_verified' not in options
            source = r.c.loot.data['death_sources'][0]
            assert source['suspected'] and not source['death_observed'] and not source['credit_known']
            assert 'unconfirmed postcast disappearance' in r.sage.calls[-1]['prompt']
            assert not r.c.hunt.target_dead_observed and not r.c.hunt.credited_kills
            assert any(event[0] == 'mouse' for event in r.backend.events)
            r.world.name = 'Young Wolf'
            r.world.health = 'black'
            await r.loot('loot_selected_corpse')
            await r.loot('interact_target')
            await r.loot('loot_verified')
            await r.loot('loot_verified')
            assert r.c.loot.data['outcome'] == 'verified_looted'
            assert not r.c.hunt.target_dead_observed and not r.c.hunt.credited_kills
            assert not any(fact['kind'] == 'selected_encounter_death' for fact in r.c.hunt.progress_facts)
            persisted = r.store.load_checkpoint('grind_loot')['loot_sweep']['death_sources'][0]
            assert persisted['suspected'] and not persisted['death_observed']
        finally:
            await r.close()
    asyncio.run(run())


@scenario
async def test_repeated_abstention_leaves_actual_search_without_passive_uncertainty(r):
    await r.loot(None)
    await r.loot(None)
    await r.loot('loot_search')
    assert 'loot_uncertain' not in r.sage.calls[-1]['options']
    assert r.c.loot.pending and r.c.loot.data['search_steps'] == 1


def test_dark_corpse_proposals_include_adjacent_and_lower_ground_on_blue_snow(tmp_path):
    path = tmp_path / 'dark-snow.png'
    image = Image.new('RGB', (1496, 967), (60, 90, 110))
    draw = ImageDraw.Draw(image)
    draw.rectangle((710, 480, 780, 580), fill=(20, 30, 35))  # Own player, excluded.
    draw.rectangle((798, 480, 860, 510), fill=(20, 30, 35))  # Adjacent corpse.
    draw.rectangle((810, 670, 875, 790), fill=(20, 30, 35))  # Corpse below player.
    image.save(path)
    points = [(choice.binding['image_x'], choice.binding['image_y']) for choice in corpse_proposals(path, 1496, 967, nearby_ground=True)]
    assert any(798 <= x <= 860 and 480 <= y <= 510 for x, y in points)
    assert any(810 <= x <= 875 and 670 <= y <= 790 for x, y in points)
    assert not any(710 <= x <= 780 and 480 <= y <= 580 for x, y in points)


@scenario
async def test_confirmed_death_inspect_offers_direct_corpse_selection(r):
    r.backend.mouse_button = lambda *args: r.backend.events.append(('mouse', *args))
    await r.loot('corpse_ground_1')
    assert not r.c.loot.data['death_sources'][0]['suspected']
    assert r.c.loot.data['stage'] == 'actions'
    assert any(event[0] == 'mouse' for event in r.backend.events)
    assert 'Young Wolf' in r.sage.calls[-1]['options']['corpse_ground_1'].description


def test_root_route_kill_loot_then_hunt_again(tmp_path):
    async def run():
        r = LootRig(tmp_path)
        r.c.config['loot_enabled'] = True
        try:
            await r.start()
            await r.choose('attack_mob_level_1')
            r.world.health = 'black'
            await r.choose('dead_credited')
            death = dict(r.c.hunt.loot_request)
            assert loot_route_pending(r.c)
            for answer in ('loot_remaining', 'loot_selected_corpse', 'interact_target',
                           'loot_verified', 'loot_verified'):
                await r.choose(answer)
            assert not loot_route_pending(r.c)
            assert r.c.loot.data['death_sources'][0]['frame_id'] == death['frame_id']
            assert r.c.loot.data['outcome'] == 'verified_looted'
            assert len(r.c.hunt.credited_kills) == 1
            await r.choose('target_enemy')
            r.world.health = 'green'
            await r.choose('attack_mob_level_1')
            assert len(r.casts()) == 2
        finally:
            await r.close()
    asyncio.run(run())


def test_root_route_attacker_interrupt_adds_death_to_retained_loot(tmp_path):
    async def run():
        r = LootRig(tmp_path)
        r.c.config['loot_enabled'] = True
        try:
            await r.start()
            await r.choose('attack_mob_level_1')
            r.world.health = 'black'
            await r.choose('dead_credited')
            combat_portrait(r)
            await r.choose('under_attack')
            first = r.c.loot.data['death_sources'][0]['frame_id']
            assert r.c.loot.pending and not loot_route_pending(r.c)
            r.world.health = 'green'
            await r.choose('attack_mob_level_1')
            r.world.health = 'black'
            await r.choose('dead_credited')
            await r.choose('loot_remaining')
            assert r.c.loot.pending and not r.c.loot.data['interrupted']
            assert len(r.c.loot.data['death_sources']) == 2
            assert r.c.loot.data['death_sources'][0]['frame_id'] == first
            assert len(r.c.hunt.credited_kills) == 2
        finally:
            await r.close()
    asyncio.run(run())


@scenario
async def test_idle_full_health_loot_never_offers_combat_interruption(r):
    await r.loot('loot_remaining')
    assert 'under_attack' not in r.sage.calls[-1]['options']
    r.world.health = 'green'  # Nearby/selected living unit is not an incoming attack cue.
    await r.loot('loot_inspect')
    assert 'under_attack' not in r.sage.calls[-1]['options']
    assert not r.c.loot.data.get('interrupted')


@scenario
async def test_real_tiny_selected_health_withholds_dead_confirmation_and_f(r):
    await r.loot('loot_remaining')
    tiny_selected_health(r)
    await r.loot('loot_inspect')
    assert not {'loot_selected_corpse', 'interact_target'} & r.sage.calls[-1]['options'].keys()
    assert r.c.loot.data.get('selected_corpse') is None


@scenario
async def test_fresh_tiny_selected_health_rejects_f_after_dead_confirmation(r):
    await r.loot('loot_remaining')
    await r.loot('loot_selected_corpse')
    async def changed():
        tiny_selected_health(r)
    r.sage.hook = changed
    result = await r.loot('interact_target')
    assert result.status == 'dispatch_guard_rejected'
    assert r.c.loot.data['attempts'] == 0
    assert r.controls['interact_target']['keycode'] not in r.physical_keys()


@scenario
async def test_actual_health_drop_reopens_combat_interruption(r):
    await r.loot('loot_remaining')
    original = r.world.capture
    def capture():
        frame = original()
        with Image.open(frame.image_path) as source:
            image = source.convert('RGB')
        ImageDraw.Draw(image).rectangle((91, 30, 140, 40), fill='black')
        image.save(frame.image_path)
        return frame
    r.world.capture = capture
    r.c.capture = capture
    await r.loot('under_attack')
    assert r.c.loot.data['interrupted'] and not loot_route_pending(r.c)


@scenario
async def test_only_fresh_incoming_log_damage_is_a_loot_combat_cue(r):
    frame = r.world.capture()
    def fact(age, dest='Test Player', event='SWING_DAMAGE'):
        return {'timestamp': datetime.fromtimestamp(time.time()-age).strftime('%m/%d %H:%M:%S.%f'),
                'event': event, 'source_name': 'Young Wolf', 'dest_name': dest}
    r.c.combat_log_facts = [fact(45)]
    r.c.combat_log_loot_context = 'Historical UNIT_DIED on own recent target; not current combat.'
    assert not _combat_cues(r.c, frame, LootSweep({}))['supported']
    r.c.combat_log_facts = [fact(1, event='UNIT_DIED'), fact(1, dest='Other Player')]
    assert not _combat_cues(r.c, frame, LootSweep({}))['supported']
    r.c.combat_log_facts = [fact(1)]
    assert _combat_cues(r.c, frame, LootSweep({}))['supported']


def test_root_postcast_disappearance_hands_off_only_suspected_loot(tmp_path):
    async def run():
        r = LootRig(tmp_path)
        r.c.config['loot_enabled'] = True
        r.backend.mouse_button = lambda *args: r.backend.events.append(('mouse', *args))
        try:
            await r.start()
            await r.choose('attack_mob_level_1')
            receipt_id = r.c.hunt.pending['receipt']['receipt_id']
            r.world.name = ''
            await r.choose('inspect_recent_corpse')
            request = r.c.hunt.loot_request
            assert request['suspected'] and not request['death_observed'] and not request['credit_known']
            assert request['receipt_id'] == receipt_id
            await r.choose('corpse_ground_1')
            source = r.c.loot.data['death_sources'][0]
            assert source['suspected'] and not source['death_observed']
            assert source['source_frame_id'] and source['source_image']
            r.world.name = 'Young Wolf'
            r.world.health = 'black'
            for answer in ('loot_selected_corpse', 'interact_target', 'loot_verified', 'loot_verified'):
                await r.choose(answer)
            assert r.c.loot.data['outcome'] == 'verified_looted'
            assert not r.c.hunt.credited_kills
            assert not any(fact['kind'] == 'selected_encounter_death' for fact in r.c.hunt.progress_facts)
        finally:
            await r.close()
    asyncio.run(run())


def test_root_resources_keep_fighting_above_threshold_and_heal_same_enemy(tmp_path, monkeypatch):
    from sage_wow.agent import grind_resources
    health = {'value': .65}
    def bars(controller, frame):
        return {'frame_id': frame.frame_id, 'player_health': health['value'],
                'health_confidence': 1., 'target_health': .5,
                'target_health_confidence': 1., 'player_mana': .9}
    monkeypatch.setattr(grind_resources, 'hud_resources', bars)
    async def run():
        r = LootRig(tmp_path)
        r.c.config.update(encounter_resources=True, preserve_target_heal=True)
        try:
            await r.start()
            await r.choose('attack_mob_level_1')
            encounter = r.c.hunt.encounter
            health['value'] = .40
            r.world.health = 'orange'
            await r.choose('own_damaged_alive')
            assert 'heal_self' not in r.sage.calls[-1]['options']
            health['value'] = .25
            await r.choose('heal_self')
            assert r.c.hunt.last_target == 'young wolf'
            assert r.c.hunt.encounter == encounter and r.c.hunt.phase == 'fight'
            assert r.c.heal_pending and '/cast [@player] Lesser Heal' in r.casts()[-1][1]
            health['value'] = .7
            r.c.wait_until = 0
            result = await r.c.process(r.world.capture())
            assert result.status == 'grind_reobserve'
            assert r.c.heal_pending is None and r.c.hunt.compact_stage == 'inspect'
            await r.choose('attack_mob_level_1')
            assert r.c.hunt.encounter == encounter
            assert r.c.hunt.last_target == 'young wolf'
            assert not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_root_low_health_finisher_casts_once_without_burst_or_heal(tmp_path, monkeypatch):
    from sage_wow.agent import grind_resources
    health = {'value': .7}
    def bars(controller, frame):
        return {'frame_id': frame.frame_id, 'player_health': health['value'],
                'health_confidence': 1., 'target_health': .20,
                'target_health_confidence': 1., 'player_mana': .9}
    monkeypatch.setattr(grind_resources, 'hud_resources', bars)
    async def run():
        r = LootRig(tmp_path)
        r.c.config.update(encounter_resources=True, preserve_target_heal=True)
        try:
            await r.start()
            await r.choose('attack_mob_level_1')
            encounter = r.c.hunt.encounter
            original = r.c.target_proposal
            async def alive(frame):
                target = await original(frame)
                return {**target, 'eligibility': 'eligible',
                        'visual_observation': {'selected_hud': 'present', 'life_state': 'alive',
                                               'level': 1}}
            monkeypatch.setattr(r.c, 'target_proposal', alive)
            burst_calls = []
            async def deny_burst(binding, index):
                burst_calls.append((binding, index))
                raise AssertionError('Finisher must not use the burst gate at low own health')
            r.executor.batch_guard = deny_burst
            r.c.config.update(committed_combat=True, smite_burst_count=3)
            health['value'] = .25
            before = len(r.casts())
            before_keys = len(r.physical_keys())
            await r.choose('finish_fight')
            assert r.sage.calls[-1]['options']['finish_fight'].binding['type'] == 'cast_guarded'
            assert len(r.casts()) == before + 1
            assert 'Smite' in r.casts()[-1][1] and 'Lesser Heal' not in r.casts()[-1][1]
            assert not burst_calls
            assert r.physical_keys()[before_keys:] == [36, 36]  # Chat submit only; no targeting key.
            assert r.c.heal_pending is None
            assert r.c.hunt.encounter == encounter and r.c.hunt.last_target == 'young wolf'
            assert r.c.hunt.pending['family'] == 'combat'
        finally:
            await r.close()
    asyncio.run(run())


async def previous_target_setup(r):
    await r.choose('attack_mob_level_1')
    r.world.name = ''
    await r.choose('inspect_recent_corpse')
    original = r.c.target_proposal
    async def observed(frame):
        proposal = await original(frame)
        return {**proposal, 'visual_observation': {'selected_hud': 'present' if proposal.get('name') else 'absent'}}
    r.c.target_proposal = observed


def test_previous_target_once_then_matching_dead_confirmation_before_f(tmp_path):
    async def run():
        r = LootRig(tmp_path)
        r.c.config['loot_enabled'] = True
        try:
            await r.start()
            await previous_target_setup(r)
            await r.loot(None)  # An offered but unchosen option consumes no attempt.
            source = r.c.loot.data['death_sources'][0]
            assert 'target_recent_corpse' in r.sage.calls[-1]['options']
            assert not source.get('previous_target_attempted')
            generation = r.c.cycle.input_generation
            await r.loot('target_recent_corpse')
            assert r.c.cycle.input_generation == generation + 5
            assert source['previous_target_attempted']
            assert r.casts()[-1] == ('text', '/targetlasttarget [noexists]')
            assert not r.c.hunt.target_dead_observed and not r.c.hunt.credited_kills
            await r.loot('loot_inspect')  # Completed command changed nothing in fake world.
            assert not {'target_recent_corpse', 'loot_selected_corpse', 'interact_target'} & r.sage.calls[-1]['options'].keys()
            r.world.name = 'Other Wolf'
            r.world.health = 'black'
            await r.loot('loot_remaining')
            await r.loot('loot_inspect')
            assert not {'loot_selected_corpse', 'interact_target'} & r.sage.calls[-1]['options'].keys()
            r.world.name = 'Young Wolf'
            r.world.health = 'green'
            await r.loot('loot_remaining')
            await r.loot('loot_inspect')
            assert not {'loot_selected_corpse', 'interact_target'} & r.sage.calls[-1]['options'].keys()
            r.world.health = 'black'
            await r.loot('loot_remaining')
            await r.loot('loot_selected_corpse')
            assert 'interact_target' not in r.sage.calls[-1]['options']
            await r.loot('interact_target')
            assert r.c.loot.data['attempts'] == 1
        finally:
            await r.close()
    asyncio.run(run())


def test_previous_target_requires_current_generation_epoch_and_authoritative_absence(tmp_path):
    async def run():
        r = LootRig(tmp_path)
        r.c.config['loot_enabled'] = True
        try:
            await r.start()
            await previous_target_setup(r)
            await r.loot(None)
            authority = r.c.loot.data['death_sources'][0]['previous_target_authority']
            assert 'target_recent_corpse' in r.sage.calls[-1]['options']
            authority['generation_after'] -= 1
            await r.loot(None)
            assert 'target_recent_corpse' not in r.sage.calls[-1]['options']
            authority['generation_after'] += 1
            epoch = authority['session_epoch']
            authority['session_epoch'] = 'retired-epoch'
            await r.loot(None)
            assert 'target_recent_corpse' not in r.sage.calls[-1]['options']
            authority['session_epoch'] = epoch
            original = r.c.target_proposal
            async def unknown(frame):
                return {**await original(frame), 'visual_observation': {}}
            r.c.target_proposal = unknown
            await r.loot(None)
            assert 'target_recent_corpse' not in r.sage.calls[-1]['options']
            assert not r.c.loot.data['death_sources'][0].get('previous_target_attempted')
        finally:
            await r.close()
    asyncio.run(run())


def test_previous_target_guard_rejects_new_selection_without_consuming_attempt(tmp_path):
    async def run():
        r = LootRig(tmp_path)
        r.c.config['loot_enabled'] = True
        try:
            await r.start()
            await previous_target_setup(r)
            async def changed():
                r.world.name = 'Other Wolf'
            r.sage.hook = changed
            result = await r.loot('target_recent_corpse')
            assert result.status == 'dispatch_guard_rejected'
            assert not r.c.loot.data['death_sources'][0].get('previous_target_attempted')
            assert ('text', '/targetlasttarget [noexists]') not in r.casts()
            assert r.c.loot.data['attempts'] == 0
        finally:
            await r.close()
    asyncio.run(run())

"""Independent offline behavioral traces; actual leveling requires live evidence.

The repository's autouse network/native-input guard remains active. Every frame,
provider answer and completed key receipt here comes from an explicit local fake.
"""
import asyncio
import json

import pytest
from PIL import Image, ImageDraw
from sage_wow.agent.grind_only import GrindController
from sage_wow.config import Profile
from sage_wow.control.executor import GateSnapshot, SafeExecutor
from sage_wow.models import Frame
from sage_wow.perception.ocr import TextObservation
from sage_wow.platform.macos.geometry import Rect
from sage_wow.sage.client import SageDecision
from sage_wow.storage import EventStore


class FakeBackend:
    def __init__(self): self.events = []
    def key(self, *args): self.events.append(('key', *args))
    def text(self, value): self.events.append(('text', value))
    def release_all(self): self.events.append(('release',))
    def close(self): pass


class ScriptedSage:
    def __init__(self): self.answers = []; self.calls = []; self.hook = None
    async def decide_image_choice(self, path, prompt, instructions, candidates, envelope, reasoning):
        options = {item.option: item for item in candidates.options}
        try: context=json.loads(prompt)
        except json.JSONDecodeError: context={'level_prompt':prompt}
        self.calls.append({'options': options, 'context': context, 'image': path,'prompt':prompt,'instructions':instructions})
        if self.hook: await self.hook()
        assert self.answers, f'Unexpected request: {tuple(options)}'
        answer = self.answers.pop(0)
        if isinstance(answer, Exception): raise answer
        assert answer is None or answer in options, f'{answer!r} absent from {tuple(options)}'
        return SageDecision(envelope, answer, options[answer].binding if answer else None,
                            .99, (), 'independent-offline', 0, {'ran': False}, {}, 0, tuple(options))


class World:
    def __init__(self, directory):
        self.directory = directory; self.count = 0; self.name = 'Young Wolf'
        self.target_level = None; self.error = ''; self.health = 'green'; self.player_health = 'green'
    def capture(self):
        self.count += 1; path = self.directory / f'acceptance-{self.count}.png'
        image = Image.new('RGB', (500, 300), '#263746'); draw = ImageDraw.Draw(image)
        draw.text((40, 5), 'Test Player', fill='white')
        draw.rectangle((40, 30, 140, 40), fill=self.player_health)
        draw.text((28, 60), '1', fill='white')
        if self.name:
            draw.text((210, 100), self.name, fill='white')
            draw.rectangle((240, 125, 380, 135), fill=self.health)
            draw.text((425, 175), str(self.target_level or 1), fill='white')
        if self.error: draw.text((180, 155), self.error, fill='red')
        image.save(path)
        return Frame.create('offline-window-7', 500, 300, image_path=str(path))
    def ocr(self, path):
        # This fake describes the full window. A new calibrated crop must not
        # inherit full-window rows in the crop's unrelated coordinate system.
        # Tests of alternate sensing supply their own explicit crop answers.
        from pathlib import Path
        if Path(path).parent.name.startswith('sage-target-ocr-'):
            return []
        def row(text, x, y, width, height):
            return TextObservation(text, .99, {'x': x, 'y': y, 'width': width, 'height': height})
        rows = [row('Test Player', 40, 5, 90, 15)]
        if self.name: rows.append(row(self.name, 210, 100, 140, 15))
        if self.name and self.target_level: rows.append(row(str(self.target_level), 425, 175, 12, 18))
        if self.error: rows.append(row(self.error, 180, 155, 250, 15))
        return rows


class Rig:
    def __init__(self, directory):
        regions = {'player_frame': [0, 0, 150, 90], 'player_health': [40, 30, 140, 40],
                   'target_frame': [200, 80, 490, 240], 'target_health': [240, 125, 380, 135],
                   'target_overlay': [200, 80, 490, 240]}
        self.controls = {name: {'keycode': index, 'verified_from': 'offline operator'}
                         for index, name in enumerate(('target_enemy', 'forward', 'backward', 'turn_left',
                         'turn_right', 'smite', 'escape', 'target_self', 'lesser_heal'))}
        values = {'character': {'name': 'Test Player'}, 'client': {'window_id': 7},
                  'calibration': {'ui_layout': {'version': 1, 'id': 'fake', 'provenance': 'offline geometry',
                  'image_width': 500, 'image_height': 300, 'regions': regions}},
                  'grind_only': {'enabled': True, 'clean_world_observation': False, 'session_id': 'independent-acceptance',
                  'duration_seconds': 7200, 'cast_wait_seconds': .1,
                  'player_level_box': [25, 55, 45, 80], 'target_level_box': [420, 170, 450, 200],
                  'evidence_max_bytes': 1024 ** 2, 'disk_reserve_bytes': 1024 ** 3},
                  'controls': {'bindings': self.controls}}
        path = directory / 'profile.yaml'; path.write_text('offline fixture')
        self.world = World(directory); self.sage = ScriptedSage(); self.backend = FakeBackend()
        rect = Rect(0, 0, 500, 300)
        self.executor = SafeExecutor(self.backend, lambda: GateSnapshot(7, 7, True, rect, rect, True, True),
                                     heartbeat_timeout=30, watchdog_interval=.01)
        self.store = EventStore(directory / 'acceptance.sqlite3')
        self.c = GrindController(Profile(path, values), self.store, self.sage, self.executor,
                                 capture=self.world.capture, ocr=self.world.ocr)
        self.executor.arm()
    async def choose(self, answer):
        self.c.wait_until = 0  # Advance only the offline controller's bounded observation wait.
        self.sage.answers.append(answer)
        result=await self.c.process(self.world.capture())
        if answer is not None and not isinstance(answer,Exception) and not self.sage.hook:
            assert result.status=='dispatched', (answer,result.status,result.detail)
        return result
    async def start(self):
        for answer in ('world_normal_confirmed', 'player_level_1'):
            assert (await self.choose(answer)).status == 'dispatched'
        assert self.c.baseline and self.c.level.last_confirmed_level == 1
    async def close(self):
        await self.executor.stop(); self.store.close()
    def physical_keys(self): return [event[1] for event in self.backend.events if event[0] == 'key' and event[-1] is True]
    def casts(self): return [event for event in self.backend.events if event[0] == 'text']


async def retain_legacy_no_effect(r):
    """Restore an accepted historical no-effect record after a real fake cast.

    The current Sage menu has no no_effect answer. These accounting scenarios
    explicitly exercise retained older state, not a new model answer.
    """
    h=r.c.hunt;pending=h.pending;frame=r.world.capture()
    assert pending and pending['family']=='combat' and h.linked(frame,r.c.cycle.input_generation)
    target=await r.c.target_proposal(frame)
    if r.c.config.get('committed_combat'):
        h.review_cast_observations(target,pending,frame,linked=True,fresh=True,
            session_epoch=r.c.cycle.session_epoch,input_generation=r.c.cycle.input_generation)
    h.no_effect_retries+=1
    h.resolve('combat_unchanged',frame,h.last_measurement,pending=pending,linked=True)
    if h.no_effect_retries==1:h.cast_obligation=None;h.retry_credit=True
    h.compact_stage='inspect';h.question_answer();h.remember_approach()


def scenario(test):
    def wrapped(tmp_path):
        async def run():
            rig = Rig(tmp_path)
            try: await rig.start(); await test(rig)
            finally: await rig.close()
        asyncio.run(run())
    return wrapped


@scenario
async def test_progressing_damage_continues_without_receipt_credit(r):
    await r.choose('attack_mob_level_1')
    for color in ('yellow', 'orange', 'red', '#301010'):
        r.world.health = color
        r.c.hunt.active_seconds += 25  # Productive combat spans more than 60 active seconds.
        await r.choose('damaged_alive')
        await r.choose('attack_mob_level_1')
    assert len(r.casts()) == 5 and not r.c.stopped
    assert not r.c.success
    assert not any(fact['kind'] in {'selected_encounter_death', 'visible_xp_change'}
                   for fact in r.c.hunt.progress_facts)


@scenario
async def test_pause_retires_pending_receipt_and_preserves_deadline(r):
    await r.choose('attack_mob_level_1')
    deadline = r.c.deadline; epoch = r.c.cycle.session_epoch
    assert r.c.hunt.pending is not None
    r.c.pause_focus()
    assert r.c.hunt.pending is None and r.c.cycle.session_epoch != epoch
    assert r.c.resume_focus() is not False
    assert r.c.deadline == deadline and not r.c.success
    await r.choose('world_normal_confirmed')
    assert 'damage_observed' not in r.sage.calls[-1]['options']
    assert not r.c.hunt.progress_facts


@scenario
async def test_two_fresh_goal_level_observations_are_success_authority(r):
    r.c.level.last_attempt_at=0
    await r.choose('player_level_3')
    assert not r.c.success and not r.c.stopped
    await r.choose('player_level_3')
    assert r.c.success and r.c.stopped and r.c.reason=='goal_level_verified'
    assert not r.casts()


@scenario
async def test_unsuitable_selection_is_physically_cleared_before_changed_search(r):
    r.world.name = 'Sten'; r.world.target_level = 5
    if not r.world.name:
        await r.choose('target_enemy')
    await r.choose('reject_selected_target')
    assert r.controls['escape']['keycode'] in r.physical_keys()
    assert not r.casts()
    r.world.name = ''; r.world.target_level = None
    await r.choose('target_cleared')
    await r.choose('target_enemy')
    # Same unsuitable result must still be clearable, never offensive.
    r.world.name = 'Test Player'
    await r.choose('reject_selected_target')
    r.world.name = ''
    await r.choose('target_cleared')
    await r.choose('turn_left')
    assert r.controls['turn_left']['keycode'] in r.physical_keys()
    assert r.physical_keys().count(r.controls['escape']['keycode']) == 2
    assert not r.casts() and not r.c.stopped


@scenario
async def test_empty_sector_requires_real_motion_before_replenishing_acquisition(r):
    r.world.name = ''
    await r.choose('target_enemy');await r.choose('no_selected_frame')
    await r.choose('target_enemy');await r.choose('no_selected_frame')
    await r.choose('turn_left')
    assert 'target_enemy' not in r.sage.calls[-1]['options']
    await r.choose('motion_useful')
    await r.choose('target_enemy')
    assert r.controls['turn_left']['keycode'] in r.physical_keys()
    assert not r.casts() and not r.c.success


@scenario
async def test_healing_self_does_not_erase_enemy_failure_or_authorize_self_attack(r):
    await r.choose('attack_mob_level_1')
    await retain_legacy_no_effect(r)
    before = dict(r.c.hunt.action_failures)
    if r.c.hunt.phase!='recover':await r.choose('recover_now')
    await r.choose('heal_self')
    r.world.name = 'Test Player'; r.world.player_health = 'yellow'
    await r.choose('resources_improved')
    assert not any(option.startswith('attack_mob_') for option in r.sage.calls[-1]['options'])
    assert all(r.c.hunt.action_failures.get(key, 0) >= count for key, count in before.items())
    await r.choose('recovered_resume')
    await r.choose('target_enemy')
    assert len(r.casts()) == 1 and not r.c.stopped


@scenario
async def test_repeated_null_changes_method_and_can_resume_useful_input(r):
    for _ in range(3): await r.choose(None)
    original_menu=r.sage.calls[-3]['options'];short_menu=r.sage.calls[-2]['options']
    assert short_menu.keys()==original_menu.keys()  # Focused fallback retains necessary methods.
    assert {'ui_blocked','cannot_assess','forward'}<=short_menu.keys()
    assert 'dead_or_unrecoverable' not in short_menu
    assert any(item.binding['type']!='observe_only' for item in short_menu.values())
    assert not r.c.stopped and not r.c.success
    assert r.c.hunt.recovery_requested and not r.c.hunt.blocked
    assert 'change_search_strategy' in r.sage.calls[-1]['options']
    await r.choose('turn_left')
    await r.choose('motion_useful')
    assert r.controls['turn_left']['keycode'] in r.physical_keys()
    assert not r.casts()
    assert all(len(call['options']) <= 20 for call in r.sage.calls)


@pytest.mark.parametrize('keep_error', [True,False])
def test_los_correction_is_atomic_and_stale_text_cannot_renew_error(tmp_path, keep_error):
    async def run():
        r = Rig(tmp_path)
        try:
            await r.start()
            await r.choose('attack_mob_level_1')
            r.world.error = 'Target not in line of sight'
            await r.choose('position_error')
            await r.choose('turn_left')
            assert r.c.hunt.pending['purpose'] == 'cast_correction'
            movement_receipt = r.c.hunt.pending['receipt']['receipt_id']
            if not keep_error:r.world.error=''
            assert (await r.choose('motion_useful')).status=='dispatched'
            outcomes = [item for item in r.c.hunt.outcomes
                        if item.get('receipt_id') == movement_receipt]
            assert len(outcomes) == 1
            await r.choose('attack_mob_level_1')
            assert len(r.casts()) == 2 and not r.c.stopped
            assert r.c.hunt.pending['family'] == 'combat'
            assert r.c.hunt.pending['receipt']['receipt_id'] != movement_receipt
        finally: await r.close()
    asyncio.run(run())


@scenario
async def test_transport_failure_is_not_a_null_or_gameplay_failure(r):
    await r.choose(TimeoutError('offline simulated transport timeout'))
    assert r.c.provider_failures == 1 and r.c.null_answers == 0
    assert not r.c.stopped and not r.c.hunt.action_failures
    await r.choose(None)
    assert r.c.null_answers == 1
    assert not r.casts()


@scenario
async def test_due_level_check_runs_at_safe_recovery_boundary(r):
    await r.choose('recover_now');await r.choose('rest')
    r.c.level.last_attempt_at = 0
    await r.choose('recovered_resume')
    await r.choose('player_level_2')
    assert not r.c.success
    assert r.c.level.last_confirmed_level == 2 and not r.c.stopped
    assert not r.casts()


@scenario
async def test_observed_resource_improvement_can_outlast_ninety_seconds(r):
    await r.choose('recover_now');await r.choose('rest')
    for color in ('#006000', '#008000', '#00a000', '#00c000'):
        await r.choose('rest')
        r.world.player_health = color
        r.c.hunt.active_seconds += 30
        await r.choose('resources_improved')
    assert r.c.hunt.active_seconds >= 120
    assert not r.c.stopped and not r.c.hunt.blocked
    await r.choose('recovered_resume')
    assert not r.c.success and not r.casts()


@scenario
async def test_focus_epoch_change_during_answer_cannot_dispatch_old_choice(r):
    deadline = r.c.deadline
    async def lose_focus(): r.c.pause_focus()
    r.sage.hook = lose_focus
    result = await r.choose('attack_mob_level_1')
    assert result.status != 'dispatched'
    assert r.c.paused and r.c.deadline == deadline
    assert not r.casts() and r.c.hunt.pending is None
    assert not r.c.success

@scenario
async def test_pending_motion_interrupted_by_recovery_is_unknown_and_debt_survives(r):
    r.world.name = '';r.c.hunt.planning_requested=True
    await r.choose('explore_visible')
    r.c.hunt.action_failures['forward'] = 1
    await r.choose('detour_backward')
    receipt_id = r.c.hunt.pending['receipt']['receipt_id']
    # This fixture has no readable coordinates: travel reconciliation retains
    # the interrupted receipt as unknown before its new urgent exit.
    await r.choose('urgent_state')
    outcomes = [outcome for outcome in r.c.hunt.outcomes if outcome['receipt_id'] == receipt_id]
    assert len(outcomes) == 1 and outcomes[0]['outcome'] == 'unknown_visual'
    debt = dict(r.c.hunt.action_failures)
    await r.choose('rest')
    assert 'forward' not in r.sage.calls[-1]['options']
    await r.choose('recovered_resume')
    assert all(r.c.hunt.action_failures.get(action, 0) >= count for action, count in debt.items())
    assert r.c.hunt.phase == 'travel' and not r.c.stopped

@scenario
async def test_transient_provider_backoff_is_one_two_four_before_low_rate_block(r):
    import time
    for delay in (1, 2, 4):
        await r.choose(RuntimeError('offline transport service unavailable'))
        assert delay - .2 <= r.c.wait_until - time.time() <= delay
        assert r.c.hunt.blocked is None and not r.c.stopped
    await r.choose(RuntimeError('offline transport service unavailable'))
    assert r.c.hunt.blocked['reason'] == 'blocked_provider'
    calls = len(r.sage.calls)
    await r.c.process(r.world.capture())
    assert len(r.sage.calls) == calls and not r.casts()

@scenario
async def test_distinct_enemy_after_self_heal_starts_fresh_encounter_but_keeps_old_outcomes(r):
    await r.choose('attack_mob_level_1')
    await retain_legacy_no_effect(r)
    old_encounter = r.c.hunt.encounter
    r.c.hunt.correction_rounds = 3
    if r.c.hunt.phase!='recover':await r.choose('recover_now')
    await r.choose('heal_self')
    r.world.name = 'Test Player'
    await r.choose('resources_improved')
    await r.choose('recovered_resume')
    await r.choose('target_enemy')
    r.world.name = 'Young Boar'
    await r.choose('attack_mob_level_1')
    assert r.c.hunt.encounter == old_encounter + 1 and r.c.hunt.correction_rounds == 0
    assert any(outcome['encounter_id'] == old_encounter and outcome['outcome'] == 'combat_unchanged'
               for outcome in r.c.hunt.outcomes)


@scenario
async def test_visibly_selected_unreadable_name_and_badge_can_be_guardedly_cleared(r):
    source_ocr = r.world.ocr
    def unreadable(path):
        return [row for row in source_ocr(path) if row.bounds['x'] < 150]
    r.c.ocr = unreadable
    r.c.hunt.compact_stage='inspect'  # Prior target selection is unconfirmed; this call must judge frame presence.
    await r.choose('reject_selected_target')
    assert r.physical_keys().count(r.controls['escape']['keycode']) == 1
    assert not any(option.startswith('attack_mob_') for option in r.sage.calls[-1]['options'])
    assert not r.casts() and not r.c.stopped

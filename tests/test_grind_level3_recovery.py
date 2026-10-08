"""Simulated level/observer composition. Passing here is not gameplay evidence."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from sage_wow.agent.grind_only import GrindController
from sage_wow.control.executor import GateSnapshot, SafeExecutor
from sage_wow.platform.macos.geometry import Rect
from sage_wow.platform.macos.capture import CaptureError
from sage_wow.storage import EventStore
from test_grind_only import profile
from test_grind_product_spec import FakeBackend, ScriptedSage, World


class SimulatedWorld(World):
    def __init__(self, directory):
        super().__init__(directory)
        self.player_level = 1
        self.player_name = 'Test Player'
        self.omit_target_ocr = False
        self.scenery_patches = []

    def capture(self):
        frame = super().capture()
        with Image.open(frame.image_path) as raw:
            image = raw.copy()
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 149, 89), fill='#263746')
        draw.text((40, 5), self.player_name, fill='white')
        draw.rectangle((40, 30, 140, 40), fill=self.player_health)
        draw.text((28, 60), str(self.player_level), fill='white')
        for box, color in self.scenery_patches:
            draw.rectangle(box, fill=color)
        image.save(frame.image_path)
        return frame

    def ocr(self, path):
        rows = super().ocr(path)
        if not rows:
            return []
        rows[0] = type(rows[0])(self.player_name, rows[0].confidence, rows[0].bounds)
        return [row for row in rows if not self.omit_target_ocr or row.bounds['x'] < 200]


NO_DECISION = object()


class RecoveryRig:
    def __init__(self, directory, *, continuation=None, observer=None, character='Test Player',
                 target_name_box=None):
        directory.mkdir(parents=True)
        self.p = profile(directory)
        self.p.values['character']['name'] = character
        cfg = self.p.values['grind_only']
        cfg.update(goal_level=3, session_id=directory.name, duration_seconds=120,
                   campaign_progress_path=str(directory/'campaign.json'), target_names=['Young Wolf'])
        if continuation:
            cfg['campaign_continuation'] = continuation
        if observer:
            cfg['selected_target_observer'] = {'enabled':True, 'backend':'sage',
                                              'model':'offline-observer', 'timeout_seconds':2}
        if target_name_box is not None:
            cfg['target_name_box'] = target_name_box
        self.world = SimulatedWorld(directory)
        self.sage, self.backend = ScriptedSage(), FakeBackend()
        rect = Rect(0, 0, 500, 300)
        self.executor = SafeExecutor(self.backend, lambda:GateSnapshot(7,7,True,rect,rect,True,True),
                                     heartbeat_timeout=30, watchdog_interval=.01)
        self.store = EventStore(directory/'events.sqlite3')
        self.c = GrindController(self.p, self.store, self.sage, self.executor,
                                capture=self.world.capture, ocr=self.world.ocr, target_observer=observer)
        self.executor.arm()

    async def choose(self, answer=NO_DECISION, *, frame=None, level_due=False):
        self.c.wait_until = 0
        if level_due:
            self.c.level.last_attempt_at = None
        if answer is not NO_DECISION:
            self.sage.answers.append(answer)
        return await self.c.process(frame or self.world.capture())

    async def start(self, level=1):
        self.world.player_level = level
        assert (await self.choose('world_normal_confirmed')).status == 'dispatched'
        assert not self.c.baseline
        assert (await self.choose(f'player_level_{level}')).status == 'dispatched'

    async def close(self):
        await self.executor.stop()
        self.store.close()

    def events(self, kind):
        return [json.loads(row[0]) for row in self.store.connection.execute(
            'SELECT payload_json FROM events WHERE event_type=? ORDER BY rowid', (kind,))]

    def campaign(self):
        return json.loads(Path(self.p.values['grind_only']['campaign_progress_path']).read_text())

    def physical(self):
        return [event for event in self.backend.events if event[0] != 'release']


async def prior_level_two(directory):
    rig = RecoveryRig(directory)
    try:
        await rig.start()
        rig.world.player_level = 2
        await rig.choose('player_level_2', level_due=True)
        return {'character':'Test Player','verified_level':2,
                'evidence_path':rig.p.values['grind_only']['campaign_progress_path']}
    finally:
        await rig.close()


def test_level_three_continuation_retains_facts_and_requires_fresh_authority(tmp_path):
    async def scenario():
        first = RecoveryRig(tmp_path/'first')
        try:
            await first.start()
            await first.choose('attack_mob_level_1')
            first.world.health = '#301010'
            await first.choose('dead_credited')
            first.world.player_level = 2
            await first.choose('player_level_2', level_due=True)
            record = first.campaign()
            assert record['verified_level'] == 2
            assert record['observation']['level'] == 2
            assert record['verified_kills_this_session']  # Explicit simulation, never a real kill.
            assert first.c.cycle.input_generation > 0
            old_epoch = first.c.cycle.session_epoch
            first.c.hunt.action_failures['old-target-debt'] = 2
            first.c.stop('offline_engineering_restart')
        finally:
            await first.close()
        second = RecoveryRig(tmp_path/'second', continuation={
            'character':'Test Player','verified_level':2,
            'evidence_path':str(tmp_path/'first'/'campaign.json')})
        try:
            assert second.c.cycle.session_epoch != old_epoch
            assert not second.c.baseline and second.c.require_world
            assert second.c.level.last_confirmed_level is None
            assert second.c.cycle.input_generation == 0 and second.c.cycle.last_receipt is None
            assert not second.c.hunt.action_failures and second.c.hunt.pending is None
            assert second.c.target_observation is None
            await second.start(2)
            assert second.c.baseline and not second.c.stopped and not second.physical()
            assert second.campaign()['prior_campaign_evidence'] == str(tmp_path/'first'/'campaign.json')
            assert second.events('grind_baseline_verified')[0]['continuation'] == record
            second.world.player_level = 3
            frame = second.world.capture()
            await second.choose('player_level_3', frame=frame, level_due=True)
            assert not second.c.success and len(second.c.goal_frame_ids) == 1
            calls = len(second.sage.calls)
            assert (await second.choose(frame=frame)).status == 'grind_reobserve'
            assert len(second.sage.calls) == calls and not second.c.success
            await second.choose('player_level_3')
            assert second.c.success and second.c.reason == 'goal_level_verified'
            success = second.events('grind_success')[0]
            assert success['level'] == 3
            assert len({row['frame_id'] for row in success['evidence']}) == 2
            assert not second.physical()
        finally:
            await second.close()
    asyncio.run(scenario())


def test_initial_level_two_without_campaign_is_rejected(tmp_path):
    async def scenario():
        rig = RecoveryRig(tmp_path/'no-prior')
        try:
            await rig.start(2)
            assert rig.c.stopped and not rig.c.baseline and not rig.c.success
            assert rig.c.reason == 'campaign_baseline_identity_or_level_mismatch'
            assert not rig.physical()
        finally:
            await rig.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mismatch', ['profile', 'visible_character', 'name_prefix'])
def test_continuation_rejects_wrong_character(tmp_path, mismatch):
    async def scenario():
        prior = await prior_level_two(tmp_path/'prior')
        rig = RecoveryRig(tmp_path/'wrong-character', continuation=prior,
                          character='Other Player' if mismatch=='profile' else 'Test Player')
        rig.world.player_name = 'Test Player Impostor' if mismatch=='name_prefix' else 'Other Player'
        try:
            await rig.start(2)
            if mismatch!='profile':
                assert not rig.c.stopped and not rig.c.baseline
                await rig.choose('player_level_2',level_due=True)
                await rig.choose('player_level_2',level_due=True)
            assert rig.c.stopped and not rig.c.baseline and not rig.c.success
            assert not rig.physical()
            assert not rig.events('grind_baseline_verified')
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_unidentified_level_then_named_unknown_cannot_establish_baseline(tmp_path):
    async def scenario():
        rig = RecoveryRig(tmp_path/'unidentified')
        try:
            rig.world.player_name = ''
            await rig.start()
            assert not rig.c.baseline and not rig.c.stopped
            rig.world.player_name = 'Test Player'
            rig.world.player_level = '?'
            await rig.choose('player_level_unknown', level_due=True)
            assert not rig.c.baseline and not rig.c.success
            assert not rig.events('grind_baseline_verified')
            assert rig.store.load_checkpoint('grind_campaign_progress') is None
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_goal_success_retains_both_actual_level_observations_across_unknown(tmp_path):
    async def scenario():
        rig = RecoveryRig(tmp_path/'goal-unknown')
        try:
            await rig.start()
            rig.world.player_level = 3
            await rig.choose('player_level_3', level_due=True)
            first = rig.c.level.latest_observation['frame_id']
            rig.world.player_level = '?'
            await rig.choose('player_level_unknown', level_due=True)
            assert not rig.c.success
            rig.world.player_level = 3
            await rig.choose('player_level_3', level_due=True)
            assert rig.c.success
            evidence = rig.events('grind_success')[0]['evidence']
            assert [row['level'] for row in evidence] == [3, 3]
            assert first in {row['frame_id'] for row in evidence}
            assert len({row['frame_id'] for row in evidence}) == 2
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_unknown_level_preserves_previous_factual_campaign(tmp_path):
    async def scenario():
        rig = RecoveryRig(tmp_path/'unknown')
        try:
            await rig.start()
            old = rig.campaign()
            rig.world.player_level = '?'
            await rig.choose('player_level_unknown', level_due=True)
            assert rig.c.level.latest_observation['level'] is None
            assert rig.c.level.last_confirmed_level == 1 and not rig.c.success
            assert rig.campaign() == old
            assert rig.store.load_checkpoint('grind_campaign_progress') == old
        finally:
            await rig.close()
    asyncio.run(scenario())


def target_observer(*, flaw=None, before_result=None):
    async def fake(path, **kw):
        if before_result:
            await before_result()
        result = {'backend':'sage','configured_model':'offline-observer','actual_model':'offline-fake',
                  'frame_id':kw['frame_id'],'scope_id':kw['scope_id'],'captured_at':kw['captured_at'],
                  'image_sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                  'observation':{'selected_hud':'present','name':'Young Wolf','level':1,
                                 'target_kind':'creature','life_state':'alive'}}
        if flaw == 'stale':
            result['captured_at'] = (datetime.now(timezone.utc)-timedelta(seconds=40)).isoformat()
        elif flaw == 'scope':
            result['scope_id'] = 'retired-session'
        elif flaw == 'dead':
            result['observation']['life_state'] = 'dead'
        elif flaw == 'nonwolf':
            result['observation']['name'] = 'Rabbit'
        return result
    return fake


async def acquire_with_missing_ocr(rig):
    rig.world.omit_target_ocr = True
    await rig.start()
    await rig.choose('target_enemy')
    assert rig.c.hunt.compact_stage == 'inspect'
    assert (await rig.choose()).status == 'grind_reobserve'


def test_missing_ocr_name_observation_enables_actual_guarded_attack(tmp_path):
    async def scenario():
        rig = RecoveryRig(tmp_path/'observer', observer=target_observer())
        try:
            await acquire_with_missing_ocr(rig)
            assert rig.c.target_observation
            result = await rig.choose('attack_mob_level_1')
            assert result.status == 'dispatched' and result.receipt['possible_input']
            assert ('text','/cast [harm,nodead] Smite') in rig.backend.events
            assert 'Fresh attributed target observation:' in rig.sage.calls[-1]['prompt']
            assert 'no_selected_frame' not in rig.sage.calls[-1]['options']
            guard = rig.events('dispatch_guard_checked')[-1]
            assert guard['approved']
            assert guard['evidence']['visual_provenance']['actual_model'] == 'offline-fake'
            # Fully reconciled target-preserving casts carry stable identity;
            # current target life still comes from the newly captured HUD.
            proposal = await rig.c.target_proposal(rig.world.capture())
            assert proposal['name'] == 'Young Wolf'
            assert proposal['visual_observation']['life_state'] == 'alive'
            assert rig.c.target_observation['generation'] == rig.c.cycle.input_generation
        finally:
            await rig.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('flaw', ['stale','scope','dead','nonwolf'])
def test_unusable_observer_fact_never_offers_attack(tmp_path, flaw):
    async def scenario():
        rig = RecoveryRig(tmp_path/flaw, observer=target_observer(flaw=flaw))
        try:
            await acquire_with_missing_ocr(rig)
            accepted = rig.events('grind_target_observer_result')[-1]['accepted']
            assert accepted == (flaw in {'dead','nonwolf'})
            before = list(rig.physical())
            await rig.choose('cannot_assess')
            assert not any(option.startswith('attack_mob_') for option in rig.sage.calls[-1]['options'])
            assert rig.physical() == before
            assert not any(event[0]=='text' for event in rig.backend.events)
        finally:
            await rig.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('changed_authority', ['generation','scope'])
def test_observer_result_is_rejected_if_authority_changes_while_awaiting(tmp_path, changed_authority):
    async def scenario():
        async def change():
            if changed_authority == 'scope':
                rig.c.cycle.invalidate('simulated_scope_retired_during_observation')
            else:
                await rig.c.cycle._dispatch_with_receipt(
                    {'type':'keypress','keycode':6,'hold_seconds':.01}, frame=rig.world.capture(),
                    request_id='simulated_intervening_input',session_epoch=rig.c.cycle.session_epoch,
                    candidate_set_version='offline',chosen_option='offline')
        rig = RecoveryRig(tmp_path/changed_authority, observer=target_observer(before_result=change))
        try:
            await acquire_with_missing_ocr(rig)
            assert rig.c.target_observation is None
            assert rig.events('grind_target_observer_result')[-1]['accepted'] is False
            assert not any(event[0]=='text' for event in rig.backend.events)
        finally:
            await rig.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['health_animation','target_disappeared','source_window','source_pixels'])
def test_selected_approach_guard_preserves_identity_without_freezing_health(tmp_path, change):
    async def scenario():
        rig = RecoveryRig(tmp_path/change)
        try:
            await rig.start()
            await rig.choose('attack_mob_level_1')
            rig.world.error='Out of range'
            await rig.choose('position_error')
            assert rig.c.hunt.cast_error['kind']=='range'
            source = rig.world.capture()
            async def while_sage_thinks():
                if change == 'health_animation':
                    rig.world.health = 'yellow'
                elif change == 'target_disappeared':
                    rig.world.name = ''
                elif change == 'source_window':
                    rig.c.capture = lambda:replace(rig.world.capture(),source='unselected-window-99')
                else:
                    with Image.open(source.image_path) as raw:
                        image = raw.copy()
                    ImageDraw.Draw(image).point((20,20),fill='white')
                    image.save(source.image_path)
            rig.sage.hook = while_sage_thinks
            before = list(rig.physical())
            if change.startswith('source_'):
                with pytest.raises(CaptureError):
                    await rig.choose('forward', frame=source)
            else:
                result = await rig.choose('forward', frame=source)
                assert result.status == ('dispatched' if change=='health_animation' else 'dispatch_guard_rejected')
            guard = rig.events('dispatch_guard_checked')[-1]
            assert guard['approved'] == (change=='health_animation')
            if change == 'health_animation':
                assert guard['evidence']['selected_target_required']
                assert ('key',rig.p.values['controls']['bindings']['forward']['keycode'],True) in rig.physical()
            else:
                assert rig.physical() == before
                if change == 'target_disappeared':
                    assert 'same_selected_name' in guard['evidence']['failed_predicates']
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_calibrated_observer_name_survives_unrelated_portrait_scenery_change(tmp_path):
    async def scenario():
        rig = RecoveryRig(tmp_path/'calibrated-scenery', observer=target_observer(),
                          target_name_box=[205,95,360,118])
        try:
            await acquire_with_missing_ocr(rig)
            cached = rig.c.target_observation
            assert cached is not None
            # A large visual change inside the overall HUD crop, outside the
            # explicit name, health and numeral calibration regions.
            rig.world.scenery_patches = [((204,145,390,220),'white')]
            proposal = await rig.c.target_proposal(rig.world.capture())
            assert proposal['name'] == 'Young Wolf' and proposal['eligibility'] == 'eligible'
            assert proposal['visual_name_box'] == (205,95,360,118)
            assert rig.c.target_observation is cached
            result = await rig.choose('attack_mob_level_1')
            assert result.status == 'dispatched' and result.receipt['possible_input']
            assert ('text','/cast [harm,nodead] Smite') in rig.backend.events
            assert rig.events('dispatch_guard_checked')[-1]['approved']
        finally:
            await rig.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('changed_region', ['name','badge'])
def test_calibrated_observer_facts_retire_when_a_required_region_changes(tmp_path, changed_region):
    async def scenario():
        rig = RecoveryRig(tmp_path/changed_region, observer=target_observer(),
                          target_name_box=[205,95,360,118])
        try:
            await acquire_with_missing_ocr(rig)
            assert rig.c.target_observation is not None
            before = list(rig.physical())
            if changed_region == 'name':
                rig.world.name = 'Rabbit'
            else:
                rig.world.target_level = 2
            current = rig.world.capture()
            proposal = await rig.c.target_proposal(current)
            assert 'visual_observation' not in proposal and proposal['name'] is None
            assert rig.c.target_observation is None
            await rig.choose('cannot_assess',frame=current)
            assert not any(option.startswith('attack_mob_') for option in rig.sage.calls[-1]['options'])
            assert rig.physical() == before
        finally:
            await rig.close()
    asyncio.run(scenario())

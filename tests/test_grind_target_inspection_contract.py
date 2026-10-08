"""Real observer wire decoding through controller composition; fake world/input."""
import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from sage_wow.agent.selected_target_vision import observe
from sage_wow.sage.client import SageClient
from test_grind_exact_band_observation import start_band
from test_grind_level3_recovery import RecoveryRig


class ContractRig(RecoveryRig):
    def __init__(self, directory):
        self.wire = []
        self.answers = {'selected_hud': 'present', 'name': 'other_or_unknown',
                        'level': '2', 'target_kind': 'creature', 'life_state': 'alive'}
        self.failure = None
        self.flaw = None
        self.client = SageClient('offline-fake', transport=httpx.MockTransport(self.respond))
        super().__init__(directory, observer=self.inspect, target_name_box=[200, 95, 410, 120])
        self.world.name = 'Burly Rockjaw Trogg'
        self.world.target_level = 2
        self.drop_name = False
        self.drop_level = True
        original = self.c.ocr
        self.c.ocr = lambda path: [row for row in original(path)
            if not (self.drop_level and row.bounds['x'] == 425)
            and not (self.drop_name and row.bounds['x'] == 210)]

    def respond(self, request):
        self.wire.append(json.loads(request.content))
        if self.failure:
            raise self.failure
        return httpx.Response(200, json={'results': [{'answers': [
            {'ok': True, 'result': {'id': key, 'kind': 'choice', 'result': {'chosen': value}}}
            for key, value in self.answers.items()]}],
            'meta': {'model': 'offline-sage-wire', 'request_count': 1, 'question_count': 5}})

    async def inspect(self, path, **kwargs):
        kwargs['sage_client'] = self.client
        result = await observe(path, **kwargs)
        if self.flaw == 'scope': result['scope_id'] = 'retired-scope'
        if self.flaw == 'stale': result['captured_at'] = '2020-01-01T00:00:00+00:00'
        return result

    async def observe_target(self):
        self.c.observer_last_at = 0
        return await self.choose()

    async def close(self):
        await self.client.close()
        await super().close()


@pytest.mark.parametrize('missing_fresh_name', [False, True])
def test_generic_creature_fuses_same_source_ocr_with_real_raw_sage_number(tmp_path, missing_fresh_name):
    async def run():
        r = ContractRig(tmp_path/'generic')
        try:
            await start_band(r)
            assert (await r.observe_target()).status == 'grind_reobserve'
            raw = r.c.target_observation['result']['observation'].copy()
            assert raw['name'] is None and raw['level'] == 2
            r.drop_name = missing_fresh_name
            result = await r.choose('attack_mob_level_2')
            assert result.status == 'dispatched' and result.receipt['possible_input']
            evidence = r.events('dispatch_guard_checked')[-1]['evidence']['visual_provenance']
            assert evidence['raw_observation'] == raw
            assert evidence['field_sources']['name'] == 'source_calibrated_target_OCR'
            assert evidence['field_sources']['level'] == 'target_observer'
            assert evidence['source_ocr']['name'] == 'Burly Rockjaw Trogg'
            assert not r.c.target_inspection_episode
            assert r.events('grind_target_inspection_resolved')[-1]['verdict'] == 'eligible'
            questions = r.wire[0]['requests'][0]['questions']
            assert len(questions) == 5 and 'other_or_unknown' in json.dumps(questions)
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('field,value', [('level', '1'), ('level', '5'), ('level', 'unknown'),
    ('target_kind', 'player'), ('target_kind', 'friendly_or_self'), ('target_kind', 'unknown'),
    ('life_state', 'dead'), ('name', 'ragged_timber_wolf')])
def test_raw_ineligible_unknown_and_conflicting_facts_do_not_authorize_attack(tmp_path, field, value):
    async def run():
        r = ContractRig(tmp_path/'negative')
        try:
            await start_band(r)
            r.answers[field] = value
            if field == 'level' and value != 'unknown': r.world.target_level = int(value)
            await r.observe_target()
            await r.choose(None)
            assert not any(k.startswith('attack_mob_level_') for k in r.sage.calls[-1]['options'])
            assert not r.physical()
            if field == 'name':
                assert r.events('grind_target_observation_rejected')[-1]['source_conflict']
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['unknown', 'reconciliation', 'timeout', 'scope', 'stale'])
def test_unresolved_episode_reaches_existing_strategy_even_when_every_observer_is_due(tmp_path, failure):
    async def run():
        r = ContractRig(tmp_path/failure)
        try:
            await start_band(r)
            if failure == 'unknown': r.answers['level'] = 'unknown'
            elif failure == 'reconciliation': r.answers['name'] = 'ragged_timber_wolf'
            elif failure == 'timeout': r.failure = httpx.ReadTimeout('offline timeout')
            else: r.flaw = failure
            r.c.hunt.active_seconds = 601
            for _ in range(2):
                assert (await r.observe_target()).status == 'grind_reobserve'
            assert len(r.wire) == 2
            r.c.observer_last_at = 0
            result = await r.choose('change_search_strategy')
            assert result.status == 'dispatched'
            assert r.c.hunt.phase == 'choose_area' and r.c.hunt.planning_requested
            assert len(r.wire) == 2 and not r.physical()
            options = r.sage.calls[-1]['options']
            assert not {'reinspect_selected_frame', 'reinspect_cast_problem'} & set(options)
            assert 'recover_now' in options
            assert 'dead_or_unrecoverable' not in options
            assert len(r.events('grind_target_inspection_exhausted')) == 1
            # Planning must remain reachable on the next observer-due frame.
            r.c.observer_last_at = 0
            await r.choose('explore_visible')
            assert len(r.wire) == 2 and r.c.hunt.phase == 'travel'
            assert 'change_search_strategy' not in r.sage.calls[-1]['options']
        finally: await r.close()
    asyncio.run(run())


def test_elapsed_active_limit_hands_off_without_spending_second_attempt(tmp_path):
    async def run():
        r = ContractRig(tmp_path/'elapsed')
        try:
            await start_band(r); r.answers['level'] = 'unknown'
            await r.observe_target()
            r.c.hunt.active_seconds += 60
            r.c.observer_last_at = 0
            await r.choose('change_search_strategy')
            assert len(r.wire) == 1 and r.c.target_inspection_episode['exhausted']
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('own',[1,3])
@pytest.mark.parametrize('exhaust_inspection',[False,True])
def test_planning_does_not_offer_its_own_reentry_and_preserves_failure_history(tmp_path,own,exhaust_inspection):
    async def run():
        r=ContractRig(tmp_path/'planning-reentry')
        try:
            await start_band(r,own)
            if exhaust_inspection:
                r.answers['level']='unknown'
                r.c.hunt.compact_stage='reinspect'
                await r.observe_target();await r.observe_target()
            else:
                r.world.name='';r.world.target_level=None;r.c.hunt.compact_stage='acquire'
                # Concrete idle hunting replans on actual failed search,
                # not an aggregate motion count detached from its methods.
                r.c.hunt.confirmed_target_absences=2
            r.c.hunt.failures['motion']=2
            assert (await r.choose('change_search_strategy')).status=='dispatched'
            episode=r.c.target_inspection_episode
            required=r.c.hunt.strategy_required
            attempts=len(r.wire);revision=r.c.hunt.search_revision
            # A new planning frame must offer actual destinations, not another
            # accepted no-op request to enter the already active planner.
            await r.choose(None)
            options=r.sage.calls[-1]['options']
            assert 'change_search_strategy' not in options
            assert 'explore_visible' in options
            if exhaust_inspection:
                assert {'recover_now','cannot_assess'}<=set(options)
            else:
                assert not {'recover_now','cannot_assess'} & set(options)
            assert 'dead_or_unrecoverable' not in options
            assert r.c.hunt.planning_requested and r.c.hunt.phase=='choose_area'
            assert r.c.target_inspection_episode is episode and r.c.hunt.strategy_required is required
            assert r.c.hunt.failures['motion']==2 and r.c.hunt.search_revision==revision
            await r.choose('explore_visible')
            assert 'change_search_strategy' not in r.sage.calls[-1]['options']
            assert r.c.hunt.phase=='travel' and len(r.wire)==attempts and not r.physical()
            assert r.c.target_inspection_episode is episode
        finally:await r.close()
    asyncio.run(run())


def test_transient_unknown_recovers_on_second_observation(tmp_path):
    async def run():
        r = ContractRig(tmp_path/'transient')
        try:
            await start_band(r); r.answers['level'] = 'unknown'
            await r.observe_target()
            r.answers['level'] = '2'
            await r.observe_target()
            assert (await r.choose('attack_mob_level_2')).status == 'dispatched'
            assert not r.c.target_inspection_episode and len(r.wire) == 2
        finally: await r.close()
    asyncio.run(run())


def test_successful_refreshes_do_not_exhaust_one_long_encounter(tmp_path):
    async def run():
        r = ContractRig(tmp_path/'refreshes')
        try:
            await start_band(r)
            for _ in range(4):
                r.c.target_observation = None  # Simulate expiry, not a new encounter.
                await r.observe_target()
                await r.choose(None)
                assert not r.c.target_inspection_episode
            assert len(r.wire) == 4 and len(r.events('grind_target_inspection_resolved')) == 4
            assert not r.events('grind_target_inspection_exhausted')
        finally: await r.close()
    asyncio.run(run())


def test_prior_observer_number_cannot_be_relabelled_as_current_ocr(tmp_path):
    async def run():
        r = ContractRig(tmp_path/'no-laundering')
        try:
            await start_band(r); r.answers['target_kind'] = 'unknown'
            await r.observe_target()
            r.answers.update(level='unknown', target_kind='creature')
            await r.observe_target()
            assert r.c.target_observation['source_ocr']['levels'] == []
            await r.choose(None)
            assert not any(k.startswith('attack_mob_level_') for k in r.sage.calls[-1]['options'])
            assert not r.physical() and r.c.target_inspection_episode['exhausted']
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('field,value', [('level', '1'), ('name', 'ragged_timber_wolf'), ('selected_hud', 'absent')])
def test_known_conflict_cannot_fall_back_to_otherwise_eligible_numeric_ocr(tmp_path, field, value):
    async def run():
        r = ContractRig(tmp_path/'known-conflict')
        try:
            await start_band(r); r.drop_level = False
            r.c.hunt.compact_stage = 'reinspect'
            r.answers[field] = value
            if field == 'selected_hud':
                r.answers.update(name='other_or_unknown', level='unknown', target_kind='unknown', life_state='unknown')
            await r.observe_target()
            for _ in range(2):
                await r.choose(None)
                assert not any(k.startswith('attack_mob_level_') for k in r.sage.calls[-1]['options'])
                assert r.c.target_inspection_episode.get('conflict')
            assert not r.physical()
        finally: await r.close()
    asyncio.run(run())


def test_exhausted_inspection_handoff_preserves_pending_selection_as_unknown(tmp_path):
    async def run():
        r = ContractRig(tmp_path/'pending-selection')
        try:
            await start_band(r)
            r.world.name = ''; r.world.target_level = None
            r.c.hunt.compact_stage = 'acquire'
            await r.choose('target_enemy')
            pending_id = r.c.hunt.pending['receipt']['receipt_id']
            r.world.name = 'Burly Rockjaw Trogg'; r.world.target_level = 2
            r.answers['level'] = 'unknown'
            await r.observe_target(); await r.observe_target()
            r.c.observer_last_at = 0
            await r.choose('change_search_strategy')
            assert r.c.hunt.pending is None
            assert any(row['receipt']['receipt_id'] == pending_id and row['status'] == 'unknown_unassessed'
                       for row in r.c.hunt.unassessed)
            await r.choose('explore_visible')
            assert r.c.hunt.phase == 'travel' and len(r.wire) == 2
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['episode', 'pending', 'scope'])
def test_late_handoff_change_cannot_archive_a_newer_obligation(tmp_path, change):
    async def run():
        r = ContractRig(tmp_path/change)
        try:
            await start_band(r); r.answers['level'] = 'unknown'
            await r.observe_target(); await r.observe_target()
            replacement = {'family': 'target', 'receipt': {'receipt_id': 'newer-receipt', 'completed': True}}
            async def changed():
                if change == 'episode': r.c.target_inspection_episode = None
                elif change == 'pending': r.c.hunt.pending = replacement
                else: r.c.cycle.invalidate('offline changed scope during handoff')
            r.sage.hook = changed
            result = await r.choose('change_search_strategy')
            assert result.status != 'dispatched'
            assert not r.c.hunt.planning_requested and not r.physical()
            if change == 'pending': assert r.c.hunt.pending is replacement
        finally: await r.close()
    asyncio.run(run())


def test_fresh_dispatch_conflict_vetoes_previously_offered_attack(tmp_path):
    async def run():
        r = ContractRig(tmp_path/'dispatch-conflict')
        try:
            await start_band(r); r.drop_level = False
            r.c.hunt.compact_stage = 'reinspect'
            await r.observe_target()
            async def changed():
                # Simulate contradictory factual evidence arriving while the
                # gameplay request is outstanding; keep pixels/OCR unchanged.
                r.c.target_observation['result']['observation']['level'] = 1
            r.sage.hook = changed
            result = await r.choose('attack_mob_level_2')
            assert result.status == 'dispatch_guard_rejected' and not r.physical()
            assert 'no_observation_conflict' in r.events('dispatch_guard_checked')[-1]['evidence']['failed_predicates']
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['new_frame', 'missing_ocr', 'ocr_drift', 'focus_epoch', 'selection_revision', 'sticky_flags'])
def test_cosmetic_or_authority_changes_do_not_renew_inspection(tmp_path, change):
    async def run():
        r = ContractRig(tmp_path/change)
        try:
            await start_band(r); r.answers['level'] = 'unknown'
            await r.observe_target(); await r.observe_target()
            episode = r.c.target_inspection_episode
            if change == 'missing_ocr': r.drop_name = True
            elif change == 'focus_epoch': r.c.cycle.invalidate('offline focus roundtrip')
            elif change == 'selection_revision': r.c.hunt.selection_revision += 20
            elif change == 'sticky_flags':
                r.c.hunt.attempt_absence = True; r.c.hunt.attempt_changed_view = True
            elif change == 'ocr_drift':
                original = r.c.ocr
                r.c.ocr = lambda path: [replace(row, text='Different Creature') if row.bounds['x'] == 210 else row for row in original(path)]
            r.c.observer_last_at = 0
            await r.choose(None)
            assert r.c.target_inspection_episode is episode and episode['exhausted']
            assert len(r.wire) == 2 and not r.physical()
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['different_target', 'useful_search'])
def test_demonstrated_new_assessment_can_get_a_fresh_allowance(tmp_path, change):
    async def run():
        r = ContractRig(tmp_path/change)
        try:
            await start_band(r); r.answers['level'] = 'unknown'
            await r.observe_target(); await r.observe_target()
            old = r.c.target_inspection_episode['id']
            if change == 'different_target': r.world.name = 'Ragged Timber Wolf'
            else:
                r.c.hunt.meaningful_sector_change({'kind': 'offline_observed_translation', 'receipt_id': 'offline-receipt'})
            assert (await r.observe_target()).status == 'grind_reobserve'
            assert r.c.target_inspection_episode['id'] != old
            assert r.c.target_inspection_episode['attempts'] == 1 and len(r.wire) == 3
        finally: await r.close()
    asyncio.run(run())

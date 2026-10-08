"""Focused reviewed probe/cadence traces, using existing fake provider/input/capture."""
import asyncio
from datetime import datetime, timedelta, timezone
import math

import pytest
from sage_wow.agent.grind_search import map_direction, movement_result, goal_relation
from sage_wow.agent.level_verification import PlayerLevelVerifier
from test_grind_travel_acceptance import TravelRig, travel_scenario
from test_grind_product_spec import Rig
from test_level_verification import make_decision
from sage_wow.models import Frame

INITIAL='We have a destination but have not measured where forward takes us. Which short action should we take? Choose the forward probe if there is room for it; otherwise choose a movement that creates room.'
AFTER_TURN='We just turned. The earlier forward direction is historical and does not describe the new facing. Which short action should we take to measure the new direction or create room for that measurement?'
INCONCLUSIVE='The first forward probe was too small to establish direction at the coordinate resolution.'


def test_axis_and_scalar_uncertainty_are_independent():
    diagonal=movement_result([0,0],[.1,.1],[1,1])
    assert diagonal['displacement']==pytest.approx(math.sqrt(2)*.1)
    assert diagonal['progress_status']=='unresolved_precision'
    assert map_direction(.1,.1)=='direction unresolved at coordinate precision'
    lateral=movement_result([0,0],[.2,0],[0,5])
    assert lateral['displacement']>.14 and lateral['progress_status']=='unresolved_precision'
    assert 'left/right unresolved' in goal_relation({'dx':0,'dy':-.4},[-.05,5])
    assert map_direction(.2,-.2)=='North-East'


@travel_scenario
async def test_exact_initial_question_and_inventory_allow_untried_direct_probe(r):
    await r.action('probe_forward')
    call=r.sage.calls[-1]
    assert call['instructions'].split('\n\n')[0]==INITIAL
    assert 'A tree or wall alone is not an emergency.' in call['instructions']
    assert 'Never walk into' not in call['instructions']
    assert set(call['options'])=={'probe_forward','detour_backward','detour_strafe_left','detour_strafe_right',
        'detour_forward_left','detour_forward_right','detour_backward_left','detour_backward_right',
        'turn_left','turn_right','begin_hunt','change_destination','urgent_state'}
    assert call['options']['probe_forward'].description=='Move forward for 0.20 seconds to measure where forward leads. This is an orientation probe.'
    assert 'remaining vector: [10.0, 0.0]' in call['prompt']
    assert not r.c.hunt.detour and r.c.hunt.pending['travel_purpose']=='probe'


@travel_scenario
async def test_two_small_probes_combine_endpoints_then_offer_advance(r):
    await r.action('probe_forward');r.position=[10.1,10.]
    await r.action('probe_forward')
    assert r.sage.calls[-1]['instructions'].split('\n\n')[0]==INCONCLUSIVE
    r.position=[10.2,10.]
    await r.action('advance_forward')
    record=r.c.hunt.last_completed_action
    assert record['probe_count']==2 and record['displacement']==pytest.approx(.1)
    assert record['probe_measurement']['displacement']==pytest.approx(.2)
    assert 'approximately East' in r.sage.calls[-1]['prompt']
    assert 'advance_forward' in r.sage.calls[-1]['options'] and 'probe_forward' not in r.sage.calls[-1]['options']


@travel_scenario
async def test_two_unresolved_probes_withhold_then_turn_remeasures(r):
    await r.action('probe_forward');await r.action('probe_forward')
    await r.action('turn_left')
    menu=r.sage.calls[-1]
    assert 'repeated approach produced no measurable displacement' in menu['instructions']
    assert not {'probe_forward','advance_forward','detour_forward'} & menu['options'].keys()
    failures=dict(r.c.hunt.travel_failures)
    await r.action('probe_forward')
    assert r.sage.calls[-1]['instructions'].split('\n\n')[0]==AFTER_TURN
    assert all(r.c.hunt.travel_failures.get(k,0)>=v for k,v in failures.items())
    assert not r.c.stopped and r.c.hunt.last_completed_action['status']=='turn_completed'


@travel_scenario
async def test_known_away_not_laundered_by_expiry_or_destination_reselection(r):
    await r.action('probe_forward');r.position=[9.6,10.]
    await r.action('change_destination')
    assert 'Measured forward movement takes us farther' in r.sage.calls[-1]['instructions']
    assert not {'probe_forward','advance_forward','detour_forward'} & r.sage.calls[-1]['options'].keys()
    r.c.hunt.choose(dict(r.c.hunt.plan['area']),r.world.capture(),'same',1)
    r.c.hunt.action_responses['forward']['captured_at']=(datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()
    await r.action('turn_left')
    assert not {'probe_forward','advance_forward','detour_forward'} & r.sage.calls[-1]['options'].keys()
    assert 'Direction still current: yes' in r.sage.calls[-1]['prompt']


@travel_scenario
async def test_selected_detour_has_one_measured_continuation_and_no_self_renewal(r):
    await r.action('probe_forward');r.position=[9.6,10.]
    await r.action('detour_strafe_left')
    detour=dict(r.c.hunt.detour)
    assert detour['start_position']==[9.6,10.] and detour['side']=='left'
    assert detour['reason']==r.sage.calls[-1]['options']['detour_strafe_left'].description
    assert not detour['continuation_credit']
    r.position=[9.6,9.6]
    await r.action('detour_forward')
    assert not r.c.hunt.detour['continuation_credit']
    r.position=[9.2,9.6]
    await r.action('turn_right')
    assert not {'probe_forward','advance_forward','detour_forward'} & r.sage.calls[-1]['options'].keys()
    assert r.c.hunt.detour['start_position']==detour['start_position']
    assert r.c.hunt.last_completed_action['purpose']=='detour'


@travel_scenario
async def test_visual_destination_and_weak_coordinates_are_truthful(r):
    r.c.hunt.plan['area'].pop('coordinate')
    await r.action('detour_backward')
    call=r.sage.calls[-1]
    assert not {'probe_forward','advance_forward','detour_forward'} & call['options'].keys()
    assert 'destination: unavailable' in call['prompt']
    assert 'same-zone geometry unavailable; no arrival claim' in call['prompt']
    r.position=None
    r.c.hunt.plan['area']['zone_reference']=None
    await r.action('turn_left')
    assert r.c.hunt.last_completed_action['status']=='unknown_visual'
    assert 'Current position: unavailable' in r.sage.calls[-1]['prompt']
    assert 'goal zone: unavailable; same-zone: unresolved' in r.sage.calls[-1]['prompt']
    assert not r.c.stopped


def test_restored_current_geometry_does_not_rewrite_unknown_historical_result(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();await r.action('probe_forward')
            r.position=None
            r.after_hidden=lambda:setattr(r,'position',[12.,10.])
            await r.action('probe_forward')
            assert r.c.hunt.last_completed_action['status']=='unknown_visual'
            assert r.c.hunt.last_completed_action.get('displacement') is None
            call=r.sage.calls[-1]
            assert 'Current position: [12.0, 10.0]' in call['prompt']
            assert 'distance: 8.00' in call['prompt']
            assert 'pair: unknown/unlinked' in call['prompt']
        finally:await r.close()
    asyncio.run(run())


def test_single_startup_read_and_schedule_survives_focus_and_hints(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start()
            level_calls=lambda:[call for call in r.sage.calls if 'player_level_1' in call['options']]
            assert len(level_calls())==1
            call=level_calls()[0]
            assert 'player_level_unknown' in call['options'] and 'urgent_threat' not in call['options']
            assert call['instructions']=="Which level numeral is visible in my player's level badge? Select the matching numeral. Select player_dead only if my own death is explicitly visible."
            attempt=r.c.level.last_attempt_at
            r.c.pause_focus();r.c.resume_focus();await r.choose('world_normal_confirmed')
            assert r.c.level.last_attempt_at==attempt
            await r.choose('recover_now');await r.choose('rest');await r.choose('recovered_resume')
            r.world.name=''
            await r.choose('target_enemy')
            assert r.c.hunt.pending['family']=='target'
            assert len(level_calls())==1
            assert all('player_level_changed_or_unclear' not in x['options'] and 'verify_player_level' not in x['options'] for x in r.sage.calls)
            r.c.level.last_attempt_at-=300
            await r.choose('player_level_3')
            assert r.c.level.last_confirmed_level==3 and len(level_calls())==2
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('answer',[None,RuntimeError('offline timeout')])
def test_null_startup_wait_has_no_level_or_navigation_debt_and_keeps_safety(tmp_path,answer):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.choose('world_normal_confirmed');await r.choose(answer)
            assert not r.c.baseline and r.c.level.last_confirmed_level is None
            assert not r.c.null_answers and not r.c.provider_failures and not r.c.hunt.blocked
            assert r.c.wait_until-r.c.level.last_attempt_at<5
            await r.choose('safe_hold')
            options=r.sage.calls[-1]['options']
            assert not any(x.startswith(('attack_','choose_area:')) for x in options)
            assert not {'target_enemy','forward','probe_forward','explore_visible'} & options.keys()
            count=len(r.sage.calls)
            for _ in range(3):
                r.c.wait_until=0
                result=await r.c.process(r.world.capture())
                assert result.status=='grind_waiting_level'
            assert len(r.sage.calls)==count
            r.world.player_health='red'
            await r.choose('recover_now');await r.choose('heal_self')
            assert r.c.hunt.pending['family']=='recovery' and len(r.physical_keys())==2
            assert r.c.level.last_confirmed_level is None
            assert len([x for x in r.sage.calls if 'player_level_1' in x['options']])==1
        finally:await r.close()
    asyncio.run(run())


def test_null_after_baseline_keeps_accepted_level_and_single_success_read(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.c.level.last_attempt_at-=300
            await r.choose(None)
            assert r.c.level.last_confirmed_level==1 and r.c.baseline
            assert not r.c.hunt.blocked and not r.c.null_answers
            await r.choose('attack_mob_level_1')
            r.c.hunt.encounter_ended=True;r.c.hunt.phase='search';r.c.level.last_attempt_at-=300
            await r.choose('player_level_3')
            assert not r.c.success
            await r.choose('player_level_3')
            assert r.c.success and r.c.reason=='goal_level_verified'
        finally:await r.close()
    asyncio.run(run())


def test_default_full_verifier_keeps_pair_and_grinding_opt_in_one_read():
    frame=Frame.create('offline',100,100,image_path='unused')
    for count in (1,2):
        verifier=PlayerLevelVerifier(required_readings=count);verifier.activate_session('epoch')
        verifier.record(decision=make_decision(frame,'epoch','player_level_1'),frame=frame)
        assert verifier.last_confirmed_level==(1 if count==1 else None)


@travel_scenario
async def test_focus_confirmation_restores_travel_and_preserves_newer_recovery_return(r):
    await r.action('probe_forward')
    r.c.pause_focus();r.c.resume_focus();await r.action('world_normal_confirmed')
    assert r.c.hunt.phase=='travel' and r.c.level.last_attempt_at is not None
    await r.action('probe_forward')
    r.c.pause_focus();r.c.resume_focus()
    r.c.hunt.phase='recover';r.c.hunt.prior_phase='travel'
    await r.action('world_normal_confirmed')
    assert r.c.hunt.phase=='recover' and r.c.hunt.prior_phase=='travel'


@travel_scenario
async def test_detour_continuation_keeps_original_start_and_reason(r):
    await r.action('detour_strafe_left')
    original=dict(r.c.hunt.detour)
    r.position=[10.,9.6]
    await r.action('detour_strafe_left')
    assert r.c.hunt.detour['start_position']==original['start_position']
    assert r.c.hunt.detour['start_frame_id']==original['start_frame_id']
    assert r.c.hunt.detour['reason']==original['reason']
    assert not r.c.hunt.detour['continuation_credit']


def test_unreadable_probe_endpoints_consume_two_attempts_without_numeric_facts(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();await r.action('probe_forward')
            for answer in ('probe_forward','turn_left'):
                r.position=None
                r.after_hidden=lambda:setattr(r,'position',[10.,10.])
                await r.action(answer)
                assert r.c.hunt.last_completed_action['status']=='unknown_visual'
                assert r.c.hunt.last_completed_action.get('displacement') is None
            assert not {'probe_forward','advance_forward','detour_forward'} & r.sage.calls[-1]['options'].keys()
            assert 'Inconclusive probes: 2' in r.sage.calls[-1]['prompt']
            assert not r.c.stopped
        finally:await r.close()
    asyncio.run(run())


def test_dispatch_rechecks_broken_forward_authority_under_fresh_clean_snapshot(tmp_path,monkeypatch):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();await r.action('probe_forward');r.position=[10.4,10.]
            await r.action('advance_forward');r.position=[10.8,10.]
            async def break_only_continuity():r.c.hunt.mapping_generation=-1
            r.sage.hook=break_only_continuity
            before=r.physical_keys().count(r.controls['forward']['keycode'])
            result=await r.choose('advance_forward')
            assert result.status!='dispatched' and r.hud
            assert r.physical_keys().count(r.controls['forward']['keycode'])==before
            assert r.c.observation_transaction['status']=='restored_verified'
        finally:await r.close()
    asyncio.run(run())


@travel_scenario
async def test_observed_return_to_failed_position_and_direction_restores_withholding(r):
    await r.action('probe_forward');r.position=[10.4,10.]
    await r.action('advance_forward');await r.action('advance_forward');await r.action('turn_left')
    failures=dict(r.c.hunt.travel_failures)
    assert r.c.hunt.failed_direction_approaches
    await r.action('probe_forward');r.position=[10.4,9.6]
    await r.action('turn_right');await r.action('probe_forward');r.position=[10.8,9.6]
    await r.action('detour_backward');r.position=[10.4,10.]
    await r.action('turn_left')
    assert not {'probe_forward','advance_forward','detour_forward'} & r.sage.calls[-1]['options'].keys()
    assert all(r.c.hunt.travel_failures.get(k,0)>=v for k,v in failures.items())
    assert not r.c.stopped

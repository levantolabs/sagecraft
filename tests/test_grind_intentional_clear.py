"""Intentional selection release preserves combat debt and uses ordinary acquisition."""
from test_grind_product_spec import retain_legacy_no_effect
import asyncio
from copy import deepcopy
from pathlib import Path
import time

import pytest

from test_grind_opportunistic_targets import rig, snapshot
from test_grind_committed_combat import install_transient_error


async def start(tmp_path):
    r=await rig(tmp_path)
    r.world.name='Rockjaw Trogg';r.world.target_level=2
    return r


async def failed_attempt(r, cause='ineffective'):
    await r.choose('attack_mob_level_2')
    if cause=='ineffective':
        await retain_legacy_no_effect(r);await r.choose('attack_mob_level_2');await retain_legacy_no_effect(r)
    elif cause=='unknown':
        r.life='unknown'
        r.c.request_recovery('offline_provider_interruption')
    else:
        install_transient_error(r,'facing','Target is not in front of you')
        await r.choose('position_error')
    await r.choose('reject_selected_target')
    assert r.c.hunt.pending['family']=='clear'
    return deepcopy(r.c.hunt.pending)


def debt(h):
    history=deepcopy(h.target_history[h.combat_history_key])
    history.pop('intentional_clear',None);history.pop('encounter_ended',None)
    return deepcopy((h.cast_obligation,h.cast_error,h.no_effect_retries,h.correction_rounds,
        h.rejected_signatures,h.unresolved_motion,h.failures['combat'],history))


async def release(r):
    r.world.name='';r.world.target_level=None
    return await r.choose('target_cleared')


@pytest.mark.parametrize('cause',['ineffective','facing','unknown'])
def test_linked_native_clear_closes_only_selected_ownership_then_ordinary_local_acquisition(tmp_path,cause):
    async def run():
        r=await start(tmp_path)
        try:
            clear=await failed_attempt(r,cause);h=r.c.hunt;owner=h.combat_history_key
            before=debt(h);recent=deepcopy(h.recent_combat)
            await release(r)
            fact=h.target_history[owner]['intentional_clear']
            assert fact['disposition']=='closed' and fact['clear']['receipt']==clear['receipt']
            assert fact['combat_receipt_id']==recent['receipt']['receipt_id']
            assert h.encounter_ended and h.terminal_reason=='selection_released_unknown'
            assert not h.target_dead_observed and not h.credited_kills
            assert debt(h)==before and h.recent_combat==recent and h.combat_history_key==owner
            await r.choose(None)
            assert set(r.sage.calls[-1]['options'])=={'target_enemy','forward','turn_left','turn_right'}
            assert 'change_search_strategy' not in r.sage.calls[-1]['options']
            assert 'inspect_recent_corpse' not in r.sage.calls[-1]['options']
            assert 'CURRENT world view' in r.sage.calls[-1]['prompt']
            assert not h.confirmed_target_absences
            await r.choose('target_enemy')
            assert h.pending['family']=='target' and debt(h)==before
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('cause',['threat','heal','ui','focus'])
def test_verified_release_waits_for_stronger_task_then_fresh_absence_without_another_clear(tmp_path,cause):
    async def run():
        r=await start(tmp_path)
        try:
            await failed_attempt(r);h=r.c.hunt;owner=h.combat_history_key
            async def interrupt():
                if cause in {'threat','focus'}:h.active_threat={'kind':'current_incoming_attack','occurred_at':time.time()}
                elif cause=='heal':r.c.heal_pending={'offline':True}
                else:r.c.require_world=True
            r.sage.hook=interrupt
            await release(r)
            r.sage.hook=None
            fact=h.target_history[owner]['intentional_clear']
            assert fact['disposition']=='verified' and not h.encounter_ended
            before=debt(h);keys=len(r.physical_keys())
            if cause=='focus':
                r.c.pause_focus();assert r.c.resume_focus()
                h.active_threat=None
                await r.choose('world_normal_confirmed')
            else:
                h.active_threat=None;r.c.heal_pending=None;r.c.require_world=False
            await r.choose(None)
            assert fact['disposition']=='closed' and h.encounter_ended and debt(h)==before
            assert len(r.physical_keys())==keys
            assert 'target_enemy' in r.sage.calls[-1]['options']
            frame,target,_=await snapshot(r)
            assert not h.reconcile_intentional_clear(r.c,frame,target)
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('positive',['name','level','bar'])
def test_new_positive_selection_permanently_supersedes_delayed_clear_even_after_later_empty_frame(tmp_path,positive):
    async def run():
        r=await start(tmp_path)
        try:
            await failed_attempt(r);h=r.c.hunt
            async def threat():h.active_threat={'kind':'current_incoming_attack','occurred_at':time.time()}
            r.sage.hook=threat;await release(r);r.sage.hook=None
            fact=h.intentional_clear_current();assert fact['disposition']=='verified'
            h.active_threat=None
            frame,target,_=await snapshot(r)
            if positive=='name':target.update(name='Rockjaw Trogg',visual_observation={'selected_hud':'present'})
            elif positive=='level':target['levels']=[2]
            else:target['hud']={'frame_id':frame.frame_id,'target_health':.5,'target_health_confidence':1.}
            h.selection_seen(target)
            assert fact['disposition']=='superseded'
            frame,target,_=await snapshot(r)
            assert not h.reconcile_intentional_clear(r.c,frame,target)
            assert not h.encounter_ended
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['failed','unlinked','epoch','source_hash','owner','partial','positive_visual','positive_level','positive_bar'])
def test_invalid_clear_or_conflicting_absence_cannot_close_combat_owner(tmp_path,fault):
    async def run():
        r=await start(tmp_path)
        try:
            clear=await failed_attempt(r);h=r.c.hunt
            if fault=='failed':
                original=h.pending['receipt']['receipt_id'];calls=len(r.sage.calls)
                r.c.wait_until=0;continuation=await r.c.process(r.world.capture())
                assert continuation.status=='dispatched' and continuation.decision is None
                assert len(r.sage.calls)==calls and continuation.receipt['authorization_id']==original
                assert h.pending['clear_pair']['original']['receipt']['receipt_id']==original
                await r.choose('clear_failed');assert not h.encounter_ended
                assert h.intentional_clear_current()['disposition']=='failed';return
            r.world.name='';r.world.target_level=None
            if fault=='source_hash':Path(clear['source_image']).write_bytes(Path(clear['source_image']).read_bytes()+b'changed')
            elif fault=='owner':h.pending['intentional_clear_owner']='other'
            if fault.startswith('positive'):
                original=r.c.target_proposal
                async def conflict(frame):
                    target=await original(frame)
                    if fault=='positive_visual':target['visual_observation']['selected_hud']='present'
                    elif fault=='positive_level':target['levels']=[2]
                    else:target['hud']={'frame_id':frame.frame_id,'target_health':.5,'target_health_confidence':1.}
                    return target
                r.c.target_proposal=conflict
                await r.choose('target_cleared')
                assert h.pending and h.pending['family']=='clear'
                assert h.compact_stage=='reinspect'
            elif fault=='source_hash':
                r.sage.answers.append('target_cleared')
                result=await r.c.process(r.world.capture())
                assert result.status=='grind_stopped' and r.c.reason=='retained_grind_source_changed'
            elif fault=='owner':
                await r.choose('target_cleared')
            else:
                original=h.verify_intentional_clear;checks=[]
                def invalidate(c,frame,target,pending,result):
                    assert result.receipt==c.cycle.last_receipt
                    assert result.decision.chosen=='target_cleared'
                    if fault=='unlinked':c.cycle._input_generation+=1
                    elif fault=='epoch':c.cycle.session_epoch='other'
                    else:pending['receipt'].update(completed=False,dispatch_unknown=True)
                    verified=original(c,frame,target,pending,result)
                    checks.append(verified);return verified
                h.verify_intentional_clear=invalidate
                await r.choose('target_cleared')
                assert checks==[False]
            assert not h.encounter_ended
            assert not (h.intentional_clear_current() or {}).get('assessment')
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('reacquisition',['pixels','tab','death'])
def test_same_name_view_change_cannot_renew_released_debt_but_authenticated_tab_and_death_can(tmp_path,reacquisition):
    async def run():
        r=await start(tmp_path)
        try:
            await failed_attempt(r);await release(r);h=r.c.hunt;owner=h.combat_history_key
            before=debt(h)
            h.meaningful_sector_change({'fixture':'existing useful-view observation'})
            assert h.attempt_absence and h.attempt_changed_view
            assert h.target_history[owner]['rejections']==before[-1]['rejections']
            before=debt(h)  # Existing sector bookkeeping is separate from attempt renewal.
            if reacquisition=='tab':await r.choose('target_enemy')
            elif reacquisition=='death':h.target_dead_observed=True
            r.world.name='Rockjaw Trogg';r.world.target_level=2
            if reacquisition=='pixels':
                await r.choose(None)
                assert 'attack_mob_level_2' not in r.sage.calls[-1]['options']
                assert debt(h)==before and h.combat_history_key==owner
                frame,target,_=await snapshot(r)
                old=deepcopy((h.target_history,h.target_lives,h.approach))
                assert not h.pixel_renewal_allowed(target)
                assert (h.target_history,h.target_lives,h.approach)==old
            else:
                await r.choose('attack_mob_level_2')
                assert h.combat_history_key!=owner
                assert h.target_history[owner]['cast_obligation']==before[0]
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('evidence',['intentional','unexplained','independent_death'])
def test_corpse_inference_suppression_is_receipt_specific_and_preserves_independent_death(tmp_path,evidence):
    async def run():
        r=await start(tmp_path)
        try:
            from test_grind_loot_budget import enable
            r.controls['interact_target']={'keycode':3,'verified_from':'offline fixture'}
            enable(r)
            if evidence=='intentional':
                await failed_attempt(r);await release(r)
                await r.choose(None)
                assert 'inspect_recent_corpse' not in r.sage.calls[-1]['options']
            elif evidence=='unexplained':
                await r.choose('attack_mob_level_2')
                r.c.hunt.archive_pending('offline unexplained disappearance')
                r.world.name='';r.world.target_level=None
                await r.choose(None)
                assert 'inspect_recent_corpse' in r.sage.calls[-1]['options']
            else:
                await r.choose('attack_mob_level_2')
                r.life='dead';r.world.health='black'
                await r.choose('dead')
                assert r.c.hunt.target_dead_observed and r.c.hunt.encounter_ended
            assert not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_actual_useful_search_motion_does_not_renew_released_attempt_on_same_name_pixels(tmp_path):
    async def run():
        r=await start(tmp_path)
        try:
            await failed_attempt(r);await release(r);h=r.c.hunt;owner=h.combat_history_key
            await r.choose('turn_left')
            motion=deepcopy(h.pending)
            assert motion['purpose']=='search' and motion['family']=='motion'
            await r.choose('motion_useful')
            assert h.attempt_absence and h.attempt_changed_view
            assert h.search_changes[-1]['evidence']['receipt_id']==motion['receipt']['receipt_id']
            r.world.name='Rockjaw Trogg';r.world.target_level=2
            # Exercise the correction-motion caller that also carries renewal metadata.
            r.c.config['committed_combat']=False
            before=deepcopy(h.target_history[owner]);renewals=[]
            original=h.renew_attempt
            def record(target):renewals.append(target);return original(target)
            h.renew_attempt=record
            await r.choose('forward')
            assert not renewals and h.combat_history_key==owner
            assert h.target_history[owner]['rejections']==before['rejections']
            assert h.target_history[owner]['cast_obligation']==before['cast_obligation']
            assert h.pending['purpose']=='cast_correction'
            assert 'attack_mob_level_2' not in r.sage.calls[-1]['options']
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('absence',['plain_missing','stale_attribution'])
def test_delayed_handoff_needs_current_attributed_absence(tmp_path,absence):
    async def run():
        r=await start(tmp_path)
        try:
            await failed_attempt(r);h=r.c.hunt
            async def threat():h.active_threat={'kind':'current_incoming_attack','occurred_at':time.time()}
            r.sage.hook=threat;await release(r);r.sage.hook=None
            fact=h.intentional_clear_current();h.active_threat=None
            frame,target,_=await snapshot(r)
            if absence=='plain_missing':target.pop('visual_observation');target.pop('visual_provenance')
            else:target['visual_provenance']['frame_id']='old-frame'
            h.selected_presence=False
            assert not h.reconcile_intentional_clear(r.c,frame,target)
            assert fact['disposition']=='verified' and not h.encounter_ended
            frame,target,_=await snapshot(r)
            assert h.reconcile_intentional_clear(r.c,frame,target)
        finally:await r.close()
    asyncio.run(run())


def test_delayed_clear_closes_old_owner_without_rewriting_later_destination_or_return_phase(tmp_path):
    async def run():
        r=await start(tmp_path)
        try:
            await failed_attempt(r);h=r.c.hunt
            async def threat():h.active_threat={'kind':'current_incoming_attack','occurred_at':time.time()}
            r.sage.hook=threat;await release(r);r.sage.hook=None
            frame,target,_=await snapshot(r)
            # Existing destination acceptance stores this plan and later goal history.
            h.choose(h.catalog['coldridge_west_boars'],frame,'later-sage-destination',3)
            plan=deepcopy(h.plan);goals=deepcopy(h.travel_policy)
            h.active_threat=None
            assert h.reconcile_intentional_clear(r.c,frame,target)
            assert h.plan==plan and h.travel_policy==goals and h.phase=='travel'
            assert h.encounter_ended and h.terminal_reason=='selection_released_unknown'
        finally:await r.close()
    asyncio.run(run())


def test_repaired_clear_to_actual_local_absences_explicit_destination_and_bounded_focused_recovery(tmp_path,monkeypatch):
    from sage_wow.agent import grind_navigation_recovery as recovery
    from sage_wow.agent.grind_search import TARGET_RETRY_SECONDS
    from test_grind_navigation_recovery import exhaust_pages,focused_modes
    async def run():
        r=await start(tmp_path)
        try:
            await failed_attempt(r);await release(r);h=r.c.hunt
            owner=h.combat_history_key;history=deepcopy(h.target_history[owner])
            for _ in range(2):
                await r.choose('target_enemy');await r.choose('no_selected_frame')
            assert h.confirmed_target_absences==2 and h.failures['target']==2
            await r.choose('change_search_strategy')
            assert 'Repeated Tab attempts found no selected enemy' not in r.sage.calls[-1]['prompt']
            await r.choose('choose_area:coldridge_west_boars')
            assert h.plan['area_id']=='coldridge_west_boars' and h.plan['phase']=='travel'
            plan=deepcopy(h.plan)
            # Current own-resource facts are explicit offline bars, as in existing navigation rigs.
            def bars(c,frame):
                return {'frame_id':frame.frame_id,'player_health':1.,'health_confidence':1.,
                    'player_mana':1.,'target_health':None,'target_health_confidence':1.}
            monkeypatch.setattr('sage_wow.agent.grind_resources.hud_resources',bars)
            original_target=r.c.target_proposal
            async def target(frame):return {**await original_target(frame),'hud':bars(r.c,frame)}
            r.c.target_proposal=target
            # A cooldown is opportunity to inspect, not refunded failures or new attack authority.
            h.active_seconds+=TARGET_RETRY_SECONDS
            await exhaust_pages(r)
            menu=deepcopy(h.travel_policy['retry_menu']);keys=len(r.physical_keys())
            await r.choose(None)
            state=recovery.record(r.c)
            assert state['requests']==1 and state['rounds_started']==1
            await r.choose('target_enemy')
            assert focused_modes(r)==['off','auto']
            assert h.pending['family']=='target' and h.plan==plan
            await r.choose('no_selected_frame')
            scout=deepcopy(h.travel_policy['last_scout'])
            assert h.phase=='travel' and h.plan==plan
            r.c.pause_focus();r.c.resume_focus();await r.choose('world_normal_confirmed')
            assert state['requests']==2 and state['rounds_started']==1
            await r.choose(None);await r.choose(None)
            assert focused_modes(r)==['off','auto','off','auto']
            assert state['requests']==4 and state['rounds_started']==2 and not recovery.available(state)
            assert recovery.MAX_GOALS==3 and len(state['goals'])<=3
            calls=len(r.sage.calls)
            result=await r.choose(None);r.sage.answers.clear()
            assert result.status=='grind_navigation_wait' and len(r.sage.calls)==calls
            retained=deepcopy(h.target_history[owner])
            # Existing destination/focus bookkeeping updates encounter-area ownership only.
            retained.pop('encounter_area');history.pop('encounter_area')
            assert h.plan==plan and retained==history
            assert h.travel_policy['last_scout']==scout
            assert h.travel_policy['retry_menu']['presented']==menu['presented']
            assert len(r.physical_keys())==keys+1 and not h.blocked
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('positive',['visual_name','visual_level','late_bar'])
def test_legal_visual_unknown_or_late_current_bar_permanently_supersedes_verified_release(tmp_path,monkeypatch,positive):
    from sage_wow.agent.grind_resources import hud_resources as original_bars
    async def run():
        r=await start(tmp_path)
        try:
            await failed_attempt(r);h=r.c.hunt
            async def threat():h.active_threat={'kind':'current_incoming_attack','occurred_at':time.time()}
            r.sage.hook=threat;await release(r);r.sage.hook=None;h.active_threat=None
            fact=h.intentional_clear_current();assert fact['disposition']=='verified'
            original=r.c.target_proposal
            async def current(frame):
                target=await original(frame)
                if positive!='late_bar':
                    target['visual_observation'].update(selected_hud='unknown',name='',level=None)
                    target['visual_observation']['name' if positive=='visual_name' else 'level']='Rockjaw Trogg' if positive=='visual_name' else 2
                else:target.pop('hud',None)
                return target
            r.c.target_proposal=current
            if positive=='late_bar':
                def bars(c,frame):
                    return {'frame_id':frame.frame_id,'player_health':1.,'health_confidence':1.,
                        'player_mana':1.,'target_health':.5,'target_health_confidence':1.}
                monkeypatch.setattr('sage_wow.agent.grind_resources.hud_resources',bars)
            await r.choose(None)
            assert fact['disposition']=='superseded' and not h.encounter_ended
            r.c.target_proposal=original
            monkeypatch.setattr('sage_wow.agent.grind_resources.hud_resources',original_bars)
            frame,target,_=await snapshot(r)
            assert not h.reconcile_intentional_clear(r.c,frame,target) and not h.encounter_ended
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['name','level'])
@pytest.mark.parametrize('absence',['attributed','plain_missing'])
def test_submitted_clear_cannot_be_verified_after_a_different_positive_selection_episode(tmp_path,change,absence):
    async def run():
        r=await start(tmp_path)
        try:
            await failed_attempt(r);h=r.c.hunt;fact=h.intentional_clear_current()
            if change=='name':r.world.name='Small Crag Boar'
            else:r.world.target_level=3
            await r.choose('target_cleared')
            assert h.pending and not fact.get('assessment')
            if absence=='plain_missing':
                original=r.c.target_proposal
                async def missing(frame):
                    target=await original(frame)
                    target['visual_observation'].update(selected_hud='unknown',name='',level=None)
                    return target
                r.c.target_proposal=missing
            await release(r)
            assert not fact.get('assessment') and not h.encounter_ended
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['different_level','numeric_dropout'])
def test_clear_installation_requires_same_combat_selection_episode_but_allows_numeric_availability_churn(tmp_path,change):
    async def run():
        r=await start(tmp_path)
        try:
            await r.choose('attack_mob_level_2');await retain_legacy_no_effect(r)
            await r.choose('attack_mob_level_2');await retain_legacy_no_effect(r)
            r.world.target_level=3 if change=='different_level' else None
            await r.choose('reject_selected_target')
            h=r.c.hunt
            if change=='different_level':assert h.intentional_clear_current() is None
            else:assert h.intentional_clear_current()['disposition']=='submitted'
            await release(r)
            assert h.encounter_ended==(change=='numeric_dropout')
        finally:await r.close()
    asyncio.run(run())


def test_submitted_clear_positive_current_bar_cannot_create_verified_fact_or_false_clear_outcome(tmp_path,monkeypatch):
    from sage_wow.agent.grind_resources import hud_resources as original_bars
    async def run():
        r=await start(tmp_path)
        try:
            await failed_attempt(r);h=r.c.hunt;fact=h.intentional_clear_current()
            def bars(c,frame):
                return {'frame_id':frame.frame_id,'player_health':1.,'health_confidence':1.,
                    'player_mana':1.,'target_health':.5,'target_health_confidence':1.}
            monkeypatch.setattr('sage_wow.agent.grind_resources.hud_resources',bars)
            assert not r.c.config['encounter_resources']
            before=len(h.outcomes)
            await release(r)
            assert not fact.get('assessment') and not h.encounter_ended
            assert h.pending and h.pending['family']=='clear' and h.compact_stage=='reinspect'
            assert not any(x['outcome']=='target_cleared' for x in h.outcomes[before:])
            monkeypatch.setattr('sage_wow.agent.grind_resources.hud_resources',original_bars)
            await release(r)
            assert fact['disposition']=='closed' and h.encounter_ended
        finally:await r.close()
    asyncio.run(run())


def test_submitted_first_absence_transition_needs_current_attribution(tmp_path):
    async def run():
        r=await start(tmp_path)
        try:
            await failed_attempt(r);h=r.c.hunt;fact=h.intentional_clear_current()
            original=r.c.target_proposal
            async def stale(frame):
                target=await original(frame)
                target['visual_provenance']['frame_id']='old-absence-frame'
                return target
            r.c.target_proposal=stale
            await release(r)
            assert not fact.get('assessment') and not h.encounter_ended
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('field',['name','level'])
@pytest.mark.parametrize('different',[False,True])
def test_submitted_observer_only_identity_conflict_cannot_later_authenticate_old_clear_but_original_identity_can(tmp_path,field,different):
    async def run():
        r=await start(tmp_path)
        try:
            await failed_attempt(r);h=r.c.hunt;fact=h.intentional_clear_current()
            r.world.name='';r.world.target_level=None
            original=r.c.target_proposal
            async def observed(frame):
                target=await original(frame)
                target['visual_observation'].update(selected_hud='unknown',name='',level=None)
                target['visual_observation'][field]=('Small Crag Boar' if different else 'Rockjaw Trogg') if field=='name' else (3 if different else 2)
                return target
            r.c.target_proposal=observed
            await r.choose('target_cleared')
            assert not fact.get('assessment') and h.pending
            r.c.target_proposal=original
            await release(r)
            assert h.encounter_ended==(not different)
            assert bool(fact.get('assessment'))==(not different)
        finally:await r.close()
    asyncio.run(run())

"""Offline recovery traces: simulated pixels/Sage, real guards and fake input."""
from sage_wow.agent.grind_search import HuntState
from test_grind_product_spec import scenario


def test_only_observed_geometry_renews_local_motion_not_destination_or_declaration():
    from types import SimpleNamespace
    hunt=HuntState({},'offline')
    old=hunt.motion_key('forward','search')
    hunt.action_failures[old]=2
    hunt.failures['target']=2
    hunt.unresolved_motion[hunt.motion_key('turn_left','search')]=1
    evidence_before=dict(hunt.action_failures)
    hunt.choose({'id':'other','label':'unverified'},SimpleNamespace(frame_id='f'),'request',1)
    hunt.request_recovery('uncertain')
    assert hunt.motion_key('forward','search')==old
    assert hunt.motion_count('forward','search')==2
    assert hunt.failures['target']==2
    hunt.meaningful_sector_change({'receipt_id':'proved-turn','outcome':'motion_useful'})
    assert hunt.motion_key('forward','search')!=old
    assert hunt.motion_count('forward','search')==0
    assert hunt.action_failures==evidence_before and sum(hunt.unresolved_motion.values())==1
    assert hunt.search_changes[-1]['evidence']['receipt_id']=='proved-turn'


@scenario
async def test_exhausted_tabs_and_forward_recover_through_new_view_with_debt_retained(r):
    r.world.name=''
    for _ in range(2):
        await r.choose('target_enemy');await r.choose('no_selected_frame')
    for _ in range(2):
        await r.choose('forward');await r.choose('motion_no_useful_effect')
    old=r.c.hunt.motion_key('forward','search')
    assert r.c.hunt.failures['target']==2 and r.c.hunt.action_failures[old]==2
    await r.choose(None);await r.choose(None)
    assert r.c.hunt.recovery_requested and not r.c.hunt.blocked and not r.c.stopped
    await r.choose('turn_left')
    options=r.sage.calls[-1]['options']
    assert {'change_search_strategy','turn_left','turn_right'}<=options.keys()
    assert 'reinspect_selected_frame' not in options
    assert 'target_enemy' not in options and 'forward' not in options
    await r.choose('motion_useful')
    assert r.c.hunt.motion_count('forward','search')==0
    assert r.c.hunt.action_failures[old]==2
    await r.choose('target_enemy');r.world.name='Young Wolf'
    await r.choose('attack_mob_level_1')
    assert len(r.casts())==1 and not r.c.hunt.credited_kills


@scenario
async def test_uncertain_planning_yields_physical_recovery_without_renewing_search_debt(r):
    r.world.name=''
    for _ in range(2):
        await r.choose('target_enemy');await r.choose('no_selected_frame')
    for _ in range(2):
        await r.choose('forward');await r.choose('motion_no_useful_effect')
    h=r.c.hunt;old=h.motion_key('forward','search');prior_outcomes=list(h.outcomes)
    await r.choose('change_search_strategy')
    assert h.phase=='choose_area' and h.planning_requested
    for _ in range(2):
        await r.choose(None)
        assert 'explore_visible' in r.sage.calls[-1]['options']
    assert h.recovery_requested and h.compact_stage=='recovery' and not h.planning_requested
    await r.choose('turn_left')
    options=r.sage.calls[-1]['options']
    assert {'turn_left','turn_right','change_search_strategy'}<=options.keys()
    assert 'reinspect_selected_frame' not in options
    assert 'explore_visible' not in options and 'target_enemy' not in options and 'forward' not in options
    assert h.motion_key('forward','search')==old and h.action_failures[old]==2 and h.failures['target']==2
    assert h.outcomes[:len(prior_outcomes)]==prior_outcomes
    assert r.physical_keys().count(r.controls['turn_left']['keycode'])==1
    assert not h.blocked and not r.c.stopped


@scenario
async def test_progress_review_never_absorbs_a_current_eligible_cast(r):
    r.c.hunt.active_seconds=361
    await r.choose('attack_mob_level_1')
    assert len(r.casts())==1 and not r.c.hunt.blocked
    assert r.c.hunt.no_progress_at==0 and r.c.hunt.progress_review_at>=361
    r.world.health='orange'
    await r.choose('own_damaged_alive')
    await r.choose('attack_mob_level_1')
    assert len(r.casts())==2 and r.c.hunt.no_progress_at>=361


@scenario
async def test_progress_review_acquisition_has_useful_recovery_then_reacquisition(r):
    r.world.name='';r.c.hunt.compact_stage='acquire';r.c.hunt.active_seconds=361
    await r.choose('turn_left')
    assert set(r.sage.calls[-1]['options'])=={'target_enemy','turn_left','turn_right','forward'}
    assert r.c.hunt.no_progress_at==0 and not r.c.hunt.blocked
    await r.choose('motion_useful')
    await r.choose('target_enemy');r.world.name='Young Wolf'
    await r.choose('attack_mob_level_1')
    assert len(r.casts())==1 and not r.c.stopped


@scenario
async def test_presence_contradiction_keeps_tab_receipt_and_does_not_charge_absence(r):
    r.world.name='';await r.choose('target_enemy')
    pending=r.c.hunt.pending
    r.world.name='Young Wolf';r.world.target_level=2
    await r.choose('no_selected_frame')
    assert r.c.hunt.pending is pending and r.c.hunt.failures['target']==0
    assert r.c.hunt.compact_stage=='reinspect' and r.c.hunt.selected_presence is None
    assert not r.c.hunt.attempt_absence and not r.casts()
    await r.choose('no_selected_frame')
    assert r.c.hunt.recovery_requested and r.c.hunt.failures['target']==0
    assert r.c.hunt.pending is None and not r.c.hunt.attempt_absence
    assert any(item['receipt_id']==pending['receipt']['receipt_id'] and item['outcome']=='unknown'
               for item in r.c.hunt.outcomes)


@scenario
async def test_unknown_cast_archived_by_recovery_cannot_become_a_free_retry(r):
    await r.choose('attack_mob_level_1')
    receipt=r.c.hunt.pending['receipt']['receipt_id']
    await r.choose('cannot_assess');await r.choose('cannot_assess')
    assert r.c.hunt.pending is None and r.c.hunt.cast_obligation
    assert any(item['receipt_id']==receipt and item['outcome']=='unknown' for item in r.c.hunt.outcomes)
    await r.choose('reinspect_selected_frame');await r.choose('cannot_assess')
    assert 'attack_mob_level_1' not in r.sage.calls[-1]['options']
    assert len(r.casts())==1


@scenario
async def test_unresolved_tabs_keep_bounded_debt_until_a_useful_view_change(r):
    r.world.name=''
    for _ in range(2):
        await r.choose('target_enemy')
        await r.choose(None);await r.choose(None)
    await r.choose(None)
    assert 'target_enemy' not in r.sage.calls[-1]['options']
    assert r.c.hunt.failures['target']==0  # Unknown selection is not false absence.
    assert len([item for item in r.c.hunt.outcomes if item['purpose']=='target' and item['outcome']=='unknown'])==2
    await r.choose('turn_left');await r.choose('motion_useful')
    await r.choose('target_enemy')
    assert not r.c.stopped


@scenario
async def test_repeated_failed_clear_keeps_changed_strategy_instead_of_endless_escape(r):
    for _ in range(2):
        await r.choose('reject_selected_target');await r.choose('clear_failed')
    await r.choose('change_search_strategy')
    assert 'reject_selected_target' not in r.sage.calls[-1]['options']
    assert r.c.hunt.planning_requested
    assert r.physical_keys().count(r.controls['escape']['keycode'])==2


@scenario
async def test_lost_unknown_then_observed_absence_changed_view_can_hunt_same_species(r):
    await r.choose('attack_mob_level_1')
    prior=r.c.hunt.pending['receipt']['receipt_id']
    r.world.name='';await r.choose('lost')
    await r.choose('turn_left');await r.choose('motion_useful')
    await r.choose('target_enemy');r.world.name='Young Wolf'
    await r.choose('attack_mob_level_1')
    assert len(r.casts())==2 and not r.c.hunt.credited_kills
    assert any(item['receipt_id']==prior and item['outcome']=='unknown' for item in r.c.hunt.outcomes)

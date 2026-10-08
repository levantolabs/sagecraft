"""Unchanged-cast diagnostics now lead directly to useful guarded choices."""
from copy import deepcopy

from test_grind_committed_combat import scenario, add_precise_resource_observer


async def unchanged_casts(r):
    add_precise_resource_observer(r,[1.])
    await r.choose('attack_mob_level_1');await r.choose('attack_mob_level_1')


def no_removed_answers(options):
    assert not {'cannot_assess','no_effect','reinspect_cast_problem','reinspect_selected_frame'} & options.keys()


@scenario
async def test_unchanged_casts_lead_to_guarded_approach_and_feedback(r):
    await unchanged_casts(r)
    h=r.c.hunt;keys=list(r.physical_keys());casts=list(r.casts())
    await r.choose('approach_for_range_check')
    review=deepcopy(h.cast_review);options=r.sage.calls[-1]['options']
    no_removed_answers(options)
    assert {'change_search_strategy','stand_for_cast'} <= options.keys()
    assert 'attack_mob_level_1' not in options
    assert review['unchanged_resource_casts']==0 and not review['damage_known']
    assert r.physical_keys()==keys+[r.controls['forward']['keycode']]
    guards=[e['payload'] for e in r.store.recent(30) if e['event_type']=='dispatch_guard_checked']
    assert guards[-1]['chosen']=='approach_for_range_check' and guards[-1]['approved']
    assert h.pending['purpose']=='cast_correction'
    await r.choose('motion_no_useful_effect')
    assert h.motion_count('forward','cast_correction',h.approach['history_key'])==1
    assert h.cast_review['reviewed_receipts']==review['reviewed_receipts']
    assert not h.credited_kills and r.casts()==casts


@scenario
async def test_unchanged_review_can_choose_strategy_and_reach_planning(r):
    await unchanged_casts(r)
    h=r.c.hunt;pending_id=h.pending['receipt']['receipt_id']
    debts=deepcopy(h.action_failures);casts=list(r.casts());keys=list(r.physical_keys())
    await r.choose('change_search_strategy')
    no_removed_answers(r.sage.calls[-1]['options'])
    assert any(o['receipt_id']==pending_id and o['outcome']=='unknown' for o in h.outcomes)
    assert h.phase=='choose_area' and h.planning_requested and h.strategy_required
    history=deepcopy(h.target_history);review=deepcopy(h.cast_review)
    await r.choose('explore_visible')
    assert 'Choose the next hunting destination' in r.sage.calls[-1]['instructions']
    assert h.phase=='travel' and not h.planning_requested
    # The fresh living view can reconcile unknown damage; all method debt stays.
    for item in history.values():item['cast_obligation']=None
    assert h.cast_review==review and h.target_history==history and h.action_failures==debts
    assert r.casts()==casts and r.physical_keys()==keys and not h.credited_kills


@scenario
async def test_new_cast_evidence_never_reintroduces_removed_inspection(r):
    health=[1.]
    add_precise_resource_observer(r,health)
    await r.choose('attack_mob_level_1');await r.choose('attack_mob_level_1')
    await r.choose(None)
    health[0]=.65
    await r.choose('attack_mob_level_1')
    no_removed_answers(r.sage.calls[-1]['options'])
    before=deepcopy(r.c.hunt.cast_review['reviewed_receipts'])
    await r.choose(None)
    no_removed_answers(r.sage.calls[-1]['options'])
    assert r.c.hunt.cast_review['reviewed_receipts']!=before
    assert r.c.hunt.cast_review['unchanged_resource_casts']==1 and len(r.casts())==3
    assert 'inspection' not in r.c.hunt.cast_review


@scenario
async def test_unchanged_resources_require_correction_or_exit_before_another_cast(r):
    await unchanged_casts(r)
    await r.choose(None)
    options=r.sage.calls[-1]['options']
    assert 'attack_mob_level_1' not in options
    assert {'approach_for_range_check','stand_for_cast','reject_selected_target','change_search_strategy'}<=options.keys()
    assert len(r.casts())==2 and not r.c.hunt.credited_kills
    await r.choose('approach_for_range_check')
    await r.choose('motion_useful')
    await r.choose('attack_mob_level_1')
    assert len(r.casts())==3 and not r.c.hunt.credited_kills


@scenario
async def test_two_actual_nulls_keep_unknown_effect_and_a_concrete_exit(r):
    await unchanged_casts(r)
    pending_id=r.c.hunt.pending['receipt']['receipt_id']
    await r.choose(None);await r.choose(None)
    assert 'inspection' not in r.c.hunt.cast_review
    assert any(o['receipt_id']==pending_id and o['outcome']=='unknown' for o in r.c.hunt.outcomes)
    await r.choose('change_search_strategy')
    no_removed_answers(r.sage.calls[-1]['options'])
    assert r.c.hunt.planning_requested and len(r.casts())==2
    assert not r.c.hunt.credited_kills and not r.c.hunt.retry_credit

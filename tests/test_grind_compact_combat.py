"""Finite integrated traces: synthetic perception, real guarded controller/executor."""
from test_grind_product_spec import retain_legacy_no_effect
import asyncio
import pytest
from test_grind_product_spec import Rig


def trace(fn):
    def run(tmp_path):
        async def scenario():
            r=Rig(tmp_path)
            try:
                await r.start()
                await fn(r)
            finally:await r.close()
        asyncio.run(scenario())
    run.__name__=fn.__name__
    return run


@trace
async def test_two_credited_cycles_include_range_correction_and_readable_corpse(r):
    r.world.name=''
    await r.choose('target_enemy')
    # Tab does not assert frame presence; inspect remains even without OCR name.
    await r.choose('no_selected_frame')
    assert 'attack_mob_level_1' not in r.sage.calls[-1]['options']
    await r.choose('target_enemy')
    r.world.name='Young Wolf';r.world.target_level=1
    await r.choose('attack_mob_level_1')
    first=r.c.hunt.encounter
    r.world.error='Out of range'
    await r.choose('position_error')
    await r.choose('forward')
    await r.choose('motion_useful')
    r.world.error=''
    await r.choose('attack_mob_level_1')
    r.world.health='orange'
    await r.choose('own_damaged_alive')
    await r.choose('attack_mob_level_1')
    r.world.health='black'
    await r.choose('dead_credited')
    assert len(r.c.hunt.credited_kills)==1
    # The readable corpse cannot silently create a new lifetime.
    await r.choose('target_enemy')
    assert 'attack_mob_level_1' not in r.sage.calls[-1]['options']
    r.world.health='green'
    await r.choose('attack_mob_level_1')
    assert r.c.hunt.encounter>first
    r.world.health='black'
    await r.choose('dead_credited')
    assert len(r.c.hunt.credited_kills)==2
    assert len({x['encounter_id'] for x in r.c.hunt.credited_kills})==2
    assert all(not x['exclusive_solo_known'] for x in r.c.hunt.credited_kills)
    assert len(r.casts())==4
    assert all(len(x['prompt'])<1500 for x in r.sage.calls[2:])
    assert not any('hunt_plan' in x['prompt'] for x in r.sage.calls[2:])


@trace
async def test_false_cue_is_rejected_once_per_receipt_without_retry(r):
    await r.choose('attack_mob_level_1')
    pending=r.c.hunt.pending
    r.world.error='Out of range'
    await r.choose('error_not_supported')
    assert r.c.hunt.pending is pending and not r.c.hunt.retry_credit
    r.world.error='OUT OF RANGE!'
    await r.choose('damaged_alive')
    assert 'position_error' not in r.sage.calls[-1]['options']
    assert len(r.casts())==1 and not r.c.hunt.credited_kills


@trace
async def test_no_effect_retry_exhaustion_keeps_correction_and_reject(r):
    await r.choose('attack_mob_level_1');await retain_legacy_no_effect(r)
    await r.choose('attack_mob_level_1');await retain_legacy_no_effect(r)
    await r.choose('cannot_assess')
    opts=r.sage.calls[-1]['options']
    assert 'attack_mob_level_1' not in opts
    assert {'forward','reject_selected_target'}<=opts.keys()
    assert len(r.casts())==2


@trace
async def test_rejected_same_name_needs_absence_changed_view_and_fresh_inspection(r):
    await r.choose('reject_selected_target')
    r.world.name=''
    await r.choose('target_cleared')
    await r.choose('target_enemy');r.world.name='Young Wolf'
    await r.choose('cannot_assess')
    assert 'attack_mob_level_1' not in r.sage.calls[-1]['options']
    r.world.name=''
    await r.choose('no_selected_frame')
    await r.choose('turn_left');await r.choose('motion_useful')
    await r.choose('target_enemy')
    r.world.name='Young Wolf'
    await r.choose('attack_mob_level_1')
    assert len(r.casts())==1
    assert any(h['rejections'] for h in r.c.hunt.target_history.values())


@trace
async def test_lost_never_credits_or_transfers_old_cast(r):
    await r.choose('attack_mob_level_1');old=r.c.hunt.pending
    r.world.name=''
    await r.choose('lost')
    assert not r.c.hunt.credited_kills and not r.c.hunt.target_dead_observed
    assert any(x['receipt']['receipt_id']==old['receipt']['receipt_id'] for x in r.c.hunt.unassessed)
    await r.choose('target_enemy');r.world.name='Young Wolf'
    await r.choose('cannot_assess')
    assert 'attack_mob_level_1' not in r.sage.calls[-1]['options']


@pytest.mark.parametrize('error,choice,expected',[
    ('Not enough mana','cast_error_observed','recover'),
    ('Spell not learned','cast_error_observed','stopped'),
    ('Spell not ready yet','cast_error_observed','approach'),
])
def test_nonpositional_capability_feedback(tmp_path,error,choice,expected):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();await r.choose('attack_mob_level_1')
            r.world.error=error;await r.choose(choice)
            if expected=='stopped':assert r.c.stopped and r.c.reason=='unsupported_capability'
            else:
                assert r.c.hunt.phase==expected
                if expected=='approach':
                    await r.choose('cast_ready');r.world.error=''
                    await r.choose('attack_mob_level_1')
        finally:await r.close()
    asyncio.run(run())


@trace
async def test_self_heal_requires_fresh_enemy_inspection_retaining_unknown_cast(r):
    await r.choose('attack_mob_level_1');old=r.c.hunt.pending['receipt']['receipt_id']
    await r.choose('recover_now')
    await r.choose('heal_self')
    r.world.name='Test Player'
    await r.choose('resources_improved')
    await r.choose('recovered_resume')
    await r.choose('target_enemy')
    r.world.name='Young Wolf'
    await r.choose('cannot_assess')
    assert r.sage.calls[-1]['instructions'].startswith('Is an actual selected target portrait/bar frame present,')
    assert 'attack_mob_level_1' not in r.sage.calls[-1]['options']
    assert any(x['receipt_id']==old and x['outcome']=='unknown' for x in r.c.hunt.outcomes)
    assert not r.c.hunt.credited_kills


@trace
async def test_background_name_passive_chat_cannot_establish_selected_frame(r):
    from sage_wow.perception.ocr import TextObservation
    r.world.name='';r.c.hunt.compact_stage='inspect'
    base=r.world.ocr
    r.c.ocr=lambda path:base(path)+[TextObservation('Say:',.99,{'x':20,'y':250,'width':40,'height':10}),
        TextObservation('Young Wolf',.99,{'x':220,'y':260,'width':100,'height':10})]
    await r.choose('no_selected_frame')
    await r.choose('target_enemy')
    assert 'close_visible_ui' not in r.sage.calls[-1]['options']
    assert not r.casts()
    # A real active field enters UI recovery, where closure remains reachable.
    r.c.ocr=lambda path:base(path)+[TextObservation('chat input',.99,{'x':20,'y':250,'width':80,'height':10})]
    await r.choose('close_visible_ui')
    assert len(r.casts())==0


@trace
async def test_session_no_progress_review_preserves_cast_and_factual_damage_clock(r):
    r.c.hunt.active_seconds=361
    await r.choose('attack_mob_level_1')
    assert len(r.casts())==1 and not r.c.hunt.blocked
    assert r.c.hunt.progress_review_at>=361 and r.c.hunt.no_progress_at==0


@pytest.mark.parametrize('change',['target','generation','identity','focus','hash','partial'])
def test_actual_cast_authority_vetoes(tmp_path,change):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start()
            async def hook():
                if change=='target':r.world.name='Other Wolf'
                elif change=='generation':r.c.cycle.input_generation+=1
                elif change=='identity':r.c.revision+=1;r.c.cycle.invalidate('identity_changed')
                elif change=='focus':
                    from sage_wow.control.executor import GateSnapshot
                    from sage_wow.platform.macos.geometry import Rect
                    rect=Rect(0,0,500,300)
                    r.executor.inspect_gate=lambda:GateSnapshot(7,8,True,rect,rect,True,True)
                elif change=='hash':
                    for path in tmp_path.glob('acceptance-*.png'):
                        if path.name==f'acceptance-{r.world.count}.png':path.write_bytes(b'changed')
            r.sage.hook=hook
            if change=='partial':
                def fail(_):raise RuntimeError('synthetic partial text dispatch')
                r.backend.text=fail
            result=await r.choose('attack_mob_level_1')
            assert not r.casts()
            assert result.status!='dispatched'
            if change=='partial':assert r.c.stopped and r.c.reason=='partial_or_unknown_grind_input'
        finally:await r.close()
    asyncio.run(run())


@trace
async def test_expired_initial_source_never_calls_provider_or_casts(r):
    from datetime import datetime, timezone, timedelta
    from dataclasses import replace
    frame=replace(r.world.capture(),captured_at=(datetime.now(timezone.utc)-timedelta(seconds=13)).isoformat())
    result=await r.c.process(frame)
    assert result.status=='grind_timing_expired'
    assert len(r.sage.calls)==2 and not r.casts()


@trace
async def test_due_level_during_fight_keeps_source_budget_for_correction(r):
    await r.choose('attack_mob_level_1');await r.choose('damaged_alive')
    r.c.level.last_attempt_at=0
    calls=len(r.sage.calls)
    await r.choose('forward')
    assert len(r.sage.calls)==calls+1
    assert not any(x.startswith('player_level_') for x in r.sage.calls[-1]['options'])
    assert r.controls['forward']['keycode'] in r.physical_keys()

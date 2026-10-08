"""Adversarial production traces for the direct loop; all inputs/providers fake."""
import asyncio

from sage_wow.perception.ocr import TextObservation
from test_grind_only import setup, baseline, cleanup


def test_friendly_level_five_selection_does_not_hide_hunting_or_offer_defense(tmp_path):
    async def run():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','reject_selected_target','target_cleared','target_enemy','no_selected_frame','change_search_strategy','search_here']);e.arm()
        c.ocr=lambda path:[row for row in f.ocr(path) if row.text.strip()]
        try:
            await baseline(c,f);f.name='Sten Stoutarm';f.numeric=5
            assert (await c.process(f.capture())).status=='dispatched'
            options=s.calls[-1]['options']
            assert 'reject_selected_target' in options
            assert not any(option.startswith('attack_') for option in options) and 'defend_attacker' not in options
            assert c.hunt.pending['family']=='clear'
            f.name='';f.numeric=None
            assert (await c.process(f.capture())).status=='dispatched'
            assert c.hunt.pending is None
            assert (await c.process(f.capture())).status=='dispatched'
            options=s.calls[-1]['options']
            assert {'forward','turn_left','target_enemy'}<=options.keys()
            assert c.hunt.pending['family']=='target'
            assert (await c.process(f.capture())).status=='dispatched'  # Tab result must be assessed.
            c.hunt.exhausted_patches=3
            assert (await c.process(f.capture())).status=='dispatched'
            assert c.hunt.phase=='choose_area'
            assert (await c.process(f.capture())).status=='dispatched'
            options=s.calls[-1]['options']
            assert 'search_here' in options
            assert any(option.startswith('choose_area:') for option in options)
            assert not any(option.startswith('attack_') for option in options) and 'defend_attacker' not in options
            assert c.hunt.phase=='search' and not any(x[0]=='text' for x in b.events)
            assert 'input_steps' not in s.calls[-1]['prompt'] and len(s.calls[-1]['prompt'])<3500
        finally:await cleanup(e,store)
    asyncio.run(run())


def notice_rows(duplicate=False):
    row=lambda text,x,y,w,h:TextObservation(text,1,{'x':x,'y':y,'width':w,'height':h})
    rows=[row('The world around you will refresh soon',130,100,250,15),row('Okay',220,150,40,18)]
    if duplicate:rows.append(row('Okay',280,150,40,18))
    return rows


def test_positive_notice_vetoes_false_clear_claim_and_two_failed_acknowledgements(tmp_path):
    async def run():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','world_normal_confirmed',
            'acknowledge_notice','blocking_ui_remains','acknowledge_notice','blocking_ui_remains','outcome_unclear','world_normal_confirmed']);e.arm()
        original=f.ocr;visible=[True]
        c.ocr=lambda path:original(path)+(notice_rows() if visible[0] and path.name.startswith('source-') else [])
        # Baseline has no notice; add it for the failed starting-scene properties.
        visible[0]=False
        b.mouse_move=lambda *args:b.events.append(('move',*args));b.mouse_button=lambda *args:b.events.append(('mouse',*args))
        try:
            await baseline(c,f);visible[0]=True;c.level.last_attempt_at=0;c.streak=3
            result=await c.process(f.capture())
            assert result.status!='dispatched'
            for _ in range(5):await c.process(f.capture())
            options=s.calls[-1]['options']
            assert 'world_normal_confirmed' not in options and 'acknowledge_notice' not in options
            assert not {'forward','target_enemy','attack_mob_level_1'}&options.keys()
            assert c.hunt.action_failures[c.hunt.action_key('acknowledge','ui')]==2
            assert len([event for event in b.events if event[0]=='mouse' and event[-1]])==2
            visible[0]=False;await c.process(f.capture())
            assert not c.require_world and c.hunt.phase!='ui_recover'
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_notice_changed_or_ambiguous_control_cannot_click(tmp_path):
    async def run():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','acknowledge_notice','outcome_unclear']);e.arm()
        original=f.ocr;duplicate=[False];visible=[False]
        c.ocr=lambda path:original(path)+(notice_rows(duplicate[0]) if visible[0] and path.name.startswith('source-') else [])
        try:
            await baseline(c,f);visible[0]=True
            async def change():duplicate[0]=True
            s.hook=change;result=await c.process(f.capture())
            assert result.status!='dispatched' and c.cycle.input_generation==0
            s.hook=None;await c.process(f.capture())
            assert 'acknowledge_notice' not in s.calls[-1]['options']
            assert not any(event[0] in {'text','mouse'} for event in b.events)
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_two_unchanged_casts_remove_all_offense_until_linked_correction(tmp_path):
    async def run():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','attack_mob_level_1',
            'no_effect','attack_mob_level_1','no_effect','forward','motion_useful','attack_mob_level_1']);e.arm()
        try:
            await baseline(c,f)
            for _ in range(4):await c.process(f.capture())
            assert not any(option.startswith('attack_') for option in s.calls[-1]['options'])
            assert c.hunt.action_failures[c.hunt.action_key('cast','combat')]==2
            await c.process(f.capture());await c.process(f.capture())
            assert not any(option.startswith('attack_') for option in s.calls[-1]['options'])
            await c.process(f.capture())
            assert len([event for event in b.events if event[0]=='text'])==3
            assert not c.stopped
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_failed_target_budget_survives_area_rotation_then_changed_sector_unlocks(tmp_path):
    async def run():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','target_enemy','no_selected_frame',
            'target_enemy','no_selected_frame','begin_hunt','turn_left','motion_useful','target_enemy']);e.arm()
        try:
            await baseline(c,f);f.name=''
            for _ in range(4):await c.process(f.capture())
            assert not c.hunt.allowed('target_enemy','target')
            area=next(iter(c.hunt.catalog.values()));c.hunt.choose(area,f.capture(),'new_request',1)
            assert not c.hunt.allowed('target_enemy','target')
            await c.process(f.capture());await c.process(f.capture());await c.process(f.capture());await c.process(f.capture())
            assert c.hunt.allowed('target_enemy','target') and c.hunt.sector==1
            assert c.hunt.result()['search_failures']>=0
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_death_damage_and_credit_are_separate_observed_facts_and_plan_survives(tmp_path):
    async def run():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','begin_hunt','attack_mob_level_1',
            'damaged_alive','attack_mob_level_1','dead','target_enemy','attack_mob_level_1','lost']);e.arm()
        try:
            await baseline(c,f);area=next(iter(c.hunt.catalog.values()));c.hunt.choose(area,f.capture(),'plan',1)
            await c.process(f.capture());await c.process(f.capture())
            assert c.hunt.plan['area_id']==area['id'] and not c.hunt.progress_facts
            f.health='red';await c.process(f.capture())
            assert [fact['kind'] for fact in c.hunt.progress_facts]==['damage_observation']
            await c.process(f.capture());f.dead=True;await c.process(f.capture())
            assert c.hunt.progress_facts[-1]['kind']=='selected_encounter_death'
            assert c.hunt.progress_facts[-1]['xp_known'] is False
            assert c.hunt.progress_facts[-1]['kill_attribution_known'] is False
            assert not c.hunt.credited_kills
            assert c.hunt.result()['kill_hints']==0 and c.hunt.plan['area_id']==area['id']
            await c.process(f.capture())  # Corpse does not block fresh Tab acquisition.
            f.dead=False;f.name='Different Creature';await c.process(f.capture())
            assert c.hunt.encounter==2 and c.hunt.death_frame is None
            f.name='Unrelated Selection';await c.process(f.capture())
            assert not {'dead','dead_credited','damaged_alive'}&s.calls[-1]['options'].keys()
            assert not any(option.startswith('attack_') for option in s.calls[-1]['options'])
            assert len(c.hunt.progress_facts)==2 and not c.hunt.credited_kills
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_compact_probe_preserves_approved_duration_without_heading_claim(tmp_path):
    async def run():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','forward','motion_useful','attack_mob_level_1','outcome_unclear']);e.arm()
        try:
            await baseline(c,f);f.name='';c.config['move_seconds']=1.2
            await c.process(f.capture())
            assert s.calls[-1]['options']['forward'].binding['hold_seconds']==1.0
            assert c.hunt.motion.heading is None
            f.name='Young Wolf';assert (await c.process(f.capture())).status=='dispatched'
            assert c.hunt.pending is None and c.hunt.motion.heading is None
            assert all(option.binding['type']=='observe_only' for option in s.calls[-1]['options'].values())
            assert (await c.process(f.capture())).status=='dispatched'
            c.level.last_confirmed_level=3;c.hunt.phase='recover'
            await c.process(f.capture())
            options=s.calls[-1]['options']
            assert {'recovered_resume','rest','ui_blocked','dead_or_unrecoverable','blocked_resources'}<=options.keys()
            assert 'recover_now' not in options and len(options)<=20
            assert all(len(call['options'])<=20 for call in s.calls)
        finally:await cleanup(e,store)
    asyncio.run(run())

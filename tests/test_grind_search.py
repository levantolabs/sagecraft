"""Finite search/travel traces using the production controller and fake inputs."""
import asyncio

from sage_wow.agent.grind_search import measure
from sage_wow.perception.ocr import TextObservation
from test_grind_only import setup, baseline, cleanup


def test_retained_area_visual_fallback_and_two_failed_tabs_change_sector_menu(tmp_path):
    async def scenario():
        area='cold_start_historical_patch_01'
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','choose_area:'+area,
            'begin_hunt','target_enemy','no_selected_frame','target_enemy','no_selected_frame','turn_left','motion_useful']);e.arm()
        try:
            await baseline(c,f);f.name='';c.hunt.planning_requested=True
            for _ in range(6):assert (await c.process(f.capture())).status=='dispatched'
            assert c.hunt.plan['area_id']==area and c.hunt.feedback['mode']=='visual'
            assert c.hunt.failures['target']==2
            assert 'target_enemy' not in s.calls[-1]['options']
            assert c.hunt.result()['search_failures']==2
            await c.process(f.capture())  # A turn receipt alone cannot clear failures.
            assert c.hunt.failures['target']==2
            await c.process(f.capture())  # Fresh Sage-confirmed changed sector.
            assert c.hunt.failures['target']==0 and c.hunt.sector==1
            assert c.hunt.result()['search_failures']==2
            assert all(2<=len(call['options'])<=20 for call in s.calls)
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_chat_closure_is_observed_and_repeated_escapes_hold_without_world_actions(tmp_path):
    async def scenario():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','close_visible_ui',
            'close_visible_ui','blocking_ui_remains','safe_hold','world_normal_confirmed']);e.arm()
        try:
            await baseline(c,f)
            # Closure now requires observed UI, not just a scripted model claim.
            from PIL import Image, ImageDraw
            visible=[True];original_capture=f.capture;original_ocr=f.ocr
            def capture():
                frame=original_capture()
                if visible[0]:
                    with Image.open(frame.image_path) as source:image=source.copy()
                    ImageDraw.Draw(image).text((10,250),'Chat input',fill='white')
                    image.save(frame.image_path)
                return frame
            def ocr(path):
                rows=original_ocr(path)
                if visible[0]:rows.append(TextObservation('Chat input',.99,
                    {'x':10,'y':250,'width':90,'height':15}))
                return rows
            f.capture=c.capture=capture;f.ocr=c.ocr=ocr
            for _ in range(6):await c.process(f.capture())
            assert c.hunt.input_effect_unverified and c.hunt.failures['ui']==2
            assert 'close_visible_ui' not in s.calls[-1]['options']
            assert not {'forward','target_enemy','attack_mob_level_1','heal_self'}&s.calls[-1]['options'].keys()
            assert c.cycle.input_generation>0 and not any(event[0]=='text' for event in b.events)
            assert c.hunt.blocked['reason']=='blocked_ui'
            c.hunt.blocked['next_observation_at']=0;c.hunt.blocked['assessment_due_at']=0
            s.choices.insert(0,'blocked_changed_assessment')
            visible[0]=False
            await c.process(f.capture());await c.process(f.capture())
            assert not c.hunt.input_effect_unverified and c.hunt.phase!='ui_recover'
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_travel_visible_opportunity_preserves_destination_and_same_name_new_encounter(tmp_path):
    async def scenario():
        area='cold_start_historical_patch_01'
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','choose_area:'+area,
            'begin_hunt','target_enemy','attack_mob_level_1','dead','target_enemy',
            'attack_mob_level_1','adopt_observed_patch']);e.arm()
        try:
            await baseline(c,f);f.name='';c.hunt.planning_requested=True
            await c.process(f.capture());await c.process(f.capture());await c.process(f.capture())
            assert c.hunt.suspended and c.hunt.plan['area_id']==area and c.hunt.phase=='search'
            f.name='Young Wolf';await c.process(f.capture())
            assert c.hunt.encounter==1 and c.hunt.plan['area_id']==area
            f.dead=True;assert (await c.process(f.capture())).status=='dispatched'
            # The still-selected observed corpse keeps normal Tab acquisition;
            # it must not create a new pre-cast approach or require Escape.
            assert (await c.process(f.capture())).status=='dispatched'
            assert c.hunt.pending['family']=='target' and 'target_enemy' in s.calls[-1]['options']
            assert c.hunt.phase=='search'
            f.dead=False
            assert (await c.process(f.capture())).status=='dispatched'
            assert c.hunt.encounter==2 and c.hunt.failures['combat']==0
            assert len([x for x in b.events if x[0]=='text'])==2
            assert c.hunt.result()['kill_hints']==0  # Dead proposal/reselection isn't a kill ledger.
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_repeated_motion_no_effect_and_interrupted_actions_keep_cumulative_evidence(tmp_path):
    async def scenario():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','explore_visible','begin_hunt',
            'forward','motion_no_useful_effect','forward','motion_useful','forward','motion_no_useful_effect','recover_now','recovered_resume']);e.arm()
        try:
            await baseline(c,f);f.name='';c.hunt.planning_requested=True;c.hunt.rejected_name='previous rejected unit'
            await c.process(f.capture())
            assert c.hunt.phase=='travel' and not c.hunt.planning_requested
            assert c.hunt.rejected_name=='previous rejected unit'
            await c.process(f.capture());assert c.hunt.phase=='search'
            for _ in range(6):await c.process(f.capture())
            assert c.hunt.failures['motion']==1 and c.hunt.result()['motion_failures']==2
            assert not c.stopped
            await c.process(f.capture())
            assert c.hunt.phase=='recover'
            await c.process(f.capture())
            assert c.hunt.phase=='search' and c.hunt.failures['motion']==1
            assert c.hunt.unassessed_count==0  # Every completed motion was explicitly reconciled.
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_same_area_reselection_and_outgrown_band_do_not_erase_learning(tmp_path):
    c,f,s,b,store,e=setup(tmp_path)
    try:
        hunt=c.hunt;first=next(iter(hunt.catalog.values()));other={**first,'id':'different_candidate'}
        hunt.choose(first,f.capture(),'r1',1);hunt.failures['target']=3;hunt.result()['search_failures']=3
        hunt.choose(other,f.capture(),'r2',1);hunt.choose(first,f.capture(),'r3',1)
        assert hunt.failures['target']==3 and hunt.result()['search_failures']==3
        hunt.result()['observed_levels']=[1];hunt.last_level=1;hunt.level_changed(4)
        assert hunt.phase=='choose_area' and hunt.result()['status']=='outgrown_observed_band'
        assert first['id'] not in [area['id'] for area in hunt.candidates(4)]
        assert first['expected_level_range'] is None and first['status']=='reference_unverified'
    finally:store.close()


def test_magnified_coordinate_and_zone_proposals_conflicts_and_quantization(tmp_path):
    c,f,s,b,store,e=setup(tmp_path)
    try:
        frame=f.capture()
        def row(text):return TextObservation(text,1,{'x':0,'y':0,'width':50,'height':10})
        def ocr(path):return [row('Coldridge Valley')] if path.name=='zone.png' else [row('29.9, 71.3')]
        measured=measure(c.profile,frame,[],ocr)
        assert measured['position']==[29.9,71.3] and measured['zone_proposals']==['Coldridge Valley']
        assert measured['crop_scale']==6 and measured['frame_id']==frame.frame_id
        hunt=c.hunt;area=next(iter(hunt.catalog.values()));hunt.choose(area,frame,'r',1)
        receipt={'completed':True,'receipt_id':'motion-r','generation_after':1,'occurred_at':frame.captured_at,
            'input_steps':[{'kind':'key_down','status':'completed','monotonic_at':1},{'kind':'key_up','status':'completed','monotonic_at':2}]}
        hunt.install('forward','motion',frame,receipt,measured)
        fresh=f.capture();feedback=hunt.feedback_for(fresh,1,{**measured,'frame_id':fresh.frame_id})
        assert feedback['displacement']==0 and feedback['heading'] is None
        assert feedback['mode']=='coordinate'
        conflict={**measured,'position':None,'position_status':'conflict'}
        assert hunt.feedback_for(f.capture(),1,conflict)['mode']=='visual'
        assert hunt.motion.heading is None
    finally:store.close()


def test_compact_travel_menu_retains_urgent_route_and_due_level_preempts_it(tmp_path):
    async def scenario():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','turn_left','player_level_1']);e.arm()
        try:
            await baseline(c,f);f.name=''
            area=next(iter(c.hunt.catalog.values()));c.hunt.choose(area,f.capture(),'travel',1)
            c.hunt.suspended=True;c.hunt.latest_patch={**area,'id':'observed_fixture'}
            assert (await c.process(f.capture())).status=='dispatched'
            options=s.calls[-1]['options']
            assert len(options)<=13
            assert {'detour_backward','turn_left','turn_right','begin_hunt','change_destination','urgent_state'}==options.keys()
            assert not {'ui_blocked','world_normal_confirmed','heal_self','attack_mob_level_1'}&options.keys()
            c.level.last_attempt_at=0
            assert (await c.process(f.capture())).status=='dispatched'
            options=s.calls[-1]['options']
            assert {'player_level_1','player_dead'}<=options.keys() and 'urgent_threat' not in options
            assert 'forward' not in options and c.hunt.plan['area_id']==area['id']
        finally:await cleanup(e,store)
    asyncio.run(scenario())

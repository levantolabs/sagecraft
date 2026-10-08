"""Never-combat selected sensing exits preserve old combat and physical debt."""
import asyncio
from copy import deepcopy

import pytest

from sage_wow.agent import grind_relocation as relocation
from test_grind_exact_band_observation import start_band
from test_grind_target_inspection_contract import ContractRig


def readable_world(r):
    from PIL import Image,ImageDraw
    from pathlib import Path
    from sage_wow.perception.ocr import TextObservation
    layout=r.p.values['calibration']['ui_layout']['regions']
    layout.update(minimap=[400,0,500,80],coordinate_primary=[400,35,495,60],
        coordinate_alternate=[395,30,500,65],coordinate_magnification=[400,35,495,60],local_caption=[250,0,395,25])
    base_capture=r.world.capture;base_ocr=r.c.ocr
    def row(text,x,y,w,h):return TextObservation(text,.99,{'x':x,'y':y,'width':w,'height':h})
    def capture():
        frame=base_capture()
        with Image.open(frame.image_path) as image:
            draw=ImageDraw.Draw(image);draw.text((260,5),'Offline Valley',fill='white');draw.text((405,40),'10.0, 10.0',fill='white');image.save(frame.image_path)
        return frame
    def ocr(path):
        if Path(path).name=='coordinates.png':return [row('10.0, 10.0',0,0,100,15)]
        if Path(path).name=='zone.png':return [row('Offline Valley',0,0,100,15)]
        return base_ocr(path)+[row('Offline Valley',260,5,100,15),row('10.0, 10.0',405,40,75,15)]
    r.world.capture=r.c.capture=capture;r.c.ocr=ocr


def clean_hud(r):
    from PIL import Image,ImageDraw
    from pathlib import Path
    layout=r.p.values['calibration']['ui_layout']['regions'];layout.update(minimap=[400,0,500,80],
        coordinate_primary=[400,35,495,60],coordinate_alternate=[395,30,500,65],
        coordinate_magnification=[400,35,495,60],local_caption=[250,0,395,25])
    r.p.values['controls']['bindings']['toggle_hud']={'keycodes':[58,6],'hold_seconds':.08,'verified_from':'offline fake hide/restore'}
    visible=True;pressed=set();frames={};base_capture=r.world.capture;base_key=r.backend.key;base_ocr=r.c.ocr
    def capture():
        frame=base_capture()
        with Image.open(frame.image_path) as image:
            draw=ImageDraw.Draw(image)
            if visible:
                draw.rectangle((0,0,149,25),fill='#e0e0e0');draw.text((40,5),'Test Player',fill='black')
                draw.rectangle((400,0,499,79),fill='#4477bb')
            else:
                for box in (layout['player_frame'],layout['minimap'],layout['target_overlay']):draw.rectangle(box,fill='#263746')
            image.save(frame.image_path)
        frames[Path(frame.image_path).name]=visible
        return frame
    def key(code,down):
        nonlocal visible
        if code==6 and not down and 58 in pressed:visible=not visible
        if down:pressed.add(code)
        else:pressed.discard(code)
        base_key(code,down)
    r.world.capture=r.c.capture=capture;r.backend.key=key
    r.c.ocr=lambda p:[] if frames.get(Path(p).name) is False else base_ocr(p)


async def historical_globals(r,monkeypatch,cues):
    from sage_wow.agent import grind_perception
    from sage_wow.agent.scene import Scene
    from test_grind_later_precast_feedback import FACING
    r.p.values['grind_only']['committed_combat']=True
    await start_band(r,4)
    values={'player_health':1.,'health_confidence':1.,'target_health':1.,'target_health_confidence':1.,'player_mana':1.}
    monkeypatch.setattr('sage_wow.agent.grind_resources.hud_resources',lambda c,f:{'frame_id':f.frame_id,**values})
    await r.observe_target()
    active=[]
    def scene(frame,**kwargs):
        value=Scene(frame.frame_id);value.target_name=r.world.name;value.target_alive=True
        value.target_health=value.health=1.;value.target_health_confidence=value.health_confidence=1.
        value.error_cues=deepcopy(active);value.error=active[0]['text'] if active else None
        return value
    monkeypatch.setattr(grind_perception,'read_hud_scene',scene)
    async def guard(binding,index):
        active[:]=[FACING,*cues] if index==1 else []
        try:return await grind_perception.observe_burst(r.c,binding,index)
        finally:active.clear()
    async def post(binding,index,phase):return await grind_perception.observe_post_cast(r.c,binding,index,phase)
    async def no_wait(seconds):pass
    r.c.config.update(smite_burst_count=3,cast_wait_seconds=1.5)
    r.executor.batch_guard=guard;r.executor.batch_post_guard=post;r.executor._batch_sleep=no_wait
    assert (await r.choose('attack_mob_level_2')).status=='dispatched'
    from sage_wow.agent.grind_cast_feedback import unresolved_cues
    assert unresolved_cues(r.c.hunt.pending)==[FACING,*cues],r.c.hunt.pending['receipt']['execution']
    await r.choose('position_error');await r.choose('reject_selected_target')
    r.world.name='';r.world.target_level=None;values['target_health']=None
    await r.choose('target_cleared')
    assert r.c.hunt.encounter_ended
    r.world.name='Burly Rockjaw Trogg';r.world.target_level=2;values['target_health']=1.
    return values


async def disposed(r, monkeypatch, *, clean=False, started=False, values=None):
    r.p.values['controls']['bindings'].update(strafe_left={'keycode':0,'verified_from':'offline fake calibration'},
        strafe_right={'keycode':2,'verified_from':'offline fake calibration'})
    r.p.values['grind_only']['committed_combat']=True
    if not started:await start_band(r,4)
    r.c.config['clean_world_observation']=clean
    values=values or {'player_health':1.,'health_confidence':1.,'target_health':1.,
        'target_health_confidence':1.,'player_mana':1.}
    per_frame={}
    def bars(c,frame):
        per_frame.setdefault(frame.frame_id,deepcopy(values))
        return {'frame_id':frame.frame_id,**per_frame[frame.frame_id]}
    monkeypatch.setattr('sage_wow.agent.grind_resources.hud_resources',bars)
    original=r.c.target_proposal
    async def target(frame):return {**await original(frame),'hud':bars(r.c,frame)}
    r.c.target_proposal=target
    r.answers['level']='unknown'
    assert (await r.observe_target()).status=='grind_reobserve'
    assert (await r.observe_target()).status=='grind_reobserve'
    assert (await r.choose('change_search_strategy')).status=='dispatched'
    h=r.c.hunt;intent=h.strategy_required['selected_exit']
    assert intent['kind']=='selected_sensing' and 'owner' not in intent
    assert (await r.choose('explore_visible')).status=='dispatched'
    assert h.phase=='travel' and h.plan['relocation_request_id']==intent['request_id']
    return values,intent


async def exhaust(r):
    for _ in range(8):
        result=await r.choose(None)
        if 'reject_selected_target' in r.sage.calls[-1]['options']:
            return result
    raise AssertionError((r.c.hunt.phase,r.c.hunt.compact_stage,r.sage.calls[-1]['options']))


def counters(h):
    return deepcopy({key:getattr(h,key) for key in ('failures','action_failures','unresolved_motion',
        'rejected_signatures','target_history','combat_history_key','encounter','encounter_ended',
        'cast_error','cast_obligation','retry_credit','correction_evidence','no_effect_retries',
        'correction_rounds','travel_failures','search_revision','last_progress_active_at','progress_facts')})


def test_selected_unknown_task_destination_survives_real_nulls_and_reaches_movement(tmp_path,monkeypatch):
    async def run():
        r=ContractRig(tmp_path/'nulls')
        try:
            _,intent=await disposed(r,monkeypatch);h=r.c.hunt
            attempts=len(r.wire);plan=deepcopy(h.plan)
            h.action_failures['old-combat-method']=2
            await r.choose(None);await r.choose(None)
            result=await r.choose('detour_backward_left')
            assert result.status=='dispatched' and result.receipt['possible_input']
            assert h.phase=='travel' and h.plan==plan and len(r.wire)==attempts
            assert h.strategy_required['selected_exit'] is intent
            assert h.action_failures['old-combat-method']==2 and not h.progress_facts
            assert 'change_search_strategy' not in r.sage.calls[-1]['options']
        finally:await r.close()
    asyncio.run(run())


def test_spent_travel_pages_offer_typed_clear_and_success_refunds_no_debt(tmp_path,monkeypatch):
    async def run():
        r=ContractRig(tmp_path/'clear')
        try:
            values,intent=await disposed(r,monkeypatch);h=r.c.hunt
            await exhaust(r)
            assert 'cannot_assess' not in r.sage.calls[-1]['options']
            assert 'change_search_strategy' not in r.sage.calls[-1]['options']
            h.failures['clear']=1;h.action_failures[h.action_key('reject','clear')]=1
            plan=deepcopy(h.plan);menu=deepcopy(h.travel_policy['retry_menu']);old=counters(h)
            assert (await r.choose('reject_selected_target')).status=='dispatched'
            pending=h.pending
            assert pending['purpose']=='selected_task_abandon' and 'intentional_clear_owner' not in pending
            assert counters(h)==old
            r.world.name='';r.world.target_level=None;values['target_health']=None
            assert (await r.choose('target_cleared')).status=='dispatched'
            assert intent['disposition']=='closed' and h.pending is None
            assert relocation.typed_release(h)
            assert counters(h)==old and h.plan==plan and h.travel_policy['retry_menu']==menu
            assert h.phase=='travel' and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('bad',['empty','wrong_episode','source_hash','plan','new_positive'])
def test_bad_typed_clear_never_closes_or_refunds(tmp_path,monkeypatch,bad):
    async def run():
        r=ContractRig(tmp_path/bad)
        try:
            values,intent=await disposed(r,monkeypatch);h=r.c.hunt
            await exhaust(r);await r.choose('reject_selected_target');p=h.pending
            h.failures['clear']=1;h.action_failures[h.action_key('reject','clear')]=1
            if bad=='empty':p['selected_task_abandon']={}
            elif bad=='wrong_episode':p['selected_task_abandon']['episode_id']='wrong'
            elif bad=='source_hash':p['source_hash']='wrong'
            elif bad=='plan':h.plan['request_id']='newer'
            if bad!='new_positive':r.world.name='';r.world.target_level=None;values['target_health']=None
            before=h.failures['clear']
            await r.choose('target_cleared')
            assert intent['disposition']!='closed' and not relocation.typed_release(h)
            assert h.failures['clear']>=before and h.action_failures[h.action_key('reject','clear')]>=1
            assert not h.progress_facts and not h.credited_kills and h.correction_evidence is None
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['name','number','dead','friendly'])
def test_real_new_positive_supersedes_before_any_travel_question(tmp_path,monkeypatch,change):
    async def run():
        r=ContractRig(tmp_path/change)
        try:
            _,intent=await disposed(r,monkeypatch);h=r.c.hunt
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            # These known source facts distinguish positive conflict from neutral
            # OCR availability; current proposal and selection_seen remain real.
            intent['target']['levels']=[2];intent['target']['life_state']='alive';intent['target']['target_kind']='creature'
            if change=='name':r.world.name='Ragged Timber Wolf'
            elif change=='number':r.drop_level=False;r.world.target_level=3
            else:
                original=r.c.target_proposal
                async def changed(frame):
                    value=await original(frame)
                    visual={**value.get('visual_observation',{}),'selected_hud':'present',
                        'life_state':'dead' if change=='dead' else 'alive',
                        'target_kind':'friendly_or_self' if change=='friendly' else 'creature'}
                    return {**value,'visual_observation':visual,'invalid_text':True,'self_target':change=='friendly'}
                r.c.target_proposal=changed
            before=len(r.sage.calls)
            exit_questions=len(r.events('grind_selected_task_exit_question'))
            result=await r.choose(None)
            assert intent.get('superseded') and not result.receipt.get('possible_input') if result.receipt else intent.get('superseded')
            assert h.phase=='search'
            assert len(r.events('grind_selected_task_exit_question'))==exit_questions
        finally:await r.close()
    asyncio.run(run())


def test_numeric_availability_and_known_travel_transport_preserve_disposition(tmp_path,monkeypatch):
    async def run():
        r=ContractRig(tmp_path/'neutral')
        try:
            _,intent=await disposed(r,monkeypatch);h=r.c.hunt
            r.drop_level=False
            frame=r.world.capture();target=await r.c.target_proposal(frame);h.selection_seen(target)
            assert relocation.selected_disposition(r.c,frame,target)['status']=='matching'
            r.drop_level=True
            frame=r.world.capture();target=await r.c.target_proposal(frame);h.selection_seen(target)
            assert relocation.selected_disposition(r.c,frame,target)['status']=='matching'
            result=await r.choose('turn_left')
            assert result.status=='dispatched' and result.receipt['possible_input'],r.sage.calls[-1]['options'].keys()
            await r.choose('motion_useful')
            await r.choose(None)
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            assert relocation.selected_disposition(r.c,frame,target)['status']=='matching'
            assert intent.get('transport_anchor')
        finally:await r.close()
    asyncio.run(run())


def test_unproved_generation_requires_fresh_explicit_resume(tmp_path,monkeypatch):
    async def run():
        r=ContractRig(tmp_path/'gap')
        try:
            _,intent=await disposed(r,monkeypatch);h=r.c.hunt
            r.c.cycle._input_generation+=1
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            assert relocation.selected_disposition(r.c,frame,target)['status']=='temporarily_unproven'
            old=deepcopy((h.plan,h.travel_policy,h.action_failures));calls=len(r.wire)
            await r.choose('resume_destination')
            assert r.sage.calls[-1]['options']['resume_destination'].binding['type']=='observe_only'
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            assert relocation.selected_disposition(r.c,frame,target)['status']=='matching'
            assert h.plan['request_id']==old[0]['request_id'] and len(r.wire)==calls
            assert h.travel_policy==old[1] and h.action_failures==old[2] and not h.progress_facts
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation',['config','plan','intent','late_bar'])
def test_typed_clear_guard_rechecks_after_provider_await(tmp_path,monkeypatch,mutation):
    async def run():
        r=ContractRig(tmp_path/mutation)
        try:
            values,intent=await disposed(r,monkeypatch);h=r.c.hunt;await exhaust(r)
            before=len(r.physical())
            async def change():
                if mutation=='config':r.c.config['cast_wait_seconds']+=.1
                elif mutation=='plan':h.plan['request_id']='newer'
                elif mutation=='intent':intent['episode_id']='changed'
                else:values['target_health']=None;r.world.name='';r.world.target_level=None
            r.sage.hook=change
            result=await r.choose('reject_selected_target')
            assert result.status!='dispatched' and len(r.physical())==before and h.pending is None
            assert intent['disposition']!='closed' and not h.progress_facts
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind,text',[('standing','You must be standing to cast'),('cooldown','Spell is not ready yet'),('interrupted','Interrupted')])
def test_real_old_global_then_selected_handoff_typed_clear_and_current_resolution(tmp_path,monkeypatch,kind,text):
    from sage_wow.agent.grind_cast_feedback import released_player_resolution
    async def run():
        r=ContractRig(tmp_path/kind)
        try:
            values=await historical_globals(r,monkeypatch,[{'kind':kind,'text':text}])
            values,intent=await disposed(r,monkeypatch,started=True,values=values);h=r.c.hunt
            await exhaust(r);old=counters(h);original_clear=deepcopy(h.intentional_clear_current())
            await r.choose('reject_selected_target')
            r.world.name='';r.world.target_level=None;values['target_health']=None
            await r.choose('target_cleared')
            assert counters(h)==old and h.intentional_clear_current()==original_clear
            await r.choose('retain_cast_error');assert h.cast_error['kind']==kind
            retained=deepcopy((h.action_failures,h.unresolved_motion,h.rejected_signatures,h.cast_obligation,h.recent_combat))
            if kind=='standing':
                await r.choose('stand_pulse');assert h.pending.get('released_error_stance')
                await r.choose('motion_useful')
            else:await r.choose('cast_ready')
            assert released_player_resolution(h) and not h.cast_blocks_acquisition()
            assert (h.action_failures,h.unresolved_motion,h.rejected_signatures,h.cast_obligation,h.recent_combat)==retained
            assert h.intentional_clear_current()==original_clear and not h.retry_credit and not h.progress_facts
            plan=deepcopy(h.plan);menu=deepcopy(h.travel_policy['retry_menu'])
            result=await r.choose('choose_area:coldridge_southeast_troggs')
            assert result.status=='dispatched' and not result.receipt['possible_input']
            assert h.phase=='travel' and h.plan['area']['id']=='coldridge_southeast_troggs'
            assert h.travel_policy['retry_menu']==menu and not h.progress_facts
        finally:await r.close()
    asyncio.run(run())


def test_failed_stance_retains_second_attempt_then_reaches_queued_global(tmp_path,monkeypatch):
    from sage_wow.agent.grind_cast_feedback import unresolved_nonlocal_cues,released_player_resolution
    async def run():
        r=ContractRig(tmp_path/'queued')
        try:
            standing={'kind':'standing','text':'You must be standing to cast'}
            cooldown={'kind':'cooldown','text':'Spell is not ready yet'}
            values=await historical_globals(r,monkeypatch,[standing,cooldown])
            values,intent=await disposed(r,monkeypatch,started=True,values=values);h=r.c.hunt
            await exhaust(r);await r.choose('reject_selected_target')
            r.world.name='';r.world.target_level=None;values['target_health']=None
            await r.choose('target_cleared');await r.choose('retain_cast_error')
            await r.choose('stand_pulse');await r.choose('motion_no_useful_effect')
            key=h.motion_key('forward','standing',h.combat_history_key)
            assert h.motion_count('forward','standing',h.combat_history_key)==1
            debt=deepcopy((h.action_failures,h.unresolved_motion,h.target_history[h.combat_history_key]['method_revision']))
            await r.choose('stand_pulse');assert h.pending.get('released_error_stance')
            await r.choose('motion_useful')
            assert released_player_resolution(h) and unresolved_nonlocal_cues(h)==[cooldown] and h.cast_blocks_acquisition()
            assert (h.action_failures,h.unresolved_motion,h.target_history[h.combat_history_key]['method_revision'])==debt
            await r.choose('retain_cast_error');assert h.cast_error['kind']=='cooldown'
            await r.choose('cast_ready');assert not h.cast_blocks_acquisition()
            assert h.action_failures.get(key,0)+h.unresolved_motion.get(key,0)==1
            assert h.motion_count('forward','standing',h.combat_history_key)==1
            assert not h.retry_credit and not h.progress_facts and intent['disposition']=='closed'
        finally:await r.close()
    asyncio.run(run())


def test_typed_clear_different_dispatch_scout_tab_withholds_new_sensing(tmp_path,monkeypatch):
    async def run():
        r=ContractRig(tmp_path/'same-name')
        try:
            readable_world(r)
            values,intent=await disposed(r,monkeypatch);h=r.c.hunt
            await exhaust(r);await r.choose('reject_selected_target')
            r.world.name='';r.world.target_level=None;values['target_health']=None
            await r.choose('target_cleared')
            empty=deepcopy(r.c.target_inspection_episode);assert empty['disposition']=='source_resolved_absent'
            planned=await r.choose('choose_area:coldridge_southeast_troggs')
            assert planned.status=='dispatched' and h.phase=='travel',(planned.status,planned.detail,h.phase,r.sage.calls[-1]['options'].keys())
            for _ in range(8):
                await r.choose(None)
                if 'target_enemy' in r.sage.calls[-1]['options']:break
            assert 'target_enemy' in r.sage.calls[-1]['options']
            result=await r.choose('target_enemy')
            assert result.status=='dispatched' and h.pending['family']=='target'
            receipt=h.pending['receipt'];count=len(r.wire)
            assert receipt['source_frame_id']!=receipt['dispatch_frame_id']
            assert 'sensing_boundary_witness' not in h.pending
            r.world.name='Burly Rockjaw Trogg';r.world.target_level=2;values['target_health']=1.
            r.answers.update(selected_hud='present',name='other_or_unknown',level='2',target_kind='creature',life_state='alive')
            r.c.observer_last_at=0
            await r.choose(None)
            assert len(r.wire)==count
            retained=r.c.target_inspection_episode
            assert retained['id']==empty['id'] and retained['attempts']==empty['attempts']
            assert 'sensing_proof' not in retained and intent['disposition']=='closed'
            assert not any(key.startswith('attack_') for key in r.sage.calls[-1]['options'])
        finally:await r.close()
    asyncio.run(run())


def test_unknown_gap_then_actual_completed_move_cannot_transport_disposition(tmp_path,monkeypatch):
    from sage_wow.agent.cycle import ActionCandidate
    async def run():
        r=ContractRig(tmp_path/'move-gap')
        try:
            _,intent=await disposed(r,monkeypatch);h=r.c.hunt
            r.c.cycle._input_generation+=1
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            old_source=intent['source']['input_generation']
            # A separately authenticated nonselection action begins AFTER the
            # unknown gap; its genuine receipt cannot prove the missing edge.
            binding={'type':'keypress','keycode':r.p.values['controls']['bindings']['turn_left']['keycode'],'hold_seconds':.01}
            from sage_wow.agent.cycle import TravelDecisionBudget
            from datetime import datetime
            started=datetime.fromisoformat(frame.captured_at).timestamp()
            r.c.travel_budget=TravelDecisionBudget(started,min(started+12,r.c.deadline))
            r.sage.answers.append('turn_left')
            result=await r.c.decide(frame,frame.image_path,'Offline transport negative','Choose turn',
                [ActionCandidate('turn_left','Known fake turn',binding,dispatch_guard=r.c.travel_guard(frame,target)),
                 ActionCandidate('wait','Observe this offline source',{'type':'observe_only'})])
            assert result.status=='dispatched' and result.receipt['possible_input'],result.detail
            h.install('turn_left','motion',frame,result.receipt,h.last_measurement,purpose='travel',target=target)
            after=r.world.capture();measurement={**h.last_measurement,'frame_id':after.frame_id,'captured_at':after.captured_at}
            h.resolve_travel(after,r.c.cycle.input_generation,measurement,r.c.cycle.session_epoch)
            assert h.recent_moves[-1]['input_completed'] and h.recent_moves[-1]['generation_before']>old_source
            target=await r.c.target_proposal(after)
            assert relocation.selected_disposition(r.c,after,target)['status']=='temporarily_unproven'
            assert not relocation.note_selected_transport(r.c,after,target)
        finally:await r.close()
    asyncio.run(run())


def test_selected_task_survives_real_clean_hide_restore_and_move(tmp_path,monkeypatch):
    async def run():
        r=ContractRig(tmp_path/'clean')
        try:
            clean_hud(r)
            _,intent=await disposed(r,monkeypatch,clean=True);source=intent['source']['input_generation']
            result=await r.choose('turn_left')
            assert result.status=='dispatched' and result.receipt['possible_input']
            assert r.c.observation_transaction['status']=='restored_verified'
            assert intent['transport_anchor']['input_generation']>source
            await r.choose('motion_useful')
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            assert relocation.selected_disposition(r.c,frame,target)['status']=='matching'
            assert not intent.get('superseded') and len(r.wire)==2
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('bad',['hash','epoch','frame','generation','timestamp'])
def test_typed_release_cached_proof_requires_exact_assessment_provenance(tmp_path,monkeypatch,bad):
    async def run():
        r=ContractRig(tmp_path/bad)
        try:
            values,intent=await disposed(r,monkeypatch);h=r.c.hunt
            await exhaust(r);await r.choose('reject_selected_target')
            r.world.name='';r.world.target_level=None;values['target_health']=None
            await r.choose('target_cleared')
            assert relocation.typed_release(h) and h.encounter==0 and h.encounter_ended
            proof=intent['assessment']
            if bad=='hash':proof['sha256']='wrong'
            elif bad=='epoch':proof['scope']['session_epoch']='wrong'
            elif bad=='frame':proof['receipt']['source_frame_id']='wrong'
            elif bad=='generation':proof['receipt']['generation_after']+=1
            else:proof['captured_at']='2020-01-01T00:00:00+00:00'
            before=deepcopy(intent)
            assert relocation.typed_release(h) is None and intent==before
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('choice,phase',[('ui_blocked','ui_recover'),('recover_now','recover')])
def test_marked_clear_consumes_unknown_and_routes_accepted_stronger_task(tmp_path,monkeypatch,choice,phase):
    async def run():
        r=ContractRig(tmp_path/choice)
        try:
            _,intent=await disposed(r,monkeypatch);h=r.c.hunt
            await exhaust(r);await r.choose('reject_selected_target')
            original=h.pending['receipt']['receipt_id'];calls=len(r.sage.calls)
            r.c.wait_until=0;continuation=await r.c.process(r.world.capture())
            assert continuation.status=='dispatched' and continuation.decision is None
            assert len(r.sage.calls)==calls and continuation.receipt['authorization_id']==original
            assert h.pending['clear_pair']['original']['receipt']['receipt_id']==original
            result=await r.choose(choice)
            assert result.status=='dispatched' and h.phase==phase and h.pending is None
            assert intent['disposition']=='assessment_exhausted' and not relocation.typed_release(h)
            assert h.failures['clear']==1 and not h.progress_facts and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('visual',[False,True])
def test_fresh_clear_absence_is_bookkept_once_even_when_selection_seen_already_saw_it(tmp_path,monkeypatch,visual):
    async def run():
        r=ContractRig(tmp_path/str(visual))
        try:
            values,intent=await disposed(r,monkeypatch);h=r.c.hunt
            await exhaust(r);await r.choose('reject_selected_target');pending=h.pending
            r.world.name='';r.world.target_level=None;values['target_health']=None
            if visual:
                original=r.c.target_proposal
                async def absent(frame):
                    target=await original(frame)
                    return {**target,'visual_observation':{'selected_hud':'absent'},
                        'visual_provenance':{'continuity_frame_id':frame.frame_id}}
                r.c.target_proposal=absent
            prior=(h.target_continuity,h.selection_revision)
            frame=r.world.capture();result=await r.choose('target_cleared',frame=frame)
            assert (h.target_continuity,h.selection_revision)==(prior[0]+1,prior[1]+1)
            target=await r.c.target_proposal(frame);now=(h.target_continuity,h.selection_revision)
            assert not relocation.close_selected_task(r.c,frame,target,pending,result)
            assert (h.target_continuity,h.selection_revision)==now and intent['disposition']=='closed'
        finally:await r.close()
    asyncio.run(run())


def test_unassessed_stance_does_not_launder_release_generation(tmp_path,monkeypatch):
    from sage_wow.agent.grind_cast_feedback import _released_selection_current
    async def run():
        r=ContractRig(tmp_path/'unassessed')
        try:
            values=await historical_globals(r,monkeypatch,[{'kind':'standing','text':'You must be standing to cast'}])
            values,intent=await disposed(r,monkeypatch,started=True,values=values);h=r.c.hunt
            await exhaust(r);await r.choose('reject_selected_target')
            r.world.name='';r.world.target_level=None;values['target_health']=None
            await r.choose('target_cleared');await r.choose('retain_cast_error');await r.choose('stand_pulse')
            h.archive_pending('offline known input with unassessed pose')
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            assert not _released_selection_current(r.c,frame,target) and not intent.get('own_state_transport')
            assert h.cast_blocks_acquisition() and not h.retry_credit and not h.progress_facts
        finally:await r.close()
    asyncio.run(run())

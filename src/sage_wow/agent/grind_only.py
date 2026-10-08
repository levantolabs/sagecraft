"""Isolated Sage-owned mob grinding. No quest/task/service graph is imported."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time

from PIL import Image, ImageChops, ImageStat, ImageDraw

from sage_wow.agent.cycle import ActionCandidate, CycleResult, DecisionCycle, DispatchValidation, TravelDecisionBudget
from sage_wow.agent.level_verification import (PlayerLevelVerifier, player_level_assessment_image)
from sage_wow.agent.ui_layout import UIRegions, from_profile, validate_frame
from sage_wow.models import Event
from sage_wow.perception.ocr import recognize_text
from sage_wow.agent.grind_search import HuntState, load_catalog, measure, travel_image, ui_evidence
from sage_wow.agent.grind_outcomes import evidence_signature
from sage_wow.agent.grind_observation import CleanWorldObservation, read_player_identity


def key(text):
    return ' '.join(re.sub(r'[^\w\s]','',str(text)).casefold().split())


def settings(profile):
    cfg=profile.values.get('grind_only') or {}
    if cfg.get('enabled') is not True or not isinstance(cfg.get('session_id'),str) or not cfg['session_id'].strip():
        raise ValueError('grind_only.enabled and a fresh session_id are required')
    limits={'duration_seconds':(7200,1,28800),'level_check_seconds':(60,15,300),
        'move_seconds':(1.2,.05,2.5),'turn_seconds':(.35,.05,.8),'cast_wait_seconds':(2.2,.1,4)}
    result=dict(cfg)
    for name in ('loot_enabled','encounter_resources','preserve_target_heal','committed_combat','combat_log_enabled','target_opening_cast','skip_loot_when_inventory_full'):
        if type(cfg.get(name,False)) is not bool:raise ValueError(f'grind_only.{name} must be boolean')
        result[name]=cfg.get(name,False)
    evidence=cfg.get('opening_creature_evidence',[])
    if (not isinstance(evidence,list) or len(evidence)>128
        or any(not isinstance(line,str) or not line.strip() for line in evidence)):
        raise ValueError('opening_creature_evidence must contain at most128 native combat log records')
    result['opening_creature_evidence']=list(evidence)
    if result['combat_log_enabled']:
        install = Path(profile.values.get('client',{}).get('installation_path',''))
        if not install.is_absolute():
            raise ValueError('Client installation path required for native combat logs')
        result['combat_log_path'] = str(install.parent / 'Logs' / 'WoWCombatLog.txt')
    count=cfg.get('smite_burst_count',1)
    if type(count) is not int or count not in (1,2,3):raise ValueError('smite_burst_count must be 1, 2 or 3')
    result['smite_burst_count']=count
    health=cfg.get('heal_health_fraction',.30)
    if type(health) not in (int,float) or not .15<=health<=.4:raise ValueError('heal_health_fraction must be between .15 and .4')
    result['heal_health_fraction']=float(health)
    result['critical_health_threshold']=float(health)
    goal=cfg.get('goal_level',3)
    if type(goal) is not int or not 2<=goal<=10:
        raise ValueError('grind_only.goal_level must be an integer from 2 to 10')
    result['goal_level']=goal
    from sage_wow.agent.grind_loot_budget import validate as validate_loot_budget
    result['optional_loot_budget']=validate_loot_budget(profile,result)
    band=cfg.get('target_level_delta',0)
    if type(band) is not int or not 0<=band<=1:
        raise ValueError('grind_only.target_level_delta must be 0 or 1')
    result['target_level_delta']=band
    from sage_wow.agent.grind_progression import validate as validate_progression
    result['hunting_progression_by_player_level']=validate_progression(
        cfg.get('hunting_progression_by_player_level',{}),load_catalog(profile))
    fixed_area=cfg.get('fixed_hunting_area')
    if fixed_area is not None and (not isinstance(fixed_area,str) or fixed_area not in load_catalog(profile)):
        raise ValueError('fixed_hunting_area must name a catalog area')
    for policy in ('target_bands_by_player_level','preferred_target_bands_by_player_level'):
        overrides=cfg.get(policy,{})
        if not isinstance(overrides,dict):raise ValueError(policy+' must be a mapping')
        normalized={}
        for own,levels in overrides.items():
            if (type(own) not in (int,str) or not re.fullmatch(r'[1-9]|10',str(own))
                or int(own) in normalized or not isinstance(levels,list) or len(levels)!=2
                or any(type(n) is not int for n in levels) or not 1<=levels[0]<=levels[1]<=10):
                raise ValueError(policy+' requires player levels 1–10 and ordered integer target ranges 1–10')
            normalized[int(own)]=list(levels)
        result[policy]=normalized
    for own,preferred in result['preferred_target_bands_by_player_level'].items():
        allowed=result['target_bands_by_player_level'].get(own,[max(1,own-2),own+band])
        if not allowed[0]<=preferred[0]<=preferred[1]<=allowed[1]:
            raise ValueError('preferred_target_bands_by_player_level must be within the allowed target band')
    names=cfg.get('target_names',[])
    if not isinstance(names,list) or any(not isinstance(name,str) or not name.strip() for name in names):
        raise ValueError('grind_only.target_names must contain literal creature names')
    observer=cfg.get('selected_target_observer') or {}
    if observer:
        if (not isinstance(observer,dict) or type(observer.get('enabled')) is not bool
            or observer.get('backend')!='sage'
            or not 1<=observer.get('timeout_seconds',12)<=20):
            raise ValueError("selected_target_observer requires enabled, backend 'sage' and bounded timeout")
    continuation=cfg.get('campaign_continuation')
    if continuation is not None:
        if not isinstance(continuation,dict) or set(continuation)!={'character','verified_level','evidence_path'}:
            raise ValueError('campaign_continuation requires character, verified_level and evidence_path')
        if type(continuation['verified_level']) is not int or not 1<=continuation['verified_level']<goal:
            raise ValueError('campaign_continuation level must precede the goal')
        evidence_path=Path(continuation['evidence_path'])
        if not evidence_path.is_file():raise ValueError('campaign_continuation evidence is missing')
        evidence=json.loads(evidence_path.read_text())
        if (evidence.get('character')!=continuation['character'] or
            evidence.get('verified_level')!=continuation['verified_level'] or
            not evidence.get('level_observation_frame_id') or not evidence.get('source_session_id')):
            raise ValueError('campaign_continuation evidence does not match the claimed progress')
        result['campaign_continuation']={**continuation,'evidence':evidence}
    from sage_wow.agent.grind_inventory import validate_policy
    seed=cfg.get('inventory_full_seed')
    if seed is not None:
        if not result['skip_loot_when_inventory_full']:raise ValueError('Inventory-full seed requires the goal policy enabled')
        result['inventory_full_seed']=validate_policy(profile,result,seed)
    carried=((result.get('campaign_continuation') or {}).get('evidence') or {}).get('loot_policy')
    if carried and result['skip_loot_when_inventory_full']:validate_policy(profile,result,carried)
    for name,(default,low,high) in limits.items():
        value=cfg.get(name,default)
        if type(value) not in (int,float) or not low<=value<=high:raise ValueError(f'Invalid grind_only.{name}')
        result[name]=float(value)
    if count>1 and not 1.5<=result['cast_wait_seconds']<=3:
        raise ValueError('Smite bursts require cast_wait_seconds between 1.5 and 3')
    layout=from_profile(profile)
    if not layout or not {'player_frame','target_overlay'}<=layout['regions'].keys():
        raise ValueError('Calibrate player_frame and target_overlay including the target level badge')
    mana_box=cfg.get('player_mana_box')
    if mana_box is not None and (not isinstance(mana_box,list) or len(mana_box)!=4
        or any(type(value) is not int for value in mana_box)
        or not 0<=mana_box[0]<mana_box[2]<=layout['image_width']
        or not 0<=mana_box[1]<mana_box[3]<=layout['image_height']):
        raise ValueError('player_mana_box must be a calibrated in-image rectangle')
    badge=cfg.get('target_level_box')
    target=layout['regions']['target_overlay']
    name_box=cfg.get('target_name_box')
    if name_box is not None and (not isinstance(name_box,list) or len(name_box)!=4
        or any(type(value) is not int for value in name_box)
        or not target[0]<=name_box[0]<name_box[2]<=target[2]
        or not target[1]<=name_box[1]<name_box[3]<=target[3]):
        raise ValueError('target_name_box must be calibrated inside target_overlay')
    if (not isinstance(badge,list) or len(badge)!=4 or any(type(v) is not int for v in badge)
        or not target[0]<=badge[0]<badge[2]<=target[2] or not target[1]<=badge[1]<badge[3]<=target[3]):
        raise ValueError('grind_only.target_level_box must isolate the badge inside target_overlay')
    if 'target_badge_continuity' in cfg:
        interior=cfg['target_badge_continuity']
        fields={'mode','inset_pixels','image_width','image_height','badge_box','ui_layout_id','verified_from'}
        if (not isinstance(interior,dict) or set(interior)!=fields
            or interior['mode']!='opaque_interior_ellipse'
            or type(interior['inset_pixels']) is not int or interior['inset_pixels']!=5
            or type(interior['image_width']) is not int or interior['image_width']!=1496
            or type(interior['image_height']) is not int or interior['image_height']!=967
            or not isinstance(interior['badge_box'],list)
            or any(type(v) is not int for v in interior['badge_box'])
            or interior['badge_box']!=[425,577,467,619] or interior['badge_box']!=badge
            or (layout['image_width'],layout['image_height'])!=(1496,967)
            or interior['ui_layout_id']!=layout['id']
            or not isinstance(interior['verified_from'],str) or not interior['verified_from'].strip()):
            raise ValueError('grind_only.target_badge_continuity requires the reviewed 42x42 inset-5 target calibration and matching UI layout')
        result['target_badge_continuity']={**interior,'badge_box':list(interior['badge_box'])}
    own=cfg.get('player_level_box');player=layout['regions']['player_frame']
    if (not isinstance(own,list) or len(own)!=4 or any(type(v) is not int for v in own)
        or not player[0]<=own[0]<own[2]<=player[2] or not player[1]<=own[1]<own[3]<=player[3]):
        raise ValueError('grind_only.player_level_box must isolate the own badge inside player_frame')
    controls=profile.values.get('controls',{}).get('bindings',{})
    if result['loot_enabled']:
        interaction=controls.get('interact_target') or {}
        if type(interaction.get('keycode')) is not int or not interaction.get('verified_from'):
            raise ValueError('Looting requires a verified interact_target binding')
    for name in ('target_enemy','forward','backward','turn_left','turn_right','smite'):
        binding=controls.get(name) or {}
        if type(binding.get('keycode')) is not int or not binding.get('verified_from'):
            raise ValueError(f'Verified {name} binding required; no spellbook prerequisite')
    clean=cfg.get('clean_world_observation',True)
    if type(clean) is not bool:raise ValueError('grind_only.clean_world_observation must be boolean')
    result['clean_world_observation']=clean
    age=cfg.get('clean_world_max_age_seconds',8)
    if type(age) not in (int,float) or not 1<=age<=10:raise ValueError('Invalid clean-world evidence age')
    result['clean_world_max_age_seconds']=float(age)
    if clean:
        toggle=controls.get('toggle_hud') or {}
        if toggle.get('keycodes')!=[58,6] or toggle.get('hold_seconds')!=.08 or not toggle.get('verified_from'):
            raise ValueError('Clean travel requires the attributed verified toggle_hud [58,6], hold .08')
        if not {'player_frame','player_health','minimap'}<=layout['regions'].keys():
            raise ValueError('Clean travel requires calibrated player_frame/player_health/minimap')
    character=profile.values.get('character',{})
    if not character.get('name'):raise ValueError('Current player name reference is required for the self-target veto')
    return result


def region_rows(rows,box):
    left,top,right,bottom=box
    return [r for r in rows if r.confidence>=.75 and left<=r.bounds['x'] and top<=r.bounds['y']
        and r.bounds['x']+r.bounds['width']<=right and r.bounds['y']+r.bounds['height']<=bottom]


def proposal(profile,cfg,frame,rows):
    layout=UIRegions(frame,(frame.width,frame.height),from_profile(profile))
    box=layout.pixels('target_overlay',(0,0,1,1));badge=tuple(cfg['target_level_box'])
    local=region_rows(rows,box)
    names=[r for r in local if re.search(r'[A-Za-z]',r.text) and not re.search(r'\d|%|/',r.text)
        and key(r.text) not in {'dead','corpse','elite','rare','level','mana','health'}]
    # Multiple lines/ambiguous target text are not permission to guess a name.
    name=names[0].text.strip() if len(names)==1 else None
    levels={int(r.text.strip()) for r in region_rows(local,badge) if re.fullmatch(r'\d{1,2}',r.text.strip())}
    character=profile.values['character'];player=' '.join(str(character.get(k) or '') for k in ('name','surname')).strip()
    player_rows=region_rows(rows,layout.pixels('player_frame',(0,0,1,1)))
    self_names={key(player),key(character['name']),*(key(r.text) for r in player_rows if re.search(r'[A-Za-z]',r.text))}
    self_target=bool(name and key(name) in self_names)
    invalid=any(key(r.text) in {'dead','corpse','elite','rare'} for r in local)
    return {'name':name,'name_row':names[0] if name else None,'levels':sorted(levels),
        'self_target':self_target,'invalid_text':invalid,'box':box,'badge':badge}


def evidence_image(frame,box,path,*,alternate=False,player_badge=None,player_hud=None,crop_label="SELECTED TARGET",omit_target=False):
    """Whole world plus a same-frame enlarged selected-target overlay."""
    with Image.open(frame.image_path) as raw:
        world=raw.convert('RGB')
        if omit_target and not player_hud:
            world.save(path);return path
        crop=world.crop(box)
        crop.thumbnail((1000,380))
        if crop.width<700:crop=crop.resize((crop.width*(3 if alternate else 2),crop.height*(3 if alternate else 2)))
        scale=1 if crop_label=='FULL WORLD UI EVIDENCE' else min(1,1100/world.width);world=world.resize((round(world.width*scale),round(world.height*scale)))
        own_box=player_hud or player_badge
        own=raw.convert('RGB').crop(own_box) if own_box else None
        if own:
            factor=2 if player_hud else 4
            own=own.resize((own.width*factor,own.height*factor))
        canvas=Image.new('RGB',(max(world.width,crop.width+(own.width+24 if own else 0)),world.height+max(crop.height,own.height if own else 0)+36),'#151b23')
        canvas.paste(world,(0,0));canvas.paste(crop,(0,world.height+36))
        draw=ImageDraw.Draw(canvas);draw.text((0,world.height+8),crop_label,fill='white')
        if own:
            canvas.paste(own,(crop.width+24,world.height+36));draw.text((crop.width+24,world.height+8),'MY HEALTH / MANA / LEVEL' if player_hud else 'MY LEVEL',fill='white')
        canvas.save(path)
    return path


class RetainedGrindSourceChanged(RuntimeError):
    pass


def composed_evidence_image(frame,box,path,*,history=(),**options):
    """Pure image/file work, including linked BEFORE panels, for a worker."""
    try:
        image=evidence_image(frame,box,path,**options)
        for before_path,expected_hash,label,gap in history:
            if expected_hash and hashlib.sha256(Path(before_path).read_bytes()).hexdigest()!=expected_hash:
                raise RetainedGrindSourceChanged('retained_grind_source_changed')
            with Image.open(image) as current,Image.open(before_path) as previous:
                current=current.convert('RGB');previous=previous.convert('RGB');previous.thumbnail((1100,800))
                paired=Image.new('RGB',(max(current.width,previous.width),current.height+previous.height+gap),'#151b23')
                paired.paste(current,(0,0));paired.paste(previous,(0,current.height+gap))
                ImageDraw.Draw(paired).text((8,current.height+8),label,fill='white');paired.save(image)
        return image
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def same_patch(original,fresh,box,*,badge=False):
    with Image.open(original) as a,Image.open(fresh) as b:
        a=a.convert('RGB').crop(box);b=b.convert('RGB').crop(box)
        if a.size!=b.size:return False
        diff=ImageChops.difference(a,b)
        mean=sum(ImageStat.Stat(diff).mean)/3
        changed=sum(1 for pixel in diff.get_flattened_data() if max(pixel)>24)/max(1,a.width*a.height)
        return (mean<=.5 and not any(max(pixel)>12 for pixel in diff.get_flattened_data())) if badge else mean<=2 and changed<=.04


def same_target_badge(original,fresh,box,calibration=None,*,combat_glow=False):
    """Continuity only: an attributed interior excludes scenery, never reads a level."""
    if calibration is None:return same_patch(original,fresh,box,badge=True)
    if tuple(box)!=tuple(calibration['badge_box']):return False
    with Image.open(original) as a,Image.open(fresh) as b:
        size=(calibration['image_width'],calibration['image_height'])
        if a.size!=size or b.size!=size:return False
        a=a.convert('RGB').crop(box);b=b.convert('RGB').crop(box)
        cx,cy=(a.width-1)/2,(a.height-1)/2
        rx,ry=cx-calibration['inset_pixels'],cy-calibration['inset_pixels']
        if rx<=0 or ry<=0:return False
        diff=ImageChops.difference(a,b)
        pixels=[pixel for index,pixel in enumerate(diff.get_flattened_data())
            if ((index%a.width-cx)/rx)**2+((index//a.width-cy)/ry)**2<=1]
        strict=bool(pixels) and sum(sum(pixel) for pixel in pixels)/(3*len(pixels))<=.5 and all(max(pixel)<=12 for pixel in pixels)
    if strict:return True
    if not combat_glow:return False
    from sage_wow.agent.grind_perception import same_badge_numeral
    return same_badge_numeral(original,fresh,box,calibration)


class GrindController:
    def __init__(self,profile,store,sage,executor,*,capture,ocr=recognize_text,archive=None,target_observer=None):
        self.profile,self.store,self.executor=profile,store,executor
        self.config=settings(profile);self.capture,self.ocr=capture,ocr
        self.cycle=DecisionCycle(sage,executor,store,tactical_max_age_seconds=10)
        self.cycle.evidence_archive=archive;self.archive=archive
        self.task_id='grind:'+self.config['session_id'];self.revision=1
        self.started_at=time.time();self.deadline=self.started_at+self.config['duration_seconds']
        self.level=PlayerLevelVerifier(self.config['level_check_seconds'],required_readings=1);self.level.activate_session(self.cycle.session_epoch)
        self.goal_frame_ids=[]
        self.baseline=False;self.stopped=False;self.success=False;self.reason=None
        self.world_resume_phase=None;self.baseline_wait_signature=None
        self.last_ui_presence=None
        self.identity_mismatch_frames=set()
        self.history=[];self.streak=0;self.last_input_at=0.;self.last_frame_id=None
        self.wait_until=0.;self.attack_fingerprint=None;self.same_casts=0;self.alternate_used=False
        self.hunt=HuntState(load_catalog(profile),self.config['session_id']);self.paused=False;self.require_world=True
        self.hunt.target_level_delta=self.config['target_level_delta']
        self.hunt.target_bands_by_player_level=deepcopy(self.config['target_bands_by_player_level'])
        self.hunt.preferred_target_bands_by_player_level=deepcopy(self.config['preferred_target_bands_by_player_level'])
        self.hunt.hunting_progression_by_player_level=deepcopy(self.config['hunting_progression_by_player_level'])
        self.hunt.fixed_hunting_area=self.config.get('fixed_hunting_area')
        self.observation_transaction=None;self.observation=CleanWorldObservation(self)
        self.hud_no_effect_count=0
        self.travel_budget=None;self._analysis_cache=None
        self.provider_failures=0;self.null_answers=0;self.last_signature=None;self.loading_started=None
        self.timing_failures=0
        self.target_observer=target_observer;self.target_observation=None;self.observer_last_at=0.
        self.target_inspection_episode=None
        self.current_hud={};self.heal_pending=None;self.resource_defer_until=0.
        self.target_opener=None
        self.clear_continuation=None
        from sage_wow.agent.grind_acquisition import load_creatures
        load_creatures(self)
        if self.config['smite_burst_count']>1:
            from sage_wow.agent.grind_perception import observe_burst
            executor.batch_guard=lambda binding,index:observe_burst(self,binding,index)
            executor.batch_step_callback=lambda payload:self.event('cast_burst_step',payload)
            if self.config.get('committed_combat',False):
                from sage_wow.agent.grind_perception import observe_post_cast
                executor.batch_post_guard=lambda binding,index,phase:observe_post_cast(self,binding,index,phase)

    def event(self,name,payload):self.store.append(Event.create(name,payload))

    def stage(self,name):
        label=getattr(self.executor,'set_phase',None)
        if label:label(name)

    async def prepare_image(self,frame,render,*args,**kwargs):
        """Keep image compression off the input supervisor's event loop."""
        if self.config['encounter_resources'] and render in (evidence_image,composed_evidence_image):
            kwargs.setdefault('player_hud',tuple(from_profile(self.profile)['regions']['player_frame']))
        authority=(self.revision,self.cycle.session_epoch,self.cycle.input_generation)
        started=time.monotonic();self.stage('decision_image_composition')
        work=asyncio.create_task(asyncio.to_thread(render,*args,**kwargs))
        try:
            image=await asyncio.shield(work)
        except asyncio.CancelledError:
            # A running worker cannot be cancelled. Remove its derived output
            # once it finishes; it never receives gameplay or SQLite authority.
            def discard(done):
                try:Path(done.result()).unlink(missing_ok=True)
                except (Exception,asyncio.CancelledError):pass
            work.add_done_callback(discard)
            raise
        except RetainedGrindSourceChanged:
            self.stop('retained_grind_source_changed')
            return CycleResult('grind_stopped',detail=self.reason)
        self.event('grind_image_composition',{'frame_id':frame.frame_id,
            'renderer':getattr(render,'__name__',type(render).__name__),
            'elapsed_seconds':time.monotonic()-started})
        if self.travel_budget and self.travel_budget.remaining()<=0:
            Path(image).unlink(missing_ok=True)
            return self.timing_expired('image_composition')
        error=self.cycle._scope_error(frame)
        if authority!=(self.revision,self.cycle.session_epoch,self.cycle.input_generation):
            error='Grinding authority changed during image composition'
        elif not self.fresh(frame,travel=self.travel_budget is not None):
            error='Grinding source expired or session paused during image composition'
        elif not self.executor.armed or not self.executor.inspect_gate().valid:
            error='Grinding input ownership/focus gate invalid after image composition'
        if error:
            Path(image).unlink(missing_ok=True)
            return self.cycle._scope_rejected(frame,'image_composition',error)
        return image

    def flush_outcomes(self):
        for outcome in self.hunt.outcome_events:
            self.event('grind_action_outcome',{**outcome,'session_epoch':self.cycle.session_epoch,'request_id':None})
        self.hunt.outcome_events.clear()

    def current(self):return not self.stopped and not self.paused and time.time()<self.deadline

    def stop(self,reason):
        from sage_wow.agent.grind_loot_budget import suspend
        suspend(self)
        self.stopped=True;self.reason=reason;self.revision+=1;self.cycle.invalidate(reason)

    def pause_focus(self,reason='focus_lost',*,defer_input_reconciliation=False):
        from sage_wow.agent.grind_loot_budget import suspend
        suspend(self)
        receipt=getattr(self.cycle,'_active_receipt',None) or self.cycle.last_receipt or {}
        if not defer_input_reconciliation and receipt.get('possible_input') and (not receipt.get('completed') or receipt.get('dispatch_unknown') or receipt.get('error')):
            self.stop('partial_or_unknown_grind_input');return
        from sage_wow.agent.grind_inspection import suspend_focus
        suspend_focus(self,reconciled=not defer_input_reconciliation)
        from sage_wow.agent.grind_resources import finish_heal_observation
        finish_heal_observation(self,reason='focus_interruption')
        from sage_wow.agent.grind_travel_scout import restore_phase
        self.world_resume_phase=restore_phase(self,self.hunt.prior_phase if self.hunt.phase in {'ui_recover','input_effect_unverified'} else self.hunt.phase)
        self.paused=True;self.revision+=1;self.cycle.invalidate(reason)
        self.target_opener=None
        self.heal_pending=None;self.resource_defer_until=0.
        self.hunt.hunt_arrival=None
        self.hunt.invalidate_travel_mapping('focus interruption');self.hunt.archive_pending('focus_interruption');self.hunt.motion.reset_heading();self.hunt.active_tick=None;self.hunt.correction_evidence=None;self.hunt.retry_credit=False;self.hunt.rest_before=None;self.flush_outcomes()
        self.level.session_epoch=self.cycle.session_epoch;self.level.pending_level=None;self.level.pending_level_frame_ids=[]
        self.last_frame_id=None;self.last_input_at=time.time()
        self.event('grind_focus_paused',{'reason':reason,'hunt_plan':self.hunt.plan,'deadline':self.deadline})

    def resume_focus(self):
        if self.stopped or time.time()>=self.deadline:return False
        self.paused=False;self.revision+=1;self.cycle.invalidate('clean_focus_resume')
        self.level.session_epoch=self.cycle.session_epoch;self.level.pending_level=None;self.level.pending_level_frame_ids=[]
        self.require_world=True;self.last_frame_id=None;self.last_input_at=time.time()
        self.hunt.motion.reset_heading();self.hunt.blocked=None
        self.event('grind_focus_resume_pending_evidence',{'baseline_retained':self.baseline,'deadline':self.deadline})
        return True

    async def rows(self,frame):
        self.stage('grind_ocr')
        if self._analysis_cache is None:
            return await asyncio.to_thread(self.ocr,Path(frame.image_path))
        identity=(frame.frame_id,frame.source,frame.captured_at,frame.width,frame.height,frame.image_path)
        digest=hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()
        cached=self._analysis_cache.get(identity)
        if cached:
            if cached['digest']!=digest:raise ValueError('Immutable OCR source hash changed')
            return cached['rows']
        rows=await asyncio.to_thread(self.ocr,Path(frame.image_path))
        if hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()!=digest:
            raise ValueError('Immutable OCR source hash changed during analysis')
        self._analysis_cache[identity]={'digest':digest,'rows':rows,
            'proposal':proposal(self.profile,self.config,frame,rows)}
        return rows

    async def raw_target_proposal(self,frame):
        rows=await self.rows(frame)
        identity=(frame.frame_id,frame.source,frame.captured_at,frame.width,frame.height,frame.image_path)
        cached=self._analysis_cache[identity] if self._analysis_cache is not None else None
        if cached is not None and 'focused_proposal' in cached:return cached['focused_proposal']
        raw=cached['proposal'] if cached is not None else proposal(self.profile,self.config,frame,rows)
        from sage_wow.agent.grind_target_ocr import reread
        result=await asyncio.to_thread(reread,self.profile,self.config,frame,rows,raw,self.ocr)
        if cached is not None:cached['focused_proposal']=result
        if result.get('focused_ocr'):self.event('grind_target_ocr_reread',result['focused_ocr'])
        return result

    async def target_proposal(self,frame):
        raw=await self.raw_target_proposal(frame)
        from sage_wow.agent.grind_perception import enrich
        result=enrich(self,frame,raw)
        if self.config.get('target_opening_cast',False):
            from sage_wow.agent.grind_acquisition import load_creatures, local_target
            from sage_wow.agent.grind_resources import hud_resources
            if not hasattr(self,'opening_creatures'):load_creatures(self)
            # Current HUD facts can resolve old inspection uncertainty. A fresh
            # explicit conflicting type/life observation remains a veto.
            local=local_target(self,frame,result,hud_resources(self,frame))
            if local:result=local
        names=self.config.get('target_names',[])
        if names and result['name'] and key(result['name']) not in {key(name) for name in names}:
            result={**result,'eligibility':'ineligible','target_policy_reason':'creature_name_outside_configured_targets'}
        return result

    def fresh(self,frame,*,travel=False):
        captured=datetime.fromisoformat(frame.captured_at).timestamp()
        if self.travel_budget is not None and (travel or self.travel_budget.question_kind!='travel'):
            return self.current() and 0<=time.time()-captured and self.travel_budget.remaining()>0 and captured>self.last_input_at
        return self.current() and 0<=time.time()-captured<=10 and captured>self.last_input_at

    async def guard(self,source,expected,*,exact_level=None,strategy_state=None):
        generation=self.cycle.input_generation;source_hash=hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest()
        from sage_wow.agent.grind_progression import dispatch_basis
        basis=dispatch_basis(self);revision=self.revision;epoch=self.cycle.session_epoch
        expected_identity=deepcopy({k:expected.get(k) for k in ('name','levels','visual_observation')})
        name_row=expected['name_row']
        if name_row:
            bounds=name_row.bounds
            name_box=(int(bounds['x']),int(bounds['y']),int(bounds['x']+bounds['width']),int(bounds['y']+bounds['height']))
        else:name_box=expected['visual_name_box']
        async def check(_):
            self.stage('attack_dispatch_validation')
            initial={'source_fresh':self.fresh(source),'input_generation':generation==self.cycle.input_generation}
            if not all(initial.values()):
                return DispatchValidation(False,detail='Grind source expired or input/stop changed',
                    evidence={'failed_predicates':[name for name,passed in initial.items() if not passed],
                        'source_hash':source_hash})
            if hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest()!=source_hash:
                return DispatchValidation(False,detail='Retained grind source changed',
                    evidence={'failed_predicates':['source_hash'],'source_hash':source_hash})
            fresh=await asyncio.to_thread(self.capture)
            if self.archive:
                fresh=replace(fresh,image_path=str(self.archive.frame(fresh,self.cycle.session_epoch,generation)))
            validate_frame(fresh,self.profile)
            fresh_hash=hashlib.sha256(Path(fresh.image_path).read_bytes()).hexdigest()
            rows=await self.rows(fresh);actual=await self.target_proposal(fresh)
            self.stage('attack_pixel_continuity')
            calibration=self.config.get('target_badge_continuity')
            combat_glow=self.config.get('committed_combat',False)
            strict_name_patch=same_patch(source.image_path,fresh.image_path,name_box)
            strict_target_badge=same_target_badge(source.image_path,fresh.image_path,expected['badge'],calibration)
            target_badge=same_target_badge(source.image_path,fresh.image_path,expected['badge'],calibration,combat_glow=combat_glow)
            player_box=tuple(self.config['player_level_box'])
            strict_player_badge=same_patch(source.image_path,fresh.image_path,player_box,badge=True)
            player_badge=strict_player_badge
            if not player_badge and combat_glow and calibration:
                from sage_wow.agent.grind_perception import same_badge_numeral
                own_calibration={**calibration,'badge_box':list(player_box)}
                player_badge=same_badge_numeral(source.image_path,fresh.image_path,player_box,own_calibration,white=True)
            fresh_name_match=bool(combat_glow and calibration and actual.get('name_row')
                and key(actual['name'])==key(expected['name']) and target_badge)
            predicates={'distinct_frame':fresh.frame_id!=source.frame_id,'same_source':fresh.source==source.source,
                'same_dimensions':(fresh.width,fresh.height)==(source.width,source.height),'fresh_frame':self.fresh(fresh),
                'ui_clear':not ui_evidence(self.profile,fresh,rows,{})['positive'],
                'same_name':key(actual['name'])==key(expected['name']),'not_self':not actual['self_target'],
                'not_invalid':not actual['invalid_text'],
                'no_observation_conflict':not actual.get('observation_conflict'),
                'observed_eligibility':actual.get('eligibility') not in {'ineligible','absent','unknown'},
                'no_level_conflict':not actual['levels'] or exact_level is None or actual['levels']==[exact_level],
                'target_badge':target_badge,
                'player_badge':player_badge,
                'name_patch':strict_name_patch or fresh_name_match}
            from sage_wow.agent.grind_progression import selected_policy, strategy_permitted, target_identity
            location = None
            entry = self.hunt.hunting_progression_by_player_level.get(self.level.last_confirmed_level or 1) or {}
            if target_identity(actual.get('name')) in {target_identity(n) for n in entry.get('opportunistic_targets', [])}:
                location = await asyncio.to_thread(measure,self.profile,fresh,rows,self.ocr)
            regional = selected_policy(self,actual,fresh,location)
            predicates['hunting_strategy']=strategy_permitted(self,actual,fresh,location)
            predicates['strategy_owner']=basis==dispatch_basis(self)
            predicates['offer_target_unchanged']=expected_identity=={k:expected.get(k) for k in expected_identity}
            predicates['authority_current']=(revision==self.revision and epoch==self.cycle.session_epoch
                and generation==self.cycle.input_generation and self.current()
                and self.fresh(source) and self.fresh(fresh) and not self.cycle._scope_error(fresh)
                and hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest()==source_hash
                and hashlib.sha256(Path(fresh.image_path).read_bytes()).hexdigest()==fresh_hash)
            if exact_level is not None:
                low,high=self.hunt.target_band(self.level.last_confirmed_level or 1)
                predicates['exact_allowed_level']=low<=exact_level<=high
            stable=all(predicates.values())
            if strategy_state is not None:
                strategy_state.clear()
                if stable:
                    strategy_state.update(result=deepcopy(regional),frame=fresh,
                        hash=hashlib.sha256(Path(fresh.image_path).read_bytes()).hexdigest(),
                        name=target_identity(actual.get('name')),basis=basis,revision=revision,epoch=epoch,
                        generation=generation,source_frame_id=source.frame_id)
            return DispatchValidation(bool(stable),fresh if stable else None,
                'Current selected dispatch confirmed' if stable else 'Current selected dispatch rejected: '
                    + ', '.join(name + (':'+regional['kind'] if name=='hunting_strategy' else '')
                        for name,passed in predicates.items() if not passed),
                {'selected_name':actual['name'],'numeric_proposals':actual['levels'],'badge_box':expected['badge'],
                 'source_hash':source_hash,'fresh_frame_id':fresh.frame_id,
                 'fresh_hash':hashlib.sha256(Path(fresh.image_path).read_bytes()).hexdigest(),
                 'predicates':predicates,'selected_target_policy':regional,'failed_predicates':[name for name,passed in predicates.items() if not passed],
                 'combat_glow_continuity':{'enabled':combat_glow,'fresh_exact_name':fresh_name_match,
                     'strict_name_patch':strict_name_patch,'strict_target_badge':strict_target_badge,
                     'name_fallback_used':not strict_name_patch and fresh_name_match,
                     'badge_glyph_fallback_used':not strict_target_badge and target_badge,
                     'strict_player_badge':strict_player_badge,
                     'player_glyph_fallback_used':not strict_player_badge and player_badge},
                 'target_badge_mode':(self.config.get('target_badge_continuity') or {}).get('mode','whole_badge'),
                'semantic_type_source':'Sage gameplay choice with attributed observer facts' if expected.get('visual_observation') else 'combined Sage choice; OCR only checks continuity',
                'visual_provenance':expected.get('visual_provenance')})
        return check

    async def ui_close_guard(self,source,expected_ui):
        """Escape closes a currently evidenced panel; clear world would open one."""
        generation=self.cycle.input_generation
        source_hash=hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest()
        async def check(_):
            if (not expected_ui['positive'] or not self.fresh(source)
                or generation!=self.cycle.input_generation
                or hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest()!=source_hash):
                return DispatchValidation(False,detail='UI closure source/input authority changed')
            fresh=await asyncio.to_thread(self.capture)
            if self.archive:fresh=replace(fresh,image_path=str(self.archive.frame(fresh,self.cycle.session_epoch,generation)))
            validate_frame(fresh,self.profile)
            from sage_wow.agent.grind_inventory import augment_ui
            fresh_rows=await self.rows(fresh)
            current_ui=augment_ui(self,fresh,fresh_rows,ui_evidence(self.profile,fresh,fresh_rows,{}))
            predicates={'distinct_frame':fresh.frame_id!=source.frame_id,'same_source':fresh.source==source.source,
                'same_dimensions':(fresh.width,fresh.height)==(source.width,source.height),
                'fresh_frame':self.fresh(fresh),'input_generation':generation==self.cycle.input_generation,
                'visible_blocking_ui':current_ui['positive'],
                'same_ui_evidence':bool(set(expected_ui['cues']) & set(current_ui['cues']))}
            approved=all(predicates.values())
            return DispatchValidation(approved,fresh if approved else None,
                'Current blocking UI confirmed' if approved else 'UI changed or closed; Escape withheld',
                {'predicates':predicates,'failed_predicates':[name for name,passed in predicates.items() if not passed],
                 'source_hash':source_hash,'current_ui_cues':current_ui['cues']})
        return check

    async def notice_guard(self,frame,rows,ui):
        from sage_wow.agent.grounded_ui import Anchor, GroundedControl, make_guard
        from sage_wow.agent.dialog_focus import DialogPanelBounds
        from sage_wow.agent.session_notice import identity
        binding=ui['acknowledge']['binding'];point=(binding['image_x'],binding['image_y'])
        buttons=[row for row in rows if key(row.text) in {'ok','okay'} and
            abs(row.bounds['x']+row.bounds['width']/2-point[0])<=2 and
            abs(row.bounds['y']+row.bounds['height']/2-point[1])<=2]
        expected=identity(ui['notice'])
        if len(buttons)!=1 or expected is None:
            async def unavailable(_):return DispatchValidation(False,detail='Unique notice/control proof unavailable')
            return unavailable
        control=GroundedControl('acknowledge_notice','Acknowledge the visible notice',point,(Anchor.from_row(buttons[0]),))
        bounds=DialogPanelBounds.parse({'left':0.,'top':0.,'right':1.,'bottom':1.})
        guarded=make_guard(self.capture,frame,bounds,control,self.ocr)
        async def check(source):
            result=await guarded(source)
            if not result.approved:return result
            fresh=result.dispatch_frame
            current=ui_evidence(self.profile,fresh,await self.rows(fresh),{})
            if not current['acknowledge'] or identity(current['notice'])!=expected:
                return DispatchValidation(False,detail='Notice identity or unique control changed')
            return result
        return check

    async def process(self,frame):
        self.navigation_wait=None
        self._analysis_cache={}
        if self.config.get('combat_log_enabled',False):
            from sage_wow.agent.grind_combat_log import refresh_combat_log
            await refresh_combat_log(self)
        ordinary_travel=(self.hunt.phase=='travel' and self.baseline and not self.require_world
            and not self.hunt.blocked and not self.level.due(time.time()))
        scoped_hunt=(self.baseline and (not self.level.due(time.time()) or self.hunt.phase=='fight') and
            (self.hunt.phase in {'choose_area','search','approach','fight'} or
             self.hunt.pending and self.hunt.pending.get('purpose') in {'approach','cast_correction','search'}))
        source_at=datetime.fromisoformat(frame.captured_at).timestamp()
        if self.baseline and self.hunt.phase=='fight' and not self.require_world:
            # A resolved encounter can expose a different selected unit in this
            # very source. Determine routing before imposing a combat-only age
            # gate; immutable analysis is reused by play().
            validate_frame(frame,self.profile)
            selected=await self.target_proposal(frame)
            changed_selection=bool(selected['name'] and not selected['self_target'] and not selected['invalid_text']
                and key(selected['name'])!=self.hunt.last_target)
            if changed_selection and self.level.due(time.time()) and self.hunt.pending is None:
                self.hunt.phase='search'
            else:scoped_hunt=scoped_hunt or changed_selection
        self.travel_budget=TravelDecisionBudget(source_at,min(source_at+12,self.deadline),question_kind='travel' if ordinary_travel else 'search') if ordinary_travel or scoped_hunt else None
        if ordinary_travel:self.hunt.active_question=None
        try:
            result=await self._process(frame)
            if result.status in {'grind_timing_expired','travel_deadline_expired'}:
                self.timing_failures+=1
                if self.timing_failures>=4:self.enter_blocked('blocked_timing',self.last_signature)
                else:self.wait_until=time.time()+(1,2,4)[self.timing_failures-1]
            return result
        finally:self.travel_budget=None;self._analysis_cache=None

    def timing_expired(self,phase):
        budget=self.travel_budget
        self.event(budget.expired_event,{'question_kind':budget.question_kind,'phase':phase,
            'source_at':budget.source_at,'deadline':budget.deadline,
            'age_seconds':time.time()-budget.source_at,'remaining_seconds':budget.remaining()})
        return CycleResult(budget.expired_status,detail='Grinding source deadline expired at '+phase)

    def select_question(self,identity,kind):
        if not self.hunt.question(identity,reserved=kind=='blocked'):
            self.stop('semantic_history_capacity_unavailable')
            return False
        self.null_answers=self.hunt.unclear
        # This method selects an actual scoped question; ordinary travel does
        # not call it. Recovery/inspection can retain the old travel phase, but
        # must use their own retry policy under the unchanged source deadline.
        if self.travel_budget:
            self.travel_budget=replace(self.travel_budget,question_kind=kind)
        return True

    def finish_question(self,result,meta):
        if result.no_input_abstention or result.status=='dispatched' and meta.get('unclear'):
            self.hunt.question_answer(unclear=True);self.wait_until=time.time()+2
            if self.hunt.unclear>=2:
                self.request_recovery('repeated_uncertain_answer')
        elif result.status=='dispatched':self.hunt.question_answer(route_only=bool(meta.get('positioning')))
        self.null_answers=self.hunt.unclear

    def request_recovery(self,reason):
        """Change the next question/method while retaining factual outcome history."""
        self.hunt.request_recovery(reason,self.last_signature)
        self.wait_until=time.time()+.15
        self.event('grind_recovery_requested',{'reason':reason,'signature':self.last_signature,
            'phase':self.hunt.phase,'question':self.hunt.active_question,
            'input_generation':self.cycle.input_generation})

    async def _process(self,frame):
        if not self.current():return CycleResult('grind_stopped',detail=self.reason or 'absolute_deadline')
        validate_frame(frame,self.profile)
        if self.observation_transaction and self.observation_transaction.get('restoration_needed'):
            from sage_wow.agent.grind_loot_budget import suspend
            suspend(self)
            return await self.observation.reconcile(frame)
        from sage_wow.agent.grind_resources import stop_if_own_death
        death=await stop_if_own_death(self,frame)
        if death is not None:return death
        if self.config['clean_world_observation'] and self.hud_no_effect_count>=2:
            if not self.hunt.blocked or self.hunt.blocked['reason']!='blocked_hud_capture_unavailable':
                self.enter_blocked('blocked_hud_capture_unavailable','retained_hud_no_effect_capability')
        if self.travel_budget is not None and self.travel_budget.remaining()<=0:
            return self.timing_expired('initial_source')
        if not self.fresh(frame,travel=self.travel_budget is not None) or frame.frame_id==self.last_frame_id:
            return CycleResult('grind_reobserve',detail='Distinct post-input fresh frame required')
        self.last_frame_id=frame.frame_id
        with self.cycle.scoped_execution_scope(task_id=self.task_id,objective_revision=self.revision,
            session_epoch=self.cycle.session_epoch,deadline_epoch=min(self.deadline,self.travel_budget.deadline+(14 if self.config['smite_burst_count']>1 else 0)) if self.travel_budget and self.travel_budget.question_kind!='travel' else self.deadline,input_generation=self.cycle.input_generation,
            is_current=self.current,max_frame_age_seconds=12 if self.travel_budget else 10):
            if self.identity_mismatch_frames:
                return await self.check_level(frame)
            if getattr(self.hunt,'level_survival',False) or self.hunt.blocked or self.require_world or self.hunt.phase in {'ui_recover','input_effect_unverified'}:
                return await self.play(frame)
            if self.level.due(time.time()) and self.hunt.phase not in {'fight','recover'}:
                return await self.check_level(frame)
            if self.baseline and self.hunt.phase not in {'fight','recover'}:
                from sage_wow.agent.grind_combat_log import configure_combat_log
                setup = await configure_combat_log(self,frame)
                if setup is not None:return setup
            return await self.play(frame)

    def enter_blocked(self, reason, signature):
        if reason in {'blocked_semantic','blocked_no_combat_progress'}:
            self.request_recovery(reason)
            return
        existing = self.hunt.blocked
        if existing and existing['reason'] == reason and existing['evidence_signature'] == signature:
            return
        now = time.time()
        self.hunt.blocked = {'reason': reason, 'evidence_signature': signature,
                             'entered_at': now, 'next_observation_at': now + 15,
                             'interval': 15, 'last_assessment_at': now,'assessment_due_at':now+60}
        self.wait_until = now + 15
        self.event('grind_encounter_transition', {'from': self.hunt.phase, 'to': 'blocked',
                   'reason': reason, 'evidence_signature': signature})

    async def clear_guard(self, source, expected):
        """Clear permission binds selection and current world, never offensive eligibility."""
        generation = self.cycle.input_generation
        digest = hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest()
        async def check(_):
            if not self.fresh(source) or generation != self.cycle.input_generation:
                return DispatchValidation(False, detail='Clear selection authority expired')
            if hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest() != digest:
                return DispatchValidation(False, detail='Clear source changed')
            fresh = await asyncio.to_thread(self.capture)
            if self.archive:
                fresh = replace(fresh, image_path=str(self.archive.frame(fresh, self.cycle.session_epoch, generation)))
            validate_frame(fresh, self.profile)
            rows = await self.rows(fresh)
            actual = await self.target_proposal(fresh)
            current_ui = ui_evidence(self.profile, fresh, rows, {})
            named=bool(expected.get('name') and actual.get('name') and self.config.get('target_name_box'))
            calibration=self.config.get('target_badge_continuity')
            combat_glow=self.config.get('committed_combat',False)
            name_pixels=(same_patch(source.image_path,fresh.image_path,tuple(self.config['target_name_box'])) if named else False)
            badge_pixels=(same_target_badge(source.image_path,fresh.image_path,expected['badge'],calibration,
                combat_glow=combat_glow) if named else False)
            # As in attack validation, a fresh matching OCR name plus the same
            # calibrated badge survives scenery moving behind a translucent
            # nameplate. This authorizes only the already selected clear, not
            # an attack or a claim that selection disappeared.
            row=actual.get('name_row')
            fresh_name_match=bool(named and combat_glow and calibration and row
                and row.confidence>=.85 and not actual.get('invalid_text')
                and key(row.text)==key(actual['name'])==key(expected['name']) and badge_pixels)
            selected_continuity=((name_pixels or fresh_name_match) and badge_pixels if named else
                same_patch(source.image_path,fresh.image_path,expected['box']))
            predicates={'fresh_frame':self.fresh(fresh),'distinct_frame':fresh.frame_id!=source.frame_id,
                'source_immutable':hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest()==digest,
                'same_source':fresh.source==source.source,'ui_clear':not current_ui['positive'],
                'same_dimensions':(fresh.width,fresh.height)==(source.width,source.height),
                'same_name':(actual['name']==expected['name'] if not actual['name'] or not expected['name']
                    else key(actual['name'])==key(expected['name'])),
                'no_level_conflict':not actual['levels'] or not expected['levels'] or actual['levels']==expected['levels'],
                'selected_unit_continuity':selected_continuity,
                'player_badge':same_patch(source.image_path,fresh.image_path,tuple(self.config['player_level_box']),badge=True)}
            stable=all(predicates.values())
            return DispatchValidation(stable, fresh if stable else None,
                                      'Clear-only selected-unit/world continuity', {'source_hash': digest,
                'predicates':predicates,'failed_predicates':[name for name,value in predicates.items() if not value],
                'expected_name':expected['name'],'actual_name':actual['name'],
                'expected_levels':expected['levels'],'actual_levels':actual['levels'],
                'clear_name_continuity':{'strict_name_pixels':name_pixels,'target_badge':badge_pixels,
                    'fresh_exact_name':fresh_name_match,'name_fallback_used':not name_pixels and fresh_name_match},
                'fresh_frame_id':fresh.frame_id,'fresh_hash':hashlib.sha256(Path(fresh.image_path).read_bytes()).hexdigest()})
        return check

    async def decide(self, frame, image, prompt, instructions, candidates, *, level_read=False, travel_budget=None, compact=False, relocation_retry=False, navigation_review=None, sensing_boundary=None, selected_clear=None):
        review_request_before = (navigation_review or {}).get('last_request_id')
        result = None
        try:
            # The same current own-health rule already used by looting applies
            # to every semantic death choice, including combat and level reads.
            # Target corpses or historical chat cannot override a living own
            # bar. Unknown health retains the death option; the explicit own
            # death-modal check in _process always runs before this decision.
            death_options={'dead_or_unrecoverable','player_dead'}
            offered=death_options.intersection(item.option for item in candidates)
            if offered:
                from sage_wow.agent.grind_resources import hud_resources, own_death_evidence
                hud=await asyncio.to_thread(hud_resources,self,frame)
                alive=(hud['player_health'] is not None and hud['player_health']>0
                    and hud['health_confidence']>=.55)
                if alive and not own_death_evidence(self,frame,await self.rows(frame)):
                    candidates=[item for item in candidates if item.option not in death_options]
                    self.event('grind_death_choice_withheld',{'frame_id':frame.frame_id,
                        'source_sha256':hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
                        'options':sorted(offered),'reason':'current_own_living_health',
                        'current_hud':hud,'input_authorized':False})
            self.stage('sage_choice_and_guarded_execution')
            from sage_wow.agent import grind_navigation_recovery as navigation_recovery
            from sage_wow.agent import grind_inspection as inspection
            from sage_wow.agent import grind_relocation as relocation
            result = await self.cycle.decide_and_execute(
                frame, image, prompt, instructions, candidates,
                # Compact describes the prompt, not permission to repeat an
                # undecidable fast request forever. Retry its current question
                # with reasoning after abstention, under the same input lease.
                # Ordinary travel and own-level reads remain fast-only. A
                # qualified relocation recovery retries its concrete question
                # under the unchanged travel deadline and input guards.
                reasoning=(('auto' if selected_clear['state'].get('previous_unclear') else 'off') if selected_clear is not None else
                    sensing_boundary['reasoning'] if sensing_boundary is not None else
                    navigation_recovery.reasoning(navigation_review) if navigation_review is not None else
                    'auto' if (self.null_answers and not level_read
                    and (relocation_retry or not (travel_budget and travel_budget.question_kind=='travel'))) else 'off'),
                max_frame_age_seconds=12 if travel_budget else 10,travel_budget=travel_budget,
                propagate_capture_interruptions=True,
                on_request_started=(lambda request: relocation.start_clear_assessment(self,selected_clear,request))
                    if selected_clear is not None else (lambda request: inspection.start_boundary(self,sensing_boundary,request))
                    if sensing_boundary is not None else (lambda request: navigation_recovery.started(self,navigation_review,request))
                    if navigation_review is not None else None)
            if level_read:
                if result.status=='decision_failed' and any(word in (result.detail or '').lower() for word in ('authentication','unauthorized','configuration')):
                    self.stop('provider_unavailable')
                return result
            if result.status == 'decision_failed' or (result.status=='scope_deadline_expired' and self.current()):
                self.provider_failures += 1
                if any(word in (result.detail or '').lower() for word in ('authentication', 'unauthorized', 'configuration')):
                    self.stop('provider_unavailable')
                elif self.provider_failures >= 4:
                    self.enter_blocked('blocked_provider', self.last_signature)
                else:
                    self.wait_until = time.time() + (1, 2, 4)[self.provider_failures - 1]
            elif result.no_input_abstention and self.hunt.active_question is None:
                self.null_answers += 1
                self.hunt.unclear += 1
                self.wait_until = time.time() + 2
                if self.null_answers >= 2:self.request_recovery('repeated_null_provider_answer')
            elif result.status == 'dispatched':
                self.provider_failures = 0;self.null_answers = 0
                # Only a decision that used this lease demonstrates timely
                # gameplay. Level reads return above; reopening a blocked
                # assessment retains debt until the ensuing decision succeeds.
                if travel_budget and not self.hunt.blocked:self.timing_failures=0
            return result
        finally:
            if sensing_boundary is not None and result is None:
                from sage_wow.agent.grind_inspection import finish_boundary
                finish_boundary(self,sensing_boundary,None,{})
            if (navigation_review is not None and navigation_review.get('last_request_id')
                and navigation_review['last_request_id'] != review_request_before
                and (result is None or result.decision is None)):
                if result is None:
                    from sage_wow.agent import grind_navigation_recovery as navigation_recovery
                    navigation_recovery.abort(self,navigation_review,'navigation_review_interrupted')
                self.event('sage_response_discarded', {'request_id': navigation_review['last_request_id'],
                    'reason': result.status if result else 'navigation_review_interrupted'})
            generated = Path(image)
            if generated.name.startswith(('grind-view-', 'grind-own-level-', 'player-level-')):
                generated.unlink(missing_ok=True)

    async def check_level(self,frame):
        from sage_wow.agent.grind_loot_budget import suspend
        suspend(self)
        if not self.level.due(time.time()):
            return CycleResult('grind_reobserve',detail='Next own-level reading waits for the configured schedule')
        if frame.frame_id in self.identity_mismatch_frames:
            self.level.last_attempt_at=None;self.wait_until=time.time()+2
            return CycleResult('grind_reobserve',detail='Player identity verification requires a distinct fresh frame')
        level_rows=await self.rows(frame)
        if ui_evidence(self.profile,frame,level_rows,{})['positive']:
            return await self.play(frame)
        self.loading_started=None
        path=await self.prepare_image(frame,player_level_assessment_image,frame,Path(frame.image_path).with_name(f'grind-own-level-{frame.frame_id}.png'),ui_layout=from_profile(self.profile))
        if isinstance(path,CycleResult):return path
        candidates=[ActionCandidate(f'player_level_{n}',f'My displayed level is {n}.',
            {'type':'observe_only','fact_kind':'player_level','level':n}) for n in range(1,11)]
        candidates.append(ActionCandidate('player_level_unknown','The own level badge is unreadable or obscured.',
            {'type':'observe_only','fact_kind':'player_level','level':None}))
        candidates.append(ActionCandidate('player_dead','My own player is explicitly shown as dead.',{'type':'observe_only'}))
        identity=' '.join(str(self.profile.values['character'].get(k) or '') for k in ('name','surname')).strip()
        prompt=(f"Task: Read my player's displayed level.\nPlayer identity: {identity}\n"
                "Evidence: Current crop of my own player frame.\nLevel source: The circular level badge beside my portrait.")
        self.level.note_request_started(time.time())
        result=await self.decide(frame,path,prompt,
            "Which level numeral is visible in my player's level badge? Select the matching numeral. Select player_dead only if my own death is explicitly visible.",candidates,level_read=True)
        receipt=result.receipt or {}
        accepted=(result.status=='dispatched' and result.decision and receipt==self.cycle.last_receipt
                  and receipt.get('completed') and not receipt.get('possible_input') and self.fresh(frame))
        if accepted:
            if result.decision.chosen=='player_dead':
                self.stop('sage_observed_player_death');return result
            player_box=from_profile(self.profile)['regions']['player_frame']
            own_names=[row.text for row in region_rows(level_rows,player_box)
                if re.search('[A-Za-z]',row.text) and key(row.text) not in {'mana','health','dead','priest','dwarf'}]
            identity_proof=await read_player_identity(self,frame,level_rows,full_name=True)
            if identity_proof.get('observation_authority_lost'):
                self.level.last_attempt_at=None
                return CycleResult('grind_reobserve',detail='Player identity source authority changed during observation')
            # World labels can overlap the broad player frame. Require one
            # contained, confident identity location rather than concatenating
            # unrelated OCR. Level/campaign identity still requires the full
            # configured name, even when the HUD visibility proof allows a base.
            identity_matches=(identity_proof['verified'] and key(identity)==key(
                ' '.join(row['text'] for row in identity_proof['rows'])))
            if not identity_matches:
                self.event('grind_player_identity_unconfirmed',{'expected':identity,'observed_name_proposals':own_names,
                    'frame_id':frame.frame_id,'reason':'fresh_player_frame_identity_required','identity_proof':identity_proof})
                self.identity_mismatch_frames.add(frame.frame_id)
                self.event('grind_player_identity_retry',{'frame_id':frame.frame_id,
                    'distinct_mismatch_frames':len(self.identity_mismatch_frames),'limit':3,
                    'baseline_retained':self.baseline,'gameplay_withheld':True})
                if len(self.identity_mismatch_frames)>=3:self.stop('player_identity_or_level_inconsistent')
                self.level.last_attempt_at=None
                self.wait_until=time.time()+2
                return result
            self.identity_mismatch_frames.clear()
            observation=self.level.record(decision=result.decision,frame=frame)
            if observation:
                self.event('grind_player_level_observed',observation)
                if observation['level_change_vs_same_session_observation']=='lower_displayed_level_check_identity_or_session':
                    self.stop('player_identity_or_level_inconsistent')
                confirmed=self.level.last_confirmed_level
                if not self.baseline and confirmed is not None:
                    prior=self.config.get('campaign_continuation')
                    expected=prior['verified_level'] if prior else 1
                    identity=' '.join(str(self.profile.values['character'].get(k) or '') for k in ('name','surname')).strip()
                    if confirmed!=expected or prior and prior['character']!=identity:
                        self.stop('campaign_baseline_identity_or_level_mismatch')
                    else:
                        self.baseline=True
                        self.event('grind_baseline_verified',{'level':confirmed,'character':identity,
                            'continuation':prior['evidence'] if prior else None,'evidence':self.level.history[-2:]})
                if self.baseline and not self.stopped and observation.get('level') is not None:
                    self.record_campaign_progress(frame,confirmed,observation)
                goal=self.config['goal_level']
                if self.baseline and confirmed==goal and observation['level']==goal:
                    if frame.frame_id not in self.goal_frame_ids:self.goal_frame_ids.append(frame.frame_id)
                    if len(self.goal_frame_ids)<2:self.level.last_attempt_at=None
                    else:
                        self.success=True;self.stop('goal_level_verified')
                        self.event('grind_success',{'level':goal,'evidence':[entry for entry in self.level.history if entry['frame_id'] in self.goal_frame_ids][-2:],
                            'claim':'Goal level freshly observed twice in authenticated frames; XP source is separate'})
                if self.baseline and confirmed and confirmed>goal:self.stop('goal_level_overshot')
                if confirmed is not None:self.hunt.level_changed(confirmed)
        elif receipt.get('possible_input') or receipt.get('dispatch_unknown'):
            self.stop('partial_or_unknown_grind_input')
        if not self.baseline and not self.stopped:
            self.wait_until=time.time()+2
        self.event('grind_level_check_completed',{'accepted':bool(accepted and self.level.latest_observation
            and self.level.latest_observation['frame_id']==frame.frame_id),'next_due_at':self.level.last_attempt_at+self.config['level_check_seconds'] if self.level.last_attempt_at else None,
            'frame_id':frame.frame_id,'status':result.status})
        return result

    def record_campaign_progress(self,frame,level,observation):
        """Persist facts only. A continuation must revalidate identity/level/input."""
        identity=' '.join(str(self.profile.values['character'].get(k) or '') for k in ('name','surname')).strip()
        prior=self.config.get('campaign_continuation') or {}
        record={'character':identity,'verified_level':level,'goal_level':self.config['goal_level'],
            'level_observation_frame_id':frame.frame_id,'level_observation_image':frame.image_path,
            'image_sha256':hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
            'source_session_id':self.config['session_id'],'session_epoch':self.cycle.session_epoch,
            'observation':observation,'prior_campaign_evidence':prior.get('evidence_path'),
            'verified_kills_this_session':list(self.hunt.credited_kills),
            'claim':'Fresh level observation; kill/XP attribution remains separate; no input authority survives restart'}
        from sage_wow.agent.grind_inventory import policy
        if policy(self):record['loot_policy']=deepcopy(policy(self))
        self.store.save_checkpoint('grind_campaign_progress',record)
        output=self.config.get('campaign_progress_path')
        if output:
            path=Path(output);path.parent.mkdir(parents=True,exist_ok=True)
            temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(record,indent=2));temporary.replace(path)

    async def play(self, frame):
        rows = await self.rows(frame)
        loading=any(re.search(r'loading',row.text,re.I) for row in rows)
        if not loading:self.loading_started=None
        target = await self.target_proposal(frame)
        self.stage('grind_world_measurement')
        measurement = await asyncio.to_thread(measure, self.profile, frame, rows, self.ocr)
        self.stage('grind_menu_and_image_preparation')
        hunt = self.hunt
        hunt.observe_zone(measurement,self.cycle.session_epoch,self.cycle.input_generation)
        hunt.tick(time.time())
        feedback = hunt.feedback_for(frame, self.cycle.input_generation, measurement)
        hunt.resolve_travel(frame,self.cycle.input_generation,measurement,self.cycle.session_epoch)
        # Invalidate context before resources/loot/UI can replace a pending
        # local translation and hide its unobserved location change.
        from sage_wow.agent.grind_hunt_entry import refresh_arrival
        refresh_arrival(self,measurement)
        from sage_wow.agent.grind_inventory import observe_capacity, augment_ui, effective_loot_enabled
        await observe_capacity(self,frame,rows)
        ui = augment_ui(self,frame,rows,ui_evidence(self.profile, frame, rows, measurement))
        from sage_wow.agent.grind_inspection import refresh as refresh_inspection
        if self.config.get('committed_combat') or self.target_inspection_episode:
            # Selection transition sensing requires this frame's calibrated
            # bars even when optional resource actions are disabled.
            from sage_wow.agent.grind_resources import hud_resources
            measured=await asyncio.to_thread(hud_resources,self,frame)
            current_hud=target.get('hud') or {}
            target={**target,'hud':{**measured,**(current_hud if current_hud.get('frame_id')==frame.frame_id else {})}}
        if not loading and not ui['positive']:refresh_inspection(self,frame,target)
        self.flush_outcomes()
        pending = hunt.pending  # Immutable transition source; do not reread after resolving.
        linked = hunt.linked(frame, self.cycle.input_generation)
        from sage_wow.agent.grind_cast_feedback import unresolved_cues
        if linked:unresolved_cues(pending,frame,measurement)
        hunt.selection_seen(target)
        from sage_wow.agent.grind_inspection import refresh_work, refresh_operation
        refresh_work(self,frame,target)
        refresh_operation(self,frame,target)
        from sage_wow.agent.grind_inspection import observe_absence_continuity
        observe_absence_continuity(self,frame,target)
        from sage_wow.agent.grind_correction_handoff import expire
        expire(self,frame,target)
        if target.get('visual_observation') and self.target_observation:
            self.target_observation['selection_revision']=hunt.selection_revision
        level = self.level.last_confirmed_level
        controls = self.profile.values['controls']['bindings']
        actual_ui_present=bool(loading or ui['positive'])
        if actual_ui_present and self.last_ui_presence is False:
            hunt.changed_situation('ui');hunt.failures['ui']=0
            if hunt.blocked and hunt.blocked.get('reason')=='blocked_ui':hunt.blocked=None
            self.event('grind_ui_episode_changed',{'frame_id':frame.frame_id,'current_ui_cues':ui['cues'],
                'reason':'blocking UI appeared after a visibly clear world'})
        self.last_ui_presence=actual_ui_present
        resource=[]
        with Image.open(frame.image_path) as raw:
            regions=UIRegions(frame,(frame.width,frame.height),from_profile(self.profile))
            for region,fallback in [('player_health',(0,0,.3,.15)),('player_mana',(0,0,.3,.15))]:
                color=raw.convert('RGB').crop(regions.pixels(region,fallback)).resize((1,1)).getpixel((0,0))
                resource.append([channel//32 for channel in color])
        own_rows=region_rows(rows,UIRegions(frame,(frame.width,frame.height),from_profile(self.profile)).pixels('player_frame',(0,0,.3,.3)))
        signature = evidence_signature(target, measurement, ui, level, resource=[resource,[row.text for row in own_rows]])
        self.last_signature = signature
        debt=hunt.travel_urgent_debt
        if debt and (debt['evidence_signature']!=signature or debt['gameplay_receipt_id']!=hunt.last_gameplay_receipt_id):
            hunt.travel_urgent_debt=None
        positive_ui = loading or ui['positive'] or self.require_world or hunt.phase in {'ui_recover', 'input_effect_unverified'}
        from sage_wow.agent.grind_loot_budget import suspend as suspend_loot_budget
        if positive_ui or hunt.blocked:
            suspend_loot_budget(self)
        if self.config['encounter_resources'] and not positive_ui and self.baseline:
            from sage_wow.agent.grind_resources import hud_resources, process_resources
            self.current_hud=await asyncio.to_thread(hud_resources,self,frame)
            target={**target,'hud':self.current_hud}
            from sage_wow.agent.grind_encounter_recovery import observe_threat
            observe_threat(self,frame,target)
            # These tasks cannot clear a global block. Keep current HUD facts
            # available, but let blocked reassessment run before task decisions.
            if not hunt.blocked:
                suspend_loot_budget(self)
                resource_result=await process_resources(self,frame,target,measurement,pending,linked)
                if resource_result is not None:return resource_result
        if effective_loot_enabled(self) and not positive_ui and self.baseline and not hunt.blocked:
            from sage_wow.agent.grind_loot import loot_route_pending, process_loot
            if loot_route_pending(self):
                return await process_loot(self,frame,target,measurement,rows)
        suspend_loot_budget(self)
        from sage_wow.agent.grind_clear import attempt as continue_clear
        continued_clear=await continue_clear(self,frame,target,measurement,ui_present=bool(positive_ui))
        if continued_clear is not None:return continued_clear
        terminal_hud=self.config['clean_world_observation'] and self.hud_no_effect_count>=2
        if terminal_hud and positive_ui:
            # The capability debt survives presentation changes. Actual UI
            # closure/world confirmation remains guarded and cannot hide/retry.
            if hunt.blocked and hunt.blocked['reason']=='blocked_hud_capture_unavailable':hunt.blocked=None
        elif (terminal_hud and self.baseline and self.level.due(time.time())
              and hunt.phase not in {'fight','recover'}):
            return await self.check_level(frame)
        blocked_episode = hunt.blocked
        reassessment = False
        if blocked_episode:
            now = time.time()
            if now < blocked_episode['next_observation_at']:
                return CycleResult('grind_blocked', detail=blocked_episode['reason'])
            changed = signature != blocked_episode['evidence_signature']
            due=blocked_episode.get('assessment_due_at',blocked_episode['last_assessment_at']+60)
            if not changed and now < due:
                interval = min(60, blocked_episode['interval'] * 2)
                blocked_episode.update(interval=interval, next_observation_at=min(now+interval,due))
                self.wait_until = blocked_episode['next_observation_at']
                return CycleResult('grind_blocked', detail=blocked_episode['reason'])
            # One observation-only assessment can restore normal fresh assessment;
            # the blocked decision itself carries no physical authority.
            reassessment = True
            blocked_episode.update(last_assessment_at=now,assessment_due_at=now+60)
        phase = 'ui_recover' if positive_ui else hunt.phase
        selected_disposition=None
        from sage_wow.agent import grind_travel_scout as travel_scout
        if not positive_ui and not reassessment and self.baseline:
            from sage_wow.agent.grind_encounter_recovery import observe_threat, route_threat
            observe_threat(self,frame,target)
            if route_threat(self,frame):phase=hunt.phase
            if hunt.reconcile_intentional_clear(self,frame,target,positive_ui=positive_ui):phase=hunt.phase
            from sage_wow.agent.grind_relocation import selected_disposition as disposition_for
            selected_disposition=disposition_for(self,frame,target,pending)
            if selected_disposition and selected_disposition['status']=='superseded':
                intent=(hunt.strategy_required or {}).get('selected_exit') or {}
                if intent.get('disposition')!='closed':intent['superseded']=deepcopy(selected_disposition)
                # A genuine newer selected task owns sensing now, including
                # its missing numeral; eligible-only preemption is too late.
                if (hunt.phase in {'travel','search','choose_area'} and
                    (pending is None or pending.get('family')=='target') and
                    hunt.positive_selection(target) and not hunt.active_threat):
                    hunt.phase='search';hunt.compact_stage='inspect';hunt.planning_requested=False
                    phase=hunt.phase
            from sage_wow.agent.grind_progression import route as route_progression
            if phase!='recover' and travel_scout.route(self):phase=hunt.phase
            if not travel_scout.active(self) and route_progression(self,frame,target,pending,measurement):phase=hunt.phase
            from sage_wow.agent.grind_hunt_entry import route_arrival
            if route_arrival(self,frame,target,measurement):phase=hunt.phase
            from sage_wow.agent import grind_navigation_recovery as navigation_recovery
            if navigation_recovery.route(self,frame,target,pending):phase=hunt.phase
        if not positive_ui and not reassessment and self.baseline and phase!='recover':
            from sage_wow.agent.grind_cast_feedback import reassess_interrupted
            error_review=await reassess_interrupted(self,frame,target,measurement)
            if error_review is not None:return error_review
        # A recovery Tab can retain the prior travel phase. Assess that completed
        # action before another travel input replaces its receipt. This selects
        # the existing acquisition/inspection flow, not permission to cast.
        from sage_wow.agent.grind_acquisition import acquisition_proof
        from sage_wow.agent.grind_progression import selected_inspection
        target_assessment = (acquisition_proof(self,frame,pending,linked) is not None
            or selected_inspection(self,frame,target,pending,measurement))
        selected_disposed=bool(selected_disposition and selected_disposition['status'] in {'matching','temporarily_unproven'}
            and not travel_scout.threat_current(self,frame))
        if selected_disposed:
            target_assessment=False
            if selected_disposition['status']=='temporarily_unproven' and not positive_ui and not reassessment:
                from sage_wow.agent.grind_relocation import decide_selected_exit
                selected_exit_result=await decide_selected_exit(self,frame,target,measurement)
                if selected_exit_result is not None:return selected_exit_result
        if not positive_ui and not reassessment and self.baseline and not target_assessment and not travel_scout.active(self):
            from sage_wow.agent.grind_relocation import resume
            from sage_wow.agent.grind_travel_recovery import resume as resume_travel_question
            from sage_wow.agent.grind_travel_menu import resume_question as resume_travel_menu
            if not resume(self,frame,target,pending,phase):
                if not resume_travel_question(self,frame,target,pending,phase):
                    resume_travel_menu(self,frame,target,pending,phase)
        if (phase=='travel' and self.baseline and not reassessment and hunt.compact_stage!='recovery' and not target_assessment
            and not (selected_disposition and selected_disposition['status']=='temporarily_unproven')):
            if self.level.due(time.time()):return await self.check_level(frame)
            return await self.travel(frame,rows,target,measurement)
        usable = bool(target['name'] and not target['self_target'] and not target['invalid_text'])
        selected_present = bool(region_rows(rows, target['box']) or target['name'])
        same_encounter = key(target['name']) == hunt.last_target
        prior_combat = bool(linked and pending and pending['family'] == 'combat' and same_encounter)
        prior_motion = bool(linked and pending and pending['family'] == 'motion')
        prior_recovery = bool(linked and pending and pending['family'] == 'recovery')
        failed_absence = bool(linked and pending and pending['family'] == 'target' and not usable)
        # Same-name living selection after unknown loss, clear or self-heal retains
        # encounter debt. Only observed death -> living or a distinguishing name
        # change can establish the next encounter; a Tab receipt cannot.
        new_encounter = bool(usable and (hunt.target_dead_observed or
                             (hunt.last_target is not None and not same_encounter)))
        health_box = UIRegions(frame, (frame.width, frame.height), from_profile(self.profile)).pixels('target_health', (0, 0, 1, 1))
        changed_health = bool(prior_combat and not same_patch(pending['source_image'], frame.image_path, health_box))
        error_cues = measurement.get('error_cues', [])
        before_errors = pending.get('measurement', {}).get('error_cues', []) if pending else []
        fresh_errors = [cue for cue in error_cues if cue not in before_errors] if prior_combat or prior_recovery else []
        if self.travel_budget and self.travel_budget.remaining()<=0:return self.timing_expired('source_analysis')
        if not positive_ui and not reassessment and self.baseline and phase!='recover':
            from sage_wow.agent.grind_acquisition import attempt as attempt_opening
            opening=None if selected_disposed else await attempt_opening(self,frame,target,measurement,pending,linked)
            if opening is not None:return opening
            if pending and pending['family']=='motion' and pending.get('purpose')!='travel':
                return await self.focused_decision(frame,target,measurement,pending,linked,level,kind='motion')
            needs_observer=(hunt.compact_stage=='reinspect' or
                (not target['name'] and hunt.compact_stage=='inspect') or
                (level in hunt.target_bands_by_player_level and target['name'] and len(target['levels'])!=1) or
                target.get('eligibility')=='unknown' or
                (self.config['committed_combat'] and hunt.cast_obligation and target['name']))
            from sage_wow.agent.grind_inspection import availability, requires_facts, operation_ready
            operation=getattr(self,'target_reinspection_operation',None) or {}
            explicit_observer=operation_ready(self,frame,target)
            if ((self.target_inspection_episode or {}).get('interrupted_lineage') and not requires_facts(self,target)
                and operation.get('state') not in {'committed','deferred'}):
                needs_observer=False
            sensing = availability(self,frame,target)
            if (not selected_disposed and (explicit_observer or needs_observer
                    and (not target.get('visual_observation') or target.get('eligibility')=='unknown'))
                and sensing['state']=='runnable'):
                from sage_wow.agent.grind_perception import inspect
                if await inspect(self,frame,target):
                    return CycleResult('grind_reobserve',detail='Attributed target facts retained; fresh frame required before gameplay decision')
            from sage_wow.agent.grind_combat import decide_combat
            return await decide_combat(self,frame,target,measurement,pending,linked,level)
        question_id=('blocked',blocked_episode['entered_at']) if reassessment else ('world',self.revision) if positive_ui else ('outcome',pending['receipt']['receipt_id']) if pending else ('hunting',phase)
        if not self.select_question(str(question_id),'blocked' if reassessment else 'world' if positive_ui else 'search'):
            return CycleResult('grind_blocked',detail='Unresolved question capacity retained; no untracked input')
        choices, metadata = [], {}
        valid = lambda: self.fresh(frame) and not self.cycle._scope_error(frame) and not hunt.blocked

        def observe(option, description, **meta):
            if meta.get('semantic') and hunt.semantic_counts.get((meta['semantic'],signature),0)>=3:return
            choices.append(ActionCandidate(option, description, {'type': 'observe_only'}))
            metadata[option] = meta

        def allowed(action, family, prospective_failure=False):
            count = hunt.motion_count(action,'search') if family=='motion' and phase in {'search','choose_area'} else hunt.action_failures.get(hunt.action_key(action, family), 0)
            if prospective_failure and linked and pending and pending['action'] == action:count += 1
            return count < 2

        def motor(option, binding_name, description, seconds=None, **meta):
            binding = controls.get(binding_name) or {}
            if not binding.get('verified_from') or type(binding.get('keycode')) is not int:return
            if seconds is None:
                seconds = self.config['move_seconds'] if binding_name == 'forward' else self.config['turn_seconds'] if binding_name.startswith('turn') else .15
            choices.append(ActionCandidate(option, description, {'type': 'keypress', 'keycode': binding['keycode'], 'hold_seconds': seconds}, precondition=valid))
            metadata[option] = {'action': binding_name, **meta}

        if reassessment:
            from sage_wow.agent.grind_blocked import task as blocked_task
            blocked_context, blocked_question, resume_description, remain_description = blocked_task(
                blocked_episode, target, ui, loading=loading, pending=pending)
            observe('blocked_changed_assessment', resume_description, unblock=True)
            observe('blocked_still_unresolved', remain_description, remain_blocked=True)
        elif positive_ui:
            loading = any(re.search(r'loading', row.text, re.I) for row in rows)
            if loading:
                if self.loading_started is None:self.loading_started = hunt.active_seconds
                observe('loading_wait', 'Loading remains visible. Observe only; do not reconnect or issue world input.', loading=True)
            else:
                self.loading_started = None
                if not ui['positive']:
                    observe('world_normal_confirmed', 'Fresh full world has no active typing field or blocking dialog/menu; passive chat and ordinary HUD are normal. Confirm usable world; no world input in this decision.', effect='world_clear')
                if ui['positive'] and linked and pending and pending['family'] == 'ui':
                    observe('blocking_ui_remains', 'The linked closure still left visible blocking UI. Record failure; never use world input through it.', effect='ui_still_blocked')
                elif ui['positive'] and hunt.unclear < 3:
                    observe('blocking_ui_remains', 'Blocking UI remains visible; choose a supported closure or hold.', semantic='ui')
                if ui['acknowledge'] and allowed('acknowledge', 'ui', bool(linked and pending and pending['family'] == 'ui')):
                    choices.append(ActionCandidate('acknowledge_notice', 'Click the current unique matching Okay once; observe closure next.', ui['acknowledge']['binding'], precondition=valid, dispatch_guard=await self.notice_guard(frame, rows, ui)))
                    metadata['acknowledge_notice'] = {'action': 'acknowledge', 'family': 'ui', 'purpose': 'ui', 'effect': 'ui_still_blocked' if linked and pending and pending['family'] == 'ui' else None}
                if ui['positive']:
                    from sage_wow.agent.grind_ui import decline_candidate
                    if (not hunt.input_effect_unverified and allowed('decline_invitation', 'ui',
                        bool(linked and pending and pending['family'] == 'ui'))):
                        decline = await decline_candidate(self, frame, rows, valid)
                        if decline:
                            choices.append(decline)
                            metadata[decline.option] = {'action': 'decline_invitation', 'family': 'ui', 'purpose': 'ui',
                                'effect': 'ui_still_blocked' if linked and pending and pending['family'] == 'ui' else None}
                    if allowed('escape', 'ui', bool(linked and pending and pending['family'] == 'ui')):
                        motor('close_visible_ui', 'escape', 'Only currently visible blocking UI/text entry: Escape once then require observed closure.', .08, family='ui', purpose='ui', effect='ui_still_blocked' if linked and pending and pending['family'] == 'ui' else None)
                        if choices and choices[-1].option=='close_visible_ui':
                            choices[-1]=replace(choices[-1],dispatch_guard=await self.ui_close_guard(frame,ui))
                    elif not any(item.option in {'decline_invitation', 'acknowledge_notice'} for item in choices):
                        hunt.input_effect_unverified = True
                        observe('safe_hold', 'No supported effective closure remains; report blocked UI and observe.', block='blocked_ui')
        elif not self.baseline:
            if self.baseline_wait_signature==signature and phase!='recover':
                self.wait_until=time.time()+2
                return CycleResult('grind_waiting_level',detail='No accepted own-level baseline; schedule retained, current safety evidence unchanged')
            self.baseline_wait_signature=signature
            observe('safe_hold', 'Own level has no accepted baseline and current world is safe. Preserve the schedule and wait without beginning hunting.')
            observe('recover_now', 'An actual current emergency requires existing survival assessment.', recover=True)
            if phase=='recover':
                if prior_recovery or (getattr(hunt,'rest_before',None) and pending is None):
                    observe('resources_improved','Linked recovery visibly improved own resources; continue safe recovery.',effect='resources_improved')
                    observe('resources_unchanged','Linked recovery remains unchanged; reassess method.',effect='resources_unchanged')
                    if fresh_errors:observe('recovery_error_observed','Linked recovery produced a current error.',effect='resources_unchanged',cast_error=fresh_errors[-1])
                if prior_motion:
                    observe('motion_useful','Linked local escape improved the current emergency situation.',effect='motion_useful')
                    observe('motion_no_useful_effect','Linked local escape had no useful observed effect.',effect='motion_no_useful_effect')
                observe('rest','No active attacker; wait briefly and compare my resources.',rest=True)
                observe('recovered_resume','Current resources/world are safe; return to waiting for the scheduled own-level reading.',recovered=True)
                for name in ('backward','turn_left','turn_right'):
                    if allowed(name,'motion'):motor(name,name,'Actual current emergency: short local escape on visibly safe ground.',family='motion',purpose='recovery_escape')
                heal=controls.get('lesser_heal') or {};self_key=controls.get('target_self') or {}
                if not hunt.no_mana and not hunt.heal_unavailable and allowed('heal','recovery') and all(binding.get('verified_from') and type(binding.get('keycode')) is int for binding in (heal,self_key)):
                    binding=({'type':'cast_self_heal','spell':'Lesser Heal'} if self.config['preserve_target_heal'] else {'type':'keypress_sequence','keycodes':[self_key['keycode'],heal['keycode']]})
                    choices.append(ActionCandidate('heal_self','Alive, genuinely low own health and sufficient mana; heal myself once, then inspect current resources and enemy.',binding,precondition=valid))
                    metadata['heal_self']={'action':'heal','family':'recovery','purpose':'heal','heal':True}
        else:
            # Ordinary acquisition/combat has already returned through the compact
            # loop. Only explicit survival recovery reaches this branch.
            resource_stalled=hunt.active_seconds-hunt.recovery_progress_at>=90
            if resource_stalled and not getattr(hunt,'recovery_reassessed',False):
                observe('reassess_recovery','No observed resource progress for 90 active seconds: reassess current capability/threat.',recovery_reassess=True)
            if prior_recovery or (getattr(hunt,'rest_before',None) and pending is None):
                observe('resources_improved','Linked own health/mana visibly improved; no enemy damage claim.',effect='resources_improved')
                observe('resources_unchanged','Linked own resources did not visibly improve; change recovery method.',effect='resources_unchanged')
                if fresh_errors:observe('recovery_error_observed','A new attributable heal error is visibly supported.',effect='resources_unchanged',cast_error=fresh_errors[-1])
            if prior_motion:
                observe('motion_useful','The linked escape improved the emergency situation.',effect='motion_useful')
                observe('motion_no_useful_effect','The linked escape had no useful effect.',effect='motion_no_useful_effect')
            if not resource_stalled:observe('rest','No current threat: wait briefly, then compare own resource bars.',rest=True)
            known_low_health=(self.current_hud.get('player_health') is not None
                and self.current_hud['player_health']<=self.config['heal_health_fraction']
                and (self.current_hud.get('health_confidence') or 0)>=.55)
            if not known_low_health:
                observe('recovered_resume','Own resources/world are now safe; resume only with fresh enemy inspection.',recovered=True)
            observe('blocked_resources','No supported safe recovery remains; observe without input.',block='blocked_resources')
            heal=controls.get('lesser_heal') or {};self_key=controls.get('target_self') or {}
            if not hunt.no_mana and not hunt.heal_unavailable and allowed('heal','recovery') and all(b.get('verified_from') and type(b.get('keycode')) is int for b in (heal,self_key)):
                binding=({'type':'cast_self_heal','spell':'Lesser Heal'} if self.config['preserve_target_heal'] else {'type':'keypress_sequence','keycodes':[self_key['keycode'],heal['keycode']]})
                choices.append(ActionCandidate('heal_self','Own health is genuinely low and mana/spell are ready: heal myself once and reassess the current enemy.',binding,precondition=valid))
                metadata['heal_self']={'action':'heal','family':'recovery','purpose':'heal','heal':True}
            for action in ('backward','turn_left','turn_right'):
                if allowed(action,'motion'):motor(action,action,'Immediate survival emergency: one short local escape on visibly safe ground.',family='motion',purpose='recovery_escape')
            observe('ui_blocked','An active typing field or blocking modal/menu prevents recovery input; passive chat does not.',ui=True)
        observe('dead_or_unrecoverable', 'I am visibly dead. Stop/release; no corpse automation. Ordinary stalls use blocked observations instead.', stop=True)
        if not positive_ui and prior_combat and any(cue['kind']=='unavailable_spell' for cue in fresh_errors):
            observe('unsupported_capability', 'Fresh linked evidence confirms required Smite/control unavailable. Stop unsuccessfully; no training.', capability=True)
        observe('outcome_unclear' if hunt.unclear < 3 else 'outcome_safe_hold', 'Effect remains unknown; do not invent success. After repeated identical uncertainty, report blocked and observe at low rate.', unclear=True)

        # Drop optional annotations/plans before concrete correction/recovery exits.
        # Episode menus retain at most 3 eligible spell choices and max 20 total.
        optional = {'xp_unchanged_observed', 'xp_change_observed', 'adopt_observed_patch', 'resume_destination', 'navigation_or_hazard_blocked'}
        core = [choice for choice in choices if choice.option not in optional and not choice.option.startswith('choose_area:')]
        extras = [choice for choice in choices if choice not in core]
        if len(core) > 20:
            # Outcome-only choices own the next assessment when an action is
            # pending. Broad search/acquire declarations are irrelevant here.
            deprioritized = {'target_enemy', 'search_here', 'explore_visible', 'change_area', 'rest', 'turn_right', 'strafe_right'}
            extras = [choice for choice in core if choice.option in deprioritized] + extras
            core = [choice for choice in core if choice.option not in deprioritized]
        choices = core + extras[:max(0, 20 - len(core))]
        if linked and pending and pending['family']=='clear' and not positive_ui:
            choices=[choice for choice in choices if choice.binding.get('type')=='observe_only']
        if self.null_answers:
            # A valid abstention gets a shorter fresh concrete menu. Preserve
            # all current outcome/level/safety exits before alternate methods.
            required = {'ui_blocked', 'dead_or_unrecoverable', 'outcome_unclear', 'outcome_safe_hold',
                        'world_normal_confirmed', 'correction_observed', 'recovered_resume',
                        'blocked_changed_assessment', 'blocked_still_unresolved', 'blocked_resources', 'safe_hold'}
            outcomes = {'combat_unchanged', 'cast_active', 'cast_error_observed', 'cast_ready',
                        'motion_useful', 'motion_no_useful_effect', 'target_cleared', 'target_clear_failed',
                        'target_still_absent', 'resources_improved', 'resources_unchanged', 'recovery_error_observed'}
            retained = [choice for choice in choices if choice.option in required | outcomes]
            concrete = [choice for choice in choices if choice not in retained and choice.binding.get('type') != 'observe_only']
            alternatives = [choice for choice in choices if choice not in retained and choice not in concrete
                            and choice.option in {'rest', 'recover_now', 'loading_wait', 'navigation_or_hazard_blocked'}]
            choices = retained + (concrete + alternatives)[:max(0, 10 - len(retained))]
            if len(choices)<2:choices += [choice for choice in core+extras if choice not in choices][:2-len(choices)]
        if len(choices) > 20:raise ValueError(f'Grinding {phase} menu exceeds finite API: {len(choices)}')
        focused_ui = False
        if (positive_ui and ui['positive'] and not loading and not reassessment
            and pending is None and not hunt.input_effect_unverified
            and {'close_visible_ui', 'decline_invitation'}.issubset(item.option for item in choices)):
            from sage_wow.agent.grind_resources import hud_resources
            own = await asyncio.to_thread(hud_resources, self, frame)
            if (own.get('frame_id') == frame.frame_id and own.get('player_health') is not None
                and own['player_health'] > self.config['critical_health_threshold']
                and (own.get('health_confidence') or 0) >= .8):
                choices = [item for item in choices if item.option in {'close_visible_ui', 'decline_invitation'}]
                focused_ui = True
        alternate = hunt.unclear >= 2 or self.null_answers > 0
        before_path = pending.get('source_image') if linked and pending else hunt.correction_evidence.get('source_image') if hunt.correction_evidence else getattr(hunt,'rest_before',{}).get('source_image') if getattr(hunt,'rest_before',None) else getattr(hunt, 'progress_before', None) if getattr(hunt, 'death_frame', None) else None
        panels=[]
        if before_path and not reassessment and not (positive_ui and not (pending and pending['family'] == 'ui')):
            before_proof=pending if linked and pending else hunt.correction_evidence or getattr(hunt,'rest_before',None)
            panels.append((before_path,before_proof['source_hash'] if before_proof else None,
                'BEFORE LINKED ACTION (historical; receipt/generation in context)',32))
        image=await self.prepare_image(frame,composed_evidence_image,frame,
            (0,0,frame.width,frame.height) if positive_ui else target['box'],
            Path(frame.image_path).with_name(f'grind-view-{frame.frame_id}.png'),history=tuple(panels),
            alternate=alternate,player_badge=tuple(self.config['player_level_box']),
            crop_label='FULL WORLD UI EVIDENCE' if positive_ui else 'CALIBRATED TARGET HUD REGION — MAY BE EMPTY')
        if isinstance(image,CycleResult):return image
        from sage_wow.agent.grind_combat import compact_context
        context=compact_context(level or 1,{**target,'presence':hunt.selected_presence},phase,hunt.compact_last_result,band=hunt.target_band(level or 1),preference=hunt.target_preference(level or 1))
        from sage_wow.agent.grind_inventory import context as inventory_context
        context+=inventory_context(self)
        question='What current UI closure, survival recovery or observation is needed? Passive chat is normal HUD. Own effects and enemy effects are separate; no input receipts imply damage or credit.'
        if reassessment:
            context, question = blocked_context, blocked_question
        if positive_ui and not loading and not reassessment:
            from sage_wow.agent.grind_ui import context as ui_context
            context = ui_context(ui)
            question = ('Dismiss the current guild invitation using either offered closure. Do not join. '
                'Use null only if neither offered dismissal is visibly safe.' if focused_ui else
                'Close the currently visible blocking UI using a supported closure. If closure input already occurred, '
                'verify whether that dialog is still present or the world is clear on this fresh view.')
        if phase=='recover' and not reassessment:
            if hunt.failures['recovery']>=2:
                context+=(f' Current own resources: {self.current_hud}. The recovery failure allowance '
                    'is spent on prior non-improving or unassessed recovery outcomes. This does not '
                    'prove no healing occurred or that the spell is unavailable. Use the current '
                    'own resources to choose an available recovery, escape, hold or safe resumption.')
            recovery_options={'resources_improved','resources_unchanged','recovery_error_observed','motion_useful','motion_no_useful_effect','rest','recovered_resume','blocked_resources','heal_self','backward','turn_left','turn_right','reassess_recovery','dead_or_unrecoverable','outcome_unclear','outcome_safe_hold','ui_blocked'}
            choices=[choice for choice in choices if choice.option in recovery_options]
        self.event('tactical_context', {'task': 'GRIND_' + phase.upper(), 'player_level': level, 'hunt_plan': hunt.plan, 'failures': hunt.failures})
        result = await self.decide(frame,image,context,question,choices,travel_budget=self.travel_budget,compact=True)
        receipt = result.receipt or {}
        chosen = getattr(result.decision, 'chosen', None)
        meta = metadata.get(chosen, {})
        accepted = (result.status == 'dispatched' and receipt == self.cycle.last_receipt and receipt.get('completed') and not receipt.get('dispatch_unknown') and not receipt.get('error'))
        if (receipt.get('possible_input') or receipt.get('dispatch_unknown')) and not accepted:
            self.stop('partial_or_unknown_grind_input')
        elif accepted:
            await self.apply_choice(frame, target, measurement, pending, linked, result, {**meta,'scoped_question':True}, chosen, level)
        self.finish_question(result,meta)
        self.flush_outcomes()
        self.same_casts = hunt.failures['combat'];self.streak = hunt.unclear
        self.history = (self.history + [{'frame_id': frame.frame_id, 'captured_at': frame.captured_at, 'target': target['name'], 'level_proposals': target['levels'], 'position_proposals': [measurement['position']] if measurement['position'] else [], 'local_caption_proposals': measurement['zone_proposals'], 'error_proposals': error_cues, 'choice': chosen, 'status': result.status, 'detail': result.detail, 'input_generation': self.cycle.input_generation, 'progress_claim': False, 'phase': hunt.phase}])[-8:]
        self.store.save_checkpoint('grind_only', {'mode': 'grind_only', 'level': self.level.last_confirmed_level, 'baseline_verified': self.baseline, 'history': self.history, 'hunt': hunt.context(), 'stopped': self.stopped, 'reason': self.reason, 'session_epoch': self.cycle.session_epoch, 'deadline': self.deadline})
        return result
    async def focused_decision(self,frame,target,measurement,pending,linked,level,*,kind,errors=()):
        """One selected-unit or immutable movement question, separate from hunting."""
        hunt=self.hunt;history=hunt.target_history.get((hunt.approach or {}).get('history_key'))
        target_name=target['name'] or (pending or {}).get('target',{}).get('name') or 'the selected target'
        if kind=='motion' and pending.get('purpose') in {'approach','cast_correction','standing'}:
            if not linked or key(target.get('name'))!=key(pending.get('target',{}).get('name')) or target['self_target'] or target['invalid_text']:
                hunt.archive_pending('selected_target_or_receipt_continuity_lost');self.flush_outcomes()
                return CycleResult('grind_reobserve',detail='Movement assessment lost target/receipt continuity; unresolved attempt retained')
        positioning=bool(hunt.approach and hunt.approach['positioning'])
        if kind=='motion':identity=str(('motion_outcome',pending['receipt']['receipt_id']))
        elif kind in {'cast_error','lost'}:identity=str(('cast_feedback',pending['receipt']['receipt_id']))
        elif kind=='capacity':identity='retained_target_capacity'
        else:identity=str(('approach',history['key']))
        if not self.select_question(identity,'motion_outcome' if kind=='motion' else 'cast_feedback' if kind=='cast_error' else 'approach'):
            return CycleResult('grind_blocked',detail='Unresolved question capacity retained; no untracked input')
        if self.travel_budget and self.travel_budget.remaining()<=0:return self.timing_expired('focused_preparation')
        choices=[];metadata={};withheld=[];controls=self.profile.values['controls']['bindings']
        valid=lambda:self.fresh(frame) and not self.cycle._scope_error(frame) and not hunt.blocked
        def observe(option,text,**meta):
            choices.append(ActionCandidate(option,text,{'type':'observe_only'}));metadata[option]=meta
        if kind=='motion':
            toward=pending.get('purpose') in {'approach','cast_correction','standing'}
            question=('Did the movement stand our player up?' if pending.get('purpose')=='standing' else
                f'Comparing BEFORE with CURRENT, did that movement bring us closer to {target_name}, face it better, or improve the approach?' if toward else
                'Did that movement establish a useful, visibly safe changed search sector?')
            observe('motion_useful',('Our player visibly changed from sitting/kneeling to standing.' if pending.get('purpose')=='standing' else
                'The world comparison shows we are closer to the selected creature, face it better, or have a clearer approach. Partial improvement counts; spell range need not already be reached.' if toward else
                'The linked BEFORE/AFTER supports a useful safe changed search sector.'),effect='motion_useful',correction=pending.get('purpose') in {'cast_correction','standing'})
            observe('motion_no_useful_effect','The linked BEFORE/AFTER shows no useful effect for this movement purpose; do not infer collision from unreadability.',effect='motion_no_useful_effect')
        elif kind=='lost':
            question='Did the earlier encounter selection disappear or change without an observed death?'
            observe('target_lost_unknown','The earlier encounter selection disappeared or changed without observed death. Retain its unassessed cast obligation; no kill/XP claim.',lost=True)
        elif kind=='capacity':
            question='The retained target-history capacity is full. Should we reject this untracked selection or keep observing safely?'
        elif kind=='cast_error':
            question=f'What current error, if any, arose from our last cast at {target_name}?'
            positional=next(cue for cue in errors if cue['kind'] in {'range','facing','los'})
            observe('cast_error_observed','The newly arising linked center-screen error is attributable to the last cast; confirm its supplied kind, without claiming correction.',effect='combat_unchanged',cast_error=positional)
            observe('cast_active','The cast or cooldown is visibly still active; wait and preserve its receipt.',cast_active=True)
        else:
            question=(f'What single local movement should we take to improve our position or facing toward {target_name}?' if positioning else
                f'What single action should we take to correct our engagement with {target_name}?' if history['cast_attempted'] else
                f'{target_name} is selected. We have not cast yet. What single action should we take to engage this target?')
            purpose='cast_correction' if history['cast_attempted'] and (history['cast_obligation'] or hunt.cast_obligation) else 'approach'
            for action in (('forward','backward','turn_left','turn_right','strafe_left','strafe_right') if positioning else ('forward','turn_left','turn_right')):
                binding=controls.get(action) or {}
                debt=hunt.motion_key(action,purpose,history['key'])
                if hunt.motion_count(action,purpose,history['key'])>=2:
                    withheld.append(action+(': outcome unresolved' if hunt.unresolved_motion.get(debt) else ': two no-useful attempts'));continue
                if not binding.get('verified_from') or type(binding.get('keycode')) is not int:
                    withheld.append(action+': missing verified binding');continue
                seconds=self.config['turn_seconds'] if action.startswith('turn') else self.config['move_seconds'] if action=='forward' else .15
                choices.append(ActionCandidate(action,f'One bounded {action} for {seconds:.2f} seconds on CURRENT visibly safe ground to improve position/facing toward this selected unit; no blind hazard entry.',
                    {'type':'keypress','keycode':binding['keycode'],'hold_seconds':seconds},precondition=valid,
                    dispatch_guard=self.travel_guard(frame,target)))
                metadata[action]={'action':action,'family':'motion','purpose':purpose}
            same=key(target['name'])==hunt.last_target and history['key']==hunt.combat_history_key
            obligation=history['cast_obligation'] or (hunt.cast_obligation if same else None)
            debt=history['combat_failures'] if not same else max(history['combat_failures'],hunt.failures['combat'])
            exhausted=history['correction_rounds']>=3 or same and hunt.encounter>0 and not hunt.encounter_ended and hunt.active_seconds-hunt.last_progress_active_at>=60
            rejected=history['rejections'] or hunt.rejected_signatures.get(hunt.rejection_signature(target),0)
            from sage_wow.agent.grind_progression import deferred, dispatch_basis
            if not positioning and not deferred(self,target,frame,measurement) and not rejected and not exhausted and (not obligation and debt<2 or hunt.retry_credit and same):
                low,high=hunt.target_band(level)
                for n in range(low,high+1):
                    if target['levels'] and target['levels']!=[n]:continue
                    description=f'The selected unit is visibly living, hostile/attackable and non-player, exactly level {n} in the permitted band; normal world and ready Smite; range and facing look plausible for one bounded attempt. Cast once; receipt is not damage.'
                    dispatch_policy={}
                    choices.append(ActionCandidate(f'attack_mob_level_{n}',description,{'type':'cast_guarded','spell':'Smite'},precondition=lambda policy_state=dispatch_policy:valid() and (not policy_state or policy_state.get('basis')==dispatch_basis(self)),dispatch_guard=await self.guard(frame,target,exact_level=n,strategy_state=dispatch_policy)))
                    metadata[f'attack_mob_level_{n}']={'dispatch_policy':dispatch_policy,'action':'cast','family':'combat','purpose':'cast','mob_level':n,
                        'new_encounter':bool(not same and hunt.last_target is not None or not history['cast_attempted'] and history['new_life'])}
            if not positioning:observe('positioning_correction','Direct forward/turn does not fit; assess one focused local movement. No input or retry granted.',positioning=True)
        if kind!='motion':
            escape=controls.get('escape') or {}
            if escape.get('verified_from') and type(escape.get('keycode')) is int:
                choices.append(ActionCandidate('reject_selected_target','This selected unit is unsuitable or the approach unsupported; clear only, then assess the clear result separately.',{'type':'keypress','keycode':escape['keycode'],'hold_seconds':.08},precondition=valid,dispatch_guard=await self.clear_guard(frame,target)))
                metadata['reject_selected_target']={'action':'reject','family':'clear','purpose':'clear','reject':True,
                    'abandon':bool(history and history['cast_attempted'])}
        observe('cannot_assess','Current evidence cannot answer this specific question. No input; retain the unresolved task/receipt.',unclear=True)
        observe('ui_blocked','A real blocking UI/text entry is visible; suspend world input and require fresh closure.',ui=True)
        observe('recover_now','An actual current urgent threat/resource emergency requires existing survival handling.',recover=True)
        observe('dead_or_unrecoverable','My own player is visibly dead; stop and release.',stop=True)
        panels=[]
        if linked and pending:
            panels.append((pending['source_image'],pending['source_hash'],'BEFORE LINKED ACTION / historical receipt',32))
            evidence=pending.get('position_error_evidence')
            if evidence and evidence['frame']['frame_id']!=frame.frame_id:
                panels.append((evidence['frame']['image_path'],evidence['sha256'],
                    'FIRST POSTCAST ERROR / historical attributable evidence',32))
        from sage_wow.agent.grind_motion_evidence import motion_context, motion_evidence_image
        movement_pair=kind=='motion' and linked and pending and len(panels)==1
        image=await self.prepare_image(frame,motion_evidence_image if movement_pair else composed_evidence_image,frame,target['box'],
            Path(frame.image_path).with_name(f'grind-view-{frame.frame_id}.png'),history=tuple(panels),
            alternate=bool(self.null_answers),player_badge=tuple(self.config['player_level_box']),
            **({'player_hud':tuple(from_profile(self.profile)['regions']['player_frame'])} if movement_pair and self.config['encounter_resources'] else {}),
            crop_label='CALIBRATED TARGET HUD REGION — MAY BE EMPTY',omit_target=pending.get('purpose')=='search')
        if isinstance(image,CycleResult):return image
        context={'player_level':level,'permitted_band':list(hunt.target_band(level)), 'target_preference':hunt.target_preference(level),
            'selected_proposals':{'name':target['name'],'levels':target['levels']},'history':history,
            'pending':({k:pending.get(k) for k in ('action','purpose','source_frame_id','target','debt_key')}|{'receipt_id':pending['receipt']['receipt_id'],'generation_after':pending['receipt']['generation_after']}) if pending else None,
            'fresh_error_proposals':list(errors),'withheld':withheld}
        self.event('grind_focused_question',{'question_kind':kind,'question_id':identity,'question':question,'options':[choice.option for choice in choices]})
        from sage_wow.agent.grind_combat import compact_context
        context=(motion_context(pending,target) if movement_pair else
            compact_context(level,target,kind,band=self.hunt.target_band(level),preference=self.hunt.target_preference(level)))
        result=await self.decide(frame,image,context,question,choices,travel_budget=self.travel_budget,compact=True)
        receipt=result.receipt or {};chosen=getattr(result.decision,'chosen',None);meta=metadata.get(chosen,{})
        accepted=result.status=='dispatched' and receipt==self.cycle.last_receipt and receipt.get('completed') and not receipt.get('dispatch_unknown') and not receipt.get('error')
        if receipt.get('possible_input') and not accepted:self.stop('partial_or_unknown_grind_input')
        elif accepted:
            if meta.get('positioning'):hunt.approach['positioning']=True
            await self.apply_choice(frame,target,measurement,pending,linked,result,{**meta,'scoped_question':True},chosen,level)
            if kind=='motion' and not meta.get('unclear') and not meta.get('ui') and not meta.get('recover'):
                if hunt.approach:
                    hunt.approach['positioning']=False
                    if pending.get('purpose') in {'approach','cast_correction','standing'}:hunt.phase='approach'
            if kind=='cast_error' and meta.get('cast_error') and hunt.approach:
                hunt.approach['positioning']=True;hunt.phase='approach';hunt.remember_approach()
        self.finish_question(result,meta);self.flush_outcomes()
        self.store.save_checkpoint('grind_only',{'hunt':hunt.context(),'deadline':self.deadline,'baseline_verified':self.baseline,'level':level})
        return result

    def enter_resource_recovery(self):
        hunt=self.hunt
        if hunt.phase!='recover':hunt.prior_phase=hunt.phase
        hunt.phase='recover';hunt.suspended_for_heal=hunt.encounter>0
        if hunt.recovery_started is None:
            hunt.recovery_started=hunt.active_seconds;hunt.recovery_progress_at=hunt.active_seconds

    async def apply_cast_error(self, error, pending, frame, result):
        """Explicit Sage assessment, from either linked or validated historical evidence."""
        from sage_wow.agent.grind_encounter_recovery import retain_error_source
        hunt=self.hunt
        hunt.cast_error={**error,'encounter_id':pending['encounter_id'],
            'selection_revision':pending.get('selection_revision'),
            'cast_receipt_id':pending['receipt']['receipt_id'],
            'first_postcast_frame':frame.frame_id,'status':'active',
            'assessment_source':retain_error_source(self,pending)}
        from sage_wow.agent.grind_cast_feedback import cue_key, retain_interrupted
        pending.setdefault('assessed_error_cues',[]).append(cue_key(error))
        retain_interrupted(hunt,pending)
        kind=error['kind']
        hunt.cast_obligation=f'Current linked {kind} error requires observed correction'
        if kind=='mana':hunt.no_mana=True
        if kind=='mana' or kind=='unavailable_spell' and pending['family']!='combat':
            self.enter_resource_recovery()
        if kind=='unavailable_spell':
            if pending['family']=='combat':
                self.event('grind_capability_failure',{'frame_id':frame.frame_id,
                    'request_id':result.decision.envelope.request_id,'session_epoch':self.cycle.session_epoch,
                    'reason':'unsupported_capability','terminal':True})
                self.stop('unsupported_capability')
            else:hunt.heal_unavailable=True
        if kind=='evade':hunt.correction_rounds+=1

    async def apply_choice(self, frame, target, measurement, pending, linked, result, meta, chosen, level):
        hunt = self.hunt
        if pending and 'selected_task_abandon' in pending:
            from sage_wow.agent.grind_relocation import close_selected_task
            close_selected_task(self,frame,target,pending,result)
            if meta.get('ui') or meta.get('recover') or meta.get('heal'):
                await self.apply_choice(frame,target,measurement,None,False,result,meta,chosen,level)
            return
        from sage_wow.agent import grind_travel_scout as travel_scout
        receipt = result.receipt
        from sage_wow.agent.grind_progression import accepted_placement
        if meta.get('mob_level') and not (meta.get('dispatch_policy') or {}).get('accepted_receipt_id'):
            accepted_placement(self,target,receipt,meta.get('dispatch_policy'),authenticate=True)
        from sage_wow.agent.grind_perception import carry_target_observation
        carry_target_observation(self,receipt)
        semantic = {'frame_id': frame.frame_id, 'request_id': (result.decision.envelope.request_id
                        if result.decision is not None else receipt['request_id']),
                    'session_epoch': self.cycle.session_epoch}
        if pending and pending['family']=='motion' and (meta.get('ui') or meta.get('recover') or meta.get('heal') or meta.get('family')=='ui'):
            hunt.archive_pending('safety_or_ui_interruption');pending=None;linked=False
        # Resolve first from saved evidence, then apply the complete transition.
        effect = meta.get('effect')
        if meta.get('correction') and pending and linked and pending['family'] == 'motion':effect = 'motion_useful'
        if effect and effect.startswith('resources_') and not pending and getattr(hunt,'rest_before',None):
            if effect=='resources_improved':hunt.recovery_progress_at=hunt.active_seconds;hunt.no_mana=False;hunt.recovery_reassessed=False
            else:hunt.failures['recovery']+=1
            rest=hunt.rest_before
            self.event('grind_action_outcome',{**semantic,'purpose':'rest','outcome':effect,'receipt_id':None,'source_frame_id':rest['frame_id'],'input_generation':rest['input_generation']})
            hunt.rest_before=None
        if effect=='motion_useful' and pending and pending.get('purpose') in {'approach','cast_correction','standing'}:
            from sage_wow.agent.grind_correction_handoff import current
            if hunt.pending is not pending or not linked or not current(self,frame,target,pending):
                self.event('grind_correction_claim_rejected',{**semantic,'claimed_outcome':effect,
                    'receipt_id':pending['receipt']['receipt_id'],'reason':'correction source or current continuity invalid'})
                effect='unknown'
        clear_verified = (meta.get('cleared') and effect=='target_cleared'
            and hunt.verify_intentional_clear(self,frame,target,pending,result))
        if chosen=='no_selected_frame' and meta.get('absence'):
            from sage_wow.agent.grind_absence_handoff import accept
            accept(self,frame,target,pending,result,meta.get('encounter_absence'))
        if (effect=='target_absent' and pending and linked and pending.get('family')=='target'
            or chosen=='no_selected_frame' and meta.get('absence')):
            from sage_wow.agent.grind_inspection import reanchor_absence
            reanchor_absence(self,frame,target,pending,result)
            from sage_wow.agent.grind_relocation import close_retained_clear
            close_retained_clear(self,frame,target,result)
        if effect:
            outcome = hunt.resolve(effect, frame, measurement, pending=pending, linked=linked)
            if outcome:self.event('grind_action_outcome', {**outcome, **semantic})
        if meta.get('cast_active'):
            self.wait_until = time.time() + 2.5
            # Keep unresolved physical receipt for the actual completion frame.
            return
        error = meta.get('cast_error')
        if error and pending and linked:
            await self.apply_cast_error(error,pending,frame,result)
            if error['kind']=='mana' or error['kind']=='unavailable_spell' and pending['family']!='combat':meta={**meta,'recover':True}
        if meta.get('correction'):
            from sage_wow.agent.grind_correction_handoff import current
            if current(self,frame,target):hunt.grant_correction()
        if meta.get('ready'):
            if hunt.cast_error and hunt.cast_error.get('status') == 'active':
                hunt.cast_error['status'] = 'ready_retry';hunt.cast_obligation = None;hunt.retry_credit = True
        if meta.get('recovery_reassess'):
            hunt.recovery_reassessed=True
            self.event('grind_capability_failure',{**semantic,'reason':'resource_reassessment','terminal':False})
        if meta.get('strategy'):
            travel_scout.transfer(self,'sage_strategy_choice')
            hunt.phase='choose_area';hunt.planning_requested=True
            if not hunt.strategy_required:
                hunt.strategy_required={**semantic,'captured_at':frame.captured_at,
                    'search_revision':hunt.search_revision}
            from sage_wow.agent.grind_relocation import record
            record(self,frame,target,semantic['request_id'],selected_task=meta.get('selected_disposition'))
        if meta.get('disengage_threat'):
            hunt.disengagement={**semantic,'encounter_id':hunt.encounter,
                'evidence':deepcopy(hunt.active_threat),'status':'intent_only_threat_unresolved'}
            hunt.phase='choose_area';hunt.planning_requested=True
            if not hunt.strategy_required:
                hunt.strategy_required={**semantic,'captured_at':frame.captured_at,
                    'search_revision':hunt.search_revision,'reason':'explicit_disengagement'}
            self.event('grind_disengagement_requested',dict(hunt.disengagement))
        if (meta.get('area') or meta.get('visual')) and hunt.strategy_required:
            from sage_wow.agent.grind_relocation import record, intent_current
            if not intent_current(self):record(self,frame,target,semantic['request_id'])
        if meta.get('area'):
            from sage_wow.agent.grind_navigation_recovery import record as navigation_review_record, note_destination
            if navigation_review_record(self):navigation_review_record(self)['explicit_planning']=None
            note_destination(self,meta['area'])
            travel_scout.transfer(self,'sage_destination_choice',search=False)
            hunt.choose(meta['area'], frame, result.decision.envelope.request_id, level)
        if meta.get('visual'):
            from sage_wow.agent.grind_navigation_recovery import record as navigation_review_record
            if navigation_review_record(self):navigation_review_record(self)['explicit_planning']=None
            travel_scout.transfer(self,'sage_visual_destination',search=False)
            # A chosen visible exploration path replaces the active coordinate
            # destination; choose() retains its results and failed approaches.
            area={'id':'visual_'+frame.frame_id,'label':'Current visible exploration path',
                'coordinate':None,'zone_reference':None,'expected_level_range':None,
                'source':{'kind':'fresh_sage_visible_region','frame_id':frame.frame_id,
                    'claim':'Sage chose visible exploration; destination and population unverified'}}
            encounter_area=hunt.encounter_area
            hunt.choose(area,frame,result.decision.envelope.request_id,level)
            # Replanning does not relocate the retained encounter's history.
            hunt.encounter_area=encounter_area
            hunt.plan['visual_reference'] = {'frame_id': frame.frame_id, 'kind': 'current_visible_path'}
        if meta.get('phase'):hunt.phase = meta['phase'];hunt.planning_requested = False
        if meta.get('opportunity'):
            hunt.suspended = True;hunt.phase = 'search'
            travel_scout.transfer(self,'accepted_hunting_opportunity')
        if meta.get('resume_destination'):
            travel_scout.transfer(self,'sage_resume_destination',search=False)
            if hunt.plan:hunt.plan['phase']='travel'
            hunt.phase = 'travel';hunt.suspended = False;hunt.encounter_area = None
        if meta.get('change_area'):
            hunt.result().update(status='depleted_or_unsupported_hypothesis', local_failures=dict(hunt.failures), tried_sectors=list(hunt.tried_sectors), sector=hunt.sector)
            hunt.exhausted_patches += 1;hunt.phase = 'choose_area';hunt.planning_requested = True
        if meta.get('ui') or meta.get('family') == 'ui':
            if hunt.phase not in {'ui_recover', 'input_effect_unverified'}:hunt.prior_phase = hunt.phase
            hunt.phase = 'ui_recover'
        if chosen == 'world_normal_confirmed':
            self.require_world = False;hunt.input_effect_unverified = False;hunt.failures['ui'] = 0;hunt.changed_situation('ui')
            from sage_wow.agent.grind_inspection import resume_focus
            resume_focus(self,frame,result)
            restore=hunt.prior_phase or self.world_resume_phase
            if hunt.phase not in {'recover','fight'}:
                hunt.phase=travel_scout.restore_phase(self,restore,fallback='choose_area' if hunt.plan is None else 'search')
                hunt.prior_phase=None
            self.world_resume_phase=None
        if meta.get('reject'):
            if hunt.approach and key(target.get('name'))==key(hunt.target_history[hunt.approach['history_key']]['name']):
                hunt.target_history[hunt.approach['history_key']]['rejections']+=1
            rejected = hunt.rejection_signature(target)
            hunt.rejected_signatures[rejected] = hunt.rejected_signatures.get(rejected, 0) + 1
            hunt.rejected_name = key(target['name']);hunt.phase = 'search'
            if meta.get('abandon'):
                # Escape is an attempted selection clear, not observed escape
                # from a hostile encounter. Its next feedback may be failed.
                hunt.terminal_reason = 'abandon_requested_unconfirmed'
                self.event('grind_encounter_transition', {**semantic, 'from': 'correct', 'to': 'abandon_requested', 'reason': hunt.terminal_reason, 'encounter_id': hunt.encounter})
            scout=meta.get('out_of_band_scout')
            scout_source_current=False
            if scout:
                try:scout_source_current=hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()==scout['source_hash']
                except OSError:pass
            if (scout and scout['session_epoch']==self.cycle.session_epoch
                and scout['input_generation']==receipt.get('generation_before')
                and receipt.get('source_frame_id')==frame.frame_id and receipt.get('possible_input')
                and hunt.known_completed_input({'receipt':receipt})
                and self.level.last_confirmed_level==level and list(hunt.target_band(level))==scout['band']
                and scout_source_current):
                fact=hunt.scout_rejected(scout,measurement,receipt,retain_destination=travel_scout.active(self))
                self.event('grind_out_of_band_scout',fact)
        if effect=='clear_failed':
            fact=hunt.intentional_clear_current() or {}
            if fact.get('disposition')=='submitted':fact['disposition']='failed'
        if meta.get('cleared'):
            hunt.target_continuity+=1
            hunt.selection_revision += 1;hunt.selection = None;hunt.phase = 'search'
            if not travel_scout.active(self) and hunt.strategy_required and hunt.strategy_required.get('reason') in {'out_of_band_scout','level_progression'}:
                hunt.phase='choose_area';hunt.planning_requested=True
        if meta.get('progression_reject') and not travel_scout.active(self):
            from sage_wow.agent.grind_progression import request
            request(hunt,level,frame)
        if meta.get('recover') or meta.get('heal'):
            if chosen=='urgent_state':hunt.disengagement=None
            self.enter_resource_recovery()
        if meta.get('recovered'):
            if hunt.suspended_for_heal:
                if self.config['preserve_target_heal']:
                    hunt.compact_stage='inspect';hunt.selected_presence=None
                else:
                    hunt.compact_stage='acquire';hunt.selected_presence=False;hunt.archive_pending('self_heal_invalidates_prior_cast_authority')
            episode=hunt.travel_urgent_episode
            if episode and episode['evidence_signature']==self.last_signature and episode['gameplay_receipt_id']==hunt.last_gameplay_receipt_id:
                prior=hunt.travel_urgent_debt
                count=prior['completed_roundtrips'] if prior and prior['evidence_signature']==self.last_signature and prior['gameplay_receipt_id']==hunt.last_gameplay_receipt_id else 0
                hunt.travel_urgent_debt={**episode,'completed_roundtrips':count+1}
            hunt.travel_urgent_episode=None
            hunt.phase = travel_scout.restore_phase(self,hunt.prior_phase);hunt.prior_phase = None
            hunt.recovery_started = None;hunt.no_mana = False
            if hunt.cast_error and hunt.cast_error.get('status')=='active' and hunt.cast_error.get('kind')=='mana':
                hunt.cast_error['status']='retired_by_resource_recovery'
                hunt.cast_obligation=None;hunt.retry_credit=True
        if meta.get('death'):
            hunt.target_continuity+=1
            hunt.disengagement=None
            if hunt.combat_history_key in hunt.target_history:
                hunt.target_history[hunt.combat_history_key]['completed']=True
            hunt.target_dead_observed = True;hunt.encounter_ended = True;hunt.phase = 'search';hunt.death_frame = frame.frame_id
            fact = {'kind': 'selected_encounter_death', **semantic, 'encounter_id': hunt.encounter, 'xp_known': False, 'kill_attribution_known': False}
            hunt.progress_facts.append(fact);self.event('grind_death_observed', fact)
        if meta.get('lost'):
            hunt.encounter_ended = True;hunt.phase = 'search';hunt.terminal_reason = 'lost_unknown'
            self.event('grind_encounter_transition', {**semantic, 'from': 'engage', 'to': 'ended', 'reason': 'lost_unknown', 'encounter_id': hunt.encounter})
        if meta.get('damage') and linked and pending:
            fact = {'kind': 'damage_observation', **semantic, 'before_frame_id': pending['source_frame_id'], 'receipt_id': pending['receipt']['receipt_id'], 'encounter_id': hunt.encounter}
            hunt.progress_facts.append(fact);self.event('grind_damage_observed', fact)
        if meta.get('xp'):
            fact = {'kind': 'visible_xp_change', **semantic, 'before_image': hunt.progress_before, 'encounter_id': hunt.encounter, 'amount_known': False, 'exclusive_cause_known': False}
            hunt.progress_facts.append(fact);hunt.xp_observed = True;self.event('grind_xp_change_observed', fact)
        if meta.get('xp_unchanged'):
            hunt.uncredited_encounters = getattr(hunt, 'uncredited_encounters', 0) + 1;hunt.xp_observed = True
            hunt.progress_facts.append({'kind': 'readable_xp_unchanged', **semantic, 'encounter_id': hunt.encounter})
            if hunt.uncredited_encounters >= 2:
                hunt.phase = 'choose_area';hunt.planning_requested = True
        if meta.get('stop'):self.stop('sage_observed_player_death')
        if meta.get('capability'):
            self.event('grind_capability_failure', {**semantic, 'reason': 'unsupported_capability', 'terminal': True});self.stop('unsupported_capability')
        if meta.get('mob_level'):
            from sage_wow.agent.grind_progression import accepted_placement
            placed=accepted_placement(self,target,receipt,meta.get('dispatch_policy'))
            travel_scout.transfer(self,'accepted_combat')
            hunt.disengagement=None
            if hunt.strategy_required:hunt.strategy_required.pop('selected_exit',None)
            from sage_wow.agent.grind_progression import primary
            if (primary(hunt,level,target['name']) or placed) and (hunt.progression_pending
                or (hunt.strategy_required or {}).get('reason')=='level_progression'):
                self.event('grind_primary_target_accepted',{**semantic,'name':target['name'],'level':meta['mob_level'],
                    'placement_policy':meta.get('dispatch_policy',{}).get('result') if placed else {'kind':'primary'}})
                if (hunt.strategy_required or {}).get('reason')=='level_progression':hunt.strategy_required=None
                hunt.planning_requested=False;hunt.progression_pending=False
            if hunt.strategy_required and hunt.strategy_required.get('reason')=='empty_local_search':
                self.event('grind_empty_search_opportunity_accepted',{**semantic,'level':meta['mob_level'],
                    'prior_search':hunt.strategy_required['frame_id']})
                hunt.strategy_required=None;hunt.planning_requested=False
            if hunt.strategy_required and hunt.strategy_required.get('reason')=='out_of_band_scout':
                self.event('grind_scouting_opportunity_accepted',{**semantic,'level':meta['mob_level'],
                    'band':list(hunt.target_band(level)),'prior_scout':hunt.strategy_required['frame_id']})
                hunt.strategy_required=None;hunt.planning_requested=False
            if hunt.approach:
                hunt.target_history[hunt.approach['history_key']]['cast_attempted']=True
                hunt.combat_history_key=hunt.approach['history_key']
            if meta.get('new_encounter'):
                hunt.changed_situation('combat');hunt.encounter_ended = True
            if hunt.encounter == 0 or meta.get('new_encounter'):
                hunt.progress_before = frame.image_path;hunt.death_frame = None;hunt.xp_observed = False
            if hunt.phase == 'travel':hunt.suspended = True
            hunt.no_acquisition_since=hunt.active_seconds;hunt.sector_new_candidate=False
            hunt.target_dead_observed = False;hunt.last_target = key(target['name'])
            if hunt.encounter_ended and hunt.encounter>0 and not meta.get('new_encounter'):hunt.encounter_ended=False
            hunt.encounter_seen(meta['mob_level'], frame, measurement,target['name']);hunt.phase = 'fight';hunt.suspended_for_heal = False
        if meta.get('unblock'):
            hunt.blocked = None;hunt.unclear = 0
            self.wait_until = 0
        if meta.get('remain_blocked'):
            episode = hunt.blocked;episode['evidence_signature']=self.last_signature;episode['interval'] = min(60, episode['interval'] * 2)
            episode['next_observation_at'] = min(time.time() + episode['interval'],episode['assessment_due_at']);self.wait_until = episode['next_observation_at']
        if meta.get('block'):self.enter_blocked(meta['block'], self.last_signature)
        if meta.get('loading'):
            self.wait_until = time.time() + 3
            if hunt.active_seconds - self.loading_started >= 90:self.enter_blocked('blocked_loading', self.last_signature)
        if meta.get('unclear') and not meta.get('scoped_question'):
            hunt.unclear += 1;self.wait_until = time.time() + 2
            if hunt.unclear >= 3:self.enter_blocked('blocked_semantic', self.last_signature)
        declaration = meta.get('semantic') or ('rest' if meta.get('rest') else None)
        if declaration:
            declaration_key = (declaration, self.last_signature)
            hunt.semantic_counts[declaration_key] = hunt.semantic_counts.get(declaration_key, 0) + 1
            if hunt.semantic_counts[declaration_key] >= 3:
                # Rest is an assessable wait, not a repeated empty declaration.
                if declaration != 'rest':hunt.unclear = max(3, hunt.unclear)
        if receipt.get('possible_input'):
            if meta.get('family') in {'motion','target','combat','recovery'}:hunt.last_gameplay_receipt_id=receipt['receipt_id']
            hunt.rest_before=None
            self.last_input_at = datetime.fromisoformat(receipt['occurred_at']).timestamp()
            if pending and hunt.pending is pending and not pending.get('outcome_consumed'):
                outcome = hunt.resolve('unknown', frame, measurement, pending=pending, linked=linked)
                if outcome:self.event('grind_action_outcome', {**outcome, **semantic})
            hunt.install(meta.get('action', chosen), meta.get('family', 'other'), frame, receipt, measurement, purpose=meta.get('purpose'), target={'name': target['name'], 'levels': target['levels']},selected_task_abandon=meta.get('selected_task_abandon'))
            if meta.get('family')=='clear':
                from sage_wow.agent.grind_clear import arm as arm_clear
                arm_clear(self,result)
            if meta.get('family')=='target':
                from sage_wow.agent.grind_acquisition import arm
                arm(self,result)
            self.wait_until = time.time() + (self.config['cast_wait_seconds'] if meta.get('family') in {'combat', 'recovery'} else .15)
            if meta.get('family') == 'combat':self.last_attack_frame = frame.image_path
        elif meta.get('rest'):
            if pending and hunt.pending is pending:
                outcome=hunt.resolve('unknown',frame,measurement,pending=pending,linked=linked)
                if outcome:self.event('grind_action_outcome',{**outcome,**semantic})
                elif hunt.pending is pending:hunt.archive_pending('rest_interrupted_unlinked_physical_outcome')
            hunt.correction_evidence=None;hunt.retry_credit=False
            # A wait is semantic, with an explicit before view, never a fabricated
            # physical receipt. It is assessed through recovery observations.
            hunt.rest_before = {'frame_id': frame.frame_id, 'source_image': frame.image_path,
                                'active_at': hunt.active_seconds,'source_hash':hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
                                'input_generation':self.cycle.input_generation,'session_epoch':self.cycle.session_epoch}
            self.wait_until = time.time() + min(15, 2.5 + hunt.failures['recovery'] * 2.5)
        elif pending and hunt.pending is pending and not meta.get('unclear') and not meta.get('remain_blocked'):
            # A semantic correction confirmation may deliberately arrive after
            # motion_useful. Keep its derived proof; retire only unresolved input.
            if not meta.get('positioning'):
                outcome = hunt.resolve('unknown', frame, measurement, pending=pending, linked=linked)
                if outcome:self.event('grind_action_outcome', {**outcome, **semantic})
        if clear_verified:hunt.reconcile_intentional_clear(self,frame,target)
        self.event('grind_recovery_budget', {**semantic, 'scope': hunt.phase, 'used': dict(hunt.failures), 'limit': 2, 'reset_basis': effect or 'none'})
        hunt.remember_approach()

    def travel_guard(self,source,expected,*,selected=None):
        from sage_wow.agent.grind_observation import digest, validate_continuity, ObservationSourceInvalid
        source_hash=digest(source);generation=self.cycle.input_generation
        requires_selected=selected if selected is not None else bool(expected.get('name') and self.hunt.phase in {'approach','fight'})
        async def check(_):
            if digest(source)!=source_hash:raise ObservationSourceInvalid('Travel source hash changed')
            self.stage('movement_dispatch_validation')
            if self.travel_budget.remaining()<=0:
                return DispatchValidation(False,detail='Travel source deadline expired before continuity capture')
            if not self.fresh(source,travel=True) or generation!=self.cycle.input_generation:
                return DispatchValidation(False,detail='Travel focus/input/source authority invalid')
            fresh=await asyncio.to_thread(self.capture)
            error=validate_continuity(self,source,fresh)
            if self.archive:fresh=replace(fresh,image_path=str(self.archive.frame(fresh,self.cycle.session_epoch,generation)))
            if self.travel_budget.remaining()<=0:
                return DispatchValidation(False,detail='Travel source deadline expired after continuity capture')
            if error:return DispatchValidation(False,detail=error)
            rows=await self.rows(fresh);actual=await self.target_proposal(fresh)
            if self.travel_budget.remaining()<=0:
                return DispatchValidation(False,detail='Travel source deadline expired after continuity OCR')
            if not self.fresh(source,travel=True) or generation!=self.cycle.input_generation:
                return DispatchValidation(False,detail='Travel focus/input/source authority invalid after continuity')
            self.stage('movement_pixel_continuity')
            predicates={'ui_clear':not ui_evidence(self.profile,fresh,rows,{})['positive'],
                'player_badge':same_patch(source.image_path,fresh.image_path,tuple(self.config['player_level_box']),badge=True)}
            if requires_selected:
                predicates.update(same_selected_name=bool(expected['name']) and key(expected['name'])==key(actual['name']),
                    selected_living=not actual['self_target'] and not actual['invalid_text'],
                    selected_level=not actual['levels'] or not expected['levels'] or expected['levels']==actual['levels'],
                    selected_badge=same_target_badge(source.image_path,fresh.image_path,expected['badge'],self.config.get('target_badge_continuity')))
            stable=all(predicates.values())
            return DispatchValidation(stable,fresh if stable else None,
                'Fresh movement identity confirmed' if stable else 'Movement identity changed; current assessment required',
                {'source_sha256':source_hash,'fresh_frame_id':fresh.frame_id,'fresh_image':fresh.image_path,
                    'selected_target_required':requires_selected,'predicates':predicates,
                    'failed_predicates':[name for name,value in predicates.items() if not value]})
        return check

    async def travel(self, frame, rows, target, measurement):
        source_at=datetime.fromisoformat(frame.captured_at).timestamp()
        budget=self.travel_budget or TravelDecisionBudget(source_at,min(source_at+12,self.deadline))
        self.travel_budget=budget
        if budget.remaining()<=0:
            self.event('travel_decision_expired',{'phase':'before_hud','source_at':source_at,'deadline':budget.deadline,
                'age_seconds':time.time()-source_at,'remaining_seconds':budget.remaining()})
            return CycleResult('travel_deadline_expired',detail='Travel source deadline expired before HUD observation')
        with self.cycle.scoped_execution_scope(task_id=self.task_id,objective_revision=self.revision,
            session_epoch=self.cycle.session_epoch,deadline_epoch=budget.deadline,input_generation=self.cycle.input_generation,
            is_current=self.current,max_frame_age_seconds=12):
            return await self._travel(frame,rows,target,measurement)

    async def _travel(self, frame, rows, target, measurement):
        """One compact same-phase decision after quantitative travel resolution."""
        hunt=self.hunt
        from sage_wow.agent.grind_relocation import decide_selected_exit, note_selected_transport
        selected_exit=await decide_selected_exit(self,frame,target,measurement)
        if selected_exit is not None:return selected_exit
        note_selected_transport(self,frame,target)
        from sage_wow.agent.grind_travel_recovery import current as retry_current, ready as retry_ready, basis as retry_basis, note as note_retry
        # A clean-HUD observation can advance input generation. Qualify the
        # question before that transaction; its existing restoration and input
        # guards still own all authority afterward.
        ordinary_retry=retry_current(self) and retry_ready(self,frame,target)
        snapshot=None
        if self.config['clean_world_observation']:
            snapshot=await self.observation.capture(frame)
            if snapshot is None:
                if self.travel_budget and self.travel_budget.remaining()<=0:
                    self.event('travel_decision_expired',{'phase':'clean_observation','source_at':self.travel_budget.source_at,'deadline':self.travel_budget.deadline,
                        'age_seconds':time.time()-self.travel_budget.source_at,'remaining_seconds':self.travel_budget.remaining()})
                    return CycleResult('travel_deadline_expired',detail='Travel source deadline expired during clean observation')
                transaction=self.observation_transaction or {}
                for name in ('hide_receipt','restore_receipt'):
                    receipt=transaction.get(name) or {}
                    if receipt.get('possible_input') and (not receipt.get('completed') or receipt.get('dispatch_unknown') or receipt.get('error')):
                        self.stop('partial_or_unknown_grind_input')
                        return CycleResult('grind_stopped',detail=self.reason)
                reason=('blocked_hud_capture_unavailable' if self.hud_no_effect_count>=2 else
                    'blocked_hud_restoration' if transaction.get('restoration_needed') else 'blocked_clean_observation')
                self.enter_blocked(reason,transaction.get('transaction_id'))
                return CycleResult('grind_blocked',detail='Clean-world observation unresolved; no fallback to cluttered travel')
            anchor=snapshot.restored
            restored_rows=await self.rows(anchor)
            restored_target=await self.target_proposal(anchor)
            from sage_wow.agent.grind_observation import hud_continuity
            proof=await read_player_identity(self,anchor,restored_rows)
            evidence=hud_continuity(self,frame,anchor,target,restored_target,restored_rows,identity_proof=proof)
            evidence['predicates']['ui_clear']=not ui_evidence(self.profile,anchor,restored_rows,{})['positive']
            evidence['failed_predicates']=[name for name,passed in evidence['predicates'].items() if not passed]
            compatible=not evidence['failed_predicates']
            self.event('grind_restored_hud_continuity',{'transaction_id':snapshot.transaction['transaction_id'],**evidence})
            if not compatible:
                hunt.prior_phase='travel';hunt.phase='recover';hunt.suspended=True
                return CycleResult('grind_reobserve',detail='HUD restored; current health/target/UI changed, fresh survival assessment required')
            self.last_frame_id=anchor.frame_id
            from sage_wow.agent.grind_resources import hud_resources
            target={**restored_target,'hud':await asyncio.to_thread(hud_resources,self,anchor)}
            measurement=await asyncio.to_thread(measure,self.profile,anchor,restored_rows,self.ocr)
            hunt.observe_zone(measurement,self.cycle.session_epoch,self.cycle.input_generation)
            path=await self.prepare_image(anchor,snapshot.image,Path(anchor.image_path).with_name(f'grind-view-{anchor.frame_id}.png'),selected_target=bool(target['name']))
            if isinstance(path,CycleResult):return path
            if not snapshot.current():
                Path(path).unlink(missing_ok=True)
                return CycleResult('grind_reobserve',detail='Clean observation authority changed during image composition')
            valid=lambda:self.fresh(anchor,travel=True) and snapshot.current() and not hunt.blocked
            guard=snapshot.guard
        else:
            anchor=frame
            path=await self.prepare_image(frame,travel_image,self.profile,frame,Path(frame.image_path).with_name(f'grind-view-{frame.frame_id}.png'))
            if isinstance(path,CycleResult):return path
            valid=lambda:self.fresh(anchor,travel=True) and not self.cycle._scope_error(anchor) and not hunt.blocked
            guard=self.travel_guard(frame,target)
        note_selected_transport(self,anchor,target)
        geometry=hunt.current_geometry(measurement)
        # Current geometry comes from restored telemetry; outcomes keep their original pair.
        hunt.feedback.update(mode='coordinate' if geometry['available'] else 'visual',
            position=geometry['position'],destination=geometry['destination'],
            distance=geometry.get('distance'),destination_minus_current=geometry.get('vector'),
            destination_map_sector=geometry.get('direction'))
        controls=self.profile.values['controls']['bindings']
        translation=min(1.,self.config['move_seconds'])
        situation,forward_option,current_forward=hunt.forward_decision(measurement,self.cycle.input_generation,
            self.cycle.session_epoch,translation,continuous=True)
        questions={
            'initial':'We have a destination but have not measured where forward takes us. Which short action should we take? Choose the forward probe if there is room for it; otherwise choose a movement that creates room.',
            'inconclusive':'The first forward probe was too small to establish direction at the coordinate resolution.',
            'progress_unclear':'Forward direction has been measured, but progress toward the destination is still unresolved at coordinate precision. Which short probe, turn or local detour best resolves or improves this approach?',
            'after_turn':'We just turned. The earlier forward direction is historical and does not describe the new facing. Which short action should we take to measure the new direction or create room for that measurement?',
            'away':'Measured forward movement takes us farther from the destination. Which turn or local detour best sets up movement toward it?',
            'progress':'Which short action best continues toward the destination? If an apparent obstruction lies on the route and we have not tried moving through it, try a short direct movement before deciding to go around it.',
            'repeated':'The repeated approach produced no measurable displacement. It may be obstructed. Which different movement should we try to change the approach or create room? Do not repeat the withheld action.',
            'detour':'Which short action continues or adjusts the selected detour? Temporary movement away is acceptable for this local bypass. Return toward the destination when the route permits.',
            'visual':'Which different movement should we try to change the approach or create room? Do not repeat the withheld action.'}
        descriptions={
            'probe_forward':'Move forward for {duration} seconds to measure where forward leads. This is an orientation probe.',
            'advance_forward':'Move forward for {duration} seconds. The current measured response supports progress toward the destination.',
            'detour_forward':'Move forward for {duration} seconds as part of the previously selected local detour. This may increase destination distance; reassess afterward.',
            'detour_backward':'Step backward for {duration} seconds to make room or escape the current approach.',
            'detour_strafe_left':'Step sideways left for {duration} seconds to go around the local obstruction or change the approach.',
            'detour_strafe_right':'Step sideways right for {duration} seconds to go around the local obstruction or change the approach.',
            'detour_forward_left':'Step diagonally forward-left for {duration} seconds as a local workaround.',
            'detour_forward_right':'Step diagonally forward-right for {duration} seconds as a local workaround.',
            'detour_backward_left':'Step diagonally backward-left for {duration} seconds to make room or change the approach.',
            'detour_backward_right':'Step diagonally backward-right for {duration} seconds to make room or change the approach.',
            'turn_left':'Turn left for {duration} seconds. The next suitable forward probe will measure the resulting direction.',
            'turn_right':'Turn right for {duration} seconds. The next suitable forward probe will measure the resulting direction.'}
        moves={
            'detour_backward':('backward',),'detour_strafe_left':('strafe_left',),'detour_strafe_right':('strafe_right',),
            'detour_forward_left':('forward','strafe_left'),'detour_forward_right':('forward','strafe_right'),
            'detour_backward_left':('backward','strafe_left'),'detour_backward_right':('backward','strafe_right'),
            'turn_left':('turn_left',),'turn_right':('turn_right',)}
        if forward_option:moves={forward_option:('forward',),**moves}
        choices=[];metadata={};withheld=[]
        for option,names in moves.items():
            bindings=[controls.get(name) or {} for name in names]
            if not all(binding.get('verified_from') and type(binding.get('keycode')) is int for binding in bindings):continue
            if len({binding['keycode'] for binding in bindings})!=len(bindings):continue
            turn=option.startswith('turn_');action=option.removeprefix('detour_') if option.startswith('detour_') else 'forward' if option in {'probe_forward','advance_forward'} else option
            seconds=self.config['turn_seconds'] if turn else translation
            if (not turn and not hunt.travel_allowed(action,measurement,seconds)) or not hunt.allowed(action,'motion'):
                withheld.append(f'{option}: repeated ineffective unchanged approach');continue
            description=descriptions[option].format(duration=f'{seconds:.2f}')
            binding={'type':'keypress','keycode':bindings[0]['keycode'],'hold_seconds':seconds} if len(bindings)==1 else {'type':'keypress_chord','keycodes':[item['keycode'] for item in bindings],'hold_seconds':seconds}
            def eligible(selected=option,action_name=action,duration=seconds):
                if not valid() or not hunt.allowed(action_name,'motion'):return False
                if selected in {'probe_forward','advance_forward','detour_forward'}:
                    return hunt.forward_decision(measurement,self.cycle.input_generation,self.cycle.session_epoch,duration,continuous=True)[1]==selected
                return True
            choices.append(ActionCandidate(option,description,binding,precondition=eligible,dispatch_guard=guard))
            metadata[option]={'action':action,'travel_purpose':'turn' if turn else 'probe' if option=='probe_forward' else 'destination progress' if option=='advance_forward' else 'detour',
                'requested_duration':seconds,'reason':description}
        if not forward_option:withheld.append(f'forward: {situation}; no current permission for this approach')
        strategy_required=deepcopy(hunt.strategy_required)
        destination_basis=hunt.destination_choice_basis(measurement)
        previous_destination_change=deepcopy(hunt.destination_change_basis)
        def destination_change_valid():
            return (valid() and hunt.destination_change_basis==previous_destination_change
                and hunt.destination_choice_basis(measurement)==destination_basis
                and previous_destination_change!=destination_basis)
        search_entry=hunt.travel_search_evidence(anchor,measurement,self.cycle.session_epoch)
        from sage_wow.agent.grind_progression import search_entry_allowed
        def search_entry_valid():
            if not valid() or hunt.strategy_required!=strategy_required or not search_entry_allowed(self,target,geometry,search_entry):return False
            return (not strategy_required or bool(search_entry and
                hunt.travel_search_evidence(anchor,measurement,self.cycle.session_epoch)==search_entry))
        for option,description in (
            ('begin_hunt','Begin searching for or assessing mobs here because the destination region is reached or a visible creature warrants assessment. This answer does not attack.'),
            ('change_destination','Return to destination selection because this route or search location should be replaced. Preserve the previous failed approaches.'),
            ('urgent_state','An actual attacker, death, resource emergency, or blocking dialog requires the existing recovery/UI flow. Ordinary terrain obstacles do not qualify.')):
            if option=='begin_hunt' and not search_entry_allowed(self,target,geometry,search_entry):
                withheld.append('begin_hunt: continue to the primary hunting destination or freshly inspect a primary creature; nearby wolves do not end this relocation')
                continue
            if option=='begin_hunt' and strategy_required and not search_entry:
                withheld.append('begin_hunt: requested strategy change needs a fresh observed changed travel endpoint')
                continue
            if option=='change_destination' and previous_destination_change==destination_basis:
                withheld.append('change_destination: already replanned this unchanged destination and position; choose a supported movement or recovery before repeating the same declaration')
                continue
            choices.append(ActionCandidate(option,description,{'type':'observe_only'},
                precondition=search_entry_valid if option=='begin_hunt' else destination_change_valid if option=='change_destination' else valid,dispatch_guard=guard))
        from sage_wow.agent import grind_travel_scout as travel_scout
        scout_offer=travel_scout.candidate(self,anchor,target,measurement,valid)
        scout_state=None
        if scout_offer:
            scout_choice,scout_state=scout_offer
            choices.append(scout_choice)
        from sage_wow.agent import grind_travel_menu as retry_menu
        from sage_wow.agent import grind_navigation_recovery as navigation_recovery
        navigation_recovery.refresh(self,anchor,measurement,target)
        full_candidate_ids=[candidate.option for candidate in choices]
        full_choices=choices
        choices,retry_page,exhausted=retry_menu.select(self,anchor,choices)
        review=None
        if exhausted:
            self.event('grind_travel_retry_menu_exhausted',{'frame_id':anchor.frame_id,
                'available_ids':full_candidate_ids,**retry_page,'progress_credit':False})
            selected_exit=await decide_selected_exit(self,anchor,target,measurement,exhausted=True)
            if selected_exit is not None:
                Path(path).unlink(missing_ok=True)
                return selected_exit
            choices,review_metadata,review=await navigation_recovery.prepare(
                self,anchor,target,measurement,full_choices,valid,guard)
            metadata.update(review_metadata)
            if not choices:
                choices,reconsider_page=navigation_recovery.reconsider(self,anchor,target,full_choices,hunting_unavailable=True)
                if not choices:
                    Path(path).unlink(missing_ok=True)
                    return navigation_recovery.wait(self,'no_action_selected',anchor,target)
                review=None
                retry_page=reconsider_page
        level=self.level.last_confirmed_level
        last=hunt.last_completed_action
        self.event('tactical_context',{'task':'GRIND_TRAVEL','player_level':level,'hunt_plan':hunt.plan,
            'travel_last':last,'current_geometry':geometry,'decision_needed':situation,'withheld':withheld,
            'clean_observation':snapshot.transaction if snapshot else None})
        from sage_wow.agent.grind_relocation import recovery_context, travel_context
        resumed_context=recovery_context(self,geometry,ordinary_retry=ordinary_retry)
        from sage_wow.agent.grind_travel_context import build as travel_question, TravelContextOverflow
        offered_ids={candidate.option for candidate in choices}
        question=questions[situation]
        if retry_page:
            question=retry_menu.question(choices)
        elif (forward_option and forward_option not in offered_ids) or not offered_ids.intersection(metadata):
            question='Choose the next action for this hunting task from the currently offered actions.'
        try:
            if review is not None:
                review_context,review_instructions=navigation_recovery.question(self,choices,anchor,measurement)
                projection={'context':review_context,'instructions':review_instructions,
                    'navigation_recovery':{**navigation_recovery.progress(review),'request':review['requests']+1}}
            else:
                projection=travel_question(self,anchor,measurement,geometry,situation,current_forward,
                    choices,withheld,question=question,retry=bool(resumed_context or retry_page),
                    ordinary_retry=not bool(travel_context(self)),snapshot=snapshot,
                    relocation_context=travel_context(self))
        except TravelContextOverflow as exc:
            Path(path).unlink(missing_ok=True)
            self.event('grind_travel_context_overflow',{'frame_id':anchor.frame_id,'detail':str(exc)})
            self.enter_blocked('blocked_travel_context',self.last_signature)
            return CycleResult('grind_blocked',detail=str(exc))
        projection['retry_page']=retry_page
        projection['full_candidate_ids']=full_candidate_ids
        self.event('grind_travel_question_context',projection)
        offered_retry_basis=retry_basis(self)
        result=await self.decide(anchor,path,projection['context'],projection['instructions'],choices,
            travel_budget=self.travel_budget,relocation_retry=bool(resumed_context),navigation_review=review)
        if review is not None:navigation_recovery.outcome(self,review,result)
        else:
            if not (retry_page or {}).get('paced_reconsideration'):
                retry_menu.record(self,anchor,choices,result,retry_page)
            note_retry(self,anchor,target,result,offered_retry_basis)
        receipt=result.receipt or {}
        chosen=getattr(result.decision,'chosen',None)
        accepted=(result.status=='dispatched' and receipt==self.cycle.last_receipt and receipt.get('completed')
                  and not receipt.get('dispatch_unknown') and not receipt.get('error'))
        if (receipt.get('possible_input') or receipt.get('dispatch_unknown')) and not accepted:
            self.stop('partial_or_unknown_grind_input')
        elif accepted:
            meta=metadata.get(chosen,{})
            if chosen=='target_enemy':
                await travel_scout.accept(self,result,scout_state,level)
            elif meta.get('area') and review is not None:
                if not navigation_recovery.selected_destination(self,review,meta['area']):
                    return CycleResult('grind_reobserve',detail='Navigation recovery destination owner changed')
                await self.apply_choice(anchor,target,measurement,None,False,result,meta,chosen,level)
            elif receipt.get('possible_input'):
                previous_gameplay_receipt_id=hunt.last_gameplay_receipt_id
                hunt.last_gameplay_receipt_id=receipt['receipt_id']
                self.last_input_at=datetime.fromisoformat(receipt['occurred_at']).timestamp()
                hunt.install(meta['action'],'motion',anchor,receipt,measurement,purpose='travel',
                             target={'name':target['name'],'levels':target['levels']})
                hunt.pending.update(requested_duration=meta['requested_duration'],travel_purpose=meta['travel_purpose'],
                    selected_option=chosen,previous_gameplay_receipt_id=previous_gameplay_receipt_id)
                if meta['travel_purpose']=='detour':
                    if chosen=='detour_forward':hunt.detour['continuation_credit']=False
                    else:
                        side='left' if chosen.endswith('left') else 'right' if chosen.endswith('right') else 'backing out'
                        hunt.select_detour(chosen,meta['reason'],side,anchor,measurement)
                        hunt.travel_policy['side']=side
                elif meta['travel_purpose']=='destination progress':hunt.detour=None
                self.wait_until=time.time()+.15
            elif chosen=='begin_hunt':
                # Recheck the retained sources after the provider/dispatch await.
                # A declaration alone must never renew exhausted local methods.
                current_entry=hunt.travel_search_evidence(anchor,measurement,self.cycle.session_epoch)
                if not search_entry_valid():
                    self.event('grind_search_entry_rejected',{'frame_id':anchor.frame_id,
                        'reason':'strategy or linked travel evidence changed during decision'})
                    return CycleResult('grind_reobserve',detail='Changed search-entry evidence; remain in travel')
                if search_entry and current_entry==search_entry:
                    hunt.meaningful_sector_change(current_entry)
                    self.event('grind_search_sector_changed',current_entry)
                travel_scout.transfer(self,'validated_begin_hunt')
                # Explicit search entry owns this transition, including every
                # third sector where ordinary local motion requests planning.
                hunt.suspended=True;hunt.phase='search';hunt.planning_requested=False
                hunt.disengagement=None
            elif chosen=='change_destination':
                if not destination_change_valid():
                    return CycleResult('grind_reobserve',detail='Destination-change evidence changed during decision; reassess travel')
                hunt.destination_change_basis=deepcopy(destination_basis)
                review_record=navigation_recovery.record(self)
                if review_record:review_record['explicit_planning']=result.decision.envelope.request_id
                hunt.phase='choose_area';hunt.planning_requested=True
                hunt.travel_policy['tactic_changes']+=1;hunt.travel_policy['side']=None
                if hunt.detour:hunt.detour['continuation_credit']=False
            elif chosen=='urgent_state':
                hunt.disengagement=None
                debt=hunt.travel_urgent_debt
                if debt and debt['completed_roundtrips']>=2 and debt['evidence_signature']==self.last_signature and debt['gameplay_receipt_id']==hunt.last_gameplay_receipt_id:
                    self.enter_blocked('blocked_semantic',self.last_signature)
                else:
                    hunt.travel_urgent_episode={'evidence_signature':self.last_signature,'gameplay_receipt_id':hunt.last_gameplay_receipt_id}
                    hunt.prior_phase='travel';hunt.phase='recover';hunt.suspended=True
        self.history=(self.history+[{'frame_id':anchor.frame_id,'captured_at':anchor.captured_at,'choice':chosen,
                     'status':result.status,'phase':hunt.phase,'progress_claim':False}])[-8:]
        self.store.save_checkpoint('grind_only',{'mode':'grind_only','level':self.level.last_confirmed_level,
            'baseline_verified':self.baseline,'history':self.history,'hunt':hunt.context(),
            'observation_transaction':self.observation_transaction,'stopped':self.stopped,'reason':self.reason,
            'session_epoch':self.cycle.session_epoch,'deadline':self.deadline})
        return result

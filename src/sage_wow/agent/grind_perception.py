"""Attributed selected-target facts; gameplay remains a Sage decision."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from datetime import datetime
from dataclasses import replace
import re
from statistics import median
from tempfile import TemporaryDirectory
import time

from PIL import Image
from sage_wow.agent.scene import Scene, _green_bar_measure, _green_bar_edge_measure, is_input_error
from sage_wow.agent.ui_layout import UIRegions
from sage_wow.perception.ocr import recognize_text

from sage_wow.agent.selected_target_vision import accept_observation, eligibility, observe, prepare_target_image
from sage_wow.agent.ui_layout import from_profile


def same_badge_numeral(original,fresh,box,calibration,*,white=False):
    """Compare the calibrated gold numeral silhouette, excluding combat glow.

    This is continuity, never a numeric level read. An empty, obscured or changed
    glyph fails; only the known badge's central gold pixels participate.
    """
    if not calibration or tuple(box)!=tuple(calibration['badge_box']):return False
    with Image.open(original) as before,Image.open(fresh) as after:
        expected=(calibration['image_width'],calibration['image_height'])
        if before.size!=expected or after.size!=expected:return False
        crops=[im.convert('RGB').crop(box) for im in (before,after)]
    width,height=crops[0].size;cx,cy=(width-1)/2,(height-1)/2
    rx,ry=cx*.64,cy*.74
    if rx<3 or ry<3:return False
    masks=[]
    for crop in crops:
        mask=set()
        for y in range(height):
            for x in range(width):
                if ((x-cx)/rx)**2+((y-cy)/ry)**2>1:continue
                r,g,b=crop.getpixel((x,y))
                gold=r>130 and g>100 and r>g*1.05 and g>b*1.25
                pale=min(r,g,b)>180 and max(r,g,b)-min(r,g,b)<35
                if (pale if white else gold):mask.add((x,y))
        masks.append(mask)
    a,b=masks
    if min(len(a),len(b))<8:return False
    return a==b if white else len(a&b)/len(a|b)>=.90


def _small_target_health(crop):
    """Retain a coherent left-edge sliver without treating scattered green as HP."""
    if crop.width < 4 or crop.height < 3:
        return None, 0.
    runs = []
    for y in range(crop.height):
        green = [g > r*1.2 and g > b*1.3 and g > 90
                 for r, g, b in crop.crop((0, y, crop.width, y+1)).get_flattened_data()]
        run = next((x for x, pixel in enumerate(green) if not pixel), crop.width)
        runs.append(run if 0 < run/crop.width <= .03 and not any(green[run:]) else 0)
    confidence = sum(run > 0 for run in runs)/crop.height
    if confidence < .8:
        return None, confidence
    return round(median(runs)/crop.width, 3), round(confidence, 3)


def read_current_bars(frame, *, ui_layout=None):
    """Read only current calibrated HUD bars, retaining measurement confidence."""
    scene=Scene(frame.frame_id)
    player_alternate=None
    with Image.open(frame.image_path) as source:
        image=source.convert('RGB')
        regions=UIRegions(frame,image.size,ui_layout)
        for name,default in [('player_health',(.203,.687,.282,.696)),
                             ('target_health',(.717,.681,.79,.70))]:
            box=regions.pixels(name,default)
            crop=image.crop(box)
            value,confidence=_green_bar_measure(crop)
            if name=='player_health':
                if confidence<.8 and name in (ui_layout or {}).get('regions',{}):
                    alternate,alternate_confidence,evidence=_green_bar_edge_measure(crop)
                    player_alternate={**evidence,'raw_value':value,'raw_confidence':confidence,
                        'accepted':alternate is not None and alternate_confidence>=.8}
                    if player_alternate['accepted']:value,confidence=alternate,alternate_confidence
                scene.health=value if value is not None and value>.02 else None
                scene.health_confidence=confidence
            else:
                if value is None:
                    sliver,sliver_confidence=_small_target_health(crop)
                    if sliver is not None:value,confidence=sliver,sliver_confidence
                scene.target_alive=value>.03 if value is not None else None
                # A positive measurement can veto a death claim even when it
                # is too small to authorize another guarded combat pulse.
                scene.target_health=value
                scene.target_health_confidence=confidence
        scene.ui_layout_evidence=regions.evidence()
        scene.health_evidence={'source':'current HUD green bar pixels',
            'frame_id':frame.frame_id,'confidence':scene.health_confidence}
        if player_alternate is not None:scene.health_evidence['alternate']=player_alternate
    return scene


def read_hud_scene(frame, *, ui_layout=None, ocr=recognize_text):
    """One focused OCR pass for target/death/errors plus current bar pixels."""
    scene=read_current_bars(frame,ui_layout=ui_layout)
    scene.player_dead=False
    scene.error_cues=[]
    scene.active_text_entry=None
    with Image.open(frame.image_path) as source:
        image=source.convert('RGB');regions=UIRegions(frame,image.size,ui_layout)
        boxes=[regions.pixels('target_frame',(.711,.655,.81,.725)),
               tuple(round(v*s) for v,s in zip((.18,.10,.82,.65),
                    (image.width,image.height,image.width,image.height))),
               regions.pixels('player_frame',(.155,.65,.30,.725)),
               (0,round(image.height*.65),round(image.width*.5),image.height)]
        crops=[image.crop(box).resize(((box[2]-box[0])*3,(box[3]-box[1])*3)) for box in boxes]
        sheet=Image.new('RGB',(max(c.width for c in crops),sum(c.height+20 for c in crops)))
        bands=[];top=0
        for crop in crops:
            sheet.paste(crop,(0,top));bands.append((top,top+crop.height));top+=crop.height+20
        with TemporaryDirectory(prefix='sage-burst-hud-') as directory:
            path=Path(directory)/'hud.png';sheet.save(path);rows=ocr(path)
    names=[];entry_rows=[]
    for row in rows:
        if row.confidence<.7:continue
        text=row.text.strip();box=row.bounds
        band=next((i for i,(top,bottom) in enumerate(bands)
            if 0<=box['x'] and box['x']+box['width']<=crops[i].width
            and top<=box['y'] and box['y']+box['height']<=bottom),None)
        if band==0:
            if re.search(r'\b(?:dead|corpse)\b',text,re.I):scene.target_dead=True
            elif (re.fullmatch(r"[A-Za-z][A-Za-z '\-]{3,63}",text)
                  and text.casefold() not in {'health','mana','elite','rare'}):names.append(text)
        elif band==1:
            from sage_wow.agent.grind_outcomes import ERROR_PATTERNS
            patterns={**ERROR_PATTERNS,'facing':ERROR_PATTERNS['facing']+'|target is not in front'}
            cue=next(({'kind':kind,'text':text} for kind,pattern in patterns.items()
                if row.confidence>=.75 and re.search(pattern,text,re.I)),None)
            if cue:
                scene.error=text;scene.error_cues.append(cue)
            elif is_input_error(text):scene.error=text
        elif band==2 and re.search(r'\b(?:dead|ghost)\b',text,re.I):scene.player_dead=True
        elif band==3:
            from sage_wow.perception.ocr import TextObservation
            entry_rows.append(TextObservation(text,row.confidence,{
                'x':boxes[3][0]+box['x']/3,'y':boxes[3][1]+(box['y']-bands[3][0])/3,
                'width':box['width']/3,'height':box['height']/3}))
    from sage_wow.agent.grind_ui import active_text_entry
    scene.active_text_entry=active_text_entry(frame,entry_rows)
    scene.target_name=names[0] if len(set(names))==1 else None
    return scene


def burst_verdict(scene,expected,*,observed_name=None,critical_health=.30):
    """Evaluate fresh safety facts without adding a gameplay decision."""
    normalize=lambda value:' '.join(re.findall(r'[a-z0-9]+',(value or '').casefold()))
    name=observed_name if observed_name is not None else scene.target_name
    reason=None
    if getattr(scene,'player_dead',False):reason='player death observed'
    elif getattr(scene,'active_text_entry',None):reason='active text entry blocks world commands'
    elif scene.error:reason='visible input error: '+scene.error[:120]
    elif scene.target_state!='alive':reason='target dead or current life state unknown'
    elif not normalize(name):reason='current target identity is unknown'
    elif normalize(name)!=normalize(expected):reason='visible target differs from the Sage-authorized target'
    elif scene.health is None or (scene.health_confidence is not None and scene.health_confidence<.55):
        reason='current player health is unreadable'
    elif scene.health<critical_health:reason=f'player health appears below {critical_health:.0%}'
    return {'continue':reason is None,'reason':reason or 'fresh HUD supports another authorized pulse',
        'evidence_source':'selected_window_focused_HUD_OCR_and_current_bar_pixels',
        'observed_target_name':name,'observed_target_state':scene.target_state,
        'target_alive_pixel_estimate':scene.target_alive,'target_dead_ocr_estimate':scene.target_dead,
        'player_health_pixel_estimate':scene.health,'player_health_confidence':scene.health_confidence,
        'target_health_pixel_estimate':scene.target_health,
        'target_health_confidence':scene.target_health_confidence,'visible_error_ocr':scene.error,
        'observed_error_cues':getattr(scene,'error_cues',[]),
        'active_text_entry':getattr(scene,'active_text_entry',None)}


async def observe_burst(controller,binding,step_index,*,phase='pre_cast'):
    """Fresh facts may stop the exact Sage-authorized burst, never choose input."""
    expected=binding['expected_target_name'];reason=None
    try:
        if not controller.current():raise ValueError('grind authority stopped or paused')
        frame=await asyncio.to_thread(controller.capture)
        captured_monotonic=time.monotonic()-(time.time()-datetime.fromisoformat(frame.captured_at).timestamp())
        if not 0<=time.time()-datetime.fromisoformat(frame.captured_at).timestamp()<=3:
            raise ValueError('fresh selected-window frame required')
        if getattr(controller,'archive',None):
            frame=replace(frame,image_path=str(controller.archive.frame(
                frame,controller.cycle.session_epoch,controller.cycle.input_generation)))
        layout=from_profile(controller.profile)
        scene=await asyncio.to_thread(read_hud_scene,frame,ui_layout=layout,ocr=controller.ocr)
        if (not controller.current() or not
                0<=time.time()-datetime.fromisoformat(frame.captured_at).timestamp()<=3):
            raise ValueError('burst guard authority or fresh-frame lease expired')
        name=scene.target_name
        name_box=controller.config.get('target_name_box')
        badge=tuple(controller.config['target_level_box'])
        from sage_wow.agent.grind_only import same_patch,same_target_badge
        identity=getattr(controller,'_cast_burst_identity',None) if step_index else None
        stable=False
        if step_index==0:
            target={'name':name,'name_row':None,'levels':[], 'self_target':False,
                'invalid_text':scene.target_dead,'badge':badge,
                'box':UIRegions(frame,(frame.width,frame.height),layout).pixels('target_overlay',(0,0,1,1))}
            target=enrich(controller,frame,target,scene)
            name=target['name']
            if target.get('visual_observation',{}).get('life_state')=='dead':scene.target_dead=True
            controller._cast_burst_identity={'source':frame,'name':name,
                'source_sha256':hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
                'scope':controller.cycle.session_epoch,'revision':controller.revision,
                'selection':getattr(controller.hunt,'selection_revision',None),
                'target_continuity':controller.hunt.target_continuity,
                'target_history_key':(controller.hunt.approach or {}).get('history_key'),
                'encounter_id':controller.hunt.encounter}
            identity=controller._cast_burst_identity
            stable=bool(name and name.casefold()==expected.casefold() and not scene.target_dead)
        elif identity and name_box:
            source=identity['source']
            same_name=bool(controller.config.get('committed_combat',False)
                and name and name.casefold()==str(identity['name']).casefold())
            name_patch=same_patch(source.image_path,frame.image_path,name_box)
            badge_continuity=same_target_badge(source.image_path,frame.image_path,badge,
                controller.config.get('target_badge_continuity'),
                combat_glow=controller.config.get('committed_combat',False))
            stable=(source.source==frame.source
                and (source.width,source.height)==(frame.width,frame.height)
                and identity['source_sha256']==hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest()
                and identity['scope']==controller.cycle.session_epoch
                and identity['revision']==controller.revision
                and identity['selection']==getattr(controller.hunt,'selection_revision',None)
                and identity['target_continuity']==controller.hunt.target_continuity
                and identity['target_history_key']==(controller.hunt.approach or {}).get('history_key')
                and identity['encounter_id']==controller.hunt.encounter
                and (same_name or name_patch) and badge_continuity)
            if stable and not name:name=identity['name']
            elif not stable:reason='selected-target identity changed during the burst'
            identity_evidence={'same_fresh_name':same_name,'name_patch':name_patch,
                'target_badge_continuity':badge_continuity,'same_selection_episode':
                    identity['selection']==getattr(controller.hunt,'selection_revision',None)}
        observation=burst_verdict(scene,expected,observed_name=name,
            critical_health=controller.config.get('critical_health_threshold',.30))
        if reason:observation.update({'continue':False,'reason':reason})
        observation.update(frame_id=frame.frame_id,captured_at=frame.captured_at,
            image_path=frame.image_path,
            image_sha256=hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
            source=frame.source,width=frame.width,height=frame.height,
            session_epoch=controller.cycle.session_epoch,controller_revision=controller.revision,
            selection_revision=controller.hunt.selection_revision,
            target_continuity=controller.hunt.target_continuity,
            target_history_key=(controller.hunt.approach or {}).get('history_key'),encounter_id=controller.hunt.encounter,
            phase=phase,step_index=step_index,expected_target_name=expected,
            identity_stable=stable,captured_at_monotonic=captured_monotonic)
        if identity:
            source=identity['source']
            observation['identity_source']={'frame_id':source.frame_id,'captured_at':source.captured_at,
                'image_path':source.image_path,'image_sha256':identity['source_sha256'],
                'source':source.source,'width':source.width,'height':source.height,
                'session_epoch':identity['scope'],'controller_revision':identity['revision'],
                'selection_revision':identity['selection'],'target_continuity':identity['target_continuity'],
                'target_history_key':identity['target_history_key'],'encounter_id':identity['encounter_id'],
                'observed_target_name':identity['name']}
        if step_index and identity and name_box:observation['identity_continuity']=identity_evidence
    except Exception as exc:
        observation={'continue':False,'reason':f'fresh screenshot guard unavailable: {type(exc).__name__}',
            'evidence_source':'capture_or_perception_failure'}
    controller.event('cast_burst_guard_observation',{'step_index':step_index,
        'expected_target_name':expected,'phase':phase,**observation})
    return observation


async def observe_post_cast(controller,binding,step_index,phase):
    """Early/final facts for the submitted pulse; cannot choose another action."""
    return await observe_burst(controller,binding,step_index+1,phase=phase)


def carry_target_observation(controller,receipt):
    """Carry identity over fully reconciled target-preserving native commands."""
    cached=controller.target_observation
    if not cached or not isinstance(receipt,dict):return False
    binding=receipt.get('selected_binding') or {}
    kind=binding.get('type')
    command={'cast_guarded':'/cast [harm,nodead] Smite',
        'cast_burst':'/cast [harm,nodead] Smite',
        'cast_self_heal':'/cast [@player] Lesser Heal'}.get(kind)
    if command is None:return False
    execution=receipt.get('execution') or {}
    steps=receipt.get('input_steps') or []
    expected_spell='Lesser Heal' if kind=='cast_self_heal' else 'Smite'
    if (binding.get('spell')!=expected_spell or receipt!=controller.cycle.last_receipt
        or not receipt.get('completed') or receipt.get('dispatch_unknown') or receipt.get('error')
        or receipt.get('session_epoch')!=controller.cycle.session_epoch
        or receipt.get('generation_before')!=cached['generation']
        or receipt.get('generation_after')!=controller.cycle.input_generation
        or cached.get('selection_revision')!=getattr(controller.hunt,'selection_revision',None)
        or any(step.get('status')!='completed' for step in steps)):
        return False
    if execution.get('kind')!=kind:return False
    if kind=='cast_burst':
        completed=execution.get('completed_steps') or []
        if any(step.get('command')!=command or not step.get('dispatched') for step in completed):return False
        if execution.get('expected_target_name')!=cached['result']['observation'].get('name'):return False
    elif execution.get('command')!=command:return False
    opening=execution.get('opening_commands') or []
    if opening and (kind not in {'cast_guarded','cast_burst'} or binding.get('start_attack') is not True
        or len(opening)!=1 or opening[0].get('command')!='/startattack [harm,nodead]'
        or not opening[0].get('dispatched')):return False
    command_count=len(execution.get('completed_steps') or []) if kind=='cast_burst' else 1
    commands=[item['command'] for item in opening]+[command]*command_count
    if len(steps)!=len(commands)*5:return False
    expected=['key_down','key_up','text','key_down','key_up']*len(commands)
    if [step.get('kind') for step in steps]!=expected:return False
    for index,step in enumerate(steps):
        details=step.get('details') or {}
        if step['kind']=='text':
            if details.get('length')!=len(commands[index//5]):return False
        elif details.get('keycode')!=36:return False
    if receipt['generation_after']-receipt['generation_before']!=len(steps):return False
    source=Path(cached['source'].image_path)
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=cached['source_sha256']:return False
    cached['generation']=receipt['generation_after']
    return True


def enrich(controller, frame, target, scene=None):
    """Reuse stable identity pixels; refresh mutable life from the current HUD."""
    from sage_wow.agent.grind_inspection import unresolved
    cached=controller.target_observation
    if not cached:return unresolved(controller,target,frame)
    from sage_wow.agent.grind_only import same_patch, same_target_badge
    envelope=cached['result'];source=cached['source']
    observation=accept_observation(envelope,frame_id=source.frame_id,
        scope_id=controller.cycle.session_epoch,now_epoch=time.time(),max_age_seconds=30,
        image_sha256=cached['submitted_sha256'])
    # Retired observations cannot contradict a later selection. In particular,
    # an absent HUD before Tab is expected to become present after that input.
    # Check the observation's authority before recording semantic conflicts.
    authority={'attributed_current_scope':bool(observation),
        'same_input_generation':cached['generation']==controller.cycle.input_generation,
        'same_source':frame.source==source.source,
        'source_integrity':Path(source.image_path).is_file() and
            hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest()==cached['source_sha256']}
    if not all(authority.values()):
        controller.event('grind_target_observation_rejected',{
            'source_frame_id':source.frame_id,'fresh_frame_id':frame.frame_id,
            'inspection_episode_id':cached.get('inspection_episode_id'),
            'reason':'observation_authority_retired','authority':authority,
            'source_conflict':False,'name_conflict':False,'level_conflict':False,
            'presence_conflict':False,'semantic_conflicts_evaluated':False})
        controller.target_observation=None
        return unresolved(controller,target,frame)
    # Keep the raw provider result intact. Missing fields can be supplied only
    # by attributed OCR on that same source, with current continuity below.
    raw_observation=dict(observation) if observation else None
    observation=dict(observation) if observation else None
    ocr_facts=cached.get('source_ocr',{})
    source_name=ocr_facts.get('name')
    source_levels=ocr_facts.get('levels',[])
    source_level=source_levels[0] if len(source_levels)==1 else None
    field_sources={field:'target_observer' for field in observation or {}}
    source_conflict=bool(observation and (
        source_name and observation.get('name') and source_name.casefold()!=observation['name'].casefold() or
        source_levels and observation.get('level') is not None and source_levels!=[observation['level']]))
    if observation and observation['selected_hud']=='present' and not source_conflict:
        if observation['name'] is None and source_name:
            observation['name']=source_name;field_sources['name']='source_calibrated_target_OCR'
        if observation['level'] is None and source_level is not None:
            observation['level']=source_level;field_sources['level']='source_calibrated_badge_OCR'
    name_box=controller.config.get('target_name_box')
    continuity_boxes=([name_box] if observation and
        observation['selected_hud']=='present' and name_box else [target['box']])
    fresh_name=target.get('name')
    same_fresh_name=bool(observation and fresh_name and
        fresh_name.casefold()==str(observation.get('name')).casefold())
    name_conflict=bool(observation and observation.get('name') and fresh_name and not same_fresh_name)
    level_conflict=bool(observation and observation.get('level') is not None and target.get('levels') and
        target['levels']!=[observation.get('level')])
    presence_conflict=bool(observation and observation['selected_hud']=='absent' and
        (source_name or source_levels or fresh_name or target.get('levels')))
    if observation and observation['selected_hud']=='absent':
        from sage_wow.agent.grind_inspection import selection_conflicts
        from sage_wow.agent.grind_resources import hud_resources
        presence_conflict |= selection_conflicts({**target,'hud':hud_resources(controller,frame)})
        presence_conflict |= selection_conflicts({'hud':cached.get('source_hud') or {}})
    region_continuity=(same_fresh_name and controller.config.get('committed_combat',False)
        or all(same_patch(source.image_path,frame.image_path,box) for box in continuity_boxes))
    badge_continuity=same_target_badge(source.image_path,frame.image_path,target['badge'],
        controller.config.get('target_badge_continuity'),
        combat_glow=controller.config.get('committed_combat',False))
    valid=bool(not source_conflict and not name_conflict and not level_conflict and not presence_conflict
        and region_continuity and badge_continuity)
    if not valid:
        rejection={
            'source_frame_id':source.frame_id,'fresh_frame_id':frame.frame_id,
            'source_conflict':source_conflict,'name_conflict':name_conflict,'level_conflict':level_conflict,
            'presence_conflict':presence_conflict,
            'inspection_episode_id':cached.get('inspection_episode_id'),
            'region_continuity':bool(region_continuity),'badge_continuity':badge_continuity,
            'source_question_completed':bool((controller.target_inspection_episode or {}).get('source_absence'))}
        controller.event('grind_target_observation_rejected',rejection)
        episode=controller.target_inspection_episode
        if (source_conflict or name_conflict or level_conflict or presence_conflict) and not (
                episode and episode.get('disposition')=='source_resolved_absent'):
            from sage_wow.agent.grind_inspection import retain_conflict
            # The rejected answer belongs to its observer source. A later blank
            # frame must not inherit the source's positive HUD contradiction.
            retain_conflict(controller,source,rejection)
        controller.target_observation=None
        return unresolved(controller,target,frame)
    observation=dict(observation)
    if observation['selected_hud']=='present':
        scene=scene or read_current_bars(frame,ui_layout=from_profile(controller.profile))
        # Identity may persist while health changes. Current dead text always
        # wins, and a dead observation cannot be revived by noisy green pixels.
        if target.get('invalid_text') or scene.target_dead or observation['life_state']=='dead':
            observation['life_state']='dead'
            cached['dead_seen']=True
        elif cached.get('dead_seen'):
            observation['life_state']='dead'
        elif frame.frame_id!=source.frame_id or scene.target_state!='unknown':
            observation['life_state']=scene.target_state
            field_sources['life_state']='current_calibrated_health_or_dead_text'
    low,high=controller.hunt.target_band(controller.level.last_confirmed_level or 1)
    verdict=eligibility(observation,min_level=low,max_level=high,
        allowed_names=tuple(controller.config.get('target_names',())))
    from sage_wow.control.target_names import plain_target_name
    if (observation['selected_hud']=='present' and
            not plain_target_name(target.get('name') or observation.get('name'))):
        # Other eligible facts do not resolve an unusable identity binding.
        # Keep this same inspection allowance and the unmodified source text.
        verdict='unknown'
    from sage_wow.agent.grind_inspection import resolve
    resolve(controller,cached,verdict,frame)
    result={**target,'visual_observation':observation,'eligibility':verdict,
        'visual_provenance':{**{k:envelope.get(k) for k in
            ('backend','configured_model','actual_model','frame_id','scope_id','image_sha256')},
            'field_sources':field_sources,'source_ocr':ocr_facts,'raw_observation':raw_observation,
            'continuity_frame_id':frame.frame_id}}
    if observation['selected_hud']=='present':
        if not result['name'] and observation['name']:
            result['name']=observation['name']
            # Source of this rectangle is calibration, not invented OCR.
            region=from_profile(controller.profile)['regions'].get('target_frame',target['box'])
            result['visual_name_box']=tuple(name_box) if name_box else (region[0],region[1],region[2],min(region[3],region[1]+30))
        if not result['levels'] and observation['level'] is not None:result['levels']=[observation['level']]
        result['self_target']=result['self_target'] or observation['target_kind'] in {'player','friendly_or_self'}
        result['invalid_text']=result['invalid_text'] or observation['life_state']=='dead'
    return result


async def inspect(controller,frame,target):
    """One bounded observation. It cannot dispatch, select a target or cast."""
    cfg=controller.config.get('selected_target_observer') or {}
    if not cfg.get('enabled'):return False
    from sage_wow.agent.grind_inspection import begin
    inspection_id=begin(controller,frame,target)
    if inspection_id is None:return False
    # Pin before any awaited analysis/render/provider work. Completion belongs
    # to this immutable source question, not a later input or replacement task.
    from copy import deepcopy
    from sage_wow.agent.grind_inspection import configuration
    source_hash=hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()
    generation=controller.cycle.input_generation;epoch=controller.cycle.session_epoch;revision=controller.revision
    episode=controller.target_inspection_episode;pending=controller.hunt.pending
    pending_snapshot=deepcopy(pending);selection_revision=controller.hunt.selection_revision
    geometry=configuration(controller)
    stronger=(controller.require_world,controller.hunt.input_effect_unverified,
        controller.hunt.phase,deepcopy(controller.hunt.blocked),deepcopy(controller.hunt.active_threat),
        deepcopy(controller.heal_pending),deepcopy(controller.hunt.loot_request),
        controller.hunt.no_mana,deepcopy(controller.hunt.disengagement))
    # Use only same-source OCR, including its bounded calibrated reread. Never
    # feed previously fused observer fields back as independent OCR evidence.
    source_ocr=await controller.raw_target_proposal(frame)
    path=Path(frame.image_path).with_name(f'grind-observer-{frame.frame_id}.png')
    await asyncio.to_thread(prepare_target_image,frame.image_path,target['box'],path)
    if controller.archive:
        path=Path(controller.archive.retain(path.read_bytes(),'.png')['path'])
    submitted_hash=hashlib.sha256(path.read_bytes()).hexdigest()
    controller.event('grind_target_observer_requested',{'frame_id':frame.frame_id,'source_image':frame.image_path,
        'submitted_image':str(path),'image_sha256':submitted_hash,'backend':cfg['backend'],
        'configured_model':cfg.get('model'),'scope_id':epoch,'input_generation':generation})
    try:
        controller.stage('selected_target_factual_observer')
        observer=controller.target_observer or observe
        result=await asyncio.wait_for(observer(path,backend=cfg['backend'],model=cfg.get('model','levanto-sage'),
            sage_client=controller.cycle.sage,frame_id=frame.frame_id,captured_at=frame.captured_at,
            scope_id=epoch,timeout_seconds=cfg.get('timeout_seconds',12),
            latency_mode=cfg.get('latency_mode','quality')),timeout=cfg.get('timeout_seconds',12)+1)
        source_rows=await controller.rows(frame)
        accepted=accept_observation(result,frame_id=frame.frame_id,scope_id=epoch,
            now_epoch=time.time(),max_age_seconds=30,image_sha256=submitted_hash)
        if (not accepted or not controller.current() or not controller.fresh(frame)
            or controller.cycle._scope_error(frame) or generation!=controller.cycle.input_generation
            or epoch!=controller.cycle.session_epoch or revision!=controller.revision
            or controller.target_inspection_episode is not episode
            or controller.hunt.pending is not pending or pending != pending_snapshot
            or selection_revision!=controller.hunt.selection_revision
            or geometry!=configuration(controller)
            or stronger!=(controller.require_world,controller.hunt.input_effect_unverified,
                    controller.hunt.phase,controller.hunt.blocked,controller.hunt.active_threat,
                    controller.heal_pending,controller.hunt.loot_request,
                    controller.hunt.no_mana,controller.hunt.disengagement)
            or hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()!=source_hash):
            raise ValueError('observation expired or authority changed')
        from sage_wow.agent.grind_resources import hud_resources
        source_hud=hud_resources(controller,frame)
        controller.target_observation={'result':result,'source':frame,'generation':generation,
            'source_sha256':source_hash,'submitted_sha256':submitted_hash,
            'inspection_episode_id':inspection_id,
            'source_hud':source_hud,
            'source_ocr':{'name':source_ocr.get('name'),
                'levels':list(source_ocr.get('levels',[])), 'frame_id':frame.frame_id,
                'source_sha256':source_hash,'name_box':controller.config.get('target_name_box') or target['box'],
                'badge_box':target['badge'], 'focused_ocr':source_ocr.get('focused_ocr')},
            'selection_revision':getattr(controller.hunt,'selection_revision',None)}
        controller.event('grind_target_observer_result',{'accepted':True,**result})
        if accepted.get('selected_hud')=='absent':
            from sage_wow.agent.grind_inspection import complete_source_absence, selection_conflicts
            actual={**source_ocr,'visual_observation':target.get('visual_observation') or {},
                'hud':source_hud}
            consistent=not selection_conflicts(actual)
            from sage_wow.agent.grind_search import ui_evidence
            consistent &= not controller.require_world and not controller.hunt.input_effect_unverified
            consistent &= not ui_evidence(controller.profile,frame,source_rows,{})['positive']
            consistent &= source_hash==hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()
            controller.event('grind_target_observer_source_consistency',{'frame_id':frame.frame_id,
                'inspection_episode_id':inspection_id,'consistent_absence':consistent,
                'source_hud':source_hud,'gameplay_outcome_consumed':False})
            if consistent:complete_source_absence(controller,frame,actual,controller.target_observation)
        controller.hunt.compact_stage='inspect'
    except asyncio.CancelledError:raise
    except Exception as exc:
        controller.target_observation=None
        controller.event('grind_target_observer_result',{'accepted':False,'error':str(exc)[:250],
            'frame_id':frame.frame_id,'backend':cfg['backend']})
        # A replaced owner is stronger work, not a provider recovery request
        # against whichever newer physical receipt is now pending.
        task_current=(controller.hunt.pending is pending and pending==pending_snapshot
            and controller.target_inspection_episode is episode
            and generation==controller.cycle.input_generation and epoch==controller.cycle.session_epoch
            and revision==controller.revision and selection_revision==controller.hunt.selection_revision
            and stronger==(controller.require_world,controller.hunt.input_effect_unverified,
                controller.hunt.phase,controller.hunt.blocked,controller.hunt.active_threat,
                controller.heal_pending,controller.hunt.loot_request,
                controller.hunt.no_mana,controller.hunt.disengagement))
        if task_current:
            # A completed acquisition still owns its factual assessment; a
            # provider failure neither assesses nor deliberately disposes it.
            if pending and pending.get('family')=='target' and controller.hunt.known_completed_input(pending):
                controller.event('grind_target_inspection_pending_retained',{
                    'receipt_id':pending['receipt']['receipt_id'],'reason':'observer_unavailable',
                    'outcome_consumed':False})
            else:controller.request_recovery('selected_target_observer_unavailable')
        else:controller.event('grind_target_observer_discarded',{
            'frame_id':frame.frame_id,'reason':'source_task_superseded','new_pending_untouched':True})
    finally:
        original=Path(frame.image_path).with_name(f'grind-observer-{frame.frame_id}.png')
        original.unlink(missing_ok=True)
    controller.observer_last_at=time.time()
    return True

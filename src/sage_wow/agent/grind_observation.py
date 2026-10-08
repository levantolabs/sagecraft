"""Reversible user-authorized clean-world evidence; never gameplay selection."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from PIL import Image, ImageChops, ImageDraw, ImageStat

from sage_wow.agent.cycle import DispatchValidation, CycleResult
from sage_wow.agent.grind_search import ui_evidence
from sage_wow.agent.ui_layout import from_profile, validate_frame
from sage_wow.models import Frame
from sage_wow.platform.macos.capture import CaptureError, ForegroundCaptureInterrupted

AUTHORIZATION_REFERENCE = 'User approved plans/grind-travel-observation-spec.md: spec it and build it then tell me when to run the wow test'
RECOVERY_AUTHORIZATION_REFERENCE = 'User approved plans/grind-travel-recovery-spec.md: twelve-second travel and owned HUD restoration recovery'
HUD_BINDING = {'type': 'keypress_chord', 'keycodes': [58, 6], 'hold_seconds': .08}
HUD_SETTLE_SECONDS = 1.5
HUD_SETTLE_SAMPLES = 6
HUD_SAMPLE_INTERVAL = .15
HUD_RECONCILIATION_LIMIT = 4


class ObservationAuthorityLost(RuntimeError):
    """Expected scope retirement during an owned observation transaction."""


class ObservationSourceInvalid(CaptureError):
    """Captured pixels cannot establish the selected source/geometry chain."""


def validate_continuity(controller, anchor, fresh):
    try:
        validate_frame(fresh,controller.profile)
    except ValueError as exc:
        raise ObservationSourceInvalid(str(exc)) from exc
    if (fresh.source,fresh.width,fresh.height)!=(anchor.source,anchor.width,anchor.height):
        raise ObservationSourceInvalid('Continuity frame source or geometry changed')
    if fresh.image_path is None and fresh.image_data is None:
        raise ObservationSourceInvalid('Continuity frame has no image payload')
    return controller.cycle._validate_dispatch_frame(anchor,fresh)


def digest(frame):
    return hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()


def differences(controller, first, second):
    """Static HUD structure is separate from changing resource/target content."""
    regions=from_profile(controller.profile)['regions']
    with Image.open(first.image_path) as a, Image.open(second.image_path) as b:
        if a.size!=b.size:raise ValueError('HUD observation geometry changed')
        a=a.convert('RGB');b=b.convert('RGB')
        result={}
        for name in ('player_frame','minimap'):
            left,top,right,bottom=regions[name]
            ac=a.crop((left,top,right,bottom));bc=b.crop((left,top,right,bottom))
            if name=='player_frame':
                masks=[regions.get('player_health'),controller.config['player_level_box']]
                # Lower player-frame content contains health/mana animation;
                # static top/name/frame content verifies visibility separately.
                masks.append([left,top+(bottom-top)//3,right,bottom])
                for box in masks:
                    if box:
                        local=(max(0,box[0]-left),max(0,box[1]-top),min(ac.width,box[2]-left),min(ac.height,box[3]-top))
                        ImageDraw.Draw(ac).rectangle(local,fill='black');ImageDraw.Draw(bc).rectangle(local,fill='black')
            result[name]=sum(ImageStat.Stat(ImageChops.difference(ac,bc)).mean)/3
        return result


def visible_player_proof(controller, frame, rows):
    """Exact configured name in one calibrated, unambiguous OCR location."""
    character=controller.profile.values['character']
    normalize=lambda text:' '.join(str(text).casefold().split())
    name=normalize(character['name']);surname=normalize(character.get('surname',''))
    allowed={name,normalize(name+' '+surname)} if surname else {name}
    box=from_profile(controller.profile)['regions']['player_frame']
    observed=[]
    for row in rows:
        item=row if isinstance(row,dict) else {'text':row.text,'confidence':row.confidence,'bounds':dict(row.bounds)}
        b=item['bounds']
        if (box[0]<=b['x'] and box[1]<=b['y'] and b['x']+b['width']<=box[2]
            and b['y']+b['height']<=box[3] and b['width']>0 and b['height']>0):observed.append(item)
    observed.sort(key=lambda item:(item['bounds']['y'],item['bounds']['x']))
    matches=[]
    for start in range(len(observed)):
        for end in range(start+1,min(len(observed),start+max(len(text.split()) for text in allowed))+1):
            group=observed[start:end]
            if any(item['confidence']<.75 for item in group):break
            if any(not 0<=right['bounds']['y']-(left['bounds']['y']+left['bounds']['height'])<=max(left['bounds']['height'],right['bounds']['height'])
                   for left,right in zip(group,group[1:])):break
            if normalize(' '.join(item['text'] for item in group)) in allowed:matches.append(set(range(start,end)))
    locations=[]
    for match in matches:
        overlapping=[location for location in locations if location & match]
        if overlapping:
            merged=set(match)
            for location in overlapping:merged.update(location);locations.remove(location)
            locations.append(merged)
        else:locations.append(match)
    accepted=set().union(*locations) if locations else set()
    conflicting=any(index not in accepted and item['confidence']>=.75
        and normalize(item['text']).split()[:1]==name.split()[:1] for index,item in enumerate(observed))
    if surname:
        for location in locations:
            last=max(location)
            if last+1<len(observed):
                left,right=observed[last],observed[last+1]
                gap=right['bounds']['y']-(left['bounds']['y']+left['bounds']['height'])
                continuation=normalize(right['text'])
                if (right['confidence']>=.75 and 0<=gap<=max(left['bounds']['height'],right['bounds']['height'])
                    and continuation.replace(' ','').isalpha() and last+1 not in accepted):conflicting=True
    verified=len(locations)==1 and not conflicting
    return {'verified':verified,'name':name,'allowed_names':sorted(allowed),'source_sha256':digest(frame),
        'rule':'exact name; confidence >= .75; contained player frame; adjacent vertical reading-order rows',
        'matching_locations':len(locations),'conflicting_name':conflicting,
        'rows':[observed[index] for index in sorted(accepted)] if verified else [],
        'source_rows':observed}


async def read_player_identity(c, frame, rows, *, full_name=False):
    """One fixed alternate sensing method; never loosen exact-name matching."""
    raw=visible_player_proof(c,frame,rows)
    normalize=lambda text:' '.join(str(text).casefold().split())
    expected=normalize(' '.join(str(c.profile.values['character'].get(k) or '') for k in ('name','surname')))
    def complete(proof):
        return proof['verified'] and normalize(' '.join(r['text'] for r in proof['rows']))==expected
    if raw['verified'] and (not full_name or complete(raw)):return raw
    # A second positively located identity must not disappear through filtering.
    if raw['matching_locations']>1:return raw
    box=tuple(from_profile(c.profile)['regions']['player_frame'])
    source_hash=digest(frame);epoch=c.cycle.session_epoch;generation=c.cycle.input_generation
    identity=(frame.frame_id,frame.source,frame.captured_at,frame.width,frame.height,frame.image_path)
    cache=(c._analysis_cache or {}).get(identity)
    def current():
        return (c.current() and c.fresh(frame,travel=c.travel_budget is not None)
            and epoch==c.cycle.session_epoch and generation==c.cycle.input_generation
            and box==tuple(from_profile(c.profile)['regions']['player_frame'])
            and digest(frame)==source_hash and not c.cycle._scope_error(frame))
    if not current():return {**raw,'verified':False,'observation_authority_lost':True}
    cached=cache.get('player_identity_alternative') if cache is not None else None
    if cached and cached['scope']==(epoch,generation,source_hash,box):return cached['proof']
    from sage_wow.perception.ocr import TextObservation
    def alternate():
        with TemporaryDirectory(prefix='grind-own-identity-') as directory:
            path=Path(directory)/'player-identity-warm.png'
            with Image.open(frame.image_path) as image:
                crop=image.convert('RGB').crop(box)
                # The fixed gold HUD text channel excludes green world labels.
                # No expected-name content, length, substring or character box
                # controls this transform; the entire player frame is retained.
                mask=Image.new('L',crop.size)
                mask.putdata([0 if r>100 and g>80 and r>=g*.95 and min(r,g)>b*1.4 else 255
                    for r,g,b in crop.get_flattened_data()])
                filtered=mask.resize((crop.width*3,crop.height*3));filtered.save(path)
            data=path.read_bytes()
            observed=c.ocr(path) if filtered.getextrema()[0]<128 else []
            mapped=[]
            for row in observed:
                b=row.bounds
                if not (b['width']>0 and b['height']>0 and 0<=b['x'] and 0<=b['y']
                    and b['x']+b['width']<=crop.width*3 and b['y']+b['height']<=crop.height*3):continue
                if filtered.crop((b['x'],b['y'],b['x']+b['width'],b['y']+b['height'])).getextrema()[0]>=128:continue
                mapped.append(TextObservation(row.text,row.confidence,
                    {'x':box[0]+b['x']/3,'y':box[1]+b['y']/3,'width':b['width']/3,'height':b['height']/3},
                    source='player_frame_warm_channel_ocr'))
            return mapped,data
    try:
        alternate_rows,data=await asyncio.wait_for(asyncio.to_thread(alternate),timeout=2)
    except (asyncio.TimeoutError,OSError,RuntimeError,ValueError) as exc:
        proof={**raw,'verified':False,'alternate_error':type(exc).__name__}
    else:
        proof=visible_player_proof(c,frame,alternate_rows)
        # Alternate observations always need the full identity, including surname.
        proof['verified']=complete(proof)
        retained=c.archive.retain(data,'.png') if c.archive else {'sha256':hashlib.sha256(data).hexdigest()}
        proof.update(alternate={'method':'whole_player_frame_warm_channel_3x','box':list(box),
            'image':retained,'original_proof':raw})
    if not current():proof={**proof,'verified':False,'observation_authority_lost':True}
    c.event('grind_player_identity_alternate',{'frame_id':frame.frame_id,'source_sha256':source_hash,
        'session_epoch':epoch,'input_generation':generation,'proof':proof})
    if cache is not None:cache['player_identity_alternative']={'scope':(epoch,generation,source_hash,box),'proof':proof}
    return proof


def hud_continuity(controller, before, after, old, new, rows, *, identity_proof=None):
    """Compare calibrated identity/resources, excluding animated portraits.

    This is travel continuity only. Current bars are not damage attribution.
    Existing pixel limits still apply to the resource and identity regions.
    """
    from sage_wow.agent.grind_only import key, same_patch, same_target_badge
    from sage_wow.agent.grind_perception import same_badge_numeral
    from sage_wow.agent.grind_resources import hud_resources
    c=controller;regions=from_profile(c.profile)['regions']
    a,b=before.image_path,after.image_path
    previous,current=hud_resources(c,before),hud_resources(c,after)
    calibration=c.config.get('target_badge_continuity')
    own_box=tuple(c.config['player_level_box'])
    own_badge=same_patch(a,b,own_box,badge=True)
    if not own_badge and calibration:
        own_badge=same_badge_numeral(a,b,own_box,{**calibration,'badge_box':list(own_box)},white=True)
    def resource(name,box,*,required=False):
        left,right=previous[name],current[name]
        measured=(left is not None and right is not None and abs(left-right)<=.02+1e-9)
        if not required and left is None and right is None:measured=True
        return bool(measured and same_patch(a,b,box))
    def presence(target,bars):
        visible=(target.get('visual_observation') or {}).get('selected_hud')
        if visible in {'present','absent'}:return visible
        return 'present' if target.get('name') or bars['target_health'] is not None else 'unknown'
    old_visual=old.get('visual_observation') or {};new_visual=new.get('visual_observation') or {}
    old_presence,new_presence=presence(old,previous),presence(new,current)
    # The legacy travel guard did not compare scenery where no target was
    # identified. Preserve that path without upgrading unknown to absent.
    unobserved_target=(old_presence==new_presence=='unknown' and not any(
        target.get('name') or target.get('levels') or target.get('invalid_text') or target.get('self_target')
        or (target.get('visual_observation') or {}).get('life_state') in {'alive','dead'}
        for target in (old,new)))
    predicates={'player_identity':(identity_proof or visible_player_proof(c,after,rows))['verified'],
        'player_badge':own_badge,
        'player_health':resource('player_health',regions['player_health'],required=True),
        'player_health_confident':min(previous['health_confidence'] or 0,current['health_confidence'] or 0)>=.8,
        'same_selected_name':key(old.get('name'))==key(new.get('name')),
        'same_selected_presence':old_presence==new_presence,
        'same_selected_kind':bool(old.get('self_target'))==bool(new.get('self_target')),
        'same_selected_life':bool(old.get('invalid_text'))==bool(new.get('invalid_text'))
            and not (old_visual.get('life_state') in {'alive','dead'} and new_visual.get('life_state') in {'alive','dead'}
                and old_visual['life_state']!=new_visual['life_state']),
        'same_selected_level':old.get('levels')==new.get('levels')}
    if old_presence==new_presence=='absent' or unobserved_target:
        predicates['no_target_health']=previous['target_health'] is None and current['target_health'] is None
    else:
        predicates['target_badge']=same_target_badge(a,b,tuple(c.config['target_level_box']),calibration,combat_glow=True)
        predicates['target_health']=resource('target_health',regions['target_health'])
        if previous['target_health'] is not None or current['target_health'] is not None:
            predicates['target_health_confident']=min(previous['target_health_confidence'] or 0,current['target_health_confidence'] or 0)>=.8
        # Ambiguous selection cues retain a conservative region check.
        if 'unknown' in {old_presence,new_presence}:
            predicates['unknown_target_region']=same_patch(a,b,regions['target_overlay'])
    # Even a small change across the survival boundary needs a fresh assessment.
    if previous['player_health'] is not None and current['player_health'] is not None:
        predicates['same_survival_band']=(previous['player_health']<=.30)==(current['player_health']<=.30)
    if c.config.get('player_mana_box'):
        predicates['player_mana']=resource('player_mana',tuple(c.config['player_mana_box']),required=True)
    name_box=c.config.get('target_name_box')
    if name_box and (old_presence=='present' or new_presence=='present'):
        predicates['target_name_pixels']=same_patch(a,b,tuple(name_box))
    return {'predicates':predicates,'failed_predicates':[name for name,passed in predicates.items() if not passed],
        'selection_observation':{'before':old_presence,'current':new_presence,'legacy_unobserved_target':unobserved_target},
        'before_resources':previous,'current_resources':current,
        'source_frame_id':before.frame_id,'current_frame_id':after.frame_id}


@dataclass
class CleanSnapshot:
    controller: object
    hidden: object
    restored: object
    transaction: dict

    def current(self):
        c=self.controller;t=self.transaction
        if digest(self.hidden)!=t['hidden_sha256'] or digest(self.restored)!=t['restored_sha256']:
            c.event('grind_observation_source_invalid',{'transaction_id':t['transaction_id'],
                'reason':'Clean observation source hash changed','source_at':t.get('travel_source_at'),
                'deadline':t.get('travel_deadline')})
            raise ObservationSourceInvalid('Clean observation source hash changed')
        age=time.time()-datetime.fromisoformat(self.hidden.captured_at).timestamp()
        return bool(c.current() and c.cycle.session_epoch==t['session_epoch']
                    and c.cycle.input_generation==t['final_generation']
                    and c.observation_transaction is t and t['status']=='restored_verified'
                    and not t['restoration_needed'] and not c.cycle._scope_error(self.restored)
                    and c.executor.armed and c.executor.inspect_gate().valid
                    and (c.travel_budget.remaining()>0 if c.travel_budget else 0<=age<=c.config['clean_world_max_age_seconds']))

    async def guard(self, anchor):
        c=self.controller
        if c.travel_budget and c.travel_budget.remaining()<=0:
            return DispatchValidation(False,detail='Travel source deadline expired before continuity capture')
        if digest(self.hidden)!=self.transaction['hidden_sha256'] or digest(self.restored)!=self.transaction['restored_sha256']:
            raise ObservationSourceInvalid('Clean observation source hash changed')
        if not self.current():return DispatchValidation(False,detail='Clean observation focus/input/scope authority invalid')
        fresh=await asyncio.to_thread(c.capture)
        if c.travel_budget and c.travel_budget.remaining()<=0:
            return DispatchValidation(False,detail='Travel source deadline expired after continuity capture')
        error=validate_continuity(c,anchor,fresh)
        if c.archive:fresh=replace(fresh,image_path=str(c.archive.frame(fresh,c.cycle.session_epoch,c.cycle.input_generation)))
        if error:return DispatchValidation(False,detail='Restored HUD dispatch anchor '+error)
        rows=await c.rows(fresh)
        if c.travel_budget and c.travel_budget.remaining()<=0:
            return DispatchValidation(False,detail='Travel source deadline expired after continuity OCR')
        if ui_evidence(c.profile,fresh,rows,{})['positive']:
            return DispatchValidation(False,detail='Actual blocking UI appeared after observation')
        old=await c.target_proposal(self.restored)
        new=await c.target_proposal(fresh)
        proof=await read_player_identity(c,fresh,rows)
        evidence=hud_continuity(c,self.restored,fresh,old,new,rows,identity_proof=proof)
        compatible=not evidence['failed_predicates']
        if c.travel_budget and c.travel_budget.remaining()<=0:
            return DispatchValidation(False,detail='Travel source deadline expired after telemetry validation')
        if not self.current():return DispatchValidation(False,detail='Clean observation focus/input/source authority invalid after continuity')
        if not compatible:
            return DispatchValidation(False,detail='Current player/target telemetry changed; reobserve before gameplay',evidence=evidence)
        return DispatchValidation(True,fresh,'Clean-world age/hash and restored player/target/UI continuity',
                                  {'transaction_id':self.transaction['transaction_id'],
                                   'hidden_frame_id':self.hidden.frame_id,'hidden_captured_at':self.hidden.captured_at,
                                   'hidden_sha256':self.transaction['hidden_sha256'],'child_receipt_ids':self.transaction['child_receipt_ids'],
                                   **evidence})

    def image(self, path, *, selected_target=False):
        """Clean pixels retain original source/time; restored telemetry is labeled."""
        c=self.controller;regions=from_profile(c.profile)['regions']
        with Image.open(self.hidden.image_path) as clean, Image.open(self.restored.image_path) as hud:
            world=clean.convert('RGB');world.thumbnail((1100,800))
            names=('player_frame','target_overlay','minimap') if selected_target else ('player_frame','minimap')
            crops=[hud.convert('RGB').crop(regions[name]) for name in names]
            for crop in crops:crop.thumbnail((400,220))
            width=max(world.width,sum(crop.width for crop in crops)+24)
            height=world.height+max(crop.height for crop in crops)+60
            result=Image.new('RGB',(width,height),'#151b23');result.paste(world,(0,0))
            draw=ImageDraw.Draw(result)
            draw.text((4,world.height+4),f'CLEAN WORLD {self.hidden.captured_at} (original hidden source); restored HUD {self.restored.captured_at}',fill='white')
            x=0
            for name,crop in zip(names,crops):
                draw.text((x,world.height+22),name+' / RESTORED TELEMETRY',fill='white')
                result.paste(crop,(x,world.height+40));x+=crop.width+8
            result.save(path)
        return path


class CleanWorldObservation:
    def __init__(self, controller):self.controller=controller

    def persist(self, transaction):
        transaction['hud_no_effect_count']=self.controller.hud_no_effect_count
        self.controller.store.save_checkpoint('grind_observation',transaction)

    def authority(self, source):
        c=self.controller
        return c.current() and c.executor.armed and c.executor.inspect_gate().valid and not c.cycle._scope_error(source)

    def known_chain(self,transaction):
        """Visibility cannot absolve a partial/unknown child or an input gap."""
        if transaction.get('chain_invalid'):return False
        c=self.controller;expected=transaction['source_generation'];last=None
        for label in ('hide_receipt','restore_receipt','correction_receipt'):
            receipt=transaction.get(label)
            if not receipt:continue
            if (not receipt.get('completed') or receipt.get('dispatch_unknown') or receipt.get('error')
                or receipt.get('generation_before')!=expected
                or receipt.get('authorization_id')!=transaction['transaction_id']
                or receipt.get('authorization_type')!='user_authorized_observation'):
                return False
            steps=receipt.get('input_steps',[])
            if (any(step.get('status')!='completed' for step in steps)
                or receipt.get('generation_after')!=expected+len(steps)):
                return False
            expected=receipt.get('generation_after');last=receipt
        return expected==c.cycle.input_generation and last==c.cycle.last_receipt

    @staticmethod
    def sample(frame):
        return {'frame':frame.__dict__,'frame_id':frame.frame_id,'captured_at':frame.captured_at,'sha256':digest(frame)}

    async def hidden_candidate(self,transaction,fresh):
        """Joint disappearance of HUD anchors with continuous world pixels.

        Missing OCR or changed pixels alone never authorizes a toggle. This is
        evidence about an owned observation command, not a gameplay inference.
        """
        c=self.controller;source=Frame(**transaction['source_frame'])
        proof=transaction.get('source_visible_proof') or {}
        if digest(source)!=transaction['source_sha256']:raise ValueError('HUD source reference hash changed')
        verified=(proof.get('source_sha256')==transaction['source_sha256']
            and visible_player_proof(c,source,proof.get('source_rows',[]))['verified'])
        rows=await c.rows(fresh)
        regions=from_profile(c.profile)['regions']
        # These rectangles contain world pixels too (especially the corners of
        # a round minimap). Text revealed by hiding an overlay is not itself
        # evidence that the overlay survived. Retain it, but use positive own
        # identity plus the independent structural anchors for visibility.
        region_rows=[]
        for row in rows:
            b=row.bounds
            if row.confidence>=.75 and any(left<=b['x'] and top<=b['y']
                and b['x']+b['width']<=right and b['y']+b['height']<=bottom
                for left,top,right,bottom in (regions['player_frame'],regions['minimap'])):
                region_rows.append(row.text)
        current_identity=visible_player_proof(c,fresh,rows)
        identity_remains=bool(current_identity['matching_locations'] or current_identity['conflicting_name'])
        change=differences(c,source,fresh)
        # Compare the central world, masking all calibrated overlays. A blank,
        # loading or occluded screen is not an observed HUD disappearance.
        with Image.open(source.image_path) as a,Image.open(fresh.image_path) as b:
            a=a.convert('RGB');b=b.convert('RGB')
            mask=Image.new('L',a.size,0);draw=ImageDraw.Draw(mask)
            w,h=a.size;draw.rectangle((w//5,h//4,4*w//5,3*h//4),fill=255)
            for box in regions.values():draw.rectangle(tuple(box),fill=0)
            area=ImageStat.Stat(mask).sum[0]/255
            world_change=max(ImageStat.Stat(ImageChops.difference(a,b),mask).mean) if area else 255
            world_light=max(ImageStat.Stat(b,mask).mean) if area else 0
        evidence={'source_visible':bool(verified),'hud_difference':change,'region_text':region_rows,
            'remaining_player_identity':identity_remains,
            'world_difference':world_change,'world_light':world_light,'world_pixels':area,
            'blocking_ui':bool(ui_evidence(c.profile,fresh,rows,{})['positive']
                or any(re.search(r'loading',row.text,re.I) for row in rows))}
        accepted=(verified and min(change.values())>2 and not identity_remains and area>0
            and world_change<=10 and world_light>15 and not evidence['blocking_ui'])
        return bool(accepted),evidence

    async def visible_candidate(self,transaction,fresh):
        c=self.controller;source=Frame(**transaction['source_frame'])
        if digest(source)!=transaction['source_sha256']:raise ValueError('HUD source reference hash changed')
        rows=await c.rows(fresh)
        proof=await read_player_identity(c,fresh,rows)
        structure=max(differences(c,source,fresh).values())<=2
        if not structure:
            structure,evidence=self.restored_support(transaction,fresh)
            transaction['restoration_overlay_evidence']=evidence
        return bool(structure
            and proof['verified']
            and not any(re.search(r'loading',row.text,re.I) for row in rows))

    def restored_support(self,transaction,fresh):
        """Restoration evidence from pixels distinguished by the owned hide.

        Rectangular HUD regions include transparent world pixels. Learn support
        only from the original visible source and both verified hidden witnesses,
        never from the candidate being assessed. This cannot authorize a hidden
        classification or another toggle. Resource continuity remains separate.
        """
        evidence={'frame_id':fresh.frame_id,'anchors':{}}
        witnesses=transaction.get('hidden_witnesses') or []
        if not transaction.get('hide_verified') or len(witnesses)!=2:
            return False,{**evidence,'reason':'two verified hidden witnesses required'}
        source=Frame(**transaction['source_frame']);frames=[source]
        expected=[transaction['source_sha256']]
        proof=transaction.get('source_visible_proof') or {}
        if not proof.get('verified') or proof.get('source_sha256')!=expected[0]:
            return False,{**evidence,'reason':'verified visible source required'}
        for witness in witnesses:
            frames.append(Frame(**witness['frame']));expected.append(witness['sha256'])
        if (len({frame.frame_id for frame in frames})!=3
            or frames[-1].frame_id!=transaction.get('hidden_frame_id')
            or expected[-1]!=transaction.get('hidden_sha256')
            or datetime.fromisoformat(frames[2].captured_at).timestamp()
                -datetime.fromisoformat(frames[1].captured_at).timestamp()<HUD_SAMPLE_INTERVAL*.8):
            return False,{**evidence,'reason':'hidden witness identity or separation invalid'}
        validate_frame(fresh,self.controller.profile)
        for frame,sha in zip(frames,expected):
            validate_frame(frame,self.controller.profile)
            if digest(frame)!=sha:raise ValueError('HUD support reference hash changed')
            if (frame.source,frame.width,frame.height)!=(fresh.source,fresh.width,fresh.height):
                raise ValueError('HUD support reference source or geometry changed')
        regions=from_profile(self.controller.profile)['regions']
        images=[]
        for frame in [*frames,fresh]:
            with Image.open(frame.image_path) as image:images.append(image.convert('RGB'))
        def channel_difference(a,b):
            red,green,blue=ImageChops.difference(a,b).split()
            return ImageChops.lighter(ImageChops.lighter(red,green),blue)
        accepted=True
        for name in ('player_frame','minimap'):
            left,top,right,bottom=regions[name]
            # Match the existing structural player mask, omitting animated
            # resources/portrait content and measuring coverage only in its
            # remaining extent.
            if name=='player_frame':bottom=top+(bottom-top)//3
            box=(left,top,right,bottom)
            visible,hidden_a,hidden_b,current=[image.crop(box) for image in images]
            usable=Image.new('L',visible.size,255)
            if name=='player_frame':
                for region in (regions.get('player_health'),self.controller.config['player_level_box']):
                    if region and region[2]>left and region[0]<right and region[3]>top and region[1]<bottom:
                        ImageDraw.Draw(usable).rectangle((max(0,region[0]-left),max(0,region[1]-top),
                            min(usable.width,region[2]-left),min(usable.height,region[3]-top)),fill=0)
            # A two-sided contrast margin separates overlay support from JPEG
            # noise; the independently hidden witnesses must agree locally.
            support=usable.copy()
            for hidden in (hidden_a,hidden_b):
                support=ImageChops.multiply(support,channel_difference(visible,hidden).point(lambda n:255 if n>=24 else 0))
            support=ImageChops.multiply(support,channel_difference(hidden_a,hidden_b).point(lambda n:255 if n<=12 else 0))
            count=lambda mask:sum(mask.histogram()[1:])
            pixels=count(support);area=count(usable);w,h=usable.size
            quadrants=[]
            for x,y,xx,yy in ((0,0,w//2,h//2),(w//2,0,w,h//2),(0,h//2,w//2,h),(w//2,h//2,w,h)):
                quadrant=(x,y,xx,yy);available=count(usable.crop(quadrant))
                if available:quadrants.append(count(support.crop(quadrant))/available)
            adequate=bool(pixels>=64 and area and pixels/area>=.10 and quadrants and min(quadrants)>=.05)
            distance=lambda a,b:sum(ImageStat.Stat(ImageChops.difference(a,b),support).mean)/3 if pixels else None
            to_visible=distance(visible,current);to_hidden=distance(hidden_b,current)
            anchor_ok=adequate and to_visible<=2 and to_hidden>2
            evidence['anchors'][name]={'pixels':pixels,'usable_pixels':area,'quadrant_coverage':quadrants,
                'support_sha256':hashlib.sha256(support.tobytes()).hexdigest(),
                'visible_difference':to_visible,'hidden_difference':to_hidden,'accepted':bool(anchor_ok)}
            accepted=accepted and anchor_ok
        evidence['reference_sha256']=expected
        evidence['accepted']=bool(accepted)
        return bool(accepted),evidence

    def promote_hidden(self,transaction,first,second):
        """Two distinct settled witnesses; retain the original samples separately."""
        if (first.frame_id==second.frame_id or
            datetime.fromisoformat(second.captured_at).timestamp()-datetime.fromisoformat(first.captured_at).timestamp()<HUD_SAMPLE_INTERVAL*.8
            or max(differences(self.controller,first,second).values())>2):return False
        transaction.update(hidden_frame=second.__dict__,hidden_frame_id=second.frame_id,
            hidden_captured_at=second.captured_at,hidden_sha256=digest(second),
            hidden_generation=self.controller.cycle.input_generation,hide_verified=True,
            hidden_witnesses=[self.sample(first),self.sample(second)])
        self.persist(transaction)
        return True

    async def settle(self,transaction,capture,accept,*,label):
        """A finite observation window, with original frame/time/hash evidence."""
        deadline=time.monotonic()+HUD_SETTLE_SECONDS
        for _ in range(HUD_SETTLE_SAMPLES):
            await asyncio.sleep(HUD_SAMPLE_INTERVAL)
            fresh=await capture()
            if fresh is None:return None
            transaction.setdefault(label+'_samples',[]).append(self.sample(fresh))
            self.persist(transaction)
            if await accept(fresh):return fresh
            if time.monotonic()>=deadline:break
        return None

    def visibility(self,transaction,fresh):
        references=[]
        for label in ('source','hidden'):
            payload=transaction.get(label+'_frame');expected=transaction.get(label+'_sha256')
            if not payload or not expected:return 'ambiguous',{'reason':'missing immutable HUD reference'}
            reference=Frame(**payload)
            validate_frame(reference,self.controller.profile)
            if digest(reference)!=expected:raise ValueError('HUD '+label+' reference hash changed')
            if (reference.source,reference.width,reference.height)!=(fresh.source,fresh.width,fresh.height):
                raise ValueError('HUD reference source or geometry changed')
            references.append(reference)
        visible,hidden=references
        separation=differences(self.controller,visible,hidden)
        to_visible=differences(self.controller,visible,fresh)
        to_hidden=differences(self.controller,hidden,fresh)
        evidence={'reference_separation':separation,'visible_difference':to_visible,'hidden_difference':to_hidden,
            'source_sha256':transaction['source_sha256'],'hidden_sha256':transaction['hidden_sha256']}
        if not transaction.get('hide_verified'):
            proof=transaction.get('source_visible_proof') or {}
            verified=proof.get('source_sha256')==transaction['source_sha256'] and visible_player_proof(
                self.controller,visible,proof.get('source_rows',[]))['verified']
            evidence['positive_visible_proof']=bool(verified)
            no_hide=(verified and max(separation.values())<=2 and max(to_visible.values())<=2
                and transaction.get('hide_receipt',{}).get('chosen_option')=='grind_hud_hide'
                and not transaction.get('restore_receipt') and not transaction.get('correction_receipt')
                and fresh.frame_id!=hidden.frame_id
                and datetime.fromisoformat(fresh.captured_at).timestamp()>datetime.fromisoformat(hidden.captured_at).timestamp())
            return ('visible_no_hide' if no_hide else 'ambiguous'),evidence
        if min(separation.values())<=2:return 'ambiguous',evidence
        visible_match=max(to_visible.values())<=2 and min(to_hidden.values())>2
        hidden_match=max(to_hidden.values())<=2 and min(to_visible.values())>2
        result='visible' if visible_match and not hidden_match else 'hidden' if hidden_match and not visible_match else 'ambiguous'
        if result=='ambiguous':
            restored,support=self.restored_support(transaction,fresh)
            evidence['restoration_overlay_evidence']=support
            if restored:result='visible'
        return result,evidence

    async def reconcile(self,frame):
        c=self.controller;t=c.observation_transaction
        validate_frame(frame,c.profile)
        if not self.known_chain(t):
            c.stop('partial_or_unknown_grind_input')
            return CycleResult('grind_stopped',detail='HUD obligation has uncertain input or an invalid receipt chain')
        if not c.fresh(frame):return CycleResult('grind_reobserve',detail='HUD reconciliation requires fresh post-input evidence')
        c.hunt.invalidate_travel_mapping('HUD restoration reconciliation retires prior snapshot')
        source_at=datetime.fromisoformat(frame.captured_at).timestamp()
        with c.cycle.scoped_execution_scope(task_id=c.task_id,objective_revision=c.revision,
            session_epoch=c.cycle.session_epoch,deadline_epoch=min(source_at+10,c.deadline),
            input_generation=c.cycle.input_generation,is_current=c.current,max_frame_age_seconds=10):
            if not self.authority(frame):return CycleResult('grind_blocked',detail='HUD restoration requires current focus/ownership')
            t['reconciliation_attempts']=t.get('reconciliation_attempts',0)+1
            self.persist(t)
            # Charge the existing bounded attempt before sensing: slow OCR
            # must not evade the terminal limit by expiring the source lease.
            from sage_wow.agent.grind_resources import stop_if_own_death
            death=await stop_if_own_death(c,frame)
            if death is not None:return death

            async def classify(fresh):
                validate_frame(fresh,c.profile)
                if not self.authority(frame):return 'authority_invalid'
                result,evidence=self.visibility(t,fresh)
                if result in {'visible','visible_no_hide'}:
                    evidence['current_visible_proof']=await self.visible_candidate(t,fresh)
                    if not evidence['current_visible_proof']:result='ambiguous'
                if not self.authority(frame):return 'authority_invalid'
                t['reconciliation']={'classification':result,'frame_id':fresh.frame_id,'captured_at':fresh.captured_at,
                    'evidence':evidence,'correction_attempted':bool(t.get('correction_attempted'))}
                self.persist(t);c.event('grind_hud_reconciliation',dict(t['reconciliation']))
                return result

            async def settled_capture(anchor):
                await asyncio.sleep(HUD_SAMPLE_INTERVAL)
                if not self.authority(frame):return None
                generation=c.cycle.input_generation
                fresh=await asyncio.to_thread(c.capture)
                validate_frame(fresh,c.profile)
                error=c.cycle._validate_dispatch_frame(anchor,fresh)
                if error:raise ValueError('HUD reconciliation '+error)
                if c.archive:fresh=replace(fresh,image_path=str(c.archive.frame(fresh,c.cycle.session_epoch,c.cycle.input_generation)))
                if not self.authority(frame):return None
                if generation!=c.cycle.input_generation or not self.known_chain(t):return None
                if not c.fresh(fresh):raise ObservationSourceInvalid('HUD reconciliation sample predates completed input or is stale')
                return fresh

            def cleared(fresh,*,no_effect=False):
                if not self.known_chain(t):
                    c.stop('partial_or_unknown_grind_input')
                    return CycleResult('grind_stopped',detail='HUD receipt chain changed before reconciliation')
                if not self.authority(frame):return CycleResult('grind_blocked',detail='HUD authority changed before reconciliation')
                if no_effect and not t.get('no_effect_charged'):
                    c.hud_no_effect_count+=1;t['no_effect_charged']=True
                if not no_effect and t.get('hide_verified') and (t.get('restore_receipt') or t.get('correction_receipt')):
                    c.hud_no_effect_count=0
                t.update(restoration_needed=False,status='hide_no_effect_visible' if no_effect else 'restoration_reconciled',final_generation=c.cycle.input_generation,
                    reconciled_frame_id=fresh.frame_id,reconciled_captured_at=fresh.captured_at)
                self.persist(t)
                if (c.hunt.blocked and c.hunt.blocked['reason']=='blocked_hud_restoration'
                    and c.hunt.blocked['evidence_signature']==t['transaction_id']):c.hunt.blocked=None
                c.wait_until=0
                if no_effect:
                    c.event('grind_hud_hide_no_effect',{'transaction_id':t['transaction_id'],
                        'hud_no_effect_count':c.hud_no_effect_count,'samples':t['no_effect_samples']})
                    if c.hud_no_effect_count>=2:
                        c.enter_blocked('blocked_hud_capture_unavailable',t['transaction_id'])
                    return CycleResult('grind_reobserve',detail='HUD hide had no effect; visible only, new decision source required')
                return CycleResult('grind_reobserve',detail='HUD restoration reconciled; new decision source required')

            classification=await classify(frame)
            if classification=='visible':return cleared(frame)
            if (classification=='ambiguous' and not t.get('hide_verified')
                and not t.get('restore_receipt') and not t.get('correction_attempted')):
                candidate,evidence=await self.hidden_candidate(t,frame)
                t.setdefault('delayed_hide_samples',[]).append({**self.sample(frame),'evidence':evidence})
                self.persist(t)
                if candidate and self.authority(frame) and self.known_chain(t):
                    settled=await settled_capture(frame)
                    if settled is not None:
                        accepted,evidence=await self.hidden_candidate(t,settled)
                        t['delayed_hide_samples'].append({**self.sample(settled),'evidence':evidence})
                        self.persist(t)
                        if (accepted and self.authority(frame) and self.known_chain(t)
                            and self.promote_hidden(t,frame,settled)):
                            frame=settled;classification=await classify(frame)
            if classification=='visible_no_hide':
                t['no_effect_samples']=[{'frame_id':frame.frame_id,'captured_at':frame.captured_at,'sha256':digest(frame)}]
                self.persist(t)
                settled=await settled_capture(frame)
                if (settled is not None and await classify(settled)=='visible_no_hide' and c.fresh(settled)
                    and datetime.fromisoformat(settled.captured_at).timestamp()>datetime.fromisoformat(frame.captured_at).timestamp()):
                    t['no_effect_samples'].append({'frame_id':settled.frame_id,'captured_at':settled.captured_at,'sha256':digest(settled)})
                    return cleared(settled,no_effect=True)
            if classification=='hidden' and not t.get('correction_attempted'):
                # Two separated observations verify a settled hidden result.
                settled=await settled_capture(frame)
                if settled is not None:
                    classification=await classify(settled)
                    if classification=='visible':return cleared(settled)
                    if classification=='hidden':
                        death=await stop_if_own_death(c,settled)
                        if death is not None:return death
                        rows=await c.rows(settled)
                        if ui_evidence(c.profile,settled,rows,{})['positive']:
                            classification='ambiguous'
                        elif self.authority(frame) and self.known_chain(t):
                            t['correction_attempted']=True;self.persist(t)
                            try:
                                result=await c.cycle.execute_observation(HUD_BINDING,frame=settled,
                                    transaction_id=t['transaction_id'],authorization_reference=RECOVERY_AUTHORIZATION_REFERENCE,
                                    chosen_option='grind_hud_restore_correction')
                            finally:
                                receipt=c.cycle.last_receipt or {}
                                if receipt.get('authorization_id')==t['transaction_id'] and receipt.get('chosen_option')=='grind_hud_restore_correction':
                                    t['correction_receipt']=receipt
                                    if receipt['receipt_id'] not in t['child_receipt_ids']:t['child_receipt_ids'].append(receipt['receipt_id'])
                                    c.last_input_at=datetime.fromisoformat(receipt['occurred_at']).timestamp()
                                t['final_generation']=c.cycle.input_generation;self.persist(t)
                            if not self.known_chain(t):
                                c.stop('partial_or_unknown_grind_input')
                                return CycleResult('grind_stopped',detail='HUD corrective input uncertain')
                            async def restored_visible(fresh):return await classify(fresh)=='visible'
                            restored=await self.settle(t,lambda:settled_capture(settled),restored_visible,label='correction')
                            if restored is not None:return cleared(restored)
            if t['reconciliation_attempts']>=HUD_RECONCILIATION_LIMIT:
                # Stopping needs no visual/input authority. Slow sensing must
                # not evade this bound merely by exhausting each frame lease.
                if c.stopped:return CycleResult('grind_stopped',detail=c.reason)
                t['status']='restoration_terminal_unresolved';self.persist(t)
                c.event('grind_hud_restoration_exhausted',{'transaction_id':t['transaction_id'],
                    'attempts':t['reconciliation_attempts'],'classification':classification,
                    'restoration_needed':True,'correction_attempted':bool(t.get('correction_attempted'))})
                c.stop('hud_restoration_unresolved')
                return CycleResult('grind_stopped',detail='HUD restoration evidence/correction allowance exhausted')
            c.enter_blocked('blocked_hud_restoration',t['transaction_id'])
            if c.hunt.blocked:
                c.hunt.blocked['next_observation_at']=time.time()+c.hunt.blocked['interval']
                c.wait_until=c.hunt.blocked['next_observation_at']
            return CycleResult('grind_blocked',detail='HUD restoration remains observation-only: '+classification)

    async def capture(self, frame):
        c=self.controller
        if c.hud_no_effect_count>=2:
            c.enter_blocked('blocked_hud_capture_unavailable','retained_hud_no_effect_capability')
            return None
        if c.observation_transaction and c.observation_transaction.get('restoration_needed'):
            return None
        if not self.authority(frame) or ui_evidence(c.profile,frame,await c.rows(frame),{})['positive']:
            return None
        proof=await read_player_identity(c,frame,await c.rows(frame))
        if not self.authority(frame):return None
        if not proof['verified']:
            c.event('grind_hud_retry_visibility_unresolved',{'source_frame_id':frame.frame_id,
                'source_sha256':proof['source_sha256'],'hud_no_effect_count':c.hud_no_effect_count})
            return None
        transaction={'transaction_id':str(uuid4()),'session_epoch':c.cycle.session_epoch,
                     'source_frame':frame.__dict__,'source_sha256':digest(frame),
                     'source_frame_id':frame.frame_id,'source_captured_at':frame.captured_at,
                     'source_generation':c.cycle.input_generation,'restoration_needed':True,
                     'status':'hide_owned_before_dispatch','authorization_reference':AUTHORIZATION_REFERENCE,
                     'child_receipt_ids':[]}
        transaction['source_visible_proof']=proof
        if c.travel_budget:transaction.update(travel_source_at=c.travel_budget.source_at,travel_deadline=c.travel_budget.deadline)
        c.observation_transaction=transaction
        self.persist(transaction)  # Crash cannot forget ownership of a toggle.
        hidden=restored=None
        cancellation=None
        capture_error=None

        async def capture_current():
            if not self.authority(frame):raise ObservationAuthorityLost('HUD capture scope/focus/stop invalid')
            generation=c.cycle.input_generation
            captured=await asyncio.to_thread(c.capture)
            validate_frame(captured,c.profile)
            error=c.cycle._validate_dispatch_frame(frame,captured)
            if error:raise ValueError('HUD capture '+error)
            if not self.authority(frame):raise ObservationAuthorityLost('HUD capture authority changed')
            if generation!=c.cycle.input_generation or not self.known_chain(transaction):
                raise ObservationAuthorityLost('HUD capture input chain changed')
            if not c.fresh(captured):raise ObservationSourceInvalid('HUD sample predates completed input or is stale')
            if c.archive:captured=replace(captured,image_path=str(c.archive.frame(captured,c.cycle.session_epoch,c.cycle.input_generation)))
            return captured

        async def toggle(label, anchor):
            result=await c.cycle.execute_observation(HUD_BINDING,frame=anchor,
                transaction_id=transaction['transaction_id'],authorization_reference=AUTHORIZATION_REFERENCE,
                chosen_option=label)
            receipt=result.receipt or {}
            if receipt:
                transaction['child_receipt_ids'].append(receipt['receipt_id'])
                c.last_input_at=datetime.fromisoformat(receipt['occurred_at']).timestamp()
            return receipt

        try:
            hide=await toggle('grind_hud_hide',frame)
            transaction['hide_receipt']=hide
            transaction['restoration_needed']=bool(hide.get('possible_input'))
            self.persist(transaction)
            if not hide.get('completed') or hide.get('dispatch_unknown') or hide.get('error'):
                transaction['status']='hide_input_uncertain'
            else:
                candidate=None
                async def hidden_settled(fresh):
                    nonlocal hidden,candidate
                    hidden=fresh
                    transaction.update(hidden_frame=fresh.__dict__,hidden_frame_id=fresh.frame_id,hidden_captured_at=fresh.captured_at,
                        hidden_sha256=digest(fresh),hidden_generation=c.cycle.input_generation,hide_verified=False,
                        hide_difference=differences(c,frame,fresh))
                    accepted,evidence=await self.hidden_candidate(transaction,fresh)
                    transaction['hide_samples'][-1]['evidence']=evidence
                    if not self.authority(frame):raise ObservationAuthorityLost('HUD hide authority changed during evidence analysis')
                    if not self.known_chain(transaction):raise ObservationAuthorityLost('HUD hide input chain changed')
                    if accepted and candidate and self.promote_hidden(transaction,candidate,fresh):return True
                    candidate=fresh if accepted else None
                    return False
                await self.settle(transaction,capture_current,hidden_settled,label='hide')
        except asyncio.CancelledError as exc:
            cancellation=exc
            transaction['error']='CancelledError'
        except ObservationAuthorityLost as exc:
            transaction['error']=str(exc)
        except (CaptureError,ValueError) as exc:
            capture_error=exc
            transaction['error']=type(exc).__name__+': '+str(exc)[:180]
        except Exception as exc:
            capture_error=exc
            transaction['error']=type(exc).__name__+': '+str(exc)[:180]
        finally:
            # Cancellation may occur after primitives but before toggle returns.
            last=c.cycle.last_receipt or {}
            if last.get('authorization_id')==transaction['transaction_id'] and last.get('chosen_option')=='grind_hud_hide' and not transaction.get('hide_receipt'):
                transaction['hide_receipt']=last
                transaction['restoration_needed']=bool(last.get('possible_input'))
                transaction['child_receipt_ids'].append(last['receipt_id'])
            hide=transaction.get('hide_receipt') or {}
            if transaction['restoration_needed'] and (hidden is None or transaction.get('hide_verified')) and hide.get('completed') and not hide.get('dispatch_unknown') and not hide.get('error') and self.authority(frame):
                try:
                    restore=await toggle('grind_hud_restore',hidden or frame)
                    transaction['restore_receipt']=restore
                    if restore.get('completed') and not restore.get('dispatch_unknown') and not restore.get('error'):
                        async def restored_visible(fresh):
                            nonlocal restored
                            restored=fresh
                            change=differences(c,frame,fresh)
                            revealed=differences(c,hidden,fresh) if hidden else None
                            transaction.update(restore_difference=change,revealed_difference=revealed)
                            # Failed clean capture can still reconcile restoration
                            # against the original HUD, but yields no clean image.
                            visible=await self.visible_candidate(transaction,fresh)
                            if not self.authority(frame):raise ObservationAuthorityLost('HUD restore authority changed during evidence analysis')
                            if not self.known_chain(transaction):raise ObservationAuthorityLost('HUD restore input chain changed')
                            if visible and (revealed is None or min(revealed.values())>2):
                                transaction['restoration_needed']=False
                                return True
                            return False
                        await self.settle(transaction,capture_current,restored_visible,label='restore')
                except asyncio.CancelledError as exc:
                    cancellation=exc
                    transaction['restore_error']='CancelledError'
                except Exception as exc:
                    transaction['restore_error']=type(exc).__name__+': '+str(exc)[:180]
                    if not isinstance(exc,ObservationAuthorityLost) and (capture_error is None or isinstance(capture_error,ForegroundCaptureInterrupted)):
                        capture_error=exc
            last=c.cycle.last_receipt or {}
            if last.get('authorization_id')==transaction['transaction_id'] and last.get('chosen_option')=='grind_hud_restore' and not transaction.get('restore_receipt'):
                transaction['restore_receipt']=last
                if last['receipt_id'] not in transaction['child_receipt_ids']:transaction['child_receipt_ids'].append(last['receipt_id'])
            transaction['final_generation']=c.cycle.input_generation
            expected=transaction['source_generation']
            for label in ('hide_receipt','restore_receipt'):
                receipt=transaction.get(label) or {}
                if receipt:
                    if receipt.get('generation_before')!=expected or receipt.get('authorization_id')!=transaction['transaction_id'] or receipt.get('authorization_type')!='user_authorized_observation':
                        transaction['chain_invalid']=True
                    expected=receipt.get('generation_after')
            if expected!=c.cycle.input_generation:transaction['chain_invalid']=True
            transaction['status']='restored_verified' if not transaction['restoration_needed'] and hidden and restored and transaction.get('hide_verified') and not transaction.get('chain_invalid') and not transaction.get('error') else 'observation_unresolved'
            if restored:transaction.update(restored_frame=restored.__dict__,restored_frame_id=restored.frame_id,restored_captured_at=restored.captured_at,restored_sha256=digest(restored))
            self.persist(transaction)
            c.event('grind_observation_transaction',dict(transaction))
        if capture_error and not isinstance(capture_error,ForegroundCaptureInterrupted):raise capture_error
        if capture_error:raise capture_error
        if cancellation:raise cancellation
        if transaction['status']!='restored_verified':return None
        c.hud_no_effect_count=0;self.persist(transaction)
        # Known non-rotating observation primitives carry orientation evidence
        # across this exact contiguous child chain, never external input.
        if c.hunt.mapping_generation==transaction['source_generation']:
            c.hunt.mapping_generation=transaction['final_generation']
        # Search displacement currentness is distinct from heading (a turn
        # deliberately retires heading). Transport only this attributed fact
        # through the same verified non-gameplay child chain, never its receipt.
        move=c.hunt.last_completed_action or {}
        if (move.get('search_generation',move.get('generation_after'))==transaction['source_generation']
                and move.get('session_epoch')==c.cycle.session_epoch
                and move.get('receipt_id')==c.hunt.last_gameplay_receipt_id):
            move['search_generation']=transaction['final_generation']
            move['search_observation_transaction_id']=transaction['transaction_id']
        return CleanSnapshot(c,hidden,restored,transaction)

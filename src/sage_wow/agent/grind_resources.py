"""Small, Sage-owned resource decisions without abandoning an active enemy."""
from dataclasses import replace
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from statistics import median
from types import SimpleNamespace
import asyncio
import hashlib
import time

from PIL import Image

from sage_wow.agent.cycle import ActionCandidate, CycleResult, DispatchValidation
from sage_wow.agent.ui_layout import from_profile, validate_frame


HEAL_OBSERVATION_SECONDS = 6.


def finish_heal_observation(c, frame=None, measurement=None, *, effect='unknown', reason):
    """Retire only the original heal, including interruption before a fresh view."""
    state=c.heal_pending
    if not state:return
    original=state['pending_snapshot'];owner=state['owner']
    ident=original['receipt']['receipt_id']
    consumed=any(item.get('receipt_id')==ident for item in c.hunt.outcomes)
    intact=owner==original
    if not consumed and not (owner.get('outcome_consumed') and owner.get('receipt')==original['receipt']):
        # A replaced or mutated pending object is not the historical owner.
        c.hunt.resolve(effect,frame or SimpleNamespace(frame_id=None),measurement or {},
            pending=owner if intact else deepcopy(original),linked=True)
    # Mutated metadata is not a new physical execution. Close all remaining
    # live aliases of this retired receipt without touching replacement work.
    if (owner.get('receipt') or {}).get('receipt_id')==ident:owner['outcome_consumed']=True
    current=c.hunt.pending
    if current and (current.get('receipt') or {}).get('receipt_id')==ident:
        current['outcome_consumed']=True
        c.hunt.pending=None
    c.event('grind_self_heal_observed',{'frame_id':getattr(frame,'frame_id',None),
        'receipt_id':ident,'source_frame_id':state['source_frame_id'],
        'before_health':state['health'],'current_health':c.current_hud.get('player_health'),
        'outcome':effect,'already_consumed':consumed,'reason':reason,
        'deadline':state['deadline'],'self_only_command':True,
        'claim':'Linked resource observation; neither exclusive healing attribution nor spell failure proof'})
    c.heal_pending=None
    c.flush_outcomes()


async def observe_heal(c, frame, measurement, pending, linked):
    from sage_wow.agent.grind_observation import read_player_identity
    from sage_wow.agent.grind_encounter_recovery import capture_scope
    state=c.heal_pending;original=state['pending_snapshot'];receipt=original['receipt']
    binding=receipt.get('selected_binding') or {};execution=receipt.get('execution') or {}
    def current():
        return (pending is state['owner'] and c.hunt.pending is pending and pending==original
            and original.get('purpose')=='heal_preserve' and original.get('family')=='recovery'
            and binding=={'type':'cast_self_heal','spell':'Lesser Heal'}
            and execution.get('kind')=='cast_self_heal' and execution.get('command')=='/cast [@player] Lesser Heal'
            and receipt.get('possible_input') and receipt.get('completed')
            and not receipt.get('error') and not receipt.get('dispatch_unknown')
            and linked and c.revision==state['revision'] and c.profile.values==state['profile']
            and c.config==state['config'] and capture_scope(c,frame)==original['source_scope']
            and c.cycle.input_generation==receipt['generation_after']
            and c.fresh(frame) and not c.cycle._scope_error(frame)
            and frame.frame_id!=state['source_frame_id'])
    try:
        valid=current() and hashlib.sha256(Path(original['source_image']).read_bytes()).hexdigest()==original['source_hash']
        observed_at=datetime.fromisoformat(frame.captured_at).timestamp()
        valid=valid and observed_at>datetime.fromisoformat(receipt['occurred_at']).timestamp()
    except (OSError,ValueError,KeyError,TypeError):valid=False
    if valid:
        identity=await read_player_identity(c,frame,await c.rows(frame),full_name=True)
        try:
            valid=(identity['verified'] and current()
                and hashlib.sha256(Path(original['source_image']).read_bytes()).hexdigest()==original['source_hash'])
        except OSError:valid=False
    if not valid:
        finish_heal_observation(c,frame,measurement,reason='original_heal_authority_lost')
        return CycleResult('grind_reobserve',detail='Original heal observation retired; use current resources')
    if observed_at>state['deadline']:
        finish_heal_observation(c,frame,measurement,reason='observation_horizon_expired')
        return CycleResult('grind_reobserve',detail='Original heal window expired; use current resources')
    hud=c.current_hud;hp=hud.get('player_health')
    comparable=(hud.get('frame_id')==frame.frame_id and hp is not None
        and min(hud.get('health_confidence') or 0,state['health_confidence'])>=.8)
    improved=comparable and hp>state['health']+.03
    if not improved and observed_at<state['deadline']:
        c.wait_until=time.time()+min(.5,max(.01,state['deadline']-observed_at))
        return CycleResult('grind_reobserve',detail='Awaiting bounded original heal resource observation')
    finish_heal_observation(c,frame,measurement,
        effect='resources_improved' if improved else 'resources_unchanged' if comparable else 'unknown',
        reason='fresh_resource_benefit' if improved else 'bounded_observation_horizon_reached')
    c.hunt.compact_stage='inspect' if (pending.get('target') or {}).get('name') else 'acquire'
    return CycleResult('grind_reobserve',detail='Self-heal resource observation settled; resume the same encounter')


def own_death_evidence(c, frame, rows):
    """Current paired death controls, never empty bars or historical chat."""
    from sage_wow.agent.grounded_ui import box_of, inside, usable, text_key
    region=(frame.width*.25,frame.height*.10,frame.width*.80,frame.height*.65)
    controls={}
    for row in usable(rows,frame.width,frame.height):
        label=text_key(row.text)
        if (row.confidence>=.75 and label in {'release spirit','recap'}
                and inside(box_of(row),region)):
            controls.setdefault(label,[]).append(row)
    if any(len(controls.get(label,[]))!=1 for label in ('release spirit','recap')):return None
    release=controls['release spirit'][0];recap=controls['recap'][0]
    if (box_of(release)[2]>box_of(recap)[0] or
            abs(release.bounds['y']-recap.bounds['y'])>max(release.bounds['height'],recap.bounds['height'])):
        return None
    return {'kind':'own_release_spirit_modal','frame_id':frame.frame_id,
        'source_sha256':hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
        'controls':[{'text':r.text,'confidence':r.confidence,'bounds':dict(r.bounds)} for r in (release,recap)]}


async def stop_if_own_death(c, frame):
    if not c.fresh(frame,travel=c.travel_budget is not None) or c.cycle._scope_error(frame):return None
    authority=(c.revision,c.cycle.session_epoch,c.cycle.input_generation)
    death=own_death_evidence(c,frame,await c.rows(frame))
    if (death and authority==(c.revision,c.cycle.session_epoch,c.cycle.input_generation)
            and c.fresh(frame,travel=c.travel_budget is not None) and not c.cycle._scope_error(frame)):
        c.event('grind_own_death_observed',death)
        c.stop('own_death_modal_observed')
        return CycleResult('grind_stopped',detail=c.reason)
    return None


async def self_heal_guard(c, source):
    """Self-only casting needs current own identity, not unchanged HUD paint."""
    from sage_wow.agent.grind_observation import read_player_identity
    from sage_wow.agent.grind_search import ui_evidence
    source_hash=hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest()
    authority=(c.revision,c.cycle.session_epoch,c.cycle.input_generation)
    source_rows=await c.rows(source)
    source_identity=await read_player_identity(c,source,source_rows,full_name=True)
    async def guard(_):
        fresh=await asyncio.to_thread(c.capture)
        if c.archive:fresh=replace(fresh,image_path=str(c.archive.frame(fresh,c.cycle.session_epoch,c.cycle.input_generation)))
        validate_frame(fresh,c.profile)
        rows=await c.rows(fresh)
        identity=await read_player_identity(c,fresh,rows,full_name=True)
        hud=await asyncio.to_thread(hud_resources,c,fresh)
        predicates={
            'source_unchanged':hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest()==source_hash,
            'same_authority':authority==(c.revision,c.cycle.session_epoch,c.cycle.input_generation),
            'scope_current':not c.cycle._scope_error(source) and not c.cycle._scope_error(fresh),
            'source_fresh':c.fresh(source),'fresh_frame':c.fresh(fresh),
            'distinct_frame':fresh.frame_id!=source.frame_id,
            'same_source':fresh.source==source.source,
            'same_dimensions':(fresh.width,fresh.height)==(source.width,source.height),
            'source_own_identity':source_identity['verified'],'fresh_own_identity':identity['verified'],
            'world_clear':not ui_evidence(c.profile,fresh,rows,{})['positive'],
            'no_death_modal':not own_death_evidence(c,fresh,rows),
            'living_health':hud.get('player_health') is not None and hud['player_health']>0
                and (hud.get('health_confidence') or 0)>=.8}
        valid=all(predicates.values())
        return DispatchValidation(valid,fresh if valid else None,'Fresh own-player/world confirmation for self-only heal',
            {'predicates':predicates,'failed_predicates':[name for name,value in predicates.items() if not value],
             'source_sha256':source_hash,'fresh_frame_id':fresh.frame_id,
             'source_identity':source_identity,'fresh_identity':identity,'fresh_resources':hud})
    return guard


def hud_resources(controller, frame):
    from sage_wow.agent.grind_perception import read_current_bars
    bars=read_current_bars(frame,ui_layout=from_profile(controller.profile))
    # Mana geometry is explicitly calibrated for this profile, never guessed.
    mana=None
    box=controller.config.get('player_mana_box')
    if box:
        with Image.open(frame.image_path) as source:
            crop=source.convert('RGB').crop(tuple(box))
            coverage=[]
            for y in range(crop.height):
                coverage.append(sum(b>r*1.3 and b>g*1.15 and b>85
                    for r,g,b in crop.crop((0,y,crop.width,y+1)).get_flattened_data())/crop.width)
            fill=median(coverage)
            if median(abs(x-fill) for x in coverage)<.15:mana=round(fill,3)
    # read_current_bars returns a Scene; only current screenshot facts escape.
    result={'frame_id':frame.frame_id,'player_health':bars.health,
        'health_confidence':bars.health_confidence,'target_health':bars.target_health,
        'target_health_confidence':bars.target_health_confidence,'player_mana':mana}
    if (bars.health_evidence or {}).get('alternate') is not None:
        result['player_health_measurement']=bars.health_evidence['alternate']
    return result


async def process_resources(c,frame,target,measurement,pending,linked):
    from sage_wow.agent.grind_only import evidence_image
    h=c.hunt;hud=c.current_hud;hp=hud.get('player_health')
    if c.heal_pending:
        return await observe_heal(c,frame,measurement,pending,linked)
    threshold=c.config['heal_health_fraction']
    if hp is None or hp>threshold:return None
    if h.failures['recovery']>=2:
        # Spent heals do not make critical health an ordinary combat question.
        # Keep the existing finite escape/hold recovery, without refunding any
        # healing, motion or unfinished encounter debt.
        c.enter_resource_recovery()
        return None
    if time.time()<c.resource_defer_until:return None
    if h.no_mana or h.heal_unavailable or not c.config['preserve_target_heal']:return None
    heal=c.profile.values['controls']['bindings'].get('lesser_heal',{})
    if not heal.get('verified_from'):return None
    # Confirmed low HP opens a choice. Ordinary damage never enters this menu.
    guard=await self_heal_guard(c,frame)
    valid=lambda:c.fresh(frame) and not c.cycle._scope_error(frame)
    options=[
        ActionCandidate('heal_self','Own health is genuinely low: cast Lesser Heal on myself once while keeping this enemy selected.',
            {'type':'cast_self_heal','spell':'Lesser Heal'},precondition=valid,dispatch_guard=guard),
        ActionCandidate('resources_unclear','The health reading conflicts with the visible player bar; obtain a fresh view.',{'type':'observe_only'}),
        ActionCandidate('player_dead','My own player is explicitly dead.',{'type':'observe_only'})]
    visual=target.get('visual_observation') or {}
    from sage_wow.agent.grind_cast_feedback import unresolved_cues, retained_error_candidate
    unresolved_error=bool(unresolved_cues(pending) or retained_error_candidate(c,frame,target)
        or h.cast_error and h.cast_error.get('status')=='active')
    from sage_wow.agent.grind_search import INEFFECTIVE_COMPLETED_CAST
    from sage_wow.agent.grind_ineffective_cast import recovery_candidate
    def semantic_retry_required():
        return (h.cast_obligation==INEFFECTIVE_COMPLETED_CAST
            or (h.retry_credit_source or {}).get('kind')=='ineffective_cast_correction')
    semantic_finisher=semantic_retry_required()
    finisher_proof=recovery_candidate(c,frame,target,h.pending,'inspect',retry=True) if semantic_finisher else None
    def ineffective_debt():
        # no_effect_retries is cumulative; later observed damage retires the
        # current combat failures and must restore ordinary finishers.
        return h.no_effect_retries>=2 and h.failures['combat']>=2
    ineffective_finisher=ineffective_debt()
    finisher_owner=h.combat_history_key
    correction_credit=dict(h.retry_credit_source or {}) if (h.retry_credit_source or {}).get('kind') in {'linked_correction','ineffective_cast_correction'} else None
    def finisher_valid():
        from sage_wow.agent.grind_correction_handoff import retry_current
        if not valid() or not retry_current(c,frame,target):return False
        if correction_credit and (not h.retry_credit or h.retry_credit_source!=correction_credit):return False
        if (ineffective_finisher or ineffective_debt()) and (not h.retry_credit or h.combat_history_key!=finisher_owner):return False
        if semantic_finisher:
            return bool(finisher_proof) and recovery_candidate(c,frame,target,h.pending,'inspect',retry=True)==finisher_proof
        return not semantic_retry_required()
    finisher=(not unresolved_error and finisher_valid() and visual.get('life_state')=='alive' and target.get('eligibility')=='eligible'
        and hud.get('target_health') is not None and hud['target_health']<=.25)
    if finisher:
        options.append(ActionCandidate('finish_fight','The selected eligible creature is nearly dead: cast one Smite now to finish it; keep the target and position.',
            {'type':'cast_guarded','spell':'Smite'},precondition=finisher_valid,
            dispatch_guard=await c.guard(frame,target,exact_level=visual['level'])))
    image=await c.prepare_image(frame,evidence_image,frame,target['box'],
        Path(frame.image_path).with_name(f'grind-view-{frame.frame_id}.png'),player_badge=tuple(c.config['player_level_box']))
    if isinstance(image,CycleResult):return image
    result=await c.decide(frame,image,
        f'Continue the current encounter. Fresh resource estimates: {hud}. Ordinary incoming damage is expected. '
        'Healing is one cast on myself and preserves enemy selection. A cast receipt does not prove healing.',
        'Choose a self-heal for genuinely low health, or finish a nearly dead enemy. Do not flee or abandon the encounter.',
        options,travel_budget=c.travel_budget,compact=True)
    receipt=result.receipt or {};chosen=getattr(result.decision,'chosen',None)
    accepted=result.status=='dispatched' and receipt==c.cycle.last_receipt and receipt.get('completed') and not receipt.get('dispatch_unknown') and not receipt.get('error')
    if receipt.get('possible_input') and not accepted:c.stop('partial_or_unknown_grind_input')
    if not accepted:return result
    if chosen=='player_dead':c.stop('sage_observed_player_death')
    elif chosen=='heal_self':
        await c.apply_choice(frame,target,measurement,pending,linked,result,
            {'action':'heal','family':'recovery','purpose':'heal_preserve','scoped_question':True},chosen,c.level.last_confirmed_level)
        c.heal_pending={'health':hp,'health_confidence':hud.get('health_confidence') or 0,
            'source_frame_id':frame.frame_id,'receipt_id':receipt['receipt_id'],
            'deadline':datetime.fromisoformat(receipt['occurred_at']).timestamp()+HEAL_OBSERVATION_SECONDS,
            'owner':h.pending,'pending_snapshot':deepcopy(h.pending),'revision':c.revision,
            'profile':deepcopy(c.profile.values),'config':deepcopy(c.config)}
    elif chosen=='finish_fight':
        await c.apply_choice(frame,target,measurement,pending,linked,result,
            {'action':'cast','family':'combat','purpose':'cast','mob_level':visual['level'],
             'scoped_question':True},chosen,c.level.last_confirmed_level)
    else:
        c.resource_defer_until=time.time()+1
        c.wait_until=time.time()+.1
    return result

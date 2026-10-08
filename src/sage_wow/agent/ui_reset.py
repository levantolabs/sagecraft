"""A user-approved bounded UI bailout after three unchanged unsuccessful retries.

This closes UI only. Sage retains gameplay choices; deadlines and transaction
records are never reset. Every Escape and fresh verification has provenance.
"""
from __future__ import annotations

import json
import time
from uuid import uuid4

from sage_wow.models import Event


def enabled(runtime):
    return runtime.profile.values.get('agent', {}).get('ui_reset', {}).get('enabled') is True


def panel_cues(frame, observations):
    """Positive UI proposals veto closure; absence alone never confirms it."""
    headings = {'spellbook','character','character info','backpack','inventory','quest log',
        'map & quest log','world map','game menu','talents','collections','social','help',
        'options','settings','merchant','trainer'}
    controls = {'goodbye','complete quest','accept','buyback','logout','exit game'}
    found=[];guild_buttons={}
    for row in observations or []:
        label=' '.join(row.text.casefold().split())
        b=row.bounds
        if (row.confidence >= .75 and 0 <= b.get('y',0) < frame.height*.82
                and 0 <= b.get('x',0) < frame.width*.80
                and label in headings | controls):
            found.append(label)
        # The guild invitation is a centered modal with these two adjacent
        # controls. Require the pair so passive chat or lone guild text cannot
        # create an Escape proposal. Recognition never accepts membership.
        if (label in {'join guild','decline invitation'} and row.confidence>=.75
            and b.get('width',0)>0 and b.get('height',0)>0
            and frame.width*.25<=b.get('x',0)<frame.width*.75
            and frame.height*.10<=b.get('y',0)<frame.height*.65):
            guild_buttons[label]=b
    join=guild_buttons.get('join guild');decline=guild_buttons.get('decline invitation')
    if (join and decline and join['x']+join['width']<=decline['x']
        and abs(join['y']-decline['y'])<=max(join['height'],decline['height'])):
        found.extend(('join guild','decline invitation'))
    return sorted(set(found))


def _stall_fingerprint(runtime, state, cues):
    return [runtime.cycle.session_epoch,runtime.cycle.input_generation,list(cues),
            runtime.v2_controller.objective,state.get('active_quest_name')]


def observe(runtime, frame, result):
    if not enabled(runtime) or not result:
        return
    controller=runtime.v2_controller
    if controller is None:
        return
    t=runtime.gameplay.tactical
    state=t.state if t is not None else runtime.gameplay.state
    record=state.setdefault('ui_reset', {'runs':0})
    if record.get('pending') or record.get('last_frame_id')==frame.frame_id:
        return
    record['last_frame_id']=frame.frame_id
    receipt=result.receipt or {}
    if result.status not in {'dispatched','needs_more_evidence','precondition_failed'}:
        record['repeats']=0
        return
    decision=result.decision
    if not decision:
        # Null answers with no input still consume a bounded repeated review.
        if receipt.get('possible_input') or result.status=='decision_failed':return
    scene=getattr(t,'_launch_last_scene',None)
    cues=(panel_cues(frame,getattr(scene,'session_all_observations',None))
          if scene is not None and scene.frame_id==frame.frame_id else [])
    if record.get('suppressed_stall') and scene is not None and scene.frame_id==frame.frame_id:
        fingerprint=_stall_fingerprint(runtime,state,cues)
        if record['suppressed_stall']==fingerprint and not receipt.get('possible_input'):
            record['repeats']=0
            if t is not None:t.persist()
            return
        if record['suppressed_stall']!=fingerprint:record.pop('suppressed_stall',None)
    progress=controller.state.get('last_verified_progress')
    key=json.dumps(progress,sort_keys=True,default=str)
    if record.get('progress_key')!=key:
        record.update(progress_key=key,repeats=0,runs=0)
    intake=state.get('quest_intake') or {}
    if ((intake.get('status')=='active' or intake.get('status')=='complete' and (intake.get('post_accept_review') or {}).get('phase')=='activation') and intake.get('last_decision_frame_id')==frame.frame_id
            and not receipt.get('possible_input')
            and (getattr(decision,'chosen',None) in {None,'inspect_npc_panel','inspect_offer_layout',
                'inspect_acceptance_evidence','inspect_giver_panel','details_unclear','details_title_mismatch',
                'acceptance_not_visible','acceptance_log_inspect','inspect_selected_outcome',
                'recover_selected_outcome','replan_selected_outcome','outcome_unclear',
                'outcome_panel_closed','inspect_after_acceptance','after_acceptance_unclear','replan_after_acceptance','inspect_active_quests','no_suitable_active_quest'} or (intake.get('post_accept_review') or {}).get('phase')=='recovery' and getattr(decision,'chosen',None) in {'close_dialog','continue_dialog','next_page','previous_page'} or str(getattr(decision,'chosen','')).startswith(('selected_details_','other_page_','remaining_offers_','accepted_visit_')))
            and not set(cues)-{'accept','goodbye','complete quest','quest log','map & quest log'}):
        # Intake owns this failure streak and its bounded parent replan. Shared
        # cleanup must not preempt it and erase the streak after each Escape.
        record['repeats']=0
        if t is not None:t.persist()
        return
    mode='no_input_review'
    if receipt.get('possible_input'):
        # Repeated UI actions count only with a positive unchanged panel proposal.
        # Ordinary movement, combat and successful transaction progress do not.
        if not receipt.get('completed') or not cues:
            record['repeats']=0
            return
        mode='repeated_ui_input'
    signature=[mode,controller.objective,state.get('active_quest_name'),getattr(decision,'chosen',None),
        runtime.cycle.input_generation if mode=='no_input_review' else None,cues]
    record['repeats']=record.get('repeats',0)+1 if record.get('signature')==signature else 1
    record['signature']=signature
    if record['repeats']>=3:
        record['pending']={'id':str(uuid4()),'epoch':runtime.cycle.session_epoch,
            'started_at':time.time(),'steps':0,'checks':0,'last_frame_id':None,
            'expected_generation':runtime.cycle.input_generation,'trigger':signature,
            'mode':mode,'visible_ui_proposals':cues,
            'last_request_id':getattr(getattr(decision,'envelope',None),'request_id',None)}
        runtime.store.append(Event.create('ui_reset_requested', {
            **record['pending'],'repeats':record['repeats'],
            'source':'user-approved three-repeat mechanical UI recovery; not a Sage gameplay choice',
            'progress_credit':False}))
    runtime.store.save_checkpoint('gameplay',state)
    if t is not None:t.persist()



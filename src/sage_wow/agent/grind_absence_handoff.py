"""An accepted current empty HUD ends selected ownership, never past uncertainty."""
from copy import deepcopy

from sage_wow.agent.grind_relocation import _source, _intact, _absence, current_target, safe_current
from sage_wow.agent.grind_search import target_identity


def capture(c, frame, target, pending):
    h=c.hunt;owner=h.combat_history_key
    history=h.target_history.get(owner) or {};recent=h.recent_combat or {}
    target=current_target(c,frame,target)
    if (pending is not None or h.pending is not None or h.encounter_ended or not owner
        or h.phase in {'recover','ui_recover','input_effect_unverified'}
        or c.last_ui_presence is not False or not safe_current(c,frame,target)
        or not _absence(target) or (h.approach or {}).get('history_key')!=owner
        or history.get('completed') or not history.get('cast_attempted')
        or history.get('encounter_id')!=h.encounter
        or recent.get('encounter_id')!=h.encounter or recent.get('target_history_key')!=owner
        or not h.last_target or target_identity((recent.get('target') or {}).get('name'))!=h.last_target
        or not h.known_completed_input(recent) or not (recent.get('receipt') or {}).get('possible_input')
        or not _intact({'image_path':recent.get('source_image'),'sha256':recent.get('source_hash'),
            'scope':recent.get('source_scope') or {}})):
        return None
    return {'owner':owner,'encounter_id':h.encounter,'last_target':h.last_target,
        'source':_source(frame,c),'recent_combat':deepcopy(recent),
        'history':deepcopy(history),'cast_obligation':h.cast_obligation,
        'cast_error':deepcopy(h.cast_error),'target_continuity':h.target_continuity,
        'selection_revision':h.selection_revision}


def accept(c, frame, target, pending, result, offered):
    """Only this unchanged owner and its current observe-only answer can close."""
    if not offered or capture(c,frame,target,pending)!=offered:return False
    h=c.hunt;receipt=result.receipt or {};decision=result.decision
    source=offered['source']
    if (not _intact(source) or result.status!='dispatched'
        or getattr(decision,'chosen',None)!='no_selected_frame'
        or receipt!=c.cycle.last_receipt or receipt.get('possible_input')
        or not h.known_completed_input({'receipt':receipt})
        or receipt.get('request_id')!=decision.envelope.request_id
        or (receipt.get('selected_binding') or {}).get('type')!='observe_only'
        or receipt.get('source_frame_id')!=frame.frame_id
        or receipt.get('session_epoch')!=c.cycle.session_epoch
        or receipt.get('generation_before')!=c.cycle.input_generation
        or receipt.get('generation_after')!=c.cycle.input_generation):return False
    fact={'owner':offered['owner'],'encounter_id':offered['encounter_id'],
        'combat_receipt_id':offered['recent_combat']['receipt']['receipt_id'],
        'source':deepcopy(source),'assessment':deepcopy(receipt),
        'disposition':'lost_unknown','claim':'Current selected HUD absent; prior damage, life, kill and escape unknown',
        'input_authority':False,'progress_credit':False,'allowance_renewed':False}
    h.encounter_ended=True;h.terminal_reason='lost_unknown'
    h.target_history[offered['owner']]['selection_loss']=fact
    h.remember_approach()
    if h.phase in {'fight','approach'}:h.phase='search'
    c.event('grind_absent_encounter_handoff',deepcopy(fact))
    return True

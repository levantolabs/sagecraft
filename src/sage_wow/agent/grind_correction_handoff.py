"""Completed corrections survive sensing gaps, never a new input/target scope."""
from datetime import datetime
import hashlib
from pathlib import Path


def current(c, frame, target, proof=None):
    from sage_wow.agent.grind_encounter_recovery import capture_scope
    from sage_wow.agent.grind_search import target_identity
    from sage_wow.agent.grind_cast_feedback import cue_key
    h = c.hunt
    proof = h.correction_evidence if proof is None else proof
    if not proof:
        return False
    receipt = proof.get('receipt') or {}
    owner = proof.get('target_history_key')
    history = h.target_history.get(owner) or {}
    visual = target.get('visual_observation') or {}
    if (not c.fresh(frame) or c.cycle._scope_error(frame) or h.blocked or h.input_effect_unverified
            or proof.get('purpose') not in {'approach', 'cast_correction', 'standing'}
            or proof.get('encounter_id') != h.encounter
            or proof.get('current_selection_revision', proof.get('selection_revision')) != h.selection_revision
            or proof.get('target_continuity') != h.target_continuity
            or proof.get('source_scope') != capture_scope(c, frame)
            or receipt.get('session_epoch') != c.cycle.session_epoch
            or receipt.get('generation_after') != c.cycle.input_generation
            or not receipt.get('possible_input') or not h.known_completed_input(proof)
            or target.get('self_target') or target.get('invalid_text')
            or visual.get('selected_hud') == 'absent'
            or visual.get('life_state') == 'dead'
            or visual.get('target_kind') in {'player', 'friendly_or_self'}
            or not target.get('name')
            or target_identity(target['name']) != target_identity((proof.get('target') or {}).get('name'))):
        return False
    if c.config.get('committed_combat', False):
        if (not owner or owner != (h.approach or {}).get('history_key')
                or history.get('completed') or history.get('retired_by_acquisition')):
            return False
        if proof.get('purpose') in {'cast_correction', 'standing'} and (
                owner != h.combat_history_key or h.encounter_ended
                or history.get('encounter_id') != h.encounter or history.get('encounter_ended')):
            return False
        if proof.get('purpose') in {'cast_correction', 'standing'}:
            error_id=proof.get('correction_error_receipt_id')
            if error_id:
                if ((h.cast_error or {}).get('cast_receipt_id')!=error_id
                        or (h.cast_error or {}).get('encounter_id')!=h.encounter
                        or proof.get('correction_cast_receipt_id')!=error_id
                        or proof.get('correction_error_cue')!=cue_key(h.cast_error)
                        or proof.get('granted') and h.cast_error.get('status')!='retired_by_linked_correction'):return False
            elif (proof.get('correction_cast_receipt_id') !=
                    ((h.recent_combat or {}).get('receipt') or {}).get('receipt_id')
                    or (h.recent_combat or {}).get('target_history_key')!=owner):return False
    try:
        if datetime.fromisoformat(frame.captured_at) <= datetime.fromisoformat(receipt['occurred_at']):
            return False
        for image_key, hash_key in [('source_image', 'source_hash'), ('assessment_image', 'assessment_hash')]:
            if image_key in proof and hashlib.sha256(Path(proof[image_key]).read_bytes()).hexdigest() != proof[hash_key]:
                return False
        if not proof.get('source_image') or not proof.get('source_hash'):
            return False
    except (OSError, KeyError, TypeError, ValueError):
        return False
    return True


def retry_current(c, frame, target):
    """Ordinary resource credits retain their existing independent authority."""
    origin = c.hunt.retry_credit_source or {}
    if origin.get('kind') not in {'linked_correction', 'ineffective_cast_correction'}:
        return True
    proof = c.hunt.correction_evidence or {}
    return bool(c.hunt.retry_credit and proof.get('granted')
        and origin.get('correction_receipt_id') == proof.get('receipt_id')
        and current(c, frame, target))


def expire(c, frame, target):
    if not retry_current(c, frame, target):
        c.hunt.retry_credit = False
        c.hunt.correction_evidence = None

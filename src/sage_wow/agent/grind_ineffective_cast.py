"""Accepted ineffective-cast outcomes need actions even without numeric diagnostics."""
import hashlib
from pathlib import Path


def recovery_candidate(c, frame, target, pending, stage, *, retry=False):
    from sage_wow.agent.grind_search import INEFFECTIVE_COMPLETED_CAST, target_identity
    from sage_wow.agent.grind_encounter_recovery import capture_scope
    from sage_wow.agent.grind_cast_feedback import retained_error_candidate
    h = c.hunt
    owner = h.combat_history_key
    history = h.target_history.get(owner) or {}
    source = h.recent_combat or {}
    receipt = source.get('receipt') or {}
    visual = target.get('visual_observation') or {}
    correction = h.correction_evidence or {}
    origin = h.retry_credit_source or {}
    if retry:
        from sage_wow.agent.grind_correction_handoff import retry_current
        if not retry_current(c, frame, target):return None
        if (not h.retry_credit or not correction.get('granted')
                or origin.get('kind') != 'ineffective_cast_correction' or origin.get('owner') != owner
                or origin.get('cast_receipt_id') != receipt.get('receipt_id')
                or origin.get('correction_receipt_id') != correction.get('receipt_id')
                or correction.get('encounter_id') != h.encounter
                or correction.get('target_history_key') != owner
                or correction.get('source_scope') != capture_scope(c, frame)
                or correction.get('generation_after') != c.cycle.input_generation
                or correction.get('purpose') not in {'cast_correction', 'standing'}):
            return None
    elif origin.get('kind') == 'ineffective_cast_correction' and h.retry_credit:
        # Invalidated proof cannot turn into a burst or strand recovery. Keep
        # the old credit unusable and offer another bounded correction/clear.
        if recovery_candidate(c, frame, target, pending, stage, retry=True):return None
    elif h.retry_credit or h.cast_obligation != INEFFECTIVE_COMPLETED_CAST:return None
    if (not c.config.get('committed_combat', False) or stage not in {'inspect', 'reinspect'}
            or pending is not None or h.pending is not None or not c.fresh(frame)
            or c.cycle._scope_error(frame) or h.blocked or h.input_effect_unverified
            or h.no_effect_retries < 2
            or h.disengagement or h.loot_request or c.heal_pending or h.no_mana
            or h.encounter_ended or history.get('encounter_ended') or history.get('completed')
            or history.get('retired_by_acquisition') or history.get('rejections')
            or history.get('encounter_id') != h.encounter
            or owner != (h.approach or {}).get('history_key')
            or source.get('encounter_id') != h.encounter or source.get('target_history_key') != owner
            or source.get('target_continuity') != h.target_continuity
            or source.get('source_scope') != capture_scope(c, frame)
            or receipt.get('session_epoch') != c.cycle.session_epoch
            or not receipt.get('possible_input') or not h.known_completed_input(source)
            or (h.cast_error or {}).get('status') == 'active'
            or retained_error_candidate(c, frame, target)
            or visual.get('selected_hud') != 'present' or visual.get('life_state') != 'alive'
            or target.get('eligibility') != 'eligible' or target.get('self_target') or target.get('invalid_text')
            or not target.get('name') or target_identity(target['name']) != h.last_target
            or target_identity((source.get('target') or {}).get('name')) != h.last_target):
        return None
    try:
        if hashlib.sha256(Path(source['source_image']).read_bytes()).hexdigest() != source['source_hash']:
            return None
        if retry and hashlib.sha256(Path(correction['source_image']).read_bytes()).hexdigest() != correction['source_hash']:
            return None
    except (KeyError, OSError, TypeError):
        return None
    return {'owner': owner, 'receipt_id': receipt['receipt_id'],
            'assessed_ineffective_casts': h.no_effect_retries,
            'correction_receipt_id': correction.get('receipt_id') if retry else None}


def action_context(target):
    return (f'World of Warcraft. Current selected living eligible creature: {target["name"]}. '
        'Sage assessed at least two completed cast attempts as ineffective. The cause is unconfirmed; '
        'this does not establish a range, facing or stance error, or unchanged resource bars. '
        'Choose one offered movement to improve the current visible approach, or clear this target '
        'and hunt another. The creature is in the world; its fixed HUD portrait does not show its direction. '
        'Use visible safe ground. The harness observes the result before permitting another cast. '
        'Earlier failed or unassessed methods keep their limits. Passive chat and normal HUD do not block actions.')

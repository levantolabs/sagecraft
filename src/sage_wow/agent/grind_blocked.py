"""Observation-only questions about the actual reason assessment paused."""
import json


PROVIDER_BLOCKS = {'blocked_timing', 'blocked_provider'}


def task(episode, target, ui, *, loading, pending):
    reason = episode['reason']
    provider = reason in PROVIDER_BLOCKS
    cause = ('An earlier decision request did not finish within its deadline.'
        if reason == 'blocked_timing' else 'Earlier decision-provider requests failed.'
        if reason == 'blocked_provider' else f'Assessment paused for {reason}.')
    hud = target.get('hud') or {}
    facts = {'current_target_name_proposal': target.get('name'),
        'current_target_level_proposals': target.get('levels', []),
        'current_hud': {k: hud.get(k) for k in ('player_health', 'health_confidence',
            'player_mana', 'target_health', 'target_health_confidence')},
        'current_blocking_ui_cues': ui.get('cues', []), 'loading': bool(loading),
        'pending_action': pending.get('action') if pending else None}
    context = (cause + ' This is a fresh observation-only reassessment. '
        'Current proposals may be incomplete; inspect the current image. '
        'Missing target text alone does not establish target absence or a kill. '
        + json.dumps(facts, separators=(',', ':')))
    if provider:
        question = ('Can the current image be read well enough to resume fresh assessment? '
            'Select blocked_changed_assessment if so, including when the scene is unchanged, '
            'the selected target is absent, or visible UI needs its own assessment. '
            'This only returns to fresh assessment; it performs no game input and resolves no past cast or movement. '
            'Choose blocked_still_unresolved only when the current view itself cannot be assessed.')
        resume = ('Resume fresh assessment of this readable current view. The earlier request failed; '
            'no world change or resolution of past actions is required. No input in this decision.')
        remain = 'The current image cannot be assessed yet; retain low-rate observation without input.'
    else:
        question = (f'Is the specific blocking condition ({reason}) now resolved on this current view? '
            'Only restore fresh assessment if the evidence supports it. No game input or past-action credit here.')
        resume = 'The stated blocking condition is now resolved; restore fresh assessment only, without input.'
        remain = 'The stated blocking condition remains; retain low-rate observation without input.'
    return context, question, resume, remain

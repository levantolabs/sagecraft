"""Sage-selected progression and service options grounded in visible UI evidence."""
from __future__ import annotations

from sage_wow.agent.cycle import ActionCandidate



SERVICE_INTENTS = {
    "service_train": ("TRAIN", "Review whether a nearby trainer or newly available spell training is useful."),
    "service_vendor": ("VENDOR", "Review visible vendor services, prices, bags, and currency before any purchase."),
    "service_rest": ("REST", "Consider recovery from the current health, mana, or danger shown in the image."),
    "service_inspect": ("INSPECT", "Inspect character, bags, and quest objectives using visible UI evidence."),
}


def filter_session_observations(observations, width: int, height: int,
                                expected_character: str | None = None) -> list:
    """Keep only non-chat session cues and safe button/known-character labels.

    In particular, never retain arbitrary login-screen OCR where credentials
    could appear. Chat cannot trigger session recovery.
    """
    visible=[]
    for obs in observations:
        b=obs.bounds
        x=b.get('x',0)+b.get('width',0)/2
        y=b.get('y',0)+b.get('height',0)/2
        # WoW chat lives in the lower-left. Ignore it as a trigger/source.
        if x < width*.38 and y > height*.58:
            continue
        visible.append(obs)
    cues=('disconnected','wow519','reconnect','enter world','log in','login',
          'loading','connecting','character selection','realm list')
    refresh_terms=('world around you will refresh','make sure you are out of combat')
    def central_modal(obs):
        b=obs.bounds
        x=b.get('x',0)+b.get('width',0)/2
        y=b.get('y',0)+b.get('height',0)/2
        return width*.25 <= x <= width*.80 and height*.12 <= y <= height*.65
    has_refresh=any(central_modal(obs) and any(term in obs.text.casefold() for term in refresh_terms)
                    for obs in visible)
    has_disconnected=any(any(token in obs.text.casefold() for token in ('disconnected','wow519')) for obs in visible)
    if not has_refresh and not any(any(token in obs.text.casefold() for token in cues) for obs in visible):
        return []
    safe_labels={'okay','ok','reconnect','enterworld','sagedecides'}
    if expected_character:
        safe_labels.add(''.join(expected_character.casefold().split()))
    def safe_label(obs):
        normalized=''.join(obs.text.casefold().split())
        if normalized in {'okay','ok'}:
            return (has_refresh or has_disconnected) and central_modal(obs)
        return normalized in safe_labels
    return [obs for obs in visible if any(token in obs.text.casefold() for token in cues)
            or (has_refresh and central_modal(obs)
                and (any(term in obs.text.casefold() for term in refresh_terms)
                     or ' '.join(obs.text.casefold().split()) in {'okay','ok','refreshnow'}))
            or safe_label(obs)]


def session_ui_candidates(observations, width: int, height: int,
                          character_name: str | None = None,
                          world_visible: bool = False) -> tuple[list[ActionCandidate], dict]:
    """Offer Sage only visible, context-matched session recovery buttons.

    OCR is fallible evidence: page context and exact button text must both be
    present. Unknown login/account screens expose no clicks.
    """
    lines=[" ".join(str(obs.text).casefold().split()) for obs in observations]
    joined=" ".join(lines)
    disconnected=any(term in joined for term in (
        'disconnected from the server','you have been disconnected','connection to the server was lost',
        'wow51900319','you were disconnected'))
    world_refresh=any(
        .25*width <= o.bounds.get('x',0)+o.bounds.get('width',0)/2 <= .80*width
        and .12*height <= o.bounds.get('y',0)+o.bounds.get('height',0)/2 <= .65*height
        and any(term in o.text.casefold() for term in (
            'the world around you will refresh','world around you will refresh',
            'make sure you are out of combat and in a safe area'))
        for o in observations)
    reconnect_context=disconnected or any(term in joined for term in (
        'reconnect to the server','unable to connect','connection lost','reconnecting'))
    expected=character_name
    def normalized_name(value):
        return ''.join(str(value).casefold().split())
    character_visible=bool(expected and normalized_name(expected) in {normalized_name(line) for line in lines})
    candidates=[]
    for index,obs in enumerate(observations):
        label=" ".join(str(obs.text).casefold().split())
        b=obs.bounds
        x=round(b.get('x',0)+b.get('width',0)/2); y=round(b.get('y',0)+b.get('height',0)/2)
        if obs.confidence < .70 or not (0<=x<width and 0<=y<height):
            continue
        button_in_modal=.25*width <= x <= .80*width and .12*height <= y <= .65*height
        if (disconnected or world_refresh) and label in {'okay','ok'} and (disconnected or button_in_modal):
            option='session_acknowledge_refresh' if world_refresh and not disconnected else 'session_dismiss_disconnected'
            context='world-refresh' if option=='session_acknowledge_refresh' else 'disconnected-server'
            candidates.append(ActionCandidate(option,
                f"OCR proposes the visible {obs.text!r} button at ({x},{y}) on a confirmed {context} notice. Confirm the screenshot shows that matching notice before clicking.",
                {'type':'click','image_x':x,'image_y':y}))
        elif label=='reconnect' and (reconnect_context or not world_visible):
            reconnect_context=True
            candidates.append(ActionCandidate('session_reconnect',
                f"OCR proposes the visible Reconnect button at ({x},{y}) on a reconnect screen. Confirm the screenshot shows this session-recovery page before clicking.",
                {'type':'click','image_x':x,'image_y':y}))
        elif character_visible and label=='enter world':
            candidates.append(ActionCandidate('session_enter_world',
                f"OCR proposes Enter World at ({x},{y}) and the expected character name {expected!r} is also visible. Click only if the screenshot clearly shows this character selected.",
                {'type':'click','image_x':x,'image_y':y}))
    candidates.extend([
        ActionCandidate('session_wait','Wait briefly for the current loading or reconnect state to change; send no game input.',{'type':'wait','seconds':1.0}),
        ActionCandidate('inspect_session_notice','Inspect the next fresh screenshot for the notice controls and expected character identity; no input.',{'type':'observe_only'}),
    ])
    evidence={'ocr_text':lines,'disconnected_context':disconnected,'world_refresh_context':world_refresh,
              'reconnect_context':reconnect_context,'expected_character':expected,
              'expected_character_visible':character_visible,
              'unsafe_login_or_loading_text':any(term in joined for term in (
                  'loading','log in','login','realm list','password','connecting','character selection')),
              'source':'fallible screenshot OCR proposals'}
    return candidates[:8],evidence



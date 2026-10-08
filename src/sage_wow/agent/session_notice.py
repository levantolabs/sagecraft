"""A bounded Sage refresh-notice interruption before V2 parent or child work.

Reuse the current frame's existing scene OCR. A notice is a UI prerequisite, not a
new quest objective or proof of progress. Never choose Refresh Now.
"""
from datetime import datetime
import json
import re
from pathlib import Path
from PIL import Image, ImageDraw

from sage_wow.agent.cycle import ActionCandidate, CycleResult
from sage_wow.agent.progression import filter_session_observations, session_ui_candidates
from sage_wow.models import Event
from sage_wow.agent.dialog_focus import DialogPanelBounds


def proposal(frame, scene, character_name):
    raw = getattr(scene, 'session_all_observations', None)
    rows = (filter_session_observations(raw, frame.width, frame.height, character_name)
            if raw is not None else getattr(scene, 'session_observations', []))
    if not rows:
        return None
    candidates, evidence = session_ui_candidates(rows, frame.width, frame.height,
        character_name, world_visible=getattr(scene, 'position', None) is not None
            or getattr(scene, 'health', None) is not None)
    if (not evidence['world_refresh_context'] or evidence['disconnected_context']
            or evidence.get('unsafe_login_or_loading_text')):
        return None
    # Frame a display-only copy around the existing same-frame notice rows.
    # Do not enlarge a candidate set, alter its points or run another OCR pass.
    framing=[]
    for row in rows:
        box=row.bounds
        x=box.get('x',0)+box.get('width',0)/2
        y=box.get('y',0)+box.get('height',0)/2
        label=' '.join(row.text.casefold().split())
        if (row.confidence>=.7 and frame.width*.25<=x<=frame.width*.80
                and frame.height*.12<=y<=frame.height*.65
                and (label in {'okay','ok','refresh now'} or any(term in label for term in
                    ('world around you will refresh','make sure you are out of combat')))):
            framing.append(box)
    crop=None
    if framing:
        pad_x,pad_y=round(frame.width*.02),round(frame.height*.02)
        crop=[max(0,min(b['x'] for b in framing)-pad_x),
              max(0,min(b['y'] for b in framing)-pad_y),
              min(frame.width,max(b['x']+b['width'] for b in framing)+pad_x),
              min(frame.height,max(b['y']+b['height'] for b in framing)+pad_y)]
    return {'frame_id': frame.frame_id, 'captured_at': frame.captured_at,
        'evidence': evidence, 'display_crop_pixels':crop,
        'acknowledge': [dict(option=c.option, description=c.description, binding=c.binding)
                        for c in candidates if c.option == 'session_acknowledge_refresh']}


def identity(notice):
    """Only the known warning and its button geometry; countdown is variable."""
    lines=(notice.get('evidence') or {}).get('ocr_text',[])
    warnings=[re.sub(r'\b\d+\s+(?:seconds?|minutes?)\b','<countdown>',line).rstrip('. ')
              for line in lines if 'world around you will refresh' in line]
    buttons=[(row.get('binding') or {}) for row in notice.get('acknowledge',[])]
    if len(warnings)!=1 or len(buttons)!=1:
        return None
    b=buttons[0]
    if type(b.get('image_x')) is not int or type(b.get('image_y')) is not int:
        return None
    return [warnings[0],round(b['image_x']/8),round(b['image_y']/8)]


def question_inputs(frame, notice, previous_click_sent, current_step=None):
    image=frame.image_path
    crop=notice.get('display_crop_pixels')
    if isinstance(crop,list) and len(crop)==4:
        bounds=DialogPanelBounds.parse(dict(zip(('left','top','right','bottom'),
            (crop[0]/frame.width,crop[1]/frame.height,crop[2]/frame.width,crop[3]/frame.height))))
        if bounds is not None:
            with Image.open(frame.image_path) as source:
                source=source.convert('RGB')
                detail=source.crop(bounds.pixels(frame.width,frame.height))
                scale=min(3.,frame.width/detail.width)
                detail=detail.resize((round(detail.width*scale),round(detail.height*scale)),Image.Resampling.LANCZOS)
                sheet=Image.new('RGB',(frame.width,frame.height+detail.height+28),(24,24,24))
                sheet.paste(source,(0,0));sheet.paste(detail,(0,frame.height+28))
                ImageDraw.Draw(sheet).text((8,frame.height+6),'Popup detail from the same screenshot',fill='white')
                image=Path(frame.image_path).with_name(f'notice-review-{frame.frame_id}.jpg')
                sheet.save(image,quality=90)
    context=json.dumps({'task':'Handle this interruption, then continue the same task.',
        'current_step':current_step or 'Choose the next gameplay objective.',
        'view':'Whole game screen above; enlarged popup below. OCR is a proposal, not a confirmed fact.',
        'previous_click_sent':previous_click_sent})
    instructions=('Choose the offered Okay action if this image shows the world-refresh notice and its Okay button. '
                  'Or leave this notice visible and continue if it does not obstruct the current task. '
                  'Inspect again if you cannot tell. Never ignore a disconnect, loading screen or unknown dialog. Refresh Now is not offered.')
    return image,context,instructions


async def review(controller, frame, notice, *, child_task=None, current_step=None):
    if not notice:
        controller.state.pop('parent_notice_episode', None)
        return None
    if notice.get('frame_id') != frame.frame_id or notice.get('captured_at') != frame.captured_at:
        return CycleResult('needs_more_evidence', detail='Session notice proposal is from another frame')
    root = controller._root()
    task = child_task or root
    epoch = controller.cycle.session_epoch
    scope = [epoch, task.task_id, task.objective_revision,root.task_id,root.objective_revision]
    now = controller.clock()
    episode = controller.state.get('parent_notice_episode')
    if not episode or episode.get('scope') != scope:
        episode = {'scope': scope, 'started_at': now, 'requests': 0, 'possible_click': False}
        controller.state['parent_notice_episode'] = episode
    current_identity=identity(notice)
    ignored=episode.get('ignored') or {}
    if (current_identity is not None and ignored.get('identity')==current_identity
            and now<ignored.get('expires_at',0)):
        controller.event_store.append(Event.create('session_notice_continuation_reused',{
            'frame_id':frame.frame_id,'task_id':task.task_id,'source_request_id':ignored.get('request_id'),
            'expires_at':ignored['expires_at'],'input_authorized':False}))
        return None
    if ignored:
        # A changed/expired notice needs a new question. Do not carry a previous
        # decision into it; each renewed continuation needs a fresh Sage answer.
        episode.pop('ignored',None)
        episode['requests']=0;episode['started_at']=now
    if episode['requests'] >= 3 or now-episode['started_at'] >= 30:
        return CycleResult('intervention_required', detail='Refresh notice unresolved after bounded Sage review')
    candidates = []
    acknowledgements = notice.get('acknowledge') or []
    if not episode['possible_click'] and len(acknowledgements) == 1:
        row = acknowledgements[0]
        binding = row.get('binding') or {}
        x, y = binding.get('image_x'), binding.get('image_y')
        if (row.get('option') == 'session_acknowledge_refresh' and binding.get('type') == 'click'
                and type(x) is int and type(y) is int and 0 <= x < frame.width and 0 <= y < frame.height):
            candidates.append(ActionCandidate(row['option'], 'Click Okay to dismiss the visible world-refresh notice.', binding))
    candidates.extend([
        ActionCandidate('session_wait', 'Inspect a fresh frame without input; wait for this notice to clear.', {'type':'observe_only'}),
        ActionCandidate('inspect_session_notice', 'Inspect the current notice and its Okay control on the next fresh screenshot; no input.', {'type':'observe_only'}),
    ])
    if current_identity is not None:
        candidates.insert(-2,ActionCandidate('session_continue_with_notice',
            'Leave this known world-refresh warning visible and continue the current task if it does not obstruct it. No input.',
            {'type':'observe_only'}))
    goal = controller.store.get_session_goal(controller.session_id)
    deadline = min(datetime.fromisoformat(goal.absolute_deadline).timestamp(), episode['started_at']+30)
    if child_task is not None:
        deadline=min(deadline,controller.child_scope_deadline(task.task_id))
    def current():
        # DecisionCycle separately tracks input generation, including its own
        # click steps. Preserve all durable root/session/pause/deadline checks.
        valid=(controller.child_scope_is_current(task.task_id,task.objective_revision,epoch)
               if child_task is not None else controller.parent_scope_is_current(root.task_id,root.objective_revision,
                   epoch,controller.cycle.input_generation))
        return valid and controller.clock() < deadline
    episode['requests'] += 1
    controller._persist()
    image,context,instructions=question_inputs(frame,notice,episode['possible_click'],current_step)
    with controller.cycle.scoped_execution_scope(task_id=task.task_id,
            objective_revision=task.objective_revision, session_epoch=epoch,
            deadline_epoch=deadline, input_generation=controller.cycle.input_generation,
            max_frame_age_seconds=10, is_current=current):
        result = await controller.cycle.decide_and_execute(frame, image,context,instructions,
            candidates, reasoning='off', max_frame_age_seconds=10)
    episode['possible_click'] |= bool((result.receipt or {}).get('possible_input'))
    if (getattr(result.decision,'chosen',None)=='session_continue_with_notice'
            and (result.receipt or {}).get('completed') and current()):
        episode['ignored']={'identity':current_identity,'expires_at':min(deadline,now+20),
            'frame_id':frame.frame_id,'request_id':result.decision.envelope.request_id}
    controller._persist()
    controller.event_store.append(Event.create('v2_child_notice_review' if child_task is not None else 'v2_parent_notice_review', {
        'frame_id':frame.frame_id, 'task_id':task.task_id,
        'request_id':getattr(getattr(result.decision,'envelope',None),'request_id',None),
        'receipt_id':(result.receipt or {}).get('receipt_id'),
        'choice':getattr(result.decision,'chosen',None), 'requests':episode['requests'],
        'possible_click':episode['possible_click'], 'progress_verified':False,
        'source_proposal':notice, 'decision_image_path':str(image)}))
    return result



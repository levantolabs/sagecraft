"""Sage-owned corpse recovery, composed with the guarded grinding controller."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
from pathlib import Path
import time

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageFilter, ImageStat

from sage_wow.agent.cycle import ActionCandidate, CycleResult, DispatchValidation
from sage_wow.agent.looting import LootSweep, corpse_proposals
from sage_wow.control.target_names import plain_target_name
from sage_wow.agent import grind_loot_budget as budget


PASSIVE_LOOT_SECONDS = 180.


def _passive_clock(c, sweep, *, reset=False):
    # Keep the checkpoint key for compatibility. The clock belongs to the
    # unresolved loot obligation: clicks and camera movement are attempts,
    # not useful outcomes that buy another full recovery interval.
    clock = sweep.data.get('passive_clock')
    if reset or not clock or clock.get('session_epoch') != c.cycle.session_epoch:
        clock = {'session_epoch': c.cycle.session_epoch, 'active_at': c.hunt.active_seconds}
        sweep.data['passive_clock'] = clock
    return clock


def loot_route_pending(c):
    """An interrupted sweep survives combat without taking every combat frame."""
    from sage_wow.agent.grind_inventory import policy, retire_loot
    if policy(c):retire_loot(c);return False
    if getattr(c.hunt, 'loot_request', None):
        return True
    sweep = getattr(c, 'loot', None)
    if sweep is None:
        saved = c.store.load_checkpoint('grind_loot')
        if saved:
            c.loot_state = saved
            c.loot = sweep = LootSweep(saved)
    if not sweep or not sweep.pending:
        return False
    if not sweep.data.get('interrupted'):
        return True
    pending = c.hunt.pending
    return (not pending and (c.hunt.encounter_ended or
                            c.hunt.compact_last_result == 'no_selected_frame'))


def _save(c):
    c.store.save_checkpoint('grind_loot', c.loot_state)


def _sweep(c):
    if not hasattr(c, 'loot'):
        c.loot_state = c.store.load_checkpoint('grind_loot') or {}
        c.loot = LootSweep(c.loot_state)
    c.loot.data.update(search_limit=4, search_seconds=1.2)
    request = getattr(c.hunt, 'loot_request', None)
    if request:
        if not budget.request_allowed(c, request):
            c.hunt.loot_request = None
            c.event('grind_loot_retired_source_ignored', {'source': deepcopy(request)})
            _save(c)
            return c.loot
        identity = budget.source_key(c, request) if budget.enabled(c) else str((request['frame_id'], request.get('receipt_id')))
        if budget.enabled(c) and any(s['identity'] == identity for s in c.loot.data.get('death_sources', [])):
            c.hunt.loot_request = None
            _save(c)
            return c.loot
        queued = budget.enabled(c) and c.loot.pending and bool(c.loot.data.get('death_sources'))
        if not c.loot.pending:
            c.loot.data['death_sources'] = []
        if queued:
            c.loot.data['suspected_kills'] = (c.loot.data['suspected_kills'] + [identity])[-32:]
        else:
            c.loot.suspect_kill(identity)
        sources = c.loot.data.setdefault('death_sources', [])
        if not any(source['identity'] == identity for source in sources):
            target = request.get('target') or {}
            recent = getattr(c.hunt, 'recent_combat', None) or {}
            receipt = recent.get('receipt') or {}
            previous_authority = None
            if (receipt.get('receipt_id') and receipt.get('completed') and receipt.get('possible_input') and
                    not receipt.get('dispatch_unknown') and not receipt.get('error') and
                    (receipt.get('selected_binding') or {}).get('type') in {'cast_guarded', 'cast_burst'} and
                    (not request.get('receipt_id') or receipt['receipt_id'] == request['receipt_id']) and
                    recent.get('encounter_id') == request.get('encounter_id', c.hunt.encounter) and
                    plain_target_name(target.get('name')) and
                    str((recent.get('target') or {}).get('name', '')).casefold() == str(target.get('name') or '').casefold()):
                previous_authority = {'receipt_id': receipt['receipt_id'],
                    'generation_after': receipt.get('generation_after'), 'session_epoch': receipt.get('session_epoch'),
                    'expected_target_name': target['name']}
            sources.append({'identity': identity, 'frame_id': request['frame_id'],
                            'receipt_id': request.get('receipt_id'),
                            'encounter_id': request.get('encounter_id', c.hunt.encounter),
                            'target': {'name': target.get('name'), 'levels': target.get('levels', [])},
                            'image_path': request.get('image_path'),
                            'suspected': bool(request.get('suspected', False)),
                            'death_observed': bool(request.get('death_observed', not request.get('suspected', False))),
                            'credit_known': bool(request.get('credit_known', False)),
                            'request_id': request.get('request_id'),
                            'source_frame_id': request.get('combat_source_frame_id', request.get('source_frame_id')),
                            'source_image': request.get('combat_source_image', request.get('source_image')),
                            'last_guard_frame': request.get('last_guard_frame'),
                            'previous_target_authority': previous_authority})
            c.loot.data['death_sources'] = sources[-32:]
            budget.register(c, sources[-1])
            _passive_clock(c, c.loot, reset=True)
            c.event('grind_loot_obligation', sources[-1])
        if not queued:
            c.loot.data.update(stage='inspect', interrupted=False, selected_corpse=None)
        c.hunt.loot_request = None
        _save(c)
    return c.loot


def _corpse_choices(frame, layout=None):
    """Shape and fixed ground points are selection proposals, never death facts."""
    regions = (layout or {}).get('regions', {})
    excluded = [regions[key] for key in ('player_frame', 'target_overlay', 'minimap', 'quest_tracker') if key in regions]
    def on_hud(candidate):
        x, y = candidate.binding['image_x'], candidate.binding['image_y']
        return any(left <= x < right and top <= y < bottom for left, top, right, bottom in excluded)
    choices = [candidate for candidate in corpse_proposals(frame.image_path, frame.width, frame.height, nearby_ground=True)
               if not on_hud(candidate)][:2]
    for index, (x, y) in enumerate(( (.54, .52), (.54, .73) )):
        px, py = round(frame.width*x), round(frame.height*y)
        if any((candidate.binding['image_x']-px)**2 + (candidate.binding['image_y']-py)**2 < (frame.width*.05)**2 for candidate in choices):
            continue
        choices.append(ActionCandidate(f'corpse_ground_{index}',
            'Left-click this cyan ground point ONLY if it lies on a visibly dead body from our recent fight. '
            'This is a fixed location proposal, not a corpse detection. Reject living creatures, players, '
            'unrelated corpses and terrain. Selection alone proves neither death attribution nor loot.',
            {'type': 'click', 'image_x': px, 'image_y': py}))
    return [candidate for candidate in choices if not on_hud(candidate)]


def _image(frame, path, proposals, names=(), before_click=None):
    with Image.open(frame.image_path) as raw:
        image = raw.convert('RGB')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=max(16, round(frame.width*.016)))
    for proposal in proposals:
        x, y = proposal.binding['image_x'], proposal.binding['image_y']
        draw.ellipse((x-11, y-11, x+11, y+11), outline='cyan', width=3)
        label = proposal.option.replace('corpse_select_', 'C').replace('corpse_ground_', 'G')
        bounds = draw.textbbox((x+18, y-12), label, font=font)
        draw.rectangle((bounds[0]-3, bounds[1]-3, bounds[2]+3, bounds[3]+3), fill='#151b23')
        draw.text((x+18, y-12), label, fill='cyan', font=font)
    # Retain the whole view and magnify the current nearby ground, including
    # the corpses adjacent to and behind the player that the old scan omitted.
    ground = image.crop((round(frame.width*.32), round(frame.height*.42),
                         round(frame.width*.69), round(frame.height*.86)))
    ground = ground.resize((round(ground.width*1.65), round(ground.height*1.65)))
    canvas = Image.new('RGB', (max(image.width, ground.width), image.height+ground.height+32), '#151b23')
    canvas.paste(image, (0, 0))
    canvas.paste(ground, (0, image.height+32))
    ImageDraw.Draw(canvas).text((8, image.height+8),
        'CURRENT NEARBY GROUND — SELECT ONLY DEAD ' + ', '.join(names), fill='white')
    if before_click:
        with Image.open(before_click) as source:
            before = source.convert('RGB')
        before.thumbnail((canvas.width, image.height))
        linked = Image.new('RGB', (canvas.width, canvas.height+before.height+32), '#151b23')
        linked.paste(canvas, (0, 0))
        ImageDraw.Draw(linked).text((8, canvas.height+8),
            'BEFORE BOUND CORPSE CLICK — ONLY NEW LOOT EVIDENCE COUNTS', fill='white')
        linked.paste(before, (0, canvas.height+32))
        canvas = linked
    canvas.save(path)
    return path


def _combat_cues(c, frame, sweep):
    """Current screenshot/log cues permit an interruption choice, never select it."""
    from sage_wow.agent.grind_perception import read_current_bars
    from sage_wow.agent.ui_layout import from_profile
    layout = from_profile(c.profile)
    bars = read_current_bars(frame, ui_layout=layout)
    captured = datetime.fromisoformat(frame.captured_at).timestamp()
    previous = sweep.data.get('combat_observation') or {}
    confidence = bars.health_confidence or 0
    hp_drop = (bars.health is not None and confidence >= .55 and
               previous.get('health') is not None and previous.get('confidence', 0) >= .55 and
               previous.get('source') == frame.source and previous.get('frame_id') != frame.frame_id and
               previous.get('epoch') == c.cycle.session_epoch and
               0 < captured-previous.get('captured_at', 0) <= 10 and
               bars.health < previous['health']-.03)
    red_fraction = 0.
    box = layout['regions']['player_frame']
    side = min(box[3]-box[1], box[2]-box[0])
    with Image.open(frame.image_path) as source:
        portrait = source.convert('RGB').crop((box[0], box[1], box[0]+side, box[1]+side))
        red = total = 0
        for y in range(side):
            for x in range(side):
                radius = ((x-side/2)**2+(y-side/2)**2)/(side/2)**2
                if not .50 <= radius <= 1.05:
                    continue
                total += 1
                r, g, b = portrait.getpixel((x, y))
                red += r >= 130 and r > g*1.65 and r > b*1.65
        red_fraction = red/max(1, total)
    red_portrait = red_fraction >= .035
    character = c.profile.values['character']
    own_names = {character['name'].casefold(),
                 ' '.join(str(character.get(key) or '') for key in ('name', 'surname')).strip().casefold()}
    incoming = []
    for fact in getattr(c, 'combat_log_facts', []):
        if (fact.get('event') not in {'SWING_DAMAGE', 'SPELL_DAMAGE', 'RANGE_DAMAGE', 'SPELL_PERIODIC_DAMAGE', 'SWING_MISSED', 'SPELL_MISSED'} or
                str(fact.get('dest_name', '')).split('-', 1)[0].casefold() not in own_names or
                str(fact.get('source_name', '')).split('-', 1)[0].casefold() in own_names):
            continue
        try:
            stamp = fact['timestamp']
            if stamp.split()[0].count('/') == 1:
                observed = datetime.strptime(str(datetime.fromtimestamp(captured).year)+'/'+stamp, '%Y/%m/%d %H:%M:%S.%f').timestamp()
            else:
                observed = datetime.strptime(stamp, '%m/%d/%Y %H:%M:%S.%f').timestamp()
            if 0 <= captured-observed <= 3:
                incoming.append({'event': fact['event'], 'source_name': fact.get('source_name'), 'timestamp': stamp})
        except (KeyError, ValueError, IndexError):
            continue
    sweep.data['combat_observation'] = {'frame_id': frame.frame_id, 'source': frame.source,
        'epoch': c.cycle.session_epoch, 'captured_at': captured, 'health': bars.health, 'confidence': confidence}
    return {'frame_id': frame.frame_id, 'supported': bool(hp_drop or red_portrait or incoming),
            'player_health': bars.health, 'health_confidence': confidence,
            'fresh_health_drop': bool(hp_drop), 'red_portrait': red_portrait,
            'red_portrait_fraction': round(red_fraction, 4), 'fresh_incoming': incoming,
            'selected_alive': bool(bars.target_health is not None and bars.target_health > 0
                                   and (bars.target_health_confidence or 0) >= .55)}


def _same_gold_text(before, after, box):
    """Compare opaque HUD glyphs while scenery moves behind a translucent bar.

    This is continuity only, never a name or death observation. A fresh matching
    name, Sage's prior corpse confirmation and the positive-health veto remain
    separate requirements. Empty text or a substantial glyph change fails.
    """
    masks = []
    for path in (before, after):
        with Image.open(path) as image:
            crop = image.convert('RGB').crop(box)
        masks.append({i for i, (r, g, b) in enumerate(crop.get_flattened_data())
                      if r > 180 and g > 144 and g > b*1.25})
    a, b = masks
    return min(len(a), len(b)) >= 16 and len(a & b)/len(a | b) >= .90


def _corpse_continuity(c, before, after, target):
    from sage_wow.agent.grind_only import same_patch, same_target_badge
    from sage_wow.agent.ui_layout import from_profile
    health = from_profile(c.profile)['regions'].get('target_health', target['box'])
    name, badge = c.config.get('target_name_box'), c.config.get('target_level_box')
    if name and badge:
        return {
            'selected_corpse_unchanged': same_patch(before, after, name) or _same_gold_text(before, after, name),
            'corpse_health_unchanged': same_patch(before, after, health) or _same_gold_text(before, after, health),
            'corpse_badge_unchanged': same_target_badge(before, after, badge,
                c.config.get('target_badge_continuity'), combat_glow=True),
        }
    return {'selected_corpse_unchanged': same_patch(before, after, target['box']),
            'corpse_health_unchanged': same_patch(before, after, health)}


def _corpse_point_continuity(before, after, box):
    """Keep the same body location despite a few animated loot-particle pixels.

    The fallback only removes small isolated spots. It retains the original
    difference limits on the filtered body and bounds raw changes separately;
    neither this comparison nor a completed click establishes death or loot.
    """
    patches=[]
    for path in (before,after):
        with Image.open(path) as image:patches.append(image.convert('RGB').crop(box))
    def difference(a,b):
        diff=ImageChops.difference(a,b)
        return (sum(ImageStat.Stat(diff).mean)/3,
            sum(max(pixel)>24 for pixel in diff.get_flattened_data())/max(1,a.width*a.height))
    mean,changed=difference(*patches)
    strict=mean<=2 and changed<=.04
    filtered_mean,filtered_changed=difference(*(patch.filter(ImageFilter.MedianFilter(5)) for patch in patches))
    filtered=(mean<=4 and changed<=.04 and filtered_mean<=2 and filtered_changed<=.04)
    return {'approved':strict or filtered,'method':'raw' if strict else 'small_particle_filter' if filtered else 'rejected',
        'raw_mean':round(mean,4),'raw_changed_fraction':round(changed,4),
        'filtered_mean':round(filtered_mean,4),'filtered_changed_fraction':round(filtered_changed,4)}


def _click_verification(c, sweep, frame):
    authority = sweep.data.get('click_loot_authority') or {}
    source = Path(authority.get('image_path', ''))
    return bool(authority and authority.get('session_epoch') == c.cycle.session_epoch and
        authority.get('input_generation') == c.cycle.input_generation and
        authority.get('source') == frame.source and
        authority.get('size') == [frame.width, frame.height] and
        authority.get('sources') == [item['identity'] for item in budget.focused_sources(c, sweep)] and
        authority.get('frame_id') != frame.frame_id and
        datetime.fromisoformat(frame.captured_at) >= datetime.fromisoformat(authority['completed_at']) and
        source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() == authority.get('sha256'))


async def _guard(c, source, target, *, selected=False, point=None, absent=False):
    """A continuity veto, using fresh screenshots and the existing cycle gate."""
    from sage_wow.agent.grind_only import same_patch
    from sage_wow.agent.grind_search import ui_evidence
    from sage_wow.agent.ui_layout import from_profile, validate_frame
    source_hash = hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest()
    generation = c.cycle.input_generation

    async def check(_):
        if (not c.fresh(source) or generation != c.cycle.input_generation or
                hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest() != source_hash):
            return DispatchValidation(False, detail='Loot source/input authority changed')
        fresh = await asyncio.to_thread(c.capture)
        validate_frame(fresh, c.profile)
        if c.archive:
            fresh = replace(fresh, image_path=str(c.archive.frame(fresh, c.cycle.session_epoch, generation)))
        rows = await c.rows(fresh)
        regions = from_profile(c.profile)['regions']
        predicates = {'fresh': c.fresh(fresh) and c.fresh(source),
                      'input_generation': generation == c.cycle.input_generation,
                      'source_immutable': hashlib.sha256(Path(source.image_path).read_bytes()).hexdigest() == source_hash,
                      'distinct_frame': fresh.frame_id != source.frame_id,
                      'same_window': fresh.source == source.source,
                      'same_size': (fresh.width, fresh.height) == (source.width, source.height),
                      'ui_clear': not ui_evidence(c.profile, fresh, rows, {})['positive'],
                      'player_badge': same_patch(source.image_path, fresh.image_path, c.config['player_level_box'], badge=True)}
        if 'player_health' in regions:
            predicates['health_unchanged'] = same_patch(source.image_path, fresh.image_path, regions['player_health'])
        if selected:
            from sage_wow.agent.grind_perception import read_current_bars
            predicates.update(_corpse_continuity(c, source.image_path, fresh.image_path, target))
            actual = await c.target_proposal(fresh)
            bars = await asyncio.to_thread(read_current_bars, fresh, ui_layout=from_profile(c.profile))
            predicates['no_positive_target_health'] = not (bars.target_health is not None
                and bars.target_health > 0 and (bars.target_health_confidence or 0) >= .55)
            predicates['selected_identity_matches'] = (bool(actual.get('name')) and
                str(actual['name']).casefold() == str(target.get('name') or '').casefold() and
                (actual.get('visual_observation') or {}).get('selected_hud') != 'absent' and
                (actual.get('visual_observation') or {}).get('life_state') != 'alive' and
                not actual.get('self_target'))
        point_evidence=None
        if point:
            x, y = point
            box = (max(0, x-28), max(0, y-20), min(source.width, x+28), min(source.height, y+20))
            point_evidence=_corpse_point_continuity(source.image_path, fresh.image_path, box)
            predicates['corpse_point_unchanged'] = point_evidence['approved']
        if absent:
            actual = await c.target_proposal(fresh)
            predicates['selected_hud_still_absent'] = (not actual.get('name') and
                (actual.get('visual_observation') or {}).get('selected_hud') == 'absent' and
                same_patch(source.image_path, fresh.image_path, target['box'], badge=True))
        approved = all(predicates.values())
        return DispatchValidation(approved, fresh if approved else None,
                                  'Fresh corpse recovery continuity',
                                  {'source_hash': source_hash, 'predicates': predicates,
                                   **({'corpse_point':point_evidence} if point_evidence else {})})
    return check


async def process_loot(c, frame, target, measurement, rows):
    if not budget.enabled(c):
        return await _process_loot(c, frame, target, measurement, rows)
    sweep = _sweep(c)
    budget.begin(c, sweep)
    try:
        return await _process_loot(c, frame, target, measurement, rows)
    finally:
        budget.checkpoint_turn(c, sweep)


async def _process_loot(c, frame, target, measurement, rows):
    """Offer one current Sage choice; keys/clicks never establish loot success."""
    from sage_wow.agent.ui_layout import from_profile
    from sage_wow.agent.grind_inventory import policy, retire_loot
    if policy(c):
        retire_loot(c)
        return CycleResult('grind_reobserve',detail='Loot skipped for confirmed full inventory; not verified looted')
    h = c.hunt
    sweep = _sweep(c)
    if not sweep.pending:
        return CycleResult('grind_reobserve', detail='No outstanding corpse recovery')
    if sweep.data.get('interrupted'):
        if not loot_route_pending(c):
            return CycleResult('grind_reobserve', detail='Loot retained while combat resumes')
        sweep.data.update(interrupted=False, stage='inspect', selected_corpse=None)
        _passive_clock(c, sweep, reset=True)
    clock = _passive_clock(c, sweep, reset=bool(sweep.data.pop('resume_after_recovery', False)))
    passive_stalled = not budget.enabled(c) and h.active_seconds-clock['active_at'] >= PASSIVE_LOOT_SECONDS
    exhausted = sweep.data['attempts'] >= 8
    if exhausted:
        # Exhausted F attempts cannot return to unbounded corpse selection or
        # approach. Sage retains positive verification and the bounded sweep.
        sweep.data.update(stage='inspect', selected_corpse=None)
    # Retain selected-corpse confirmation and F after the recovery deadline.
    # An honest exit is added to both stages without forcing a new inspection.
    stage = sweep.data.get('stage', 'inspect')
    combat_cues = await asyncio.to_thread(_combat_cues, c, frame, sweep)
    c.event('grind_loot_combat_cues', combat_cues)
    if budget.retire_expired(c, sweep, frame, combat=combat_cues['supported']):
        return CycleResult('grind_reobserve', detail='Optional loot budget ended unverified; fresh hunting handoff')
    sweep.data['click_verification_current'] = _click_verification(c, sweep, frame)
    if not sweep.verification_available:
        sweep.data['confirmations'] = 0
    bindings = c.profile.values['controls']['bindings']
    options, meta, proposals = [], {}, []
    sources = budget.focused_sources(c, sweep)
    names = list(dict.fromkeys(source['target']['name'] for source in sources if source.get('target', {}).get('name')))
    valid = lambda: c.fresh(frame) and not c.cycle._scope_error(frame) and not h.blocked
    passive_scope = {'clock': deepcopy(clock), 'sources': deepcopy(sources),
        'last_action': deepcopy(sweep.data.get('last_action')),
        'loot_request': deepcopy(h.loot_request),
        'input_generation': c.cycle.input_generation, 'session_epoch': c.cycle.session_epoch}

    def stalled_current():
        return (valid() and sweep.pending and not h.input_effect_unverified
            and c.cycle.session_epoch == passive_scope['session_epoch']
            and c.cycle.input_generation == passive_scope['input_generation']
            and sweep.data.get('passive_clock') == passive_scope['clock']
            and budget.focused_sources(c, sweep) == passive_scope['sources']
            and sweep.data.get('last_action') == passive_scope['last_action']
            and h.loot_request == passive_scope['loot_request']
            and h.active_seconds-clock['active_at'] >= PASSIVE_LOOT_SECONDS)

    def observe(name, description, **data):
        if name == 'loot_uncertain' and budget.enabled(c):
            description = ('Cannot establish whether the current bound corpse is empty. Missing target, closed loot '
                'window, quest count, and lack of visible corpse alone do NOT prove completion.')
        if name == 'loot_verified' and budget.enabled(c):
            description = ('Positive visual evidence that the CURRENT bound corpse has been emptied after interaction, '
                'with no items remaining in its open loot window and no loot remaining on this corpse. '
                'Never infer success just from pressing F or gaining one item. Other queued corpses are separate tasks.')
        if (name not in {'under_attack','recover_now','dead_or_unrecoverable'}
            and not budget.permits(c, sweep)):
            return
        click_bound = name == 'loot_verified' and sweep.data['click_verification_current']
        options.append(ActionCandidate(name, description, {'type': 'observe_only'},
            precondition=lambda: valid() and (not click_bound or _click_verification(c, sweep, frame))
                and (name in {'under_attack','recover_now','dead_or_unrecoverable'} or budget.permits(c,sweep))
                and (not (passive_stalled and name == 'loot_unavailable') or stalled_current())))
        meta[name] = data

    async def motor(name, binding, description, *, selected=False, point=None, absent=False, **data):
        acquisition = bool(data.get('select') or data.get('search') or data.get('approach'))
        if not budget.permits(c, sweep, acquisition=acquisition):
            return
        original = await _guard(c, frame, target, selected=selected, point=point, absent=absent)
        async def guarded(source):
            result = await original(source)
            if not budget.permits(c, sweep, acquisition=acquisition):
                return DispatchValidation(False, None, 'Optional loot budget expired before input', result.evidence)
            return result
        options.append(ActionCandidate(name, description, binding,
            precondition=lambda: valid() and budget.permits(c,sweep,acquisition=acquisition), dispatch_guard=guarded))
        meta[name] = data

    if not exhausted and (target.get('visual_observation') or {}).get('selected_hud') == 'absent' and not target.get('name'):
        for source in reversed(sources):
            authority = source.get('previous_target_authority') or {}
            if (not source.get('previous_target_attempted') and plain_target_name(authority.get('expected_target_name')) and
                    type(authority.get('generation_after')) is int and
                    authority['generation_after'] == c.cycle.input_generation and
                    authority.get('session_epoch') == c.cycle.session_epoch):
                await motor('target_recent_corpse', {'type': 'target_previous', 'expected_target_name': authority['expected_target_name']},
                    'The selected HUD is empty and no input occurred after our bound recent fight. Reselect its previous target once using /targetlasttarget [noexists], to inspect ' + authority['expected_target_name'] + '. This selects only; completion proves no death or loot. Confirm matching selected DEAD corpse on a fresh frame before F.',
                    absent=True, select=True, previous_target_source=source['identity'])
                break

    async def select_proposals():
        points = await asyncio.to_thread(_corpse_choices, frame, from_profile(c.profile))
        for candidate in points:
            await motor(candidate.option, candidate.binding,
                        'Left-click cyan marker ' + candidate.option.replace('corpse_select_', 'C').replace('corpse_ground_', 'G') +
                        ' to SELECT the likely ' + ', '.join(names) + ' corpse near the recent fight and inspect its identity. Choose only if the marker overlaps a visibly dead body. Reject living creatures. The client may also loot; verify new visible loot separately. F requires subsequent dead-selection confirmation.',
                        point=(candidate.binding['image_x'], candidate.binding['image_y']), select=True)
        return points

    if stage == 'inspect':
        for candidate in sweep.candidates(bindings, passive_stalled=passive_stalled):
            if candidate.option == 'under_attack' and not combat_cues['supported']:
                continue
            if not sweep.verification_available and candidate.option == 'loot_uncertain':
                continue  # Before F the useful question is locating/selecting, not empty-corpse status.
            if exhausted and candidate.option == 'loot_remaining':
                continue
            if exhausted and not passive_stalled and candidate.option == 'loot_unavailable':
                observe(candidate.option, 'Eight interaction attempts and a full surrounding sweep found no supported way to recover our recent loot. End this failed recovery explicitly, even if an unlootable corpse remains visible. Record unavailable, never successfully looted.')
                continue
            if candidate.option == 'loot_search':
                await motor(candidate.option, candidate.binding, candidate.description, search=True)
            else:
                observe(candidate.option, candidate.description)
        if not exhausted:
            proposals = await select_proposals()
    else:
        if combat_cues['supported']:
            observe('under_attack', 'Fresh own-health loss, red player combat portrait, or an incoming player attack event supports ongoing combat. Resume fighting only if a hostile is actually attacking; nearby living creatures alone are insufficient.')
        if not passive_stalled:
            observe('loot_inspect', 'Inspect loot outcome on a fresh view; input, empty target and closed window never prove completed loot.')
        if sweep.data['click_verification_current']:
            for candidate in sweep.candidates(bindings, passive_stalled=passive_stalled):
                if candidate.option in {'loot_verified', 'loot_uncertain'}:
                    observe(candidate.option, candidate.description)
        selected_hud = (target.get('visual_observation') or {}).get('selected_hud')
        selected_present = selected_hud != 'absent' and bool(target.get('name') or selected_hud == 'present')
        matching_name = bool(target.get('name') and target['name'].casefold() in {name.casefold() for name in names})
        known_living = combat_cues['selected_alive'] or (target.get('visual_observation') or {}).get('life_state') == 'alive'
        confirmed_selection = selected_present and matching_name and not known_living and not target.get('self_target')
        if confirmed_selection:
            observe('loot_selected_corpse', 'The CURRENT selected portrait/bar belongs to a visibly DEAD lootable corpse from our bound recent fight, and the selection matches that corpse. Confirm only dead selected units, never living units or a ground corpse with another unit selected.', corpse=True)
        selected = sweep.data.get('selected_corpse')
        binding = bindings.get('interact_target', {})
        predicates = {'confirmed_selection': bool(confirmed_selection), 'prior_confirmation': bool(selected),
            'input_generation': bool(selected and selected['input_generation'] == c.cycle.input_generation),
            'confirmation_scope': bool(selected and selected.get('session_epoch') == c.cycle.session_epoch),
            'confirmation_identity': bool(selected and selected.get('name') and
                selected['name'].casefold() == str(target.get('name') or '').casefold()),
            'confirmation_window': bool(selected and selected.get('source') == frame.source and
                selected.get('size') == [frame.width, frame.height]),
            'confirmation_fresh': bool(selected and 0 <= time.time()-datetime.fromisoformat(selected['captured_at']).timestamp() <= 10),
            'source_exists': bool(selected and Path(selected['image_path']).is_file()),
            'not_self': not target.get('self_target'), 'no_positive_target_health': not known_living,
            'binding_verified': bool(binding.get('verified_from') and type(binding.get('keycode')) is int),
            'attempts_available': sweep.data['attempts'] < 8}
        predicates['source_immutable'] = bool(predicates['source_exists'] and
            hashlib.sha256(Path(selected['image_path']).read_bytes()).hexdigest() == selected['sha256'])
        if predicates['source_immutable']:
            predicates.update(_corpse_continuity(c, selected['image_path'], frame.image_path, target))
        available = all(predicates.values())
        c.event('grind_loot_interact_availability', {'frame_id': frame.frame_id,
            'available': available, 'predicates': predicates, 'target_name': target.get('name'),
            'selected_hud': selected_hud, 'click_verification_current': sweep.data['click_verification_current']})
        if available:
            await motor('interact_target', {'type': 'keypress', 'keycode': binding['keycode'], 'hold_seconds': .08},
                        'The SAME previously confirmed selected dead corpse from our fight remains selected and in reach. Interact once to loot with verified F. Reject if living or selection changed.', selected=True, interact=True)
        proposals = await select_proposals()
        for name in ('forward', 'turn_left', 'turn_right', 'backward'):
            binding = bindings.get(name, {})
            if binding.get('verified_from') and type(binding.get('keycode')) is int:
                seconds = min(c.config.get('move_seconds', 1), 1) if name == 'forward' else .35
                await motor(name, {'type': 'keypress', 'keycode': binding['keycode'], 'hold_seconds': seconds},
                            f'{name}: short safe movement toward or around our visibly located recent corpse. Stay in its vicinity; no new enemy pull or onward travel.', approach=True)
    if passive_stalled and 'loot_unavailable' not in meta:
        for candidate in sweep.candidates(bindings, passive_stalled=True):
            if candidate.option == 'loot_unavailable':
                observe(candidate.option, candidate.description)
            elif candidate.option == 'loot_search':
                await motor(candidate.option, candidate.binding, candidate.description, search=True)
    observe('recover_now', 'Immediate health/resource emergency requires the existing healing/recovery stage.', recover=True)
    player_alive = (combat_cues['player_health'] is not None and combat_cues['player_health'] > 0
                    and combat_cues['health_confidence'] >= .55)
    if not player_alive:
        observe('dead_or_unrecoverable', 'Our own player is explicitly dead. Stop and release inputs.', stop=True)
    identity = str(('loot', stage, tuple(sweep.data['suspected_kills']), sweep.data['attempts'], sweep.data.get('search_steps', 0)))
    if not c.select_question(identity, 'loot'):
        return CycleResult('grind_blocked', detail='Corpse question could not be retained')
    click_authority = sweep.data.get('click_loot_authority') if sweep.data['click_verification_current'] else None
    image = await c.prepare_image(frame, _image, frame,
                                  Path(frame.image_path).with_name(f'grind-view-{frame.frame_id}.png'), proposals, names,
                                  click_authority['image_path'] if click_authority else None)
    if isinstance(image, CycleResult):
        return image
    relevant = [{key: source.get(key) for key in ('frame_id', 'receipt_id', 'target', 'suspected', 'death_observed', 'credit_known')}
                for source in sources[-4:]]
    emptied = 'the current bound corpse is emptied' if budget.enabled(c) else 'all recent corpses are emptied'
    if click_authority:
        task = 'VERIFY ANY LOOT CAUSED BY THE BOUND CORPSE CLICK'
        question = ('Compare CURRENT with BEFORE BOUND CORPSE CLICK. Choose loot_verified only for NEW positive loot evidence '
                    f'from that bound corpse and evidence {emptied}. Old chat, a click, empty target or closed '
                    'window alone prove nothing. If loot remains, confirm the selected dead corpse then F; inspect if uncertain.')
    elif 'interact_target' in meta:
        task = 'INTERACT WITH THE CONFIRMED SELECTED DEAD CORPSE USING F'
        question = 'The dead selected corpse was confirmed on the previous view. Choose interact_target to press F now if it remains selected and reachable. Approach if too far; do not reselect the same corpse. Never interact with a living selected unit.'
    elif stage == 'actions' and 'loot_selected_corpse' in meta:
        task = 'CONFIRM THE SELECTED CORPSE IS DEAD'
        question = 'Is the CURRENT selected target our dead creature corpse? Choose loot_selected_corpse if visibly dead; this permits F on a fresh view. Selection alone does not prove looting. Use loot_inspect if the selected HUD is absent; never confirm a living creature.'
    elif sweep.data['attempts']:
        task = 'VERIFY LOOT AFTER F'
        question = f'Inspect the result after F. Choose loot_verified only for positive evidence that {emptied}. If loot remains choose a corpse point or loot_remaining; if uncertain search another angle. Pressing F or a closed window alone proves no loot.'
    else:
        task = 'SELECT THE LIKELY DEAD CREATURE BODY TO INSPECT IDENTITY'
        question = 'Which cyan marker lies ON the likely dead creature? Choose that left-click selection now to inspect identity. C markers are shape proposals; G markers are fixed ground points. Search only when no marker overlaps a corpse. Under_attack requires actual ongoing hostile attacks, not nearby living creatures. Use null for an unsafe or unreadable screen.'
        if 'target_recent_corpse' in meta:
            question = 'The HUD is empty and the bound recent fight had no intervening input. Choose target_recent_corpse to reselect that previous creature once and inspect its identity. This asserts no death or loot. Alternatively choose a point on its likely corpse. A fresh matching DEAD selected target must be confirmed before F.'
    if passive_stalled:
        task = 'RESOLVE STALLED LOOT RECOVERY'
        question = ('Three active minutes of recovery attempts have not resolved this loot task. Clicks, turns and '
                    'uncertain observations do not restart that interval. Choose a currently supported guarded corpse '
                    'selection, dead-corpse confirmation, interaction or active search if useful, positive loot verification only if supported, or loot_unavailable '
                    'to leave this recovery UNVERIFIED. Repeating the same passive inspection is no longer useful. '
                    'No timeout establishes loot, death, kill credit or a completed search.')
        c.event('grind_loot_passive_recovery', {'frame_id': frame.frame_id,
            'active_seconds_unresolved': h.active_seconds-clock['active_at'],
            'clock': clock, 'last_action': passive_scope['last_action'],
            'source_identities': [source['identity'] for source in sources]})
    context = (f'NEXT TASK: {task}: {", ".join(names)}. '
               f'Recent fight recovery sources: {relevant}. '
               f'Stage {stage}; interactions {sweep.data["attempts"]}; search steps {sweep.data.get("search_steps", 0)}. '
               'A suspected source is an unconfirmed postcast disappearance, not an observed death or kill credit. '
               'A corpse click may also loot: verify new positive evidence from its bound before/after views before repeating input. '
               'If loot remains, select body, confirm selected dead corpse, F, then positively verify. '
               'Nearby same-name LIVING creatures must be rejected. If no point lies on our corpse, search another angle. '
               'Death and loot observations are separate from kill credit. Other players corpses are not our obligation.')
    context += f' Current concrete combat cues: {combat_cues}. '
    if click_authority:
        context += f' Completed source-bound corpse click receipt {click_authority["receipt_id"]}; F attempts remain {sweep.data["attempts"]}. This opens verification only, not loot credit. '
    if not combat_cues['supported']:
        context += 'No fresh own-health drop, red combat portrait or incoming player attack is observed. Nearby living creatures are not active combat; combat interruption is withheld.'
    combat_log = getattr(c, 'combat_log_loot_context', '') or getattr(c, 'combat_log_context', '')
    if combat_log:
        context += ' Attributed client combat log: ' + combat_log + '. Logged death does not locate a clickable corpse or prove loot.'
    result = await c.decide(frame, image, context, question,
                            options, compact=True, travel_budget=c.travel_budget)
    chosen = getattr(result.decision, 'chosen', None)
    receipt = result.receipt or {}
    data = meta.get(chosen, {})
    if data.get('previous_target_source') and receipt.get('possible_input'):
        for source in sources:
            if source['identity'] == data['previous_target_source']:
                source['previous_target_attempted'] = True
    accepted = (result.status == 'dispatched' and result.decision and
                receipt == c.cycle.last_receipt and receipt.get('completed') and
                not receipt.get('dispatch_unknown') and not receipt.get('error'))
    if chosen == 'loot_verified' and click_authority and not _click_verification(c, sweep, frame):
        accepted = False
        sweep.data.update(confirmations=0, click_verification_current=False)
        c.event('grind_loot_verification_rejected', {'frame_id': frame.frame_id,
            'reason': 'bound_click_authority_changed_during_decision'})
    if chosen == 'loot_unavailable' and passive_stalled and not stalled_current():
        accepted = False
        c.event('grind_loot_unverified_exit_rejected', {'frame_id': frame.frame_id,
            'reason': 'passive_recovery_scope_changed_during_decision'})
    if result.no_input_abstention:
        sweep.observe('loot_uncertain', frame.frame_id)
    if receipt.get('possible_input') and not accepted:
        c.stop('partial_or_unknown_grind_input')
    elif accepted:
        if receipt.get('possible_input'):
            h.tick(time.time())
            _passive_clock(c, sweep)
            c.last_input_at = datetime.fromisoformat(receipt['occurred_at']).timestamp()
            h.last_gameplay_receipt_id = receipt['receipt_id']
            sweep.data['last_action'] = {'choice': chosen, 'frame_id': frame.frame_id, 'receipt_id': receipt['receipt_id']}
            sweep.data['selected_corpse'] = None
            sweep.data.update(click_loot_authority=None, click_verification_current=False)
            if data.get('select') and (receipt.get('selected_binding') or {}).get('type') == 'click':
                sweep.data['click_loot_authority'] = {'receipt_id': receipt['receipt_id'],
                    'frame_id': frame.frame_id, 'image_path': frame.image_path,
                    'sha256': hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
                    'completed_at': receipt['occurred_at'], 'input_generation': c.cycle.input_generation,
                    'session_epoch': c.cycle.session_epoch, 'sources': [source['identity'] for source in sources],
                    'source': frame.source, 'size': [frame.width, frame.height]}
            if data.get('interact'):
                sweep.interacted()
            if data.get('search'):
                sweep.searched()
            sweep.data['stage'] = 'actions' if data.get('select') else 'inspect'
            c.wait_until = time.time() + .15
        elif data.get('corpse'):
            sweep.data['selected_corpse'] = {'frame_id': frame.frame_id, 'image_path': frame.image_path,
                                           'captured_at': frame.captured_at,
                                           'request_id': result.decision.envelope.request_id,
                                           'input_generation': c.cycle.input_generation,
                                           'session_epoch': c.cycle.session_epoch, 'name': target.get('name'),
                                           'source': frame.source, 'size': [frame.width, frame.height],
                                           'sha256': hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()}
        elif chosen == 'loot_remaining':
            sweep.data['stage'] = 'actions'
        elif chosen == 'loot_inspect':
            sweep.data['stage'] = 'inspect'
        elif chosen == 'under_attack':
            sweep.data.update(interrupted=True, stage='inspect', selected_corpse=None)
            # The old corpse shortcut must not prevent inspecting an attacker.
            h.target_dead_observed = False
            h.encounter_ended = False
            h.compact_stage = 'inspect' if target.get('name') and not target.get('invalid_text') else 'acquire'
            h.selected_presence = None
            h.phase = 'search'
            h.compact_last_result = 'loot_combat_interrupted'
        elif data.get('recover'):
            await c.apply_choice(frame, target, measurement, None, False, result,
                                 {'recover': True, 'scoped_question': True}, chosen, c.level.last_confirmed_level)
            sweep.data.update(stage='inspect', selected_corpse=None, resume_after_recovery=True)
        elif data.get('stop'):
            c.stop('sage_observed_player_death')
        sweep.observe(chosen, frame.frame_id, passive_stalled=passive_stalled)
        if not sweep.pending:
            h.phase = 'search'
            h.compact_stage = 'acquire'
        c.event('grind_loot_assessment', {'frame_id': frame.frame_id, 'choice': chosen,
                                        'request_id': result.decision.envelope.request_id,
                                        'pending': sweep.pending, 'outcome': sweep.data.get('outcome'),
                                        'receipt_id': receipt.get('receipt_id')})
    c.finish_question(result, {'unclear': chosen == 'loot_uncertain', **data})
    budget.record(c, sweep, result, data, accepted)
    _save(c)
    return result

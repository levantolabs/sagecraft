"""Hunting hypotheses and receipt-linked observations; never movement authority."""
from __future__ import annotations

from datetime import datetime
from copy import deepcopy
from types import SimpleNamespace
import hashlib
import ast
import math
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time

from PIL import Image, ImageDraw
import yaml

from sage_wow.agent.coordinate_motion import MotionModel, completed_hold_seconds, VECTOR_ERROR
from sage_wow.agent.scene import parse_position, is_input_error
from sage_wow.agent.ui_layout import UIRegions, from_profile
from sage_wow.agent.session_notice import proposal as notice_proposal
from sage_wow.agent.ui_reset import panel_cues
from sage_wow.agent.grind_outcomes import error_proposals

PRECISION_EPSILON = 1e-9
TARGET_RETRY_SECONDS = 15.
UNKNOWN_COMPLETED_CAST = 'Prior combat effect unknown/unassessed; fresh living target assessment required'
INEFFECTIVE_COMPLETED_CAST = 'Linked ineffective cast: assess a different correction or abandon'


def normalized_zone(text):
    text=' '.join(str(text).split())
    while text and not text[0].isalnum():text=text[1:]
    while text and not text[-1].isalnum():text=text[:-1]
    return text.strip().casefold()


def zone_identity(measurement):
    return tuple(dict.fromkeys(value for text in measurement.get('zone_proposals',[])
        if (value:=normalized_zone(text))))


def target_identity(text):
    return ' '.join(re.sub(r'[^\w\s]','',str(text or '')).casefold().split())


def map_direction(dx, dy, axis_error=.1):
    horizontal = 'East' if dx > axis_error + PRECISION_EPSILON else 'West' if dx < -axis_error - PRECISION_EPSILON else ''
    vertical = 'South' if dy > axis_error + PRECISION_EPSILON else 'North' if dy < -axis_error - PRECISION_EPSILON else ''
    return '-'.join(part for part in (vertical, horizontal) if part) or 'direction unresolved at coordinate precision'


def movement_result(start, end, destination=None):
    dx, dy = end[0] - start[0], end[1] - start[1]
    result = {'dx': dx, 'dy': dy, 'displacement': math.hypot(dx, dy),
              'direction': map_direction(dx, dy), 'vector_error_bound': VECTOR_ERROR,
              'progress_error_bound': VECTOR_ERROR}
    if destination:
        before, after = math.dist(start, destination), math.dist(end, destination)
        progress = before - after
        result.update(distance_before=before, distance_after=after, progress=progress,
                      progress_status='closer' if progress > VECTOR_ERROR + PRECISION_EPSILON else
                      'farther' if progress < -VECTOR_ERROR - PRECISION_EPSILON else 'unresolved_precision')
    return result


def goal_relation(response, vector):
    """Interval bounds in displayed map axes, never a physical turn angle."""
    dx, dy = response['dx'], response['dy']
    gx, gy = vector
    dot = dx * gx + dy * gy
    cross = dx * gy - dy * gx
    dot_error = .1 * (abs(gx) + abs(gy)) + .05 * (abs(dx) + abs(dy)) + .01
    cross_error = dot_error
    relation = 'ahead' if dot > dot_error + PRECISION_EPSILON else 'behind' if dot < -dot_error - PRECISION_EPSILON else 'ahead/behind unresolved'
    side = 'right' if cross > cross_error + PRECISION_EPSILON else 'left' if cross < -cross_error - PRECISION_EPSILON else 'left/right unresolved'
    return f'{relation}; {side} in displayed map axes'


def catalog_path(profile):
    configured=profile.values.get('grind_only',{}).get('area_catalog')
    path=Path(configured) if configured else Path(__file__).resolve().parents[3]/'profiles/hunting-areas.yaml'
    return path if path.is_absolute() else profile.path.parent/path


def load_catalog(profile):
    path=catalog_path(profile)
    data=yaml.safe_load(path.read_text()) or {}
    records=data.get('areas',[])
    if not isinstance(records,list) or len(records)>32:raise ValueError('Invalid bounded hunting catalog')
    result={}
    for record in records:
        ident=record.get('id')
        if not isinstance(ident,str) or not re.fullmatch(r'[a-zA-Z0-9_\-]{1,64}',ident) or ident in result:
            raise ValueError('Hunting areas need unique stable IDs')
        source=record.get('source');coordinate=record.get('coordinate');levels=record.get('expected_level_range')
        if not isinstance(source,dict) or not source.get('kind') or not source.get('claim'):
            raise ValueError('Hunting areas require source attribution')
        if coordinate is not None and (not isinstance(coordinate,list) or len(coordinate)!=2 or
            any(type(n) not in (float,int) or not 0<=n<=100 for n in coordinate)):
            raise ValueError('Invalid displayed-zone coordinate hypothesis')
        if levels is not None and (not isinstance(levels,list) or len(levels)!=2 or not 1<=levels[0]<=levels[1]<=10):
            raise ValueError('Invalid expected level range')
        result[ident]=dict(record)
    return result


def measure(profile,frame,rows,ocr):
    regions=UIRegions(frame,(frame.width,frame.height),from_profile(profile))
    box=regions.pixels('coordinate_primary',(.83,.1,.99,.25))
    inside=lambda r,b: r.confidence>=.75 and b[0]<=r.bounds['x'] and b[1]<=r.bounds['y'] and r.bounds['x']+r.bounds['width']<=b[2] and r.bounds['y']+r.bounds['height']<=b[3]
    proposals=[parse_position(r.text) for r in rows if inside(r,box) and parse_position(r.text) is not None]
    error=None
    try:
        with TemporaryDirectory(prefix='grind-coordinates-') as directory:
            path=Path(directory)/'coordinates.png'
            with Image.open(frame.image_path) as image:
                crop=image.crop(box);crop=crop.resize((crop.width*6,crop.height*6));crop.save(path)
            for row in ocr(path):
                point=parse_position(row.text) if row.confidence>=.75 else None
                if point is not None:proposals.append(point)
    except Exception as exc:error=type(exc).__name__
    points={tuple(p) for p in proposals}
    zone_box=regions.pixels('local_caption',(.6,0,.9,.12))
    broad_box=(round(frame.width*.6),0,round(frame.width*.9),round(frame.height*.12))
    observations=[{'text':r.text,'bounds':dict(r.bounds),'extraction':'source_ocr',
        'caption_member':inside(r,zone_box)} for r in rows if inside(r,zone_box) or inside(r,broad_box)]
    zones=[r.text.strip() for r in rows if inside(r,zone_box)]
    try:
        with TemporaryDirectory(prefix='grind-zone-') as directory:
            path=Path(directory)/'zone.png'
            with Image.open(frame.image_path) as image:
                crop=image.crop(zone_box);crop.resize((crop.width*6,crop.height*6)).save(path)
            for row in ocr(path):
                if row.confidence>=.75 and row.text.strip():
                    zones.append(row.text.strip())
                    observations.append({'text':row.text,'bounds':dict(row.bounds),'extraction':'caption_resample',
                        'caption_member':True,'original_box':list(zone_box),'independent_vote':False})
    except Exception:pass
    raw=list(dict.fromkeys(zones));normalized={}
    for text in raw:
        identity=normalized_zone(text)
        if identity and identity not in normalized:
            display=' '.join(text.split())
            while display and not display[0].isalnum():display=display[1:]
            normalized[identity]=display.strip()
    zones=list(normalized.values())
    return {'position':list(next(iter(points))) if len(points)==1 else None,
        'position_status':'readable_proposal' if len(points)==1 else 'conflict' if points else 'unreadable',
        'zone_proposals':zones,'zone_raw_proposals':raw,'zone_observations':observations,
        'zone_normalized_keys':list(normalized),'zone_status':'unambiguous' if len(zones)==1 else 'conflict' if zones else 'weak',
        'coordinate_space':'client_displayed_zone_coordinates',
        'coordinate_box':list(box),'crop_scale':6,'ocr_error':error,'frame_id':frame.frame_id,
        'captured_at':frame.captured_at,'errors':[r.text for r in rows if is_input_error(r.text)],
        'error_cues':error_proposals(rows, frame)}


def grid_boxes(profile,frame):
    regions=(from_profile(profile) or {}).get('regions',{})
    excluded=[regions[k] for k in ('player_frame','target_overlay','minimap','quest_tracker') if k in regions]
    overlap=lambda a,b:a[0]<b[2] and b[0]<a[2] and a[1]<b[3] and b[1]<a[3]
    boxes={}
    for label,index in zip('ABC',range(3)):
        box=[round(frame.width*(.28+index*.16)),round(frame.height*.30),round(frame.width*(.43+index*.16)),round(frame.height*.67)]
        if not any(overlap(box,b) for b in excluded):boxes[label]=box
    return boxes


def travel_image(profile,frame,path):
    """Same-frame world labels and calibrated magnified coordinate/minimap insets."""
    regions=UIRegions(frame,(frame.width,frame.height),from_profile(profile))
    with Image.open(frame.image_path) as source:
        world=source.convert('RGB');draw=ImageDraw.Draw(world)
        for label,box in grid_boxes(profile,frame).items():draw.rectangle(box,outline='cyan',width=3);draw.text((box[0]+4,box[1]+4),f'Region {label}',fill='cyan')
        world.thumbnail((1100,800))
        coordinates=source.crop(regions.pixels('coordinate_primary',(.83,.1,.99,.25)))
        coordinates=coordinates.resize((coordinates.width*6,coordinates.height*6));coordinates.thumbnail((650,350))
        minimap=source.crop(regions.pixels('minimap',(.75,0,1,.3)));minimap.thumbnail((400,350))
        image=Image.new('RGB',(max(world.width,coordinates.width+minimap.width+16),world.height+max(coordinates.height,minimap.height)+36),'#151b23')
        image.paste(world,(0,0));image.paste(coordinates,(0,world.height+36));image.paste(minimap,(coordinates.width+16,world.height+36))
        ImageDraw.Draw(image).text((0,world.height+12),'CURRENT COORDINATES / MINIMAP (same original frame)',fill='white');image.save(path)
    return path


def ui_evidence(profile,frame,rows,measurement):
    notice=notice_proposal(frame,SimpleNamespace(session_all_observations=rows,
        position=measurement.get('position'),health=None),profile.values['character']['name'])
    cues=panel_cues(frame,rows)
    cues += [r.text.strip() for r in rows if r.confidence>=.75 and re.search(r'loading',r.text,re.I)]
    from sage_wow.agent.grind_ui import active_text_entry
    entry=active_text_entry(frame,rows)
    chat=[entry['label']] if entry else []
    acknowledgements=(notice or {}).get('acknowledge',[])
    return {'notice':notice,'positive':bool(notice or cues or chat),
        'cues':(['world refresh notice'] if notice else [])+cues+chat,
        'acknowledge':acknowledgements[0] if len(acknowledgements)==1 else None,
        'active_text_entry':entry}


class HuntState:
    def __init__(self,catalog,session_id):
        self.catalog=catalog;self.session_id=session_id;self.phase='choose_area';self.plan=None;self.suspended=False
        self.area_results={};self.pending=None;self.unassessed=[];self.unassessed_count=0
        self.failures={'target':0,'ui':0,'motion':0,'combat':0,'recovery':0,'clear':0};self.sector=0;self.tried_sectors=[]
        self.encounter=0;self.encounter_ended=True;self.last_target=None;self.cast_obligation=None
        self.motion=MotionModel();self.feedback={};self.prior_phase=None;self.last_measurement=None
        self.latest_patch=None;self.learned={};self.last_level=None;self.input_effect_unverified=False;self.unclear=0;self.planning_requested=False;self.rejected_name=None;self.encounter_area=None
        self.selection_revision=0;self.selection=None;self.rejected_signatures={};self.outcomes=[];self.outcome_events=[]
        self.target_continuity=0;self.continuity_name=None;self.continuity_level=None;self.continuity_absent=False;self.continuity_ineligible=False
        self.cast_error=None;self.correction_evidence=None;self.retry_credit=False;self.correction_rounds=0
        self.blocked=None;self.active_seconds=0.;self.active_tick=None;self.last_progress_active_at=0.
        self.recovery_started=None;self.recovery_progress_at=0.;self.no_mana=False;self.heal_unavailable=False
        self.semantic_counts={};self.exhausted_patches=0;self.no_acquisition_since=0.;self.suspended_for_heal=False
        self.last_completed_action=None;self.recent_moves=[];self.travel_failures={}
        self.destination_change_basis=None
        self.travel_approach_revision=0;self.last_gameplay_receipt_id=None
        self.travel_urgent_episode=None;self.travel_urgent_debt=None
        self.travel_policy={'side':None,'landmark':None,'loop_suspected':False,'tactic_changes':0}
        self.action_responses={};self.mapping_generation=None;self.mapping_epoch=None;self.mapping_zone=None
        self.probe_series=None;self.forward_history=None;self.failed_direction_approaches=[]
        self.detour=None
        self.established_zone=None
        self.approach=None;self.target_history={};self.target_lives={}
        self.unresolved_motion={};self.archived_motion_receipts=[];self.combat_history_key=None
        self.question_debt={};self.active_question=None
        self.pinned_boundary_question=None
        self.question_history=[];self.archived_question_count=0;self.archived_question_unclear=0
        self.reserved_question_debt={};self.active_question_reserved=False
        self.encounter_serial=0
        self.compact_stage=None;self.selected_presence=None;self.compact_last_result=None
        self.attempt_absence=False;self.attempt_changed_view=False;self.no_progress_at=0.
        self.no_effect_retries=0;self.own_damage_evidence=None;self.eligible_inspection=None;self.credited_kills=[]
        self.action_failures={};self.situations={k:0 for k in self.failures};self.progress_facts=[];self.target_dead_observed=False
        self.target_level_delta=0
        self.target_bands_by_player_level={}
        self.preferred_target_bands_by_player_level={}
        self.hunting_progression_by_player_level={}
        self.fixed_hunting_area=None
        self.progression_pending=False
        self.scouting_observations=[]
        # A declared destination or focus cycle is not changed local geometry.
        # Keep measured/unknown outcomes under their old revision when an
        # evidenced useful movement opens a new local allowance.
        self.search_revision=0;self.search_changes=[];self.strategy_required=None
        self.recovery_requested=None;self.recovery_history=[];self.progress_review_at=0.
        self.loot_request=None;self.recent_combat=None;self.cast_review=None
        self.active_threat=None;self.threat_health_observation=None;self.disengagement=None
        self.last_completed_target_active_at=None
        self.empty_search_plan_revision=None
        self.recovery_plan_revision=None
        self.hunt_arrival=None
        self.confirmed_target_absences=0

    def target_band(self,level):
        if level in self.target_bands_by_player_level:return tuple(self.target_bands_by_player_level[level])
        return max(1,level-2),level+self.target_level_delta

    def target_preference(self,level):
        from sage_wow.agent.grind_progression import context
        strategy=context(self,level)
        if self.fixed_hunting_area:
            strategy+='User instruction: stay on '+self.catalog[self.fixed_hunting_area]['label']+' and hunt the configured nearby creatures until the level goal. Use short local targeting/combat adjustments; do not tour other areas. '
        preferred=self.preferred_target_bands_by_player_level.get(level)
        if not preferred:return strategy
        return (strategy+f'Preferred creature levels: {preferred[0]} to {preferred[1]} for XP efficiency when practical. '
            'This is a preference only: other allowed levels remain valid kills. '
            'Do not reject an otherwise eligible selected creature or abandon a fight solely because it is outside the preferred range. '
            'Weigh nearby opportunities against time spent searching.')

    def scout_rejected(self,evidence,measurement,receipt,*,retain_destination=False):
        """One unsuitable creature is a scouting fact, not an area census."""
        fact={**deepcopy(evidence),'clear_receipt_id':receipt['receipt_id'],
            'position':deepcopy(measurement.get('position')),
            'zone_proposals':list(measurement.get('zone_proposals',[])),
            'plan_hypothesis_id':(self.plan or {}).get('area_id'),
            'claim':'This selected living creature was outside the requested band; other local creatures and area population remain unverified'}
        self.scouting_observations=(self.scouting_observations+[fact])[-24:]
        # Do not append to observed_levels: candidates() treats those suitable
        # encounter ranges as area evidence, which would exclude mixed patches.
        if retain_destination:return fact
        self.phase='choose_area';self.planning_requested=True
        if not self.strategy_required:
            self.strategy_required={key:fact[key] for key in ('frame_id','captured_at','session_epoch','source_hash')}
            self.strategy_required.update(reason='out_of_band_scout',search_revision=self.search_revision,
                band=list(fact['band']),observed_level=fact['level'])
        return fact

    def action_key(self,action,family):
        return f'{family}:{self.situations[family]}:{action}'

    def allowed(self,action,family):
        return self.action_failures.get(self.action_key(action,family),0)<2

    def target_selection_available(self):
        """After two misses, permit one nonoffensive selection per cooldown.

        Time creates an opportunity to inspect, never target/attack authority.
        The missed-attempt ledger and other action limits remain unchanged.
        """
        if self.input_effect_unverified:return False
        if self.pending and self.pending.get('family')=='target' and not self.known_completed_input(self.pending):return False
        if self.failures['target']<2 and self.allowed('target_enemy','target'):return True
        return (self.last_completed_target_active_at is not None
            and self.active_seconds-self.last_completed_target_active_at>=TARGET_RETRY_SECONDS)

    def intentional_clear_current(self):
        return self.target_history.get(self.combat_history_key, {}).get('intentional_clear')

    def intentional_clear_explains(self, recent):
        fact = self.intentional_clear_current() or {}
        return bool(fact.get('assessment') and fact.get('combat_receipt_id')
            == (recent or {}).get('receipt', {}).get('receipt_id')
            and fact.get('owner') == (recent or {}).get('target_history_key')
            and fact.get('encounter_id') == (recent or {}).get('encounter_id'))

    def pixel_renewal_allowed(self, target):
        """A verified selection release is not death or a fresh acquisition."""
        if self.target_dead_observed:return True
        name = target_identity(target.get('name'))
        owner = str((name,self.target_lives.get(name,0)))
        history = self.target_history.get(owner) or {}
        return not (history.get('intentional_clear', {}).get('assessment')
            or history.get('selection_loss', {}).get('assessment'))

    @staticmethod
    def positive_selection(target):
        visual = target.get('visual_observation') or {}; hud = target.get('hud') or {}
        return bool(target.get('name') or target.get('levels')
            or visual.get('selected_hud') == 'present' or visual.get('name')
            or visual.get('level') is not None
            or hud.get('target_health') is not None and hud['target_health'] > 0
                and (hud.get('target_health_confidence') or 0) >= .8)

    @classmethod
    def selection_absence_conflict(cls, target):
        return bool(cls.positive_selection(target) or target.get('self_target') or target.get('invalid_text'))

    @classmethod
    def attributed_selection_absent(cls, target, frame):
        provenance = target.get('visual_provenance') or {}
        return bool((target.get('visual_observation') or {}).get('selected_hud') == 'absent'
            and provenance.get('continuity_frame_id', provenance.get('frame_id')) == frame.frame_id
            and not cls.selection_absence_conflict(target))

    def install_intentional_clear(self, pending):
        """Attach only the current completed clear to its actual combat owner."""
        owner = self.combat_history_key; history = self.target_history.get(owner) or {}
        recent = self.recent_combat or {}; receipt = pending.get('receipt') or {}
        if (pending.get('family') != 'clear' or not receipt.get('possible_input')
            or not self.known_completed_input(pending) or not history.get('cast_attempted')
            or history.get('completed') or (self.approach or {}).get('history_key') != owner
            or history.get('encounter_id') != self.encounter or recent.get('encounter_id') != self.encounter
            or recent.get('target_history_key') != owner or not self.known_completed_input(recent)
            or type(pending.get('target_continuity')) is not int
            or pending['target_continuity'] != recent.get('target_continuity')
            or target_identity((pending.get('target') or {}).get('name')) != self.last_target
            or target_identity(history.get('name')) != self.last_target
            or target_identity((recent.get('target') or {}).get('name')) != self.last_target
            or receipt.get('session_epoch') != pending.get('source_scope', {}).get('session_epoch')):
            return
        fact = {'owner': owner, 'encounter_id': self.encounter, 'disposition': 'submitted',
            'combat_receipt_id': recent['receipt']['receipt_id'],
            'clear': deepcopy({k: pending.get(k) for k in ('receipt', 'source_frame_id', 'source_image',
                'source_hash', 'source_scope', 'selection_revision', 'target_continuity', 'target')}),
            'source_selection': deepcopy(self.selection), 'action_authority': False}
        history['intentional_clear'] = fact
        pending['intentional_clear_owner'] = owner

    def verify_intentional_clear(self, c, frame, target, pending, result):
        """Record verified release before resolve consumes the completed clear."""
        fact = self.intentional_clear_current() or {}; clear = fact.get('clear') or {}
        receipt = (pending or {}).get('receipt') or {}; assessment = result.receipt or {}
        scope = {'session_epoch': c.cycle.session_epoch, 'source': frame.source,
            'width': frame.width, 'height': frame.height}
        source_continuity = clear.get('target_continuity')
        same_episode = type(source_continuity) is int and (self.target_continuity == source_continuity
            or self.target_continuity == source_continuity + 1 and self.attributed_selection_absent(target, frame))
        if (fact.get('disposition') != 'submitted' or self.pending is not pending
            or pending is None or pending.get('intentional_clear_owner') != fact.get('owner')
            or pending.get('outcome_consumed') or pending.get('family') != 'clear'
            or self.encounter != fact.get('encounter_id')
            or (self.approach or {}).get('history_key') != fact.get('owner')
            or not self.known_completed_input(pending) or not receipt.get('possible_input')
            or receipt != clear.get('receipt') or clear.get('source_scope') != scope
            or receipt.get('session_epoch') != c.cycle.session_epoch
            or receipt.get('generation_after') != c.cycle.input_generation
            or not self.linked(frame, c.cycle.input_generation)
            or pending.get('selection_revision') != clear.get('selection_revision')
            or pending.get('target_continuity') != clear.get('target_continuity')
            or not same_episode
            or self.selection_absence_conflict(target) or not c.current() or not c.fresh(frame)
            or c.cycle._scope_error(frame) or result.status != 'dispatched'
            or getattr(result.decision, 'chosen', None) != 'target_cleared'
            or assessment != c.cycle.last_receipt or assessment.get('possible_input')
            or not self.known_completed_input({'receipt': assessment})
            or assessment.get('generation_before') != receipt.get('generation_after')
            or assessment.get('source_frame_id') != frame.frame_id):
            return False
        try:
            if hashlib.sha256(Path(clear['source_image']).read_bytes()).hexdigest() != clear['source_hash']:
                return False
        except (OSError, KeyError):return False
        fact.update(disposition='verified', assessment={'frame_id': frame.frame_id,
            'captured_at': frame.captured_at, 'source': frame.source, 'width': frame.width, 'height': frame.height,
            'image_path': frame.image_path, 'sha256': hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
            'session_epoch': c.cycle.session_epoch, 'input_generation': c.cycle.input_generation,
            'request_id': assessment['request_id'], 'receipt_id': assessment['receipt_id']},
            claim='Intentional selection release; creature life, damage and escape remain unknown')
        return True

    def reconcile_intentional_clear(self, c, frame, target, *, positive_ui=False):
        fact = self.intentional_clear_current() or {}
        if fact.get('disposition') != 'verified':return False
        if not c.current() or not c.fresh(frame) or c.cycle._scope_error(frame):return False
        from sage_wow.agent.grind_resources import hud_resources
        hud = target.get('hud') or {}
        if hud.get('frame_id') != frame.frame_id:hud = hud_resources(c, frame)
        target = {**target, 'hud': hud}
        if self.positive_selection(target):
            fact['disposition'] = 'superseded';return False
        if (fact.get('encounter_id') != self.encounter
            or (self.approach or {}).get('history_key') != fact.get('owner')):
            fact['disposition'] = 'superseded';return False
        assessment = fact['assessment']
        immediate = assessment['frame_id'] == frame.frame_id and assessment['session_epoch'] == c.cycle.session_epoch
        if (self.pending or self.active_threat or self.disengagement or self.blocked or self.input_effect_unverified
            or self.loot_request or c.heal_pending or self.no_mana or positive_ui or c.require_world
            or self.phase in {'recover','ui_recover','input_effect_unverified'}
            or not c.current() or not c.fresh(frame) or c.cycle._scope_error(frame)
            or self.selection_absence_conflict(target)
            or not (immediate or self.attributed_selection_absent(target, frame))):return False
        if (hud.get('player_health') is None or (hud.get('health_confidence') or 0) < .8
            or hud['player_health'] <= c.config['critical_health_threshold'] or hud.get('player_mana') == 0):return False
        self.encounter_ended = True;self.terminal_reason = 'selection_released_unknown'
        self.target_history[fact['owner']]['encounter_ended'] = True
        fact.update(disposition='closed', closure_frame_id=frame.frame_id,
            closure_session_epoch=c.cycle.session_epoch)
        if (self.phase in {'fight','approach'} and not self.planning_requested
            and (self.plan or {}).get('phase') != 'travel' and not (self.strategy_required or {}).get('selected_exit')):
            self.phase='search'
        if self.phase=='search':self.compact_stage='acquire';self.selected_presence=False
        c.event('grind_intentional_clear_handoff', deepcopy(fact))
        return True

    def cast_blocks_acquisition(self):
        """Historical effect uncertainty restricts attacks, not idle scouting.

        Reuse retained, owner-bound completed-input evidence. This does not
        resolve damage, clear debt, renew methods or depend on rolling history.
        Every actual attack still assesses the freshly selected living unit.
        """
        from sage_wow.agent.grind_cast_feedback import unresolved_nonlocal_cues
        if unresolved_nonlocal_cues(self):return True
        if not self.cast_obligation:
            return False
        recent = self.recent_combat or {}
        attributed_ended=(self.encounter_ended and self.encounter > 0
            and not self.input_effect_unverified
            and recent.get('encounter_id') == self.encounter
            and self.combat_history_key in self.target_history
            and recent.get('target_history_key') == self.combat_history_key
            and bool(self.last_target)
            and target_identity((recent.get('target') or {}).get('name')) == self.last_target
            and self.known_completed_input(recent))
        if not attributed_ended:return True
        if (self.cast_error or {}).get('status')=='active':
            from sage_wow.agent.grind_cast_feedback import ended_target_error_allows_scouting
            return not ended_target_error_allows_scouting(self)
        return self.cast_obligation not in {UNKNOWN_COMPLETED_CAST, INEFFECTIVE_COMPLETED_CAST}

    def plan_empty_search(self, frame, target):
        """Hand a repeatedly empty local search to Sage's destination choice.

        Two misses plus one cooldown retry are enough evidence to change the
        question. This chooses no destination, sends no input and resets no
        failed-method debt. One handoff per observed sector leaves physical
        recovery available if Sage cannot choose a destination.
        """
        if (self.phase not in {'choose_area','search'} or self.pending is not None
            or self.input_effect_unverified or self.active_threat or self.cast_blocks_acquisition()
            or self.selected_presence is not False or target.get('name')
            or (target.get('visual_observation') or {}).get('selected_hud')=='present'
            or self.confirmed_target_absences<3 or self.strategy_required
            or self.empty_search_plan_revision==self.search_revision):
            return None
        self.empty_search_plan_revision=self.search_revision
        self.strategy_required={'reason':'empty_local_search','frame_id':frame.frame_id,
            'captured_at':frame.captured_at,'search_revision':self.search_revision,
            'target_absences':self.confirmed_target_absences}
        self.phase='choose_area';self.planning_requested=True
        return dict(self.strategy_required)

    def failed_action(self,pending):
        key=pending.get('debt_key') or (self.action_key(pending['action'],pending['family']) if pending['family'] in self.situations else None)
        if key:self.action_failures[key]=self.action_failures.get(key,0)+1

    def plan_unresolved_recovery(self, frame, target):
        """Change an exhausted empty-world question, without choosing an action.

        A recovery menu can itself fail. Three unresolved answers permit one
        fresh destination review per observed sector, including during travel.
        Planning is not progress and cannot renew physical-method allowances.
        """
        visual=target.get('visual_observation') or {}
        # Repeated model abstention is not evidence that an arrived patch is
        # empty. Existing failed-selection/search-method handling owns that.
        if self.phase=='search' and self.hunt_arrival:return None
        absent=(visual.get('selected_hud')=='absent' or
            self.selected_presence is False and visual.get('selected_hud')!='present')
        if (self.compact_stage!='recovery' or self.unclear<3
            or self.phase not in {'choose_area','search','travel'}
            or self.planning_requested or self.pending is not None or self.blocked
            or self.input_effect_unverified or self.active_threat or self.cast_blocks_acquisition()
            or self.loot_request or not self.encounter_ended
            or target.get('name') or not absent
            or self.recovery_plan_revision==self.search_revision):
            return None
        self.recovery_plan_revision=self.search_revision
        fact={'reason':'unresolved_recovery_question','frame_id':frame.frame_id,
            'captured_at':frame.captured_at,'search_revision':self.search_revision,
            'question_id':self.active_question,'unclear_answers':self.unclear,
            'prior_destination':(self.plan or {}).get('area_id'),
            'action_authority':False,'progress_credit':False}
        # Keep a level/target-specific relocation constraint if one exists.
        if not self.strategy_required:self.strategy_required=dict(fact)
        self.phase='choose_area';self.planning_requested=True
        return fact

    def motion_key(self,action,purpose,target_key=None):
        selected=purpose in {'approach','cast_correction','standing'}
        owner=target_key if selected else 'local_search'
        method=self.target_history.get(target_key,{}).get('method_revision',0) if selected else self.search_revision
        return str(('motion',purpose,owner,action,method))

    def request_recovery(self,reason,signature=None):
        """Change the next question, retaining unknown effects and local debt."""
        if self.recovery_requested is None:
            self.recovery_requested={'reason':reason,'signature':signature,
                'active_at':self.active_seconds,'search_revision':self.search_revision,
                'last_action':deepcopy(self.last_completed_action)}
            self.recovery_history=(self.recovery_history+[dict(self.recovery_requested)])[-24:]
        self.recovery_requested['latest_reason']=reason
        self.archive_pending('recovery_requires_fresh_assessment:'+reason)
        self.compact_stage='recovery'
        self.planning_requested=False  # An unresolved plan must not shadow the recovery menu.
        # Only ordinary semantic/progress blocks are recoverable by this path.
        if self.blocked and self.blocked['reason'] in {'blocked_semantic','blocked_no_combat_progress'}:
            self.blocked=None
        return self.recovery_requested

    def motion_count(self,action,purpose,target_key=None):
        debt=self.motion_key(action,purpose,target_key)
        return self.action_failures.get(debt,0)+self.unresolved_motion.get(debt,0)

    def archive_motion(self,pending):
        if pending['family']=='motion' and pending.get('purpose')!='travel':
            receipt=pending['receipt']['receipt_id']
            if not pending.get('unresolved_debt_charged'):
                pending['unresolved_debt_charged']=True
                self.archived_motion_receipts=(self.archived_motion_receipts+[receipt])[-24:]
                debt=pending.get('debt_key') or self.motion_key(pending['action'],pending.get('purpose','motion'),pending.get('target_history_key'))
                self.unresolved_motion[debt]=self.unresolved_motion.get(debt,0)+1
            return True
        return False

    def approach_for(self,target,*,renew_completed=True):
        name=target_identity(target.get('name'))
        revision=self.target_lives.get(name,0)
        prior=self.target_history.get(str((name,revision)),{})
        if renew_completed and (prior.get('completed') or self.target_dead_observed and name==self.last_target):
            revision+=1;self.target_lives[name]=revision;self.target_dead_observed=False
        identity=str((name,revision))
        if identity not in self.target_history:
            # Saturation preserves retained debt rather than erasing an old
            # same-name task to make room for more input allowances.
            if len(self.target_history)>=24:
                retired=next((ident for ident,record in self.target_history.items()
                    if record.get('completed') or record.get('retired_by_acquisition')),None)
                if retired is None:return None
                retired_record=self.target_history.pop(retired)
                self.retire_motion_debt(retired)
                retired_name=target_identity(retired_record['name'])
                if retired_name!=name and not any(target_identity(record['name'])==retired_name for record in self.target_history.values()):
                    self.target_lives.pop(retired_name,None)
            self.target_history[identity]={'key':identity,'name':target['name'],'cast_attempted':False,
                'cast_obligation':None,'combat_failures':0,'correction_rounds':0,'outcomes':[],
                'new_life':revision>0,'rejections':0,'method_revision':0}
        history=self.target_history[identity]
        if not self.approach or self.approach['history_key']!=identity:
            self.remember_approach()
            self.approach={'history_key':identity,'selected':{'name':target['name'],'levels':target['levels']},
                'prior_phase':self.phase,'destination':self.plan,'positioning':False}
            if history['cast_attempted']:
                self.combat_history_key=identity;self.last_target=name
                self.cast_obligation=history['cast_obligation'];self.failures['combat']=history['combat_failures']
                self.correction_rounds=history['correction_rounds'];self.encounter=history['encounter_id']
                self.encounter_ended=history['encounter_ended'];self.cast_error=deepcopy(history.get('cast_error'))
                self.last_progress_active_at=history['last_progress_active_at']
                for field in ('progress_before','death_frame','xp_observed','encounter_area','no_effect_retries','own_damage_evidence','eligible_inspection'):
                    setattr(self,field,deepcopy(history.get(field,0 if field=='no_effect_retries' else None)))
                self.retry_credit=False;self.correction_evidence=None
        return history

    def renew_attempt(self,target):
        """Local allowance episode; not a creature GUID or species identity claim."""
        self.remember_approach();self.archive_pending('evidenced_new_local_attempt')
        self.target_continuity+=1
        name=target_identity(target.get('name'))
        self.target_lives[name]=self.target_lives.get(name,0)+1
        self.approach=None;self.combat_history_key=None;self.encounter_ended=True
        self.cast_obligation=None;self.cast_error=None;self.failures['combat']=0
        self.retry_credit=False;self.correction_evidence=None;self.correction_rounds=0
        self.no_effect_retries=0;self.own_damage_evidence=None;self.target_dead_observed=False
        self.attempt_absence=False;self.attempt_changed_view=False
        return self.approach_for(target,renew_completed=False)

    def retire_motion_debt(self,target_key,*,before_revision=None):
        """Compact completed lifetimes or superseded, evidenced method records."""
        history=self.target_history.get(target_key)
        for debt_map in (self.action_failures,self.unresolved_motion):
            for debt,count in list(debt_map.items()):
                if not debt.startswith("('motion',"):continue
                parts=ast.literal_eval(debt)
                if parts[2]!=target_key or before_revision is not None and parts[4]>=before_revision:continue
                if history is not None:
                    summary=history.setdefault('prior_method_debt',{})
                    method=str((parts[1],parts[3],'unresolved' if debt_map is self.unresolved_motion else 'measured'))
                    summary[method]=summary.get(method,0)+count
                del debt_map[debt]

    def remember_approach(self):
        if not self.approach or self.approach['history_key']!=self.combat_history_key or self.combat_history_key not in self.target_history:return
        history=self.target_history[self.approach['history_key']]
        if history['cast_attempted']:
            history.update(cast_obligation=self.cast_obligation,combat_failures=self.failures['combat'],
                correction_rounds=self.correction_rounds,encounter_id=self.encounter,encounter_ended=self.encounter_ended,
                cast_error=deepcopy(self.cast_error),last_progress_active_at=self.last_progress_active_at)
            history.update({field:getattr(self,field,None) for field in ('progress_before','death_frame','xp_observed','encounter_area','no_effect_retries','own_damage_evidence','eligible_inspection')})

    def question(self,identity,*,reserved=False):
        if reserved:
            if identity not in self.reserved_question_debt:self.reserved_question_debt={identity:0}
            self.active_question=identity;self.active_question_reserved=True
            self.unclear=self.reserved_question_debt[identity];return True
        if identity not in self.question_debt and len(self.question_debt)>=64:
            retired=next(key for key in self.question_debt if key!=self.pinned_boundary_question)
            count=self.question_debt.pop(retired)
            record={'question_id':retired,'unclear_answers':count,
                'active_at':self.active_seconds,'action_authority':False}
            self.question_history=(self.question_history+[record])[-64:]
            self.archived_question_count+=1;self.archived_question_unclear+=count
            self.outcome_events.append({'purpose':'semantic_question','outcome':'archived_unresolved',**record})
        self.active_question_reserved=False
        self.active_question=identity
        self.unclear=0 if identity==self.pinned_boundary_question else self.question_debt.get(identity,0)
        return True

    def question_answer(self,*,unclear=False,route_only=False):
        if self.active_question is None:return
        if self.active_question==self.pinned_boundary_question:return
        debt=self.reserved_question_debt if self.active_question_reserved else self.question_debt
        if unclear:debt[self.active_question]=debt.get(self.active_question,0)+1
        elif not route_only:debt.pop(self.active_question,None)
        self.unclear=debt.get(self.active_question,0)

    def changed_situation(self,family):
        self.situations[family]+=1
        if len(self.action_failures)>64:
            for key in list(self.action_failures):
                if any(key.startswith(f'{f}:') for f in self.situations) and not any(key.startswith(f'{f}:{n}:') for f,n in self.situations.items()):
                    del self.action_failures[key]
                    if len(self.action_failures)<=64:break

    def prompt_context(self):
        pending=self.pending
        return {'phase':self.phase,'destination':({'id':self.plan['area_id'],
            'coordinate':self.plan.get('area',{}).get('coordinate'),
            'status':self.plan.get('area',{}).get('status','visible hypothesis')} if self.plan else None),
            'suspended':self.suspended,'last_action':({'action':pending['action'],'family':pending['family'],
                'source_frame_id':pending['source_frame_id'],'receipt_id':pending['receipt']['receipt_id']} if pending else None),
            'unresolved_attempt_counts':{key:count for key,count in self.action_failures.items() if any(key.startswith(f'{f}:{n}:') for f,n in self.situations.items())},
            'blocked_actions':[key.rsplit(':',1)[-1] for key,count in self.action_failures.items()
                if count>=2 and any(key.startswith(f'{f}:{n}:') for f,n in self.situations.items())],
            'failure_counts':self.failures,'correction':self.cast_obligation,
            'approach':self.approach,'target_history':self.target_history,'unresolved_motion':self.unresolved_motion,
            'active_question':self.active_question,'question_debt':self.question_debt,
            'interrupted_boundary_question':({'question_key':self.pinned_boundary_question,
                'accounting_kind':'admitted_boundary_requests',
                'admitted_count':self.question_debt.get(self.pinned_boundary_question,0)} if self.pinned_boundary_question else None),
            'question_history':self.question_history,'archived_question_count':self.archived_question_count,
            'archived_question_unclear':self.archived_question_unclear,
            'travel':dict(self.feedback),'last_completed_action':self.last_completed_action,
            'recent_moves':self.recent_moves,'active_detour':self.detour,'probe_series':self.probe_series,'forward_history':self.forward_history,'failed_direction_approaches':self.failed_direction_approaches,'travel_policy':self.travel_policy,'travel_urgent_debt':self.travel_urgent_debt,'travel_failed_approaches':self.travel_failures,'established_zone':self.established_zone,
            'unassessed_count':self.unassessed_count,'selection_revision':self.selection_revision,
            'search_revision':self.search_revision,'strategy_required':self.strategy_required,'recovery_requested':self.recovery_requested,
            'cast_error':self.cast_error,'correction_evidence':self.correction_evidence,'retry_credit':self.retry_credit,
            'retry_credit_source':self.retry_credit_source,
            'blocked':self.blocked,'rejected_local':list(self.rejected_signatures),'no_mana':self.no_mana}

    @property
    def pending_action(self):
        return self.pending

    def invalidate_travel_mapping(self, reason):
        self.action_responses.clear()
        self.mapping_generation=None
        self.mapping_epoch=None
        self.travel_policy['mapping_unavailable_reason']=reason
        self.motion.reset_heading()
        self.probe_series=None

    def current_geometry(self, measurement):
        area=(self.plan or {}).get('area',{})
        destination=area.get('coordinate');position=measurement.get('position')
        usable=bool(destination and position and measurement.get('position_status')=='readable_proposal'
                    and zone_identity(measurement)==(normalized_zone(area.get('zone_reference','')),))
        geometry={'position':position,'destination':destination,'available':usable,
                  'position_status':measurement.get('position_status')}
        if usable:
            vector=[destination[i]-position[i] for i in range(2)]
            distance=math.hypot(*vector)
            geometry.update(vector=vector,distance=distance,direction=map_direction(*vector,axis_error=.05),
                            inside_radius=distance<=area.get('arrival_radius',.6))
        return geometry

    def observe_zone(self,measurement,epoch,generation):
        keys=zone_identity(measurement);prior=self.established_zone
        measurement['established_zone_history']=dict(prior) if prior else None
        if len(keys)==1:
            transition=bool(prior and prior['key']!=keys[0])
            measurement['zone_resolution']='transition' if transition else 'observed_continuity' if prior else 'established_observation'
            self.established_zone={'key':keys[0],'frame_id':measurement['frame_id'],
                'captured_at':measurement['captured_at'],'session_epoch':epoch,'input_generation':generation}
            if transition:self.invalidate_travel_mapping('observed zone transition')
        else:
            measurement['zone_resolution']='conflict' if keys else 'weak_historical_only'
            self.invalidate_travel_mapping('ambiguous or weak actual zone observation')

    def inconclusive_probes(self, measurement, seconds):
        return self.travel_failures.get(self.travel_key('forward',measurement,seconds),0)

    def returned_failed_direction(self, measurement, response):
        if not response:return False
        for failed in self.failed_direction_approaches:
            if (tuple(failed['zone'])==zone_identity(measurement) and measurement.get('position')
                and math.dist(failed['position'],measurement['position'])<=VECTOR_ERROR+PRECISION_EPSILON):
                a,b=failed['response'],response
                cross=a['dx']*b['dy']-a['dy']*b['dx']
                error=.1*(abs(a['dx'])+abs(a['dy'])+abs(b['dx'])+abs(b['dy']))+.02
                dot=a['dx']*b['dx']+a['dy']*b['dy']
                # Compatible cones support the same approach; opposed vectors do not.
                if dot>error+PRECISION_EPSILON and abs(cross)<=error+PRECISION_EPSILON:return True
        return False

    def destination_choice_basis(self, measurement):
        """New frame IDs and visual-plan labels alone are not a new approach."""
        area=(self.plan or {}).get('area') or {}
        return {'destination':deepcopy(area.get('coordinate')),
            'destination_zone':area.get('zone_reference'),
            'position':deepcopy(measurement.get('position')),
            'zone':list(zone_identity(measurement)),
            'travel_receipt_id':(self.last_completed_action or {}).get('receipt_id')}

    def forward_decision(self, measurement, generation, epoch, seconds, *, continuous=False):
        geometry=self.current_geometry(measurement)
        current=self.action_direction('forward',measurement,generation,epoch,continuous=continuous)
        historical=self.forward_history
        same_approach=bool(historical and historical['revision']==self.travel_approach_revision
                           and tuple(historical['zone'])==zone_identity(measurement))
        response=current['response'] if current else historical['response'] if same_approach else None
        debt=self.inconclusive_probes(measurement,seconds)
        if not geometry['available']:return 'visual',None,current
        if not self.travel_allowed('forward',measurement,seconds) or self.returned_failed_direction(measurement,response):
            return 'repeated',None,current
        if current:
            # Re-evaluate the measured response against today's goal and position.
            position=geometry['position'];end=[position[0]+response['dx'],position[1]+response['dy']]
            progress=movement_result(position,end,geometry['destination'])['progress_status']
            # Individually resolved steps can each have an inconclusive scalar
            # distance change. Use linked endpoints to expose accumulated drift,
            # without summing rounded deltas or extrapolating a longer motor.
            window=response.get('progress_window',[])
            if progress=='unresolved_precision' and len(window)>1:
                progress=movement_result(window[0]['position_before'],position,geometry['destination'])['progress_status']
            if progress=='closer':return 'progress','advance_forward',current
            if self.detour and self.detour.get('continuation_credit'):
                return 'detour','detour_forward',current
            if progress=='farther':return 'away',None,current
        elif same_approach:
            position=geometry['position'];end=[position[0]+response['dx'],position[1]+response['dy']]
            if movement_result(position,end,geometry['destination'])['progress_status']=='farther':
                if self.detour and self.detour.get('continuation_credit'):return 'detour','detour_forward',current
                return 'away',None,current
        if self.detour and self.detour.get('continuation_credit'):return 'detour','detour_forward',current
        if debt>=2:return 'repeated',None,current
        if current:return 'progress_unclear','probe_forward',current
        if self.travel_policy.get('mapping_unavailable_reason','').startswith('turn'):
            return 'after_turn','probe_forward',current
        return ('inconclusive' if self.probe_series or debt else 'initial'),'probe_forward',current

    def select_detour(self, option, reason, side, frame, measurement):
        if self.detour:
            self.detour.update(option=option,side=side,continuation_credit=False,last_result=None)
            return
        self.detour={'option':option,'purpose':'detour','reason':reason,
                     'start_position':measurement.get('position'),'side':side,
                     'start_frame_id':frame.frame_id,'reassess_after':'this single movement',
                     'continuation_credit':False,'last_result':None}

    def travel_key(self, action, measurement, seconds):
        position=measurement.get('position')
        point=tuple(round(value,1) for value in position) if position else None
        zone=zone_identity(measurement)
        if len(zone)!=1 and self.established_zone:zone=(self.established_zone['key'],)
        for prior_key in self.travel_failures:
            prior_action,prior_point,prior_zone,prior_seconds,revision=ast.literal_eval(prior_key)
            if (prior_action==action and prior_zone==zone and revision==self.travel_approach_revision
                    and (point is None or prior_point is None or math.dist(prior_point,point)<=VECTOR_ERROR)):
                return prior_key
        return str((action,point,zone,round(seconds,2),self.travel_approach_revision))

    def travel_allowed(self, action, measurement, seconds):
        return self.travel_failures.get(self.travel_key(action,measurement,seconds),0)<2

    def resolve_travel(self, frame, generation, measurement, session_epoch):
        """Resolve travel facts before observation inputs advance generation.

        This never directly grants search-sector progress or clears debt.
        Unknown coordinates remain unknown; a completed turn has no translation
        objective. The last five records are history, not dispatch authority.
        """
        pending=self.pending
        if not pending or pending.get('purpose')!='travel' or pending.get('outcome_consumed'):
            return None
        receipt=pending['receipt']
        before=pending.get('measurement',{})
        action=pending['action']
        turn=action.startswith('turn_')
        binding=receipt.get('selected_binding',{})
        requested=binding.get('hold_seconds',pending.get('requested_duration'))
        steps=receipt.get('input_steps',[])
        downs=[step['monotonic_at'] for step in steps if step.get('kind')=='key_down' and step.get('status')=='completed' and step.get('monotonic_at') is not None]
        ups=[step['monotonic_at'] for step in steps if step.get('kind')=='key_up' and step.get('status')=='completed' and step.get('monotonic_at') is not None]
        actual=max(ups)-min(downs) if downs and ups else None
        pair=(self.linked(frame,generation) and receipt.get('completed') and not receipt.get('dispatch_unknown')
            and not receipt.get('error') and receipt.get('session_epoch',session_epoch)==session_epoch)
        zone=zone_identity(before)
        valid=bool(pair and before.get('position') and measurement.get('position')
                   and len(zone)==1 and zone==zone_identity(measurement)
                   and before.get('position_status')=='readable_proposal'
                   and measurement.get('position_status')=='readable_proposal'
                   and math.dist(before['position'],measurement['position'])<=2)
        purpose=pending.get('travel_purpose','turn' if turn else 'destination progress')
        try:after_hash=hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()
        except (OSError,TypeError):after_hash=None
        record={'action':action,'purpose':purpose,'receipt_id':receipt['receipt_id'],'input_completed':bool(pair),
                'previous_gameplay_receipt_id':pending.get('previous_gameplay_receipt_id'),
                'search_revision':self.search_revision,'attributable_pair':valid,
                'source_image':pending.get('source_image'),'source_hash':pending.get('source_hash'),
                'after_image':frame.image_path,'after_hash':after_hash,
                'frame_source':frame.source,'frame_size':[frame.width,frame.height],
                'source_frame_id':pending['source_frame_id'],'after_frame_id':frame.frame_id,
                'source_captured_at':before.get('captured_at'),'after_captured_at':frame.captured_at,
                'generation_before':receipt.get('generation_before'),'generation_after':receipt.get('generation_after'),
                'session_epoch':session_epoch,'requested_duration':requested,'actual_duration':actual,
                'position_before':before.get('position'),'position_after':measurement.get('position'),
                'before_quality':before.get('position_status'),'after_quality':measurement.get('position_status'),
                'zone_before':zone,'zone_after':measurement.get('zone_proposals'),
                'coordinate_resolution':.1,'delta_axis_uncertainty':.1,
                'distance_uncertainty_bound':VECTOR_ERROR,'units':'displayed map-coordinate points',
                'axis_orientation':'x increases east; y increases south in displayed map axes; not physical bearings',
                'status':'turn_completed' if turn and pair else 'unknown_visual'}
        destination=(self.plan or {}).get('area',{}).get('coordinate')
        same_destination_zone=bool(destination and normalized_zone((self.plan or {}).get('area',{}).get('zone_reference','')) in zone)
        if valid and not turn:
            start=before['position'];end=measurement['position']
            dx,dy=end[0]-start[0],end[1]-start[1]
            displacement=math.hypot(dx,dy)
            record.update(movement_result(start,end,destination if same_destination_zone else None),
                          status='observed_displacement' if displacement>VECTOR_ERROR+PRECISION_EPSILON else 'unresolved_precision')
            response_start=start;response_seconds=actual
            previous_response=self.action_responses.get(action)
            carries_forward=bool(action=='forward' and previous_response
                and previous_response.get('facing_revision')==self.travel_approach_revision
                and previous_response.get('continuity_receipt_id')==pending.get('previous_gameplay_receipt_id')
                and previous_response.get('session_epoch')==session_epoch
                and previous_response.get('latest_endpoint') is not None
                and math.dist(previous_response['latest_endpoint'],start)<=.01
                and tuple(previous_response['zone'])==zone)
            progress_step={'position_before':list(start),'position_after':list(end),
                'receipt_id':receipt['receipt_id'],'source_frame_id':pending['source_frame_id'],
                'after_frame_id':frame.frame_id}
            progress_window=([*previous_response.get('progress_window',[]),progress_step][-5:]
                if carries_forward else [progress_step])
            if action=='forward':
                series=self.probe_series
                compatible=bool(series and series['revision']==self.travel_approach_revision
                    and series['epoch']==session_epoch and tuple(series['zone'])==zone
                    and series['last_receipt_id']==pending.get('previous_gameplay_receipt_id')
                    and math.dist(series['last_position'],start)<=.01 and series['count']==1)
                if compatible:
                    response_start=series['start_position'];response_seconds=(series['seconds'] or 0)+(actual or 0)
                    if not carries_forward:progress_window=[*series.get('progress_window',[]),progress_step][-5:]
                self.probe_series={'start_position':response_start,'last_position':end,
                    'count':2 if compatible else 1,'revision':self.travel_approach_revision,'epoch':session_epoch,
                    'zone':zone,'seconds':response_seconds,'last_receipt_id':receipt['receipt_id'],
                    'progress_window':progress_window}
                record['probe_measurement']=movement_result(response_start,end,destination if same_destination_zone else None)
                record['probe_count']=self.probe_series['count']
            else:self.probe_series=None
            response_dx,response_dy=end[0]-response_start[0],end[1]-response_start[1]
            response_displacement=math.hypot(response_dx,response_dy)
            resolved=response_displacement>VECTOR_ERROR+PRECISION_EPSILON and map_direction(response_dx,response_dy)!='direction unresolved at coordinate precision'
            if same_destination_zone:
                vector=[destination[i]-start[i] for i in range(2)]
                before_distance=math.hypot(*vector);after_distance=math.dist(end,destination)
                lateral=(vector[0]*dy-vector[1]*dx)/before_distance if before_distance else None
                progress=before_distance-after_distance
                record.update(destination_minus_current=[destination[i]-end[i] for i in range(2)],
                    distance_before=before_distance,distance_after=after_distance,
                    distance_change=after_distance-before_distance,progress=progress,lateral_displacement=lateral,
                    lateral_orientation='positive is right of before-to-destination line in x-east/y-south displayed axes',
                    progress_status='closer' if progress>VECTOR_ERROR+PRECISION_EPSILON else 'farther' if progress < -VECTOR_ERROR-PRECISION_EPSILON else 'unresolved_precision')
            approach=self.travel_key(action,before,requested or 0)
            if (not resolved if purpose=='probe' else displacement<=VECTOR_ERROR+PRECISION_EPSILON):
                self.travel_failures[approach]=self.travel_failures.get(approach,0)+1
                record['suspected_collision_or_input_ineffectiveness']=self.travel_failures[approach]>=2
                historical=self.forward_history if action=='forward' else None
                if record['suspected_collision_or_input_ineffectiveness'] and historical and historical['revision']==self.travel_approach_revision:
                    anchor={'position':start,'zone':zone,'response':dict(historical['response'])}
                    if anchor not in self.failed_direction_approaches:self.failed_direction_approaches.append(anchor)
            # Numeric movement, including a farther detour, is only factual.
            # It never replenishes an already-failed local approach.
            points=[move.get('position_before') for move in self.recent_moves[-4:] if move.get('position_before')]
            revisits=sum(math.dist(point,end)<=VECTOR_ERROR for point in points)
            loop=displacement>VECTOR_ERROR and revisits>=2
            if loop:
                self.travel_policy['loop_suspected']=True
                self.travel_failures[approach]=max(2,self.travel_failures.get(approach,0))
            record['revisitation_cycle_suspected']=loop
            if resolved and response_seconds and zone:
                self.probe_series=None
                self.action_responses[action]={'dx':response_dx,'dy':response_dy,'actual_duration':response_seconds,
                    'frame_id':frame.frame_id,'receipt_id':receipt['receipt_id'],'captured_at':frame.captured_at,
                    'session_epoch':session_epoch,'zone':zone,'generation':generation,
                    'vector_error_bound':VECTOR_ERROR,'source':'independently observed response of this exact action; no cardinal-vector sum'}
                self.action_responses[action].update(latest_endpoint=end,facing_revision=self.travel_approach_revision,
                    continuity_receipt_id=receipt['receipt_id'])
                self.mapping_generation=generation;self.mapping_epoch=session_epoch;self.mapping_zone=zone
                self.travel_policy.pop('mapping_unavailable_reason',None)
                if action=='forward':
                    self.action_responses[action]['progress_window']=progress_window
                    self.forward_history={'response':dict(self.action_responses[action]),'revision':self.travel_approach_revision,'zone':zone}
                    if self.returned_failed_direction(measurement,self.action_responses[action]):
                        self.travel_failures[self.travel_key(action,measurement,requested or 0)]=2
            else:
                if carries_forward:
                    previous_response.update(latest_endpoint=end,continuity_receipt_id=receipt['receipt_id'],
                        progress_window=progress_window)
                    record['retained_direction_receipt_id']=previous_response['receipt_id']
                else:self.action_responses.pop(action,None)
                self.mapping_generation=generation;self.mapping_epoch=session_epoch;self.mapping_zone=zone
                if not carries_forward:self.travel_policy['mapping_unavailable_reason']='displacement unresolved at coordinate precision'
            if action=='forward':
                record['forward_progress_receipts']=[step['receipt_id'] for step in progress_window]
                record['forward_progress_measurement']=movement_result(progress_window[0]['position_before'],end,
                    destination if same_destination_zone else None)
            if purpose=='detour' and self.detour:
                self.detour['last_result']=record['status']
                if action!='forward':self.detour['continuation_credit']=resolved and not loop
            duration=actual if actual is not None else requested
            record['narrative']=f"{action} {duration if duration is not None else 'unknown'} seconds; purpose {purpose}; position {start} to {end}; displacement x {dx:+.1f}, y {dy:+.1f}, {map_direction(dx,dy)}; "+('no displacement resolved at 0.1 coordinate precision' if displacement<=VECTOR_ERROR+PRECISION_EPSILON else f'{displacement:.2f} map-coordinate units displaced')
            if purpose=='probe':record['narrative']+=f"; {record['probe_count']} probe endpoint measurement: {map_direction(response_dx,response_dy)}, displacement {response_displacement:.2f} ±{VECTOR_ERROR:.2f}"
            if same_destination_zone:
                remaining=record['destination_minus_current']
                record['destination_map_sector']=map_direction(*remaining,axis_error=.05)
                record['narrative']+=f"; {record['progress_status']} (progress {record['progress']:+.2f} ±{VECTOR_ERROR:.2f}); {record['distance_after']:.2f} map points remaining, destination map sector {record['destination_map_sector']}"
        else:
            self.invalidate_travel_mapping('turn completed' if turn else 'unknown/unlinked/out-of-zone displacement')
            record['narrative']='Turn completed; no translation objective' if turn and pair else 'Coordinate pair unavailable or unlinked; visual travel, displacement unknown'
            if purpose=='probe' and receipt.get('completed'):
                approach=self.travel_key(action,before,requested or 0)
                self.travel_failures[approach]=self.travel_failures.get(approach,0)+1
                record['probe_status']='inconclusive_coordinate_pair_unavailable'
            if purpose=='detour' and self.detour:self.detour['continuation_credit']=False
        if valid and not record.get('revisitation_cycle_suspected'):
            previous=self.last_completed_action or {}
            continuous=(previous.get('search_origin') and previous.get('attributable_pair')
                and (not self.strategy_required or (previous.get('source_captured_at') or '')>=self.strategy_required['captured_at'])
                and previous.get('receipt_id')==pending.get('previous_gameplay_receipt_id')
                and previous.get('search_revision')==self.search_revision and previous.get('session_epoch')==session_epoch
                and tuple(previous.get('zone_before',()))==zone
                and previous.get('frame_source')==frame.source and previous.get('frame_size')==[frame.width,frame.height]
                and previous.get('position_after') and math.dist(previous['position_after'],before['position'])<=.01)
            # Keep the original exhausted-sector endpoint even when the bounded
            # recent-move list drops its first record (e.g. a long out-and-back).
            record['search_origin']=deepcopy(previous['search_origin']) if continuous else {
                'position':before['position'],'source_frame_id':pending['source_frame_id'],
                'source_image':pending.get('source_image'),'source_hash':pending.get('source_hash'),
                'source_captured_at':before.get('captured_at')}
            record['search_displacement_observed']=(record['status']=='observed_displacement'
                or bool(continuous and previous.get('search_displacement_observed')))
        pending['outcome_consumed']=True
        self.pending=None
        self.last_completed_action=record
        self.recent_moves=(self.recent_moves+[record])[-5:]
        outcome={'purpose':'travel','outcome':record['status'],'receipt_id':receipt['receipt_id'],
                 'source_frame_id':pending['source_frame_id'],'frame_id':frame.frame_id,
                 'generation_after':receipt.get('generation_after'),'encounter_id':pending['encounter_id']}
        if record.get('progress_status')=='closer':
            self.last_progress_active_at=self.active_seconds
        self.outcomes=(self.outcomes+[outcome])[-64:]
        self.outcome_events.append(outcome)
        return record

    def travel_search_evidence(self, frame, measurement, session_epoch):
        """A fresh search entry may consume a contiguous, changed travel endpoint.

        Travel receipts, turns and declarations alone do not renew search. Keep
        the old attempt ledger; only the local search revision can change.
        """
        position=measurement.get('position');zone=zone_identity(measurement)
        if (self.pending or self.input_effect_unverified or not position or len(zone)!=1
            or measurement.get('position_status')!='readable_proposal'):return None
        endpoint=position;receipt_id=self.last_gameplay_receipt_id;chain=[]
        for move in reversed(self.recent_moves):
            if (move.get('receipt_id')!=receipt_id or not move.get('attributable_pair')
                or self.strategy_required and (move.get('source_captured_at') or '')<self.strategy_required['captured_at']
                or move.get('search_revision')!=self.search_revision or move.get('session_epoch')!=session_epoch
                or tuple(move.get('zone_before',()))!=zone or move.get('revisitation_cycle_suspected')
                or move.get('frame_source')!=frame.source or move.get('frame_size')!=[frame.width,frame.height]
                or not move.get('position_after') or math.dist(move['position_after'],endpoint)>.01
                or datetime.fromisoformat(move['after_captured_at'])>datetime.fromisoformat(frame.captured_at)):
                break
            try:
                if any(hashlib.sha256(Path(move[path]).read_bytes()).hexdigest()!=move[digest]
                    for path,digest in (('source_image','source_hash'),('after_image','after_hash'))):return None
            except (OSError,KeyError,TypeError):return None
            chain.append(move);endpoint=move['position_before']
            receipt_id=move.get('previous_gameplay_receipt_id')
        if not chain or not chain[0].get('search_displacement_observed'):return None
        origin=chain[0].get('search_origin') or {}
        endpoint=origin.get('position')
        if not endpoint or math.dist(endpoint,position)<=VECTOR_ERROR+PRECISION_EPSILON:return None
        try:
            if hashlib.sha256(Path(origin['source_image']).read_bytes()).hexdigest()!=origin['source_hash']:return None
        except (OSError,KeyError,TypeError):return None
        return {'outcome':'observed_travel_search_reentry','source_frame_id':origin['source_frame_id'],
            'source_image':origin['source_image'],'source_hash':origin['source_hash'],
            'source_captured_at':origin.get('source_captured_at'),
            'frame_id':frame.frame_id,'receipt_id':chain[0]['receipt_id'],
            'travel_receipts':[move['receipt_id'] for move in reversed(chain)],
            'session_epoch':session_epoch,'search_revision':self.search_revision,
            'position_before':endpoint,'position_after':position,'zone':list(zone),
            'displacement':math.dist(endpoint,position),'coordinate_uncertainty':VECTOR_ERROR}

    def action_direction(self, action, measurement, generation, session_epoch, now=None, *, continuous=False):
        now=time.time() if now is None else now
        response=self.action_responses.get(action)
        if self.mapping_generation is not None and (self.mapping_generation!=generation or self.mapping_epoch!=session_epoch or tuple(self.mapping_zone or ())!=zone_identity(measurement)):
            self.invalidate_travel_mapping('input/focus/zone authority changed')
            return None
        if not response:return None
        if continuous:
            if (action!='forward' or response.get('facing_revision')!=self.travel_approach_revision
                or response.get('latest_endpoint') is None or measurement.get('position') is None
                or math.dist(response['latest_endpoint'],measurement['position'])>.01):
                self.invalidate_travel_mapping('forward endpoint/facing continuity changed');return None
        elif not 0<=now-datetime.fromisoformat(response['captured_at']).timestamp()<=10:return None
        dx,dy=response['dx'],response['dy']
        # Only the independently measured exact action is labeled. No rotated
        # unscaled heading or invented diagonal normalization is used.
        direction=map_direction(dx,dy)
        return {'label':f'approximately {direction} in displayed map axes','response':response,
                'uncertainty':'each measured delta axis ±0.1; physical compass bearing/diagonal speed unverified'} if direction!='direction unresolved at coordinate precision' else None

    def result(self,area_id=None):
        ident=area_id or self.encounter_area or (self.plan or {}).get('area_id') or 'current_patch'
        return self.area_results.setdefault(ident,{'area_id':ident,'observed_levels':[], 'encounters':0,
            'kill_hints':0,'search_failures':0,'motion_failures':0,'blocked_approaches':0,'status':'unverified',
            'evidence':[],'reconsider_after':0})

    def candidates(self,level):
        from sage_wow.agent.grind_progression import area_allowed, policy
        low,high=self.target_band(level);result=[]
        for ident,area in {**self.catalog,**self.learned}.items():
            if self.fixed_hunting_area and ident!=self.fixed_hunting_area:continue
            if not area_allowed(self,level,area):continue
            known=self.area_results.get(ident,{})
            observed=known.get('observed_levels',[])
            expected=area.get('expected_level_range')
            if observed and (max(observed)<low or min(observed)>high):continue
            if expected and (expected[1]<low or expected[0]>high):continue
            if known.get('status')=='depleted_or_unsupported_hypothesis' and not self.fixed_hunting_area:continue
            result.append(area)
        preferred=(policy(self,level) or {}).get('area_ids',[])
        if preferred:result.sort(key=lambda a:preferred.index(a['id']) if a['id'] in preferred else len(preferred))
        return result[:4]

    def choose(self,area,frame,request_id,level):
        self.hunt_arrival=None
        self.progression_pending=False
        if self.plan:
            previous=self.result();previous['local_failures']=dict(self.failures);previous['tried_sectors']=list(self.tried_sectors);previous['sector']=self.sector
        self.plan={'area_id':area['id'],'area':dict(area),'selected_frame_id':frame.frame_id,'request_id':request_id,
            'selected_level':level,'band':list(self.target_band(level)),'phase':'travel','claim':'chosen hypothesis; current population and route unverified'}
        intent=(self.strategy_required or {}).get('selected_exit')
        if intent:self.plan['relocation_request_id']=intent['request_id']
        self.phase='travel';self.suspended=False;self.planning_requested=False;self.encounter_area=None
        # The accepted destination completes ordinary recovery's handoff.
        # Retained recovery/failure evidence is not the next question's mode.
        if self.compact_stage=='recovery':self.compact_stage='acquire'
        # The avatar has not moved when a destination is selected. Its current
        # search/selection debt remains applicable until observed local change.
        self.motion.reset_heading()

    def level_changed(self,level):
        from sage_wow.agent.grind_progression import policy
        if policy(self,level) and policy(self,level)!=policy(self,self.last_level):
            self.progression_pending=True
        if self.last_level is not None and self.last_level!=level:
            levels=self.result().get('observed_levels',[])
            if levels and max(levels)<self.target_band(level)[0]:
                record=self.result();record['status']='outgrown_observed_band';record['reconsider_after']=time.time()+60
                self.phase='choose_area';self.planning_requested=True
            self.motion.reset_heading();self.cast_obligation=None
        self.last_level=level

    @staticmethod
    def known_completed_input(pending):
        receipt=pending.get('receipt',{})
        return bool(receipt.get('completed') and not receipt.get('dispatch_unknown') and not receipt.get('error'))

    def confirm_living_target(self,target,frame):
        """Fresh life facts renew combat authority, never establish prior damage."""
        visual=target.get('visual_observation') or {}
        if (visual.get('selected_hud')!='present' or visual.get('life_state')!='alive'
            or target.get('eligibility')!='eligible' or self.input_effect_unverified
            or self.blocked):return False
        history=self.approach_for(target,renew_completed=False) if target.get('name') else None
        if not history:return False
        if (history.get('intentional_clear', {}).get('assessment')
            or history.get('selection_loss', {}).get('assessment')) and not self.target_dead_observed:return False
        obligation=history.get('cast_obligation') or self.cast_obligation
        if not obligation or not obligation.startswith('Prior combat effect unknown/unassessed'):
            return False
        if self.cast_error and self.cast_error.get('status')=='active':return False
        history['cast_obligation']=None
        if history['key']==self.combat_history_key:self.cast_obligation=None
        self.selected_presence=True;self.compact_stage='inspect'
        self.recovery_requested=None
        self.progress_facts.append({'kind':'fresh_living_target_after_unknown_damage',
            'frame_id':frame.frame_id,'target_name':target['name'],
            'prior_damage_known':False,'kill_attribution_known':False})
        return True

    def review_cast_observations(self,target,pending,frame,*,linked,fresh,session_epoch,input_generation):
        """Diagnostic bar continuity is not damage or ineffective-cast proof."""
        name=target_identity(target.get('name'));owner=str((name,self.target_lives.get(name,0)))
        after=target.get('hud') or {};visual=target.get('visual_observation') or {}
        ah=after.get('target_health');health_known=ah is not None and (after.get('target_health_confidence') or 0)>=.8
        if (not fresh or not name or target.get('self_target') or target.get('invalid_text')
            or after.get('frame_id')!=frame.frame_id or visual.get('selected_hud')=='absent'
            or visual.get('life_state')=='dead' or health_known and ah<=0
            or self.target_history.get(owner,{}).get('completed')):return None
        if not (health_known and ah>0 or visual.get('selected_hud')=='present' and visual.get('life_state')=='alive'):return None
        scope={'session_epoch':session_epoch,'input_generation':input_generation,
            'source':frame.source,'width':frame.width,'height':frame.height}
        review=self.cast_review
        # Retained diagnostics remain history. They are not current questions
        # after a different selection/input, local lifetime or capture scope.
        if pending:
            receipt=pending.get('receipt',{})
            if (not linked or pending.get('family')!='combat' or not self.known_completed_input(pending)
                or not receipt.get('possible_input') or receipt.get('session_epoch')!=session_epoch
                or receipt.get('generation_after')!=input_generation or pending.get('target_history_key')!=owner
                or target_identity((pending.get('target') or {}).get('name'))!=name):return None
        elif not review or review.get('owner')!=owner or review.get('scope')!=scope:return None
        if review and review.get('owner')==owner and review.get('scope')==scope:
            if health_known and review.get('target_health') is not None and ah<review['target_health']-.025:
                review['retired_by']={'frame_id':frame.frame_id,'reason':'Current target health decreased; prior unchanged bars are no longer the current diagnosis'}
            if review.get('retired_by'):return None
        if not pending:return review
        before=pending.get('measurement',{}).get('combat_hud',{})
        if before.get('frame_id')!=pending.get('source_frame_id'):return None
        if not self.cast_review or self.cast_review['owner']!=owner:
            self.cast_review={'owner':owner,'unchanged_resource_casts':0,'reviewed_receipts':[],
                'damage_known':False,'reason':'No comparable current resource observations'}
        review=self.cast_review;receipt=pending['receipt']['receipt_id']
        if receipt in review['reviewed_receipts']:return review if review.get('scope')==scope else None
        bh,ah=before.get('target_health'),after.get('target_health')
        bm,am=before.get('player_mana'),after.get('player_mana')
        confidence=min(before.get('target_health_confidence') or 0,after.get('target_health_confidence') or 0)
        if bh is None or ah is None or confidence<.8:return review if review.get('scope')==scope else None
        previous_scope=review.get('scope')
        if review.get('retired_by') or previous_scope and previous_scope!={**scope,'input_generation':pending['receipt'].get('generation_before')}:
            review['unchanged_resource_casts']=0
        review.pop('retired_by',None)
        review['reviewed_receipts']=(review['reviewed_receipts']+[receipt])[-12:]
        review.update(frame_id=frame.frame_id,scope=scope,target_health=ah)
        if ah<bh-.025:
            review.update(unchanged_resource_casts=0,reason='Current target bar is lower; own damage remains separately attributed')
        elif bm is not None and am is not None and abs(ah-bh)<=.015 and abs(am-bm)<=.015:
            review['unchanged_resource_casts']+=1
            review['reason']='Target health and own mana remained unchanged after completed native cast input; check stance/range/facing instead of assuming a successful cast'
        return review

    def retain_combat_evidence(self,pending):
        if pending and pending.get('family')=='combat' and self.known_completed_input(pending):
            self.recent_combat={k:deepcopy(pending.get(k)) for k in
                ('family','measurement','source_frame_id','source_image','source_hash','source_scope','receipt','target',
                 'encounter_id','selection_revision','target_continuity','target_history_key',
                 'rejected_error_cues','assessed_error_cues','position_error_evidence')}
            self.recent_combat['handoff_requested']=False
            self.retain_burst_guard_evidence(pending['receipt'])

    def retain_burst_guard_evidence(self,receipt):
        recent=self.recent_combat
        if not recent or not self.known_completed_input({'receipt':receipt}):return
        execution=receipt.get('execution') or {}
        if (execution.get('kind') not in {'cast_burst','cast_guarded'} or
            target_identity(execution.get('expected_target_name'))!=target_identity((recent.get('target') or {}).get('name'))):return
        guards=[]
        observations=list(execution.get('guard_observations',[]))+list(execution.get('post_cast_observations',[]))
        for observation in observations:
            path=observation.get('image_path')
            if path and Path(path).is_file():
                guards.append({'frame_id':observation.get('frame_id'),'image_path':path,
                    'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                    'receipt_id':receipt['receipt_id'],'reason':observation.get('reason')})
        if guards:recent['last_guard_frame']=guards[-1]

    def archive_pending(self,reason):
        if self.pending:
            pending=self.pending
            from sage_wow.agent.grind_cast_feedback import retain_interrupted
            if not pending.get("outcome_consumed"):retain_interrupted(self,pending)
            # Preserve a no-credit recovery route after semantic archival.
            if not self.recent_combat or self.recent_combat.get('receipt',{}).get('receipt_id')!=pending['receipt']['receipt_id']:
                self.retain_combat_evidence(pending)
            if not pending.get('outcome_consumed'):
                pending['outcome_consumed']=True
                outcome={'purpose':pending.get('purpose',pending['family']),'outcome':'unknown',
                    'reason':reason,'receipt_id':pending['receipt']['receipt_id'],
                    'source_frame_id':pending['source_frame_id'],'frame_id':None,
                    'generation_after':pending['receipt']['generation_after'],
                    'encounter_id':pending['encounter_id'],'selection_revision':pending.get('selection_revision')}
                self.outcomes.append(outcome);self.outcome_events.append(outcome)
            if not self.archive_motion(self.pending) and not (pending['family']=='combat' and self.known_completed_input(pending)):self.failed_action(self.pending)
            if self.pending['family']=='combat':
                if not self.known_completed_input(pending):self.input_effect_unverified=True
                self.cast_obligation=(UNKNOWN_COMPLETED_CAST
                    if self.known_completed_input(pending) else 'Prior combat input unknown; input integrity reconciliation required')
                self.remember_approach()
            self.unassessed_count+=1
            self.unassessed=(self.unassessed+[{'status':'unknown_unassessed','reason':reason,**self.pending}])[-24:]
            self.pending=None

    def tick(self, now):
        if self.active_tick is not None:self.active_seconds += max(0., now-self.active_tick)
        self.active_tick=now

    def selection_seen(self, target):
        fact = self.intentional_clear_current() or {}
        if fact.get('disposition') == 'verified' and self.positive_selection(target):
            fact['disposition'] = 'superseded'
        elif fact.get('disposition') == 'submitted':
            source = (fact.get('clear') or {}).get('target') or {}
            visual = target.get('visual_observation') or {}
            source_levels = source.get('levels') or []
            recent = self.recent_combat or {}
            if (len(source_levels) != 1 and fact.get('combat_receipt_id') == recent.get('receipt', {}).get('receipt_id')
                and recent.get('target_continuity') == fact.get('clear', {}).get('target_continuity')):
                source_levels = (recent.get('target') or {}).get('levels') or []
            levels = target.get('levels') or []
            numerals = ([levels[0]] if len(levels) == 1 else []) + ([visual['level']] if visual.get('level') is not None else [])
            if (any(name and target_identity(name) != target_identity(source.get('name'))
                    for name in (target.get('name'), visual.get('name')))
                or len(source_levels) == 1 and any(n != source_levels[0] for n in numerals)):
                fact['disposition'] = 'superseded'
        # Numeric OCR availability is not a new acquisition. Keep a separate
        # positive identity/absence lifecycle for historical question evidence;
        # the exact selection revision still protects current dispatch scope.
        prior_continuity=self.target_continuity
        name=target_identity(target.get('name'))
        visual=target.get('visual_observation') or {}
        absent=visual.get('selected_hud')=='absent'
        numbers=target.get('levels') or []
        level=numbers[0] if len(numbers)==1 else None
        if name and self.continuity_name and name!=self.continuity_name:
            self.target_continuity+=1;self.continuity_level=None
        if level is not None and self.continuity_level is not None and level!=self.continuity_level:
            self.target_continuity+=1
        if level is not None:self.continuity_level=level
        ineligible=bool(target.get('self_target') or visual.get('target_kind') in {'player','friendly_or_self'} or visual.get('life_state')=='dead')
        if ineligible and not self.continuity_ineligible:self.target_continuity+=1
        self.continuity_ineligible=ineligible
        if absent and not self.continuity_absent:self.target_continuity+=1
        if name:self.continuity_name=name
        self.continuity_absent=absent
        current=(target.get('name'),tuple(target.get('levels',[])),target.get('self_target'),target.get('invalid_text'))
        if current!=self.selection:
            previous=self.selection;revision=self.selection_revision
            # Only numeric availability churn preserves a completed correction.
            # Exact decision scope still advances; explicit positive changes,
            # ambiguous numbers and other OCR changes retain invalidation.
            numeric_gap=bool(previous and current[0] and current[0]==previous[0]
                and current[2:]==previous[2:] and not any(current[2:])
                and self.target_continuity==prior_continuity and not absent and not ineligible
                and {previous[1],current[1]}=={(),(self.continuity_level,)}
                and self.continuity_level is not None)
            self.selection_revision+=1;self.selection=current
            if numeric_gap:
                for proof in (self.pending,self.correction_evidence):
                    if (proof and proof.get('purpose') in {'approach','cast_correction','standing'}
                        and proof.get('target_continuity')==self.target_continuity
                        and proof.get('current_selection_revision',proof.get('selection_revision'))==revision):
                        proof['current_selection_revision']=self.selection_revision
            else:
                self.retry_credit=False;self.correction_evidence=None
        elif self.target_continuity!=prior_continuity:
            self.retry_credit=False;self.correction_evidence=None
        return current

    def rejection_signature(self, target):
        return str((target.get('name'),tuple(target.get('levels',[])),self.sector))

    def meaningful_sector_change(self,evidence=None):
        self.sector+=1;self.tried_sectors.append(self.sector)
        self.search_revision+=1
        self.search_changes=(self.search_changes+[{'revision':self.search_revision,
            'area_id':(self.plan or {}).get('area_id'),'active_at':self.active_seconds,
            'evidence':deepcopy(evidence)}])[-24:]
        self.strategy_required=None
        self.recovery_requested=None
        if self.compact_stage=='recovery':self.compact_stage='acquire'
        if self.attempt_absence:self.attempt_changed_view=True
        self.changed_situation('target');self.changed_situation('motion');self.changed_situation('clear')
        self.failures['target']=0;self.rejected_signatures.clear();self.rejected_name=None
        self.confirmed_target_absences=0
        self.unclear=0;self.sector_new_candidate=True
        if len(self.tried_sectors)%3==0:self.phase='choose_area';self.planning_requested=True

    def linked(self,frame,generation):
        return bool(self.pending and generation==self.pending['receipt']['generation_after']
            and datetime.fromisoformat(frame.captured_at).timestamp()>datetime.fromisoformat(self.pending['receipt']['occurred_at']).timestamp())

    def feedback_for(self,frame,generation,measurement):
        self.feedback={'position_status':measurement['position_status'],'mode':'visual','heading':None}
        area=(self.plan or {}).get('area',{});destination=area.get('coordinate')
        zone=area.get('zone_reference')
        same_zone=bool(zone and zone_identity(measurement)==(normalized_zone(zone),))
        if same_zone and measurement['position'] and destination:
            self.feedback.update(mode='coordinate',position=measurement['position'],destination=destination,
                distance=math.dist(measurement['position'],destination),arrival_tolerance=area.get('arrival_radius',.6),
                destination_minus_current=[destination[i]-measurement['position'][i] for i in range(2)])
            delta=self.feedback['destination_minus_current']
            self.feedback['destination_map_sector']=map_direction(*delta,axis_error=.05)
        if self.linked(frame,generation):
            before=self.pending.get('measurement',{});after=measurement
            if before.get('position') and after['position'] and zone_identity(before)==zone_identity(after) and zone_identity(after):
                displacement=math.dist(before['position'],after['position']);self.feedback['displacement']=displacement
                self.feedback['displacement_above_quantization']=displacement>VECTOR_ERROR
                seconds=completed_hold_seconds(self.pending['receipt'])
                if self.pending['action']=='forward' and seconds:
                    self.motion.observe(before['position'],after['position'],seconds,
                        receipt_id=self.pending['receipt']['receipt_id'],frame_id=frame.frame_id,
                        captured_at=datetime.fromisoformat(frame.captured_at).timestamp())
                if self.motion.eligible(time.time()):self.feedback['heading']=self.motion.context()
                if destination and self.feedback['mode']=='coordinate':
                    self.feedback['distance_change']=math.dist(after['position'],destination)-math.dist(before['position'],destination)
            else:self.motion.reset_heading()
        elif self.pending and self.pending.get('purpose')!='travel':self.archive_pending('input_generation_or_postcompletion_link_lost')
        self.last_measurement=measurement
        return self.feedback

    def resolve(self, effect, frame, measurement, *, pending=None, linked=False):
        """Consume a saved receipt once; derived correction evidence survives consumption."""
        pending = pending or self.pending
        if not linked or not pending or pending.get('outcome_consumed'):
            return None
        family = pending['family']
        compatible={'combat':{'combat_effect','combat_unchanged','cast_active','cooldown','interrupted'},
            'motion':{'motion_useful','motion_blocked','motion_no_useful_effect'},
            'target':{'target_absent','target_acquired'},'clear':{'target_cleared','clear_failed'},
            'ui':{'world_clear','ui_still_blocked'},'recovery':{'resources_improved','resources_unchanged'}}
        if effect!='unknown' and effect not in compatible.get(family,set()):return None
        if effect=='unknown' and family=='combat':
            from sage_wow.agent.grind_cast_feedback import retain_interrupted
            retain_interrupted(self,pending)
        pending['outcome_consumed'] = True
        purpose = pending.get('purpose', family)
        # A released-owner stand proves only our player's stance. Even a
        # malformed marker must never fall through to ordinary target/sector
        # success accounting; the caller authenticates positive assessments.
        released_stance = 'released_error_stance' in pending or 'selected_task_abandon' in pending
        record = self.result()
        outcome = {'purpose': purpose, 'outcome': effect, 'receipt_id': pending['receipt']['receipt_id'],
                   'source_frame_id': pending['source_frame_id'], 'frame_id': frame.frame_id,
                   'generation_after': pending['receipt']['generation_after'],
                   'encounter_id': pending['encounter_id'], 'selection_revision': pending.get('selection_revision')}
        self.outcomes.append(outcome)
        self.outcomes = self.outcomes[-64:]
        record['evidence'] = (record['evidence'] + [outcome])[-32:]
        failures = {'target_absent', 'ui_still_blocked', 'motion_blocked', 'motion_no_useful_effect',
                    'combat_unchanged', 'resources_unchanged', 'clear_failed', 'unknown'}
        success = effect not in failures and effect not in {'cast_active', 'cooldown', 'interrupted'}
        if effect in failures and not (effect=='unknown' and family=='combat' and self.known_completed_input(pending)):
            unknown_motion=effect=='unknown' and self.archive_motion(pending)
            if not unknown_motion:
                self.failed_action(pending)
                if family in self.failures:self.failures[family] += 1
        elif success and family in self.failures and not released_stance:
            self.failures[family] = 0
            if not pending.get('debt_key'):self.action_failures.pop(self.action_key(pending['action'], family), None)
        if effect == 'target_absent':
            record['search_failures'] += 1;self.confirmed_target_absences += 1
        elif effect == 'target_acquired':self.confirmed_target_absences=0
        if effect in {'motion_blocked', 'motion_no_useful_effect'}:
            record['motion_failures'] += 1;self.motion.reset_heading()
        if effect == 'world_clear':self.input_effect_unverified = False
        if effect == 'combat_unchanged':
            self.cast_obligation = INEFFECTIVE_COMPLETED_CAST
            if pending.get('retry_probe'):self.correction_rounds += 1
        if effect=='unknown' and family=='combat':
            if not self.known_completed_input(pending):self.input_effect_unverified=True
            obligation=(UNKNOWN_COMPLETED_CAST
                if self.known_completed_input(pending) else 'Prior combat input unknown; input integrity reconciliation required')
            owner=pending.get('target_history_key')
            if owner is None or owner==self.combat_history_key:self.cast_obligation=obligation
            if owner in self.target_history:self.target_history[owner]['cast_obligation']=obligation
        if effect == 'combat_effect':
            self.cast_obligation = None;self.cast_error = None;self.correction_rounds = 0
            self.last_progress_active_at = self.active_seconds
            record['status'] = 'seen_this_session'
        if effect == 'motion_useful' and not released_stance:
            self.last_progress_active_at = self.active_seconds
            if purpose in {'cast_correction', 'approach', 'standing'}:
                self.correction_evidence = {**outcome, 'source_image': pending['source_image'],
                    'source_hash': pending['source_hash'], 'source_scope': deepcopy(pending.get('source_scope')),
                    'target_history_key': pending.get('target_history_key'),
                    'ineffective_cast_recovery': deepcopy(pending.get('ineffective_cast_recovery')),
                    'receipt': deepcopy(pending['receipt']), 'target': deepcopy(pending.get('target')),
                    'target_continuity': pending.get('target_continuity'),
                    'current_selection_revision': pending.get('current_selection_revision',pending.get('selection_revision')),
                    'correction_cast_receipt_id': pending.get('correction_cast_receipt_id'),
                    'correction_error_receipt_id': pending.get('correction_error_receipt_id'),
                    'correction_error_cue': pending.get('correction_error_cue'),
                    'assessment_image': frame.image_path,
                    'assessment_hash': hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()}
            else:self.meaningful_sector_change(outcome)
        if family=='motion' and pending.get('target_history_key') in self.target_history:
            history=self.target_history[pending['target_history_key']]
            history['outcomes']=(history['outcomes']+[outcome])[-24:]
            if effect=='motion_useful' and not released_stance:
                history['method_revision']+=1
                # Keep recent exact method records and compact older changed
                # relations into bounded per-purpose/action accounting.
                self.retire_motion_debt(pending['target_history_key'],before_revision=history['method_revision']-2)
        if effect == 'resources_improved':
            self.recovery_progress_at = self.active_seconds;self.no_mana = False;self.recovery_reassessed=False
        if self.pending is pending:self.pending = None
        record['local_failures'] = dict(self.failures)
        # A completed closure that left the panel visible is a method failure,
        # not unknown native input. Other independently allowed closures remain
        # available; the UI route still excludes every world action.
        if family == 'ui' and not self.known_completed_input(pending):
            self.input_effect_unverified = True
        return outcome

    def effect(self, effect, frame, measurement, *, linked, level=None):
        return self.resolve(effect, frame, measurement, linked=linked)

    @property
    def retry_credit(self):
        return self._retry_credit

    @retry_credit.setter
    def retry_credit(self, value):
        # A newly granted ordinary resource/cooldown credit supersedes the old
        # source. Clearing only correction evidence never changes the source.
        origin=getattr(self,'retry_credit_source',None) or {}
        if (not value and origin.get('kind')=='ineffective_cast_correction'
                and origin.get('owner')==self.combat_history_key
                and origin.get('cast_receipt_id')==((self.recent_combat or {}).get('receipt') or {}).get('receipt_id')
                and self.no_effect_retries>=2 and self.failures['combat']>=2
                and not self.cast_obligation):
            # Invalidation of an unused correction restores its old constraint.
            # A newly installed combat receipt already replaced recent_combat,
            # so consuming the credit does not recreate the old obligation.
            self.cast_obligation=INEFFECTIVE_COMPLETED_CAST
            self.remember_approach()
        if (not value and origin.get('kind')=='linked_correction'
                and origin.get('owner')==self.combat_history_key
                and (origin.get('error_receipt_id') and origin['error_receipt_id']==(self.cast_error or {}).get('cast_receipt_id')
                     or not origin.get('error_receipt_id') and origin.get('cast_receipt_id')==((self.recent_combat or {}).get('receipt') or {}).get('receipt_id'))
                and not self.cast_obligation):
            self.cast_obligation=origin.get('prior_obligation')
            if self.cast_error and self.cast_error.get('cast_receipt_id')==origin.get('cast_receipt_id'):
                self.cast_error['status']=origin.get('prior_error_status')
            self.remember_approach()
        self._retry_credit = value
        self.retry_credit_source = None

    def grant_correction(self):
        if not self.correction_evidence or self.correction_evidence.get('granted'):return False
        obligation=self.cast_obligation;error_status=(self.cast_error or {}).get('status')
        self.cast_obligation = None
        if self.cast_error:self.cast_error['status'] = 'retired_by_linked_correction'
        self.retry_credit = True
        self.retry_credit_source = {'kind':'linked_correction',
            'owner':self.correction_evidence.get('target_history_key'),
            'cast_receipt_id':self.correction_evidence.get('correction_cast_receipt_id'),
            'error_receipt_id':self.correction_evidence.get('correction_error_receipt_id'),
            'correction_receipt_id':self.correction_evidence['receipt_id'],
            'prior_obligation':obligation,'prior_error_status':error_status}
        semantic = self.correction_evidence.get('ineffective_cast_recovery')
        if semantic:
            self.retry_credit_source = {'kind': 'ineffective_cast_correction',
                'owner': semantic['owner'], 'cast_receipt_id': semantic['receipt_id'],
                'correction_receipt_id': self.correction_evidence['receipt_id']}
        self.correction_evidence['granted']=True
        return True

    def install(self, action, family, frame, receipt, measurement, *, purpose=None, target=None, selected_task_abandon=None):
        from sage_wow.agent.grind_cast_feedback import cue_key
        self.archive_pending('next_action_before_prior_effect_assessed')
        if purpose!='selected_task_abandon':self.correction_evidence=None
        self.pending = {'action': action, 'family': family, 'purpose': purpose or family,
                        'source_frame_id': frame.frame_id, 'source_image': frame.image_path,
                        'source_hash': hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
                        'measurement': measurement, 'receipt': dict(receipt),
                        'encounter_id': self.encounter, 'selection_revision': self.selection_revision,
                        'target_continuity':self.target_continuity,
                        'correction_cast_receipt_id':((self.cast_error or {}).get('cast_receipt_id')
                            if (self.cast_error or {}).get('status')=='active'
                            else ((self.recent_combat or {}).get('receipt') or {}).get('receipt_id')),
                        'correction_error_cue':cue_key(self.cast_error) if (self.cast_error or {}).get('status')=='active' else None,
                        'correction_error_receipt_id':((self.cast_error or {}).get('cast_receipt_id')
                            if (self.cast_error or {}).get('status')=='active' else None),
                        'target': target, 'phase': self.phase, 'outcome_consumed': False,
                        'source_scope':{'session_epoch':receipt.get('session_epoch'),
                            'source':frame.source,'width':frame.width,'height':frame.height},
                        'retry_probe': bool(self.retry_credit and family == 'combat')}
        if purpose=='selected_task_abandon':self.pending['selected_task_abandon']=deepcopy(selected_task_abandon)
        elif family=='clear':self.install_intentional_clear(self.pending)
        if family=='target' and action=='target_enemy' and receipt.get('possible_input') and self.known_completed_input(self.pending):
            fact=self.intentional_clear_current() or {}
            if fact.get('disposition')=='verified':fact['disposition']='superseded'
            self.target_continuity+=1
            # Sage/dispatch latency belongs before completion, not to the next
            # retry interval. tick excludes any previously paused active clock.
            self.tick(time.time())
            self.last_completed_target_active_at=self.active_seconds
        if family=='motion' and purpose!='travel':
            identity=self.approach['history_key'] if self.approach and purpose in {'approach','cast_correction','standing'} else None
            self.pending.update(target_history_key=identity,debt_key=self.motion_key(action,purpose or family,identity))
        if family=='combat':
            history=self.target_history.get(self.combat_history_key) or {}
            if history.get('unassessed_error_feedback'):
                history['unassessed_error_feedback']['status']='superseded_by_new_cast'
            self.pending['target_history_key']=self.combat_history_key
            self.retain_combat_evidence(self.pending)
        if family == 'combat':
            # A newly completed cast consumes the allowance; invalidation of
            # an unused correction instead restores its owned constraint.
            self.retry_credit_source=None;self.retry_credit=False;self.correction_evidence=None
        if (family == 'motion' and action.startswith('turn_')
            and receipt.get('possible_input') and self.known_completed_input(self.pending)):
            # Facing is shared across search, recovery, combat and travel.
            # Retain the old failures/directions, but require a fresh probe.
            self.travel_approach_revision+=1
            self.travel_policy['tactic_changes']+=1
            self.invalidate_travel_mapping('turn input changes character orientation')
        elif purpose=='travel' and not action.startswith('turn_'):
            self.mapping_generation=receipt.get('generation_after')
            if action!='forward':self.probe_series=None
        elif receipt.get('possible_input'):self.invalidate_travel_mapping('non-travel gameplay input')
        if action != 'forward':self.motion.reset_heading()

    def encounter_seen(self,level,frame,measurement,name=None):
        coordinate=measurement['position'];zones=measurement['zone_proposals']
        zone=zones[0] if zones else None
        identity=f'{zone}:{round(coordinate[0],1)}:{round(coordinate[1],1)}' if coordinate and zone else f'visible:{self.session_id}:{self.encounter}'
        ident='observed_'+hashlib.sha256(identity.encode()).hexdigest()[:12]
        self.encounter_area=ident
        record=self.result(ident)
        if self.encounter_ended:
            self.encounter=max(self.encounter,self.encounter_serial)+1;self.encounter_serial=self.encounter
            self.failures['combat']=0;self.cast_obligation=None;self.cast_error=None;self.encounter_ended=False
            self.no_effect_retries=0;self.own_damage_evidence=None
            self.last_progress_active_at=self.active_seconds;self.correction_rounds=0
            record['encounters']+=1
        if level not in record['observed_levels']:record['observed_levels'].append(level)
        names=record.setdefault('observed_target_names',[])
        if name and name not in names:names.append(name)
        from sage_wow.agent.grind_progression import primary
        if not primary(self, self.last_level or 1, name):
            return
        record['status']='seen_this_session';record['last_seen_at']=frame.captured_at
        self.latest_patch={'id':ident,'label':'Current Sage-confirmed suitable encounter patch',
            'coordinate':coordinate if zone else None,'zone_reference':zone,
            'coordinate_space':'client_displayed_zone_coordinates' if coordinate and zone else None,
            'expected_level_range':[min(record['observed_levels']),max(record['observed_levels'])],
            'observed_target_names':list(names),
            'status':'seen_this_session','source':{'kind':'current_sage_combined_attack_choice',
            'claim':'living eligible creature seen, no kill/XP established; missing coordinates requires fresh visible reacquisition',
            'source_frame_ids':[frame.frame_id]}}
        self.learned[ident]=self.latest_patch
        if len(self.learned)>16:self.learned.pop(next(iter(self.learned)))

    def context(self):
        return {'phase':self.phase,'hunt_plan':self.plan,'destination_suspended':self.suspended,
            'fixed_hunting_area':self.fixed_hunting_area,
            'scouting_observations':self.scouting_observations,
            'hunting_progression':self.hunting_progression_by_player_level,
            'progression_pending':self.progression_pending,
            'destination_change_basis':self.destination_change_basis,
            'last_completed_target_active_at':self.last_completed_target_active_at,
            'area_results':dict(list(self.area_results.items())[-20:]),'failures':self.failures,'search_sector':self.sector,
            'tried_sectors':self.tried_sectors[-8:],'encounter_id':self.encounter,
            'pending_outcome':self.pending,'action_failures':self.action_failures,'situations':self.situations,'progress_facts':self.progress_facts[-20:],'unassessed_count':self.unassessed_count,
            'unassessed_recent':self.unassessed[-3:],'unclear_outcomes':self.unclear,'travel_feedback':self.feedback,
            'blocked':self.blocked,'action_outcomes':self.outcomes[-5:],'active_seconds':self.active_seconds,
            'search_revision':self.search_revision,'search_changes':self.search_changes,'strategy_required':self.strategy_required,
            'question_history':self.question_history,'archived_question_count':self.archived_question_count,
            'archived_question_unclear':self.archived_question_unclear,
            'recovery_requested':self.recovery_requested,'recovery_history':self.recovery_history,
            'recovery_plan_revision':self.recovery_plan_revision,
            'last_combat_progress_at':self.no_progress_at,'progress_review_at':self.progress_review_at,
            'cast_error':self.cast_error,'selection_revision':self.selection_revision,
            'target_continuity':self.target_continuity,
            'active_threat':self.active_threat,'disengagement':self.disengagement,
            'approach':self.approach,'target_history':self.target_history,'unresolved_motion':self.unresolved_motion,
            'active_question':self.active_question,'question_debt':self.question_debt,
            'interrupted_boundary_question':({'question_key':self.pinned_boundary_question,
                'accounting_kind':'admitted_boundary_requests',
                'admitted_count':self.question_debt.get(self.pinned_boundary_question,0)} if self.pinned_boundary_question else None),
            'last_completed_action':self.last_completed_action,'recent_moves':self.recent_moves,'travel_failures':self.travel_failures,'active_detour':self.detour,'probe_series':self.probe_series,'forward_history':self.forward_history,'failed_direction_approaches':self.failed_direction_approaches,'travel_policy':self.travel_policy,'travel_urgent_debt':self.travel_urgent_debt,'established_zone':self.established_zone,
            'cast_review':self.cast_review,'recent_combat':self.recent_combat,'loot_request':self.loot_request,'credited_kills':self.credited_kills[-20:],'local_attempt_identity':self.combat_history_key,'cast_correction_obligation':self.cast_obligation,'input_effect_unverified':self.input_effect_unverified}

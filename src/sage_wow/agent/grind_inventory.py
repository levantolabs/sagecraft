"""Goal-scoped full-bag facts retire loot work, never grant input authority."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from sage_wow.agent.ui_layout import from_profile


def character(profile):
    return ' '.join(str(profile.values['character'].get(k) or '') for k in ('name','surname')).strip()


def validate_policy(profile, config, policy):
    if (not isinstance(policy,dict) or policy.get('state')!='suppressed_inventory_full'
        or policy.get('character')!=character(profile) or policy.get('goal_level')!=config['goal_level']):
        raise ValueError('Inventory-full policy must match this character and leveling goal')
    evidence=policy.get('evidence') or {}
    if (not isinstance(evidence,dict) or evidence.get('source') not in {'user_report','current_screen_error'}
        or evidence.get('reason')!='inventory_full' or not evidence.get('detail')):
        raise ValueError('Inventory-full policy requires attributed positive capacity evidence')
    try:
        observed=datetime.fromisoformat(evidence['observed_at'])
        if observed.tzinfo is None:raise ValueError('Timezone required')
    except (KeyError,TypeError,ValueError) as exc:
        raise ValueError('Inventory-full evidence needs a timezone-qualified observation time') from exc
    if evidence['source']=='current_screen_error' and not all(evidence.get(k) for k in ('frame_id','image_sha256','rows')):
        raise ValueError('Screen capacity evidence requires its frame, digest and error rows')
    return deepcopy(policy)


def policy(c):
    if not c.config.get('skip_loot_when_inventory_full',False):return None
    current=getattr(c,'loot_policy',None)
    if current:return current
    candidate=(c.config.get('inventory_full_seed') or c.store.load_checkpoint('grind_loot_policy')
        or ((c.config.get('campaign_continuation') or {}).get('evidence') or {}).get('loot_policy'))
    if candidate:
        c.loot_policy=validate_policy(c.profile,c.config,candidate)
        persist(c)
        c.event('grind_loot_policy',{'policy':c.loot_policy,'input_authority':False})
    return getattr(c,'loot_policy',None)


def persist(c):
    c.store.save_checkpoint('grind_loot_policy',c.loot_policy)
    progress=c.store.load_checkpoint('grind_campaign_progress')
    if progress and progress.get('character')==character(c.profile) and progress.get('goal_level')==c.config['goal_level']:
        progress={**progress,'loot_policy':deepcopy(c.loot_policy)}
        c.store.save_checkpoint('grind_campaign_progress',progress)
        output=c.config.get('campaign_progress_path')
        if output:
            path=Path(output);path.parent.mkdir(parents=True,exist_ok=True)
            temp=path.with_suffix('.tmp');temp.write_text(json.dumps(progress,indent=2));temp.replace(path)


def effective_loot_enabled(c):
    return c.config.get('loot_enabled',False) and policy(c) is None


def retire_loot(c):
    """Abandon collection honestly. Pending motor/recovery authority is untouched."""
    if not policy(c):return
    from sage_wow.agent.grind_loot import _sweep, _save
    existing=getattr(c,'loot',None)
    saved=c.store.load_checkpoint('grind_loot') if existing is None else None
    if c.hunt.loot_request or (existing and existing.pending) or (saved and saved.get('loot_sweep',{}).get('pending')):
        sweep=_sweep(c)
        sweep.data.update(pending=False,confirmations=0,selected_corpse=None,
            click_verification_current=False,click_loot_authority=None,
            outcome='skipped_inventory_full_not_verified_looted')
        for source in sweep.data.get('death_sources',[]):source['previous_target_authority']=None
        _save(c)
        c.event('grind_loot_skipped',{'outcome':sweep.data['outcome'],'policy':c.loot_policy,
            'death_sources':deepcopy(sweep.data.get('death_sources',[])),'verified_looted':False})
    if c.hunt.compact_stage=='loot':c.hunt.compact_stage='acquire'


async def observe_capacity(c, frame, rows):
    """Only a fresh central error during loot can create the factual policy."""
    if policy(c):retire_loot(c);return
    if not c.config.get('skip_loot_when_inventory_full',False):return
    from sage_wow.agent.grind_loot import loot_route_pending
    if not loot_route_pending(c):return
    errors=[]
    for row in rows:
        b=row.bounds;text=' '.join(row.text.casefold().strip(' .!').split())
        if (row.confidence>=.75 and text in {'inventory is full','inventory full','your inventory is full','bags are full','your bags are full'}
            and .18*frame.width<=b['x']<=.82*frame.width and .12*frame.height<=b['y']<=.65*frame.height):
            errors.append({'text':row.text,'confidence':row.confidence,'bounds':dict(b)})
    if not errors or not c.fresh(frame) or c.cycle._scope_error(frame):return
    from sage_wow.agent.grind_observation import read_player_identity
    authority=(c.revision,c.cycle.session_epoch,c.cycle.input_generation)
    digest=hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()
    own=await read_player_identity(c,frame,rows,full_name=True)
    exact=' '.join(' '.join(r['text'] for r in own['rows']).casefold().split())==character(c.profile).casefold()
    if (not own['verified'] or not exact or not c.fresh(frame) or c.cycle._scope_error(frame)
        or authority!=(c.revision,c.cycle.session_epoch,c.cycle.input_generation)
        or digest!=hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()):return
    c.loot_policy={'state':'suppressed_inventory_full','character':character(c.profile),'goal_level':c.config['goal_level'],
        'evidence':{'source':'current_screen_error','reason':'inventory_full','observed_at':frame.captured_at,
            'detail':'Confident current central capacity error during outstanding loot work',
            'frame_id':frame.frame_id,'image_sha256':digest,'rows':errors}}
    persist(c);c.event('grind_loot_policy',{'policy':c.loot_policy,'input_authority':False});retire_loot(c)


def loot_panel(c, frame, rows):
    """A local Items title plus item row proposes closure, never capacity."""
    regions=from_profile(c.profile)['regions']
    def usable(row):
        b=row.bounds;x=b['x'];y=b['y'];w=b['width'];h=b['height']
        # Use a central control proposal region, as for other blocking-modal
        # controls. Lower/left passive chat is not a panel title or item row.
        # Sage must still choose closure; fresh matching cues gate Escape.
        return (row.confidence>=.75 and w>0 and h>0 and .18*frame.width<=x<x+w<=.82*frame.width
            and .12*frame.height<=y<y+h<.72*frame.height
            and not any(l<=x and t<=y and x+w<=r and y+h<=bottom
                for name,(l,t,r,bottom) in regions.items()
                if name in {'player_frame','target_overlay','minimap','quest_tracker'}))
    headings=[r for r in rows if r.text.strip().casefold()=='items' and usable(r)]
    if len(headings)!=1:return None
    title=headings[0];b=title.bounds
    items=[r for r in rows if usable(r) and r is not title and re.search(r'[A-Za-z]',r.text)
        and r.text.strip().casefold() not in {'dead','items'}
        and b['y']+b['height']<=r.bounds['y']<=b['y']+min(.10*frame.height,6*b['height'])
        and abs(r.bounds['x']-b['x'])<=.15*frame.width]
    return {'title':title.text,'items':[r.text for r in items]} if items else None


def augment_ui(c,frame,rows,ui):
    from sage_wow.agent.grind_loot_budget import handoff_pending
    panel=loot_panel(c,frame,rows) if policy(c) or handoff_pending(c) else None
    return {**ui,'positive':True,'cues':[*ui['cues'],'loot_items_panel'],'loot_panel':panel} if panel else ui


def context(c):
    return (' Inventory capacity was positively reported for this character and leveling goal. '
        'Loot collection is suspended until this goal ends; do not inspect corpses or collect items. '
        'Close any currently visible loot panel, verify normal world, then continue hunting. '
        'Skipped items are NOT verified looted.') if policy(c) else ''

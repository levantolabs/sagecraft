"""Calibrated badge continuity with synthetic glyphs; no ignored run dependency."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image, ImageDraw, ImageFont

from sage_wow.agent.grind_only import settings, same_patch, same_target_badge
from sage_wow.control.executor import GateSnapshot
from sage_wow.models import Frame
from sage_wow.perception.ocr import TextObservation
from sage_wow.platform.macos.geometry import Rect
from test_grind_only import setup, baseline, cleanup


BADGE=[425,577,467,619]
CALIBRATION={'mode':'opaque_interior_ellipse','inset_pixels':5,'image_width':1496,
    'image_height':967,'badge_box':BADGE,'ui_layout_id':'offline-badge-calibration',
    'verified_from':'Synthetic calibrated opaque badge and complete numeral field'}


def row(text,x,y,w,h):
    return TextObservation(text,.99,{'x':x,'y':y,'width':w,'height':h},'offline fixture')


class BadgeWorld:
    def __init__(self,directory):
        self.directory=directory;self.count=0;self.change=None;self.numeric=None
    def capture(self):
        self.count+=1;path=self.directory/f'badge-{self.count}.png'
        image=Image.new('RGB',(1496,967),'#263746');draw=ImageDraw.Draw(image)
        draw.text((157,519),'Young Wolf',fill='white')
        draw.text((155,375),'Test Player',fill='white')
        draw.text((82,452),'1',fill='white')
        draw.rectangle((200,545,380,553),fill='green')
        draw.ellipse((425,577,466,618),fill='#14171c',outline='#b8a050',width=2)
        if self.change not in {'absent','covered'}:
            if self.change=='skull':
                draw.ellipse((438,587,454,602),fill='white')
                draw.ellipse((441,591,444,594),fill='black')
                draw.ellipse((448,591,451,594),fill='black')
                draw.rectangle((443,602,449,607),fill='white')
            else:
                glyph='2' if self.change=='level' else '1'
                draw.text((446,598),glyph,font=ImageFont.load_default(size=22),anchor='mm',fill='white')
        if self.change=='absent':draw.rectangle((425,577,466,618),fill='#263746')
        if self.change=='occlusion':draw.rectangle((441,591,451,600),fill='red')
        if self.change=='corner':draw.rectangle((425,577,430,582),fill='red')
        if self.change=='player':draw.rectangle((82,452,88,462),fill='red')
        image.save(path)
        return Frame.create('offline-window-7',1496,967,image_path=str(path))
    def ocr(self,path):
        rows=[row('Young Wolf',157,519,160,20),row('Test Player',155,375,130,15)]
        if self.numeric is not None:rows.append(row(str(self.numeric),442,589,10,18))
        return rows


def build(tmp_path,*,opt_in=True,answers=()):
    c,_,s,b,store,e=setup(tmp_path,answers)
    layout=c.profile.values['calibration']['ui_layout']
    layout.update(id=CALIBRATION['ui_layout_id'],image_width=1496,image_height=967)
    layout['regions']={'player_frame':[70,360,400,490],'player_health':[150,400,300,412],
        'target_overlay':[145,500,480,628],'target_frame':[145,500,480,628],
        'target_health':[200,545,380,553]}
    cfg=c.profile.values['grind_only'];cfg.update(target_level_box=list(BADGE),player_level_box=[73,437,105,486])
    if opt_in:cfg['target_badge_continuity']=deepcopy(CALIBRATION)
    c.config=settings(c.profile)
    world=BadgeWorld(tmp_path);c.capture=world.capture;c.ocr=world.ocr
    rect=Rect(0,0,1496,967);e.inspect_gate=lambda:GateSnapshot(7,7,True,rect,rect,True,True)
    return c,world,s,b,store,e


@pytest.mark.parametrize('field,value',[
    ('mode','auto'),('inset_pixels',6),('inset_pixels',100),('inset_pixels',True),
    ('image_width',500),('image_height',300),('image_width',1496.0),
    ('badge_box',[425,577,435,587]),('badge_box',[425,577,467,619.0]),
    ('ui_layout_id','other-layout'),('verified_from','')])
def test_invalid_mask_calibration_is_rejected(tmp_path,field,value):
    c,_,_,_,store,_=build(tmp_path)
    try:
        c.profile.values['grind_only']['target_badge_continuity'][field]=value
        with pytest.raises(ValueError,match='target_badge_continuity'):settings(c.profile)
    finally:store.close()


def test_calibration_cannot_silently_follow_new_layout_or_roi(tmp_path):
    c,_,_,_,store,_=build(tmp_path)
    try:
        c.profile.values['calibration']['ui_layout']['image_width']=1500
        with pytest.raises(ValueError,match='target_badge_continuity'):settings(c.profile)
        c.profile.values['calibration']['ui_layout']['image_width']=1496
        c.profile.values['grind_only']['target_level_box']=[424,577,466,619]
        with pytest.raises(ValueError,match='target_badge_continuity'):settings(c.profile)
    finally:store.close()


@pytest.mark.parametrize('change',['level','skull','covered','absent','occlusion','player','corner'])
def test_actual_guard_uses_whole_glyph_and_preserves_own_badge_proof(tmp_path,change):
    async def run():
        c,w,_,b,store,e=build(tmp_path)
        try:
            source=w.capture();expected=await c.target_proposal(source)
            guarded=await c.guard(source,expected,exact_level=1)
            w.change=change
            result=await guarded(source)
            assert result.approved==(change=='corner')
            assert b.events==[] and c.cycle.input_generation==0
            assert result.evidence['fresh_frame_id']!=source.frame_id
            assert len(result.evidence['fresh_hash'])==64
            assert result.evidence['target_badge_mode']=='opaque_interior_ellipse'
            if change=='corner':
                assert result.evidence['failed_predicates']==[]
                assert not same_patch(source.image_path,result.dispatch_frame.image_path,BADGE,badge=True)
            else:assert result.evidence['failed_predicates']==['player_badge' if change=='player' else 'target_badge']
        finally:await cleanup(e,store)
    asyncio.run(run())


@pytest.mark.parametrize('opt_in',[False,True])
def test_actual_controller_cast_corner_motion_only_with_explicit_calibration(tmp_path,opt_in):
    async def run():
        c,w,s,b,store,e=build(tmp_path,opt_in=opt_in,answers=['player_level_1','attack_mob_level_1']);e.arm()
        try:
            await baseline(c,w)
            async def move_world():w.change='corner'
            s.hook=move_world
            result=await c.process(w.capture())
            assert (result.status=='dispatched')==opt_in
            assert [event for event in b.events if event[0]=='text']==([('text','/cast [harm,nodead] Smite')] if opt_in else [])
            assert not c.hunt.credited_kills  # Continuity and a receipt establish no kill credit.
        finally:await cleanup(e,store)
    asyncio.run(run())


@pytest.mark.parametrize('change',['numeric','name','self','dead','elite','rare','ui','stale','hash','generation','stop','source','duplicate'])
def test_opt_in_does_not_remove_existing_guard_predicates(tmp_path,change):
    async def run():
        c,w,_,b,store,e=build(tmp_path)
        try:
            source=w.capture();expected=await c.target_proposal(source)
            if change=='stale':source=replace(source,captured_at=(datetime.now(timezone.utc)-timedelta(seconds=11)).isoformat())
            guarded=await c.guard(source,expected,exact_level=1)
            if change=='numeric':w.numeric=2
            elif change in {'name','self','dead','elite','rare','ui'}:
                original=w.ocr
                def changed_rows(path):
                    rows=original(path)
                    if change in {'name','self'}:rows[0]=row('Other Wolf' if change=='name' else 'Test Player',157,519,160,20)
                    elif change=='ui':rows.append(row('active text entry',100,900,150,15))
                    else:rows.append(row(change.title(),390,545,25,15))
                    return rows
                c.ocr=changed_rows
            elif change=='hash':
                with Image.open(source.image_path) as image:
                    image.putpixel((0,0),(255,0,0));image.save(source.image_path)
            elif change=='generation':c.cycle._input_generation+=1
            elif change=='stop':c.stop('offline stop')
            elif change=='source':c.capture=lambda:replace(w.capture(),source='offline-other-window')
            elif change=='duplicate':c.capture=lambda:source
            result=await guarded(source)
            assert not result.approved and result.evidence['failed_predicates']
            assert b.events==[]
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_mask_exact_coverage_and_mean_max_thresholds(tmp_path):
    c,w,_,_,store,_=build(tmp_path)
    try:
        source=w.capture();fresh=w.capture()
        mask=[(x,y) for y in range(42) for x in range(42) if ((x-20.5)/15.5)**2+((y-20.5)/15.5)**2<=1]
        assert len(mask)==740
        with Image.open(fresh.image_path) as image:
            pixel=image.getpixel((446,584));image.putpixel((446,584),tuple(v+13 for v in pixel));image.save(fresh.image_path)
        assert not same_target_badge(source.image_path,fresh.image_path,BADGE,CALIBRATION)
        # Mean limit remains independent of the per-channel maximum.
        with Image.open(source.image_path) as image:
            for x,y in mask:
                pixel=image.getpixel((425+x,577+y));image.putpixel((425+x,577+y),tuple(v+1 if v<255 else v-1 for v in pixel))
            image.save(fresh.image_path)
        assert not same_target_badge(source.image_path,fresh.image_path,BADGE,CALIBRATION)
    finally:store.close()

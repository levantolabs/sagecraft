"""Bounded same-frame identity sensing; all OCR, gameplay and captures are fake."""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from sage_wow.agent.grind_observation import read_player_identity
from sage_wow.perception.ocr import TextObservation
from test_grind_level3_recovery import RecoveryRig
from test_grind_travel_acceptance import TravelRig
from test_grind_travel_recovery import events


def alternate_row(text='Test Player', y=15, confidence=.99):
    return TextObservation(text, confidence, {'x':120,'y':y,'width':270,'height':36})


def overlapping_ocr(r, *, answers=None, hook=None):
    original=r.c.ocr
    calls=[]
    capture=r.world.capture
    def gold_capture():
        frame=capture()
        if not hasattr(r,'hud') or r.hud:
            with Image.open(frame.image_path) as image:
                draw=ImageDraw.Draw(image)
                draw.rectangle((40,4,135,20),fill='#263746')
                draw.text((40,5),'Test Player',fill=(240,220,60))
                # A second gold line lets duplicate-identity negative evidence
                # remain physically supported in the transformed fixture.
                if answers and len(answers)>1:draw.text((40,30),'Test Player',fill=(240,220,60))
                image.save(frame.image_path)
        return frame
    r.world.capture=r.c.capture=gold_capture
    def ocr(path):
        if Path(path).name=='player-identity-warm.png':
            calls.append(str(path))
            with Image.open(path) as image:
                assert image.mode=='L' and image.size==(450,270)
            if hook:hook()
            return answers if answers is not None else [alternate_row()]
        return [replace(row,text='Test Play Otherlabel') if row.text=='Test Player' else row
            for row in original(path)]
    r.c.ocr=ocr
    return calls


def test_level_uses_full_alternate_name_without_rewriting_raw_ocr(tmp_path):
    async def run():
        r=RecoveryRig(tmp_path/'level')
        try:
            await r.start();r.p.values['character'].update(name='Test',surname='Player')
            calls=overlapping_ocr(r)
            await r.choose('player_level_1',level_due=True)
            assert len(calls)==1 and not r.c.identity_mismatch_frames
            proof=r.events('grind_player_identity_alternate')[-1]['proof']
            assert proof['verified'] and proof['rows'][0]['text']=='Test Player'
            assert proof['rows'][0]['bounds']['x']==40
            assert proof['alternate']['original_proof']['source_rows'][0]['text']=='Test Play Otherlabel'
            assert not r.physical() and not r.c.stopped
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('answer',[
    [alternate_row('Other Player')], [alternate_row('Test Other')],
    [alternate_row('Test')], [alternate_row('Test Player Impostor')],
    [alternate_row('Not Test Player')], [alternate_row('Test Player',confidence=.5)],
    [alternate_row(),alternate_row(y=90)], [],
])
def test_alternate_identity_never_accepts_partial_wrong_or_ambiguous_names(tmp_path,answer):
    async def run():
        r=RecoveryRig(tmp_path/'negative')
        try:
            await r.start();r.p.values['character'].update(name='Test',surname='Player')
            calls=overlapping_ocr(r,answers=answer)
            for _ in range(3):await r.choose('player_level_1',level_due=True)
            assert len(calls)==3 and r.c.reason=='player_identity_or_level_inconsistent'
            assert not r.physical()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['epoch','generation','stop','layout','pixels','stale'])
def test_changed_authority_during_alternate_read_cannot_record_level(tmp_path,change):
    async def run():
        r=RecoveryRig(tmp_path/'authority')
        try:
            await r.start();last=r.c.level.latest_observation.copy()
            loop=asyncio.get_running_loop()
            frame=None
            def mutate():
                if change=='epoch':r.c.cycle.session_epoch='different-epoch'
                elif change=='generation':r.c.cycle._input_generation+=1
                elif change=='stop':loop.call_soon_threadsafe(r.c.stop,'offline stop')
                elif change=='layout':r.p.values['calibration']['ui_layout']['regions']['player_frame']=[0,0,151,90]
                elif change=='pixels':
                    with Image.open(frame.image_path) as image:
                        image.putpixel((1,1),(255,255,255));image.save(frame.image_path)
                else:r.c.fresh=lambda *args,**kwargs:False
            overlapping_ocr(r,hook=mutate)
            frame=r.world.capture()
            result=await r.choose('player_level_1',frame=frame,level_due=True)
            assert result.status=='grind_reobserve'
            assert r.c.level.latest_observation==last and not r.physical()
            assert r.events('grind_player_identity_alternate')[-1]['proof']['observation_authority_lost']
        finally:await r.close()
    asyncio.run(run())


def test_exact_raw_identity_needs_no_alternate_and_duplicate_raw_stays_rejected(tmp_path):
    async def run():
        r=RecoveryRig(tmp_path/'raw')
        try:
            await r.start();calls=[];original=r.c.ocr
            def ocr(path):
                if Path(path).name=='player-identity-warm.png':calls.append(str(path));raise AssertionError('unneeded alternate')
                return original(path)
            r.c.ocr=ocr
            await r.choose('player_level_1',level_due=True)
            frame=r.world.capture();rows=original(Path(frame.image_path));rows.append(replace(rows[0],bounds={**rows[0].bounds,'y':55}))
            proof=await read_player_identity(r.c,frame,rows,full_name=True)
            assert not proof['verified'] and not calls
        finally:await r.close()
    asyncio.run(run())


def test_hud_hide_restore_and_dispatch_share_bounded_identity_evidence(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();calls=overlapping_ocr(r)
            await r.action('probe_forward')
            assert r.c.observation_transaction['status']=='restored_verified'
            assert r.c.observation_transaction['source_visible_proof']['alternate']
            proofs=events(r,'grind_player_identity_alternate')
            assert len({p['frame_id'] for p in proofs})==len(proofs)==len(calls)
            assert all(p['proof']['verified'] for p in proofs)
            assert r.c.hunt.pending['purpose']=='travel' and not r.pressed
            assert r.hud and r.toggle_count==2
        finally:await r.close()
    asyncio.run(run())


def test_fixed_transform_keeps_gold_and_removes_green_without_name_based_crop(tmp_path):
    async def run():
        r=RecoveryRig(tmp_path/'pixels')
        try:
            await r.start();frame=r.world.capture()
            with Image.open(frame.image_path) as image:
                draw=ImageDraw.Draw(image)
                draw.rectangle((5,5,15,15),fill=(240,220,60))
                draw.rectangle((20,5,30,15),fill=(100,240,40))
                draw.rectangle((135,5,145,15),fill=(240,220,60))
                image.save(frame.image_path)
            original=r.c.ocr;raw=original(Path(frame.image_path));raw[0]=replace(raw[0],text='Merged unreadable label')
            def ocr(path):
                with Image.open(path) as image:
                    assert image.size==(450,270)
                    assert image.getpixel((30,30))==0
                    assert image.getpixel((75,30))==255
                    assert image.getpixel((420,30))==0  # Preserve full region, including possible suffix.
                return [replace(alternate_row(),bounds={'x':15,'y':15,'width':420,'height':36})]
            r.c.ocr=ocr
            assert (await read_player_identity(r.c,frame,raw,full_name=True))['verified']
        finally:await r.close()
    asyncio.run(run())

"""Two real invitation closures; fresh guards and post-input verification."""
import asyncio
from copy import deepcopy

import pytest
from PIL import Image

from sage_wow.agent.grind_ui import decline_control
from sage_wow.models import Frame
from sage_wow.perception.ocr import TextObservation
from test_grind_guild_invitation import retained_rows,attach_invitation
from test_grind_loot_integration import LootRig


@pytest.mark.parametrize('scale,dx,dy',[(1,0,0),(.8,20,10),(1.3,-25,18)])
def test_control_coordinates_come_from_current_original_rows(scale,dx,dy):
    frame=Frame.create('offline',round(1496*scale),round(967*scale))
    rows=[TextObservation(row.text,row.confidence,{k:round(v*scale)+(dx if k=='x' else dy if k=='y' else 0)
        for k,v in row.bounds.items()}) for row in retained_rows()]
    control=decline_control(frame,rows);b=rows[1].bounds
    assert control and control.point==(round(b['x']+b['width']/2),round(b['y']+b['height']/2))
    assert control.option=='decline_invitation' and len(control.anchors)==2


@pytest.mark.parametrize('bad',['duplicate','missing_join','low_confidence','passive_chat'])
def test_ambiguous_or_unpaired_controls_never_create_click(bad):
    rows=retained_rows()
    if bad=='duplicate':rows+=rows[1:]
    elif bad=='missing_join':rows=rows[1:]
    elif bad=='low_confidence':rows=[TextObservation(r.text,.74,r.bounds) for r in rows]
    else:rows=[TextObservation('Join Guild Decline Invitation',1.,{'x':5,'y':740,'width':200,'height':15})]
    assert decline_control(Frame.create('offline',1496,967),rows) is None


@pytest.mark.parametrize('action',['close_visible_ui','decline_invitation'])
def test_current_invitation_menu_has_real_closures_then_verifies_before_hunting(tmp_path,action):
    async def run():
        r=LootRig(tmp_path);r.backend.mouse_button=lambda *a:r.backend.events.append(('mouse',*a))
        try:
            await r.start();visible=attach_invitation(r);r.world.name=''
            h=r.c.hunt;h.selected_presence=False;h.encounter_ended=True
            h.cast_obligation='retained historical constraint'
            debt=deepcopy((h.cast_obligation,h.action_failures,h.credited_kills))
            await r.choose(action)
            call=r.sage.calls[-1]
            assert set(call['options'])=={'close_visible_ui','decline_invitation'}
            assert 'Dismiss the current guild invitation' in call['instructions']
            assert 'Current task: close' in call['prompt']
            assert h.pending['family']=='ui' and h.phase=='ui_recover'
            assert (h.cast_obligation,h.action_failures,h.credited_kills)==debt
            visible[0]=False
            await r.choose('world_normal_confirmed')
            assert not r.c.require_world and not h.pending and not r.casts()
            assert h.cast_obligation==debt[0] and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['disappear','move','dim','duplicate','focus','generation'])
def test_decline_rechecks_fresh_control_and_scope_before_input(tmp_path,change):
    async def run():
        r=LootRig(tmp_path);r.backend.mouse_button=lambda *a:r.backend.events.append(('mouse',*a))
        try:
            await r.start();visible=attach_invitation(r)
            dim=[False]
            if change=='dim':
                original_capture=r.c.capture
                def capture():
                    frame=original_capture()
                    if dim[0]:
                        with Image.open(frame.image_path) as image:
                            image.point(lambda x:x//3).save(frame.image_path)
                    return frame
                r.c.capture=capture
            async def mutate():
                if change=='disappear':visible[0]=False
                elif change=='focus':r.c.pause_focus()
                elif change=='generation':r.c.cycle._input_generation+=1
                elif change=='dim':
                    dim[0]=True
                else:
                    original=r.c.ocr
                    def ocr(path):
                        rows=original(path);out=[]
                        for row in rows:
                            if row.text=='Decline Invitation':
                                if change=='move':row=TextObservation(row.text,row.confidence,{**row.bounds,'x':row.bounds['x']+12})
                                else:out.append(row)
                            out.append(row)
                        return out
                    r.c.ocr=ocr
            r.sage.hook=mutate
            result=await r.choose('decline_invitation')
            assert result.status!='dispatched'
            assert not any(e[0]=='mouse' for e in r.backend.events) and not r.casts()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['down','up'])
def test_partial_decline_stops_without_claiming_closure(tmp_path,failure):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();attach_invitation(r)
            def partial(button,down):
                r.backend.events.append(('mouse',button,down))
                if down==(failure=='down'):raise RuntimeError('offline partial UI click')
            r.backend.mouse_button=partial
            async def noop():pass
            r.sage.hook=noop
            await r.choose('decline_invitation')
            assert r.c.stopped and r.c.reason=='partial_or_unknown_grind_input'
            assert not r.casts() and not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_exhausted_escape_keeps_independent_decline_allowance_without_reset(tmp_path):
    async def run():
        r=LootRig(tmp_path);r.backend.mouse_button=lambda *a:r.backend.events.append(('mouse',*a))
        try:
            await r.start();visible=attach_invitation(r);h=r.c.hunt
            for _ in range(2):
                await r.choose('close_visible_ui')
                await r.choose('blocking_ui_remains')
            escape_key=h.action_key('escape','ui')
            assert h.action_failures[escape_key]==2 and h.failures['ui']==2
            assert not h.input_effect_unverified
            await r.choose('decline_invitation')
            assert 'close_visible_ui' not in r.sage.calls[-1]['options']
            assert h.pending['action']=='decline_invitation' and not h.input_effect_unverified
            assert h.action_failures[escape_key]==2
            visible[0]=False;await r.choose('world_normal_confirmed')
            assert not h.pending and not r.c.require_world
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('abstention',[False,True])
def test_both_exhausted_closures_preserve_blocked_ui_without_world_input(tmp_path,abstention):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();attach_invitation(r);await r.choose('close_visible_ui')
            await r.choose('blocking_ui_remains');h=r.c.hunt
            if abstention:await r.choose(None)
            for action in ('escape','decline_invitation'):
                h.action_failures[h.action_key(action,'ui')]=2
            await r.choose('safe_hold')
            assert not {'close_visible_ui','decline_invitation','target_enemy','forward'} & r.sage.calls[-1]['options'].keys()
            assert h.input_effect_unverified and h.blocked['reason']=='blocked_ui'
            assert r.physical_keys()==[r.controls['escape']['keycode']] and not r.casts()
        finally:await r.close()
    asyncio.run(run())

"""Live-run retained corpse-point pixels, offline guards and no loot credit."""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image,ImageChops

from sage_wow.agent.cycle import ActionCandidate
from sage_wow.agent.grind_loot import _corpse_point_continuity
from sage_wow.agent.grind_only import same_patch
from test_grind_loot_integration import LootRig


FIXTURES=Path(__file__).parent/'fixtures'/'loot_sparkle_point'
RECORDS=json.loads((FIXTURES/'provenance.json').read_text())['records']
BOX=(0,0,56,40)


@pytest.mark.parametrize('record',RECORDS,ids=lambda record:f"retained-{record['pair']}")
def test_retained_sparkles_preserve_point_but_settling_body_does_not(record):
    paths=[]
    for fixture in record['fixtures']:
        path=FIXTURES/fixture['file'];paths.append(path)
        assert hashlib.sha256(path.read_bytes()).hexdigest()==fixture['sha256']
    assert not same_patch(*paths,BOX)  # Reproduce the original veto exactly.
    result=_corpse_point_continuity(*paths,BOX)
    assert result['approved']==record['expected']
    assert result==record['metrics']


@pytest.mark.parametrize('dx,dy',[(1,0),(0,1),(1,1),(3,2),(5,5)])
def test_retained_body_translation_cannot_be_mistaken_for_particle_noise(tmp_path,dx,dy):
    before=FIXTURES/'pair15-source.png';after=tmp_path/'moved.png'
    with Image.open(before) as image:ImageChops.offset(image,dx,dy).save(after)
    assert not _corpse_point_continuity(before,after,BOX)['approved']


@pytest.mark.parametrize('change',['removed','different_body','broad_change'])
def test_removed_or_substantially_changed_point_is_rejected(tmp_path,change):
    before=FIXTURES/'pair15-source.png';after=tmp_path/'changed.png'
    with Image.open(before) as image:image=image.convert('RGB')
    if change=='removed':image=Image.new('RGB',image.size,(40,60,75))
    elif change=='different_body':image=image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    else:image.paste((180,180,170),(20,10,36,30))
    image.save(after)
    assert not _corpse_point_continuity(before,after,BOX)['approved']


@pytest.mark.parametrize('fresh_fixture,approved',[('pair15-fresh.png',True),('shifted',False)])
def test_retained_point_runs_through_native_guard_without_death_or_loot_credit(tmp_path,monkeypatch,fresh_fixture,approved):
    from sage_wow.agent import grind_loot
    monkeypatch.setattr(grind_loot,'_corpse_choices',lambda *args:[ActionCandidate(
        'corpse_select_0','offline body point',{'type':'click','image_x':100,'image_y':250})])
    async def run():
        rig=LootRig(tmp_path)
        try:
            await rig.start();rig.death()
            rig.backend.mouse_button=lambda *args:rig.backend.events.append(('mouse',*args))
            original=rig.world.capture;state={'fresh':False}
            with Image.open(FIXTURES/'pair15-source.png') as image:before=image.convert('RGB')
            if fresh_fixture=='shifted':after=ImageChops.offset(before,1,0)
            else:
                with Image.open(FIXTURES/fresh_fixture) as image:after=image.convert('RGB')
            def capture():
                frame=original()
                with Image.open(frame.image_path) as image:scene=image.convert('RGB')
                scene.paste(after if state['fresh'] else before,(72,230));scene.save(frame.image_path)
                return frame
            rig.world.capture=rig.c.capture=capture
            async def advance_particles():state['fresh']=True
            rig.sage.hook=advance_particles
            result=await rig.loot('corpse_select_0')
            assert result.status==('dispatched' if approved else 'dispatch_guard_rejected')
            guards=[json.loads(row[0]) for row in rig.store.connection.execute(
                "SELECT payload_json FROM events WHERE event_type='dispatch_guard_checked'")]
            guard=next(item for item in guards if item['chosen']=='corpse_select_0')
            assert guard['approved']==approved
            assert guard['evidence']['corpse_point']['approved']==approved
            assert bool([e for e in rig.backend.events if e[0]=='mouse'])==approved
            assert rig.c.loot.data['attempts']==0 and rig.c.loot.data['confirmations']==0
            assert rig.c.loot.pending and not rig.c.hunt.credited_kills
            # A click and stable corpse-point pixels must never authorize F on
            # a subsequently selected living creature.
            rig.sage.hook=None;rig.world.health='green'
            await rig.loot(None)
            assert not {'loot_selected_corpse','interact_target'} & rig.sage.calls[-1]['options'].keys()
        finally:await rig.close()
    asyncio.run(run())

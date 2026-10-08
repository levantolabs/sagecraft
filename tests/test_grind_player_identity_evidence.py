"""Own level readings use isolated positive identity, never nearby world labels."""
import asyncio
from dataclasses import replace

import pytest

from sage_wow.perception.ocr import TextObservation
from test_grind_level3_recovery import RecoveryRig


def row(text,x=2,y=20,w=30,h=10,confidence=.99):
    return TextObservation(text,confidence,{'x':x,'y':y,'width':w,'height':h})


@pytest.mark.parametrize('extra',[
    row('<OTHER>'),row('Nearby Creature',x=130,w=100),
    row('Guild label',y=50),row('Another Player',x=1,y=1,w=30),
])
def test_unrelated_world_text_does_not_replace_positive_own_identity(tmp_path,extra):
    async def run():
        r=RecoveryRig(tmp_path/'identity-noise')
        try:
            r.p.values['character'].update(name='Test',surname='Player')
            await r.start();original=r.c.ocr
            r.c.ocr=lambda path:original(path)+[extra]
            for _ in range(3):
                await r.choose('player_level_1',level_due=True)
                assert not r.c.identity_mismatch_frames and not r.c.stopped
            assert r.c.level.last_confirmed_level==1
            assert not r.events('grind_player_identity_unconfirmed') and not r.physical()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('case',['wrong_name','wrong_surname','prefix','base_only','missing','low_confidence','duplicate','outside'])
def test_full_character_identity_remains_required_despite_a_matching_level(tmp_path,case):
    async def run():
        r=RecoveryRig(tmp_path/'identity-negative')
        try:
            r.p.values['character'].update(name='Test',surname='Player')
            await r.start();original=r.c.ocr
            def changed(path):
                rows=original(path);name=rows[0]
                if case=='wrong_name':rows[0]=replace(name,text='Other Player')
                elif case=='wrong_surname':rows[0]=replace(name,text='Test Other')
                elif case=='prefix':rows[0]=replace(name,text='Test Player Impostor')
                elif case=='base_only':rows[0]=replace(name,text='Test')
                elif case=='missing':rows.pop(0)
                elif case=='low_confidence':rows[0]=replace(name,confidence=.6)
                elif case=='duplicate':rows.append(row('Test Player',x=40,y=55,w=90))
                elif case=='outside':rows[0]=replace(name,bounds={'x':140,'y':5,'width':90,'height':15})
                rows.append(row('<WORLD>'))
                return rows
            r.c.ocr=changed
            for _ in range(3):await r.choose('player_level_1',level_due=True)
            assert r.c.stopped and r.c.reason=='player_identity_or_level_inconsistent'
            assert len(r.events('grind_player_identity_unconfirmed'))==3
            assert r.c.level.last_confirmed_level==1 and not r.physical()
        finally:await r.close()
    asyncio.run(run())

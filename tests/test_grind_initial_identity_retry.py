"""Offline startup identity ambiguity uses distinct frames, never prefix success."""
import asyncio
from test_grind_level3_recovery import RecoveryRig


def test_contaminated_initial_name_then_exact_identity_establishes_baseline(tmp_path):
    async def run():
        r=RecoveryRig(tmp_path/'startup')
        try:
            r.world.player_name='Test Player: Young Wolf'
            await r.start()
            assert not r.c.baseline and not r.c.stopped and not r.physical()
            assert r.c.level.last_confirmed_level is None
            assert len(r.c.identity_mismatch_frames)==1
            r.world.player_name='Test Player'
            await r.choose('player_level_1',level_due=True)
            assert r.c.baseline and not r.c.stopped and not r.physical()
            assert r.c.level.last_confirmed_level==1
            assert not r.c.identity_mismatch_frames
        finally:await r.close()
    asyncio.run(run())


def test_persistent_different_name_stops_after_three_distinct_frames_without_input(tmp_path):
    async def run():
        r=RecoveryRig(tmp_path/'wrong')
        try:
            r.world.player_name='Other Player'
            await r.start()
            assert not r.c.baseline and not r.c.stopped
            await r.choose('player_level_1',level_due=True)
            assert not r.c.stopped and len(r.c.identity_mismatch_frames)==2
            await r.choose('player_level_1',level_due=True)
            assert r.c.stopped and r.c.reason=='player_identity_or_level_inconsistent'
            assert len(r.c.identity_mismatch_frames)==3
            assert not r.c.baseline and not r.physical()
            assert not r.events('grind_baseline_verified')
            assert r.c.level.last_confirmed_level is None
        finally:await r.close()
    asyncio.run(run())


def test_repeated_mismatched_frame_does_not_consume_another_retry(tmp_path):
    async def run():
        r=RecoveryRig(tmp_path/'distinct')
        try:
            await r.choose('world_normal_confirmed')
            r.world.player_name='Test Player Impostor'
            source=r.world.capture()
            await r.choose('player_level_1',frame=source)
            calls=len(r.sage.calls)
            r.c.level.last_attempt_at=None;r.c.wait_until=0
            result=await r.c.process(source)
            assert result.status=='grind_reobserve'
            assert len(r.sage.calls)==calls
            assert len(r.c.identity_mismatch_frames)==1
            assert not r.c.baseline and not r.c.stopped and not r.physical()
        finally:await r.close()
    asyncio.run(run())


def test_later_ambiguous_identity_holds_combat_until_exact_fresh_read(tmp_path):
    async def run():
        r=RecoveryRig(tmp_path/'later')
        try:
            await r.start()
            assert r.c.baseline
            r.world.player_name='Test Player: Young Wolf'
            r.world.player_level=2
            await r.choose('player_level_2',level_due=True)
            assert not r.c.stopped and r.c.level.last_confirmed_level==1
            assert r.c.identity_mismatch_frames and not r.physical()
            r.c.hunt.phase='fight'
            r.world.player_name='Test Player'
            await r.choose('player_level_2')
            assert r.c.level.last_confirmed_level==2 and not r.c.stopped
            assert not r.c.identity_mismatch_frames and not r.physical()
        finally:await r.close()
    asyncio.run(run())


def test_later_persistent_wrong_identity_stops_before_gameplay(tmp_path):
    async def run():
        r=RecoveryRig(tmp_path/'later-wrong')
        try:
            await r.start()
            r.world.player_name='Other Player'
            for _ in range(3):await r.choose('player_level_1',level_due=True)
            assert r.c.stopped and r.c.reason=='player_identity_or_level_inconsistent'
            assert not r.physical()
            assert r.c.level.last_confirmed_level==1
        finally:await r.close()
    asyncio.run(run())

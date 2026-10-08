"""Synthetic client log rows and fake input only; no native capture or provider."""
import asyncio
from datetime import datetime
import sys

import pytest

from sage_wow.agent.grind_combat_log import CombatLogTail, parse_line
from test_grind_product_spec import Rig


NOW = datetime(2026, 10, 2, 14, 15, 10).timestamp()


def row(event='SPELL_DAMAGE', name='Test Player', guid='Creature-1-2-3', stamp='10/2 14:15:10.000'):
    return f'{stamp}  {event},Player-1,"{name}",0x511,0x0,{guid},"Ragged Young Wolf",0xa28,0x0,585,"Smite",2,17\n'


def test_parse_native_row_and_no_unrecognized_event():
    fact = parse_line(row())
    assert fact['source_name'] == 'Test Player'
    assert fact['dest_name'] == 'Ragged Young Wolf'
    assert fact['dest_guid'] == 'Creature-1-2-3'
    assert parse_line('bad') is None
    assert parse_line(row(event='IGNORED')) is None


def test_tail_starts_after_old_rows_and_handles_partial_line(tmp_path):
    path = tmp_path / 'combat.txt'
    path.write_text(row())
    reader = CombatLogTail(path, ['Test Player'], clock=lambda: NOW)
    assert reader.poll() == []
    with path.open('a') as stream:
        stream.write(row()[:-1])
    assert reader.poll() == []
    with path.open('a') as stream:
        stream.write('\n')
    facts = reader.poll()
    assert len(facts) == 1 and facts[0]['own_source']
    assert facts[0]['event'] == 'SPELL_DAMAGE'


def test_only_death_of_logged_own_guid_is_linked_and_old_flush_is_ignored(tmp_path):
    path = tmp_path / 'combat.txt'
    path.touch()
    reader = CombatLogTail(path, ['Test Player'], clock=lambda: NOW)
    with path.open('a') as stream:
        stream.write(row(name='Other Player'))
        stream.write(row(event='UNIT_DIED', name='nil'))
        stream.write(row(stamp='10/2 13:00:00.000'))
        stream.write(row())
        stream.write(row(event='UNIT_DIED', name='nil', guid='Creature-different'))
        stream.write(row(event='UNIT_DIED', name='nil'))
    facts = reader.poll()
    assert [fact['event'] for fact in facts] == ['SPELL_DAMAGE', 'UNIT_DIED']
    assert not facts[1]['own_source'] and facts[1]['recent_own_target']
    assert all('not corpse position' in fact['claim'] for fact in facts)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Rotation is detected by inode; Linux can reuse the inode of a deleted file. Live combat-log reading is macOS-only.')
def test_rotation_and_truncation_do_not_replay_old_encounter(tmp_path):
    path = tmp_path / 'combat.txt'
    path.write_text(row() * 3)
    reader = CombatLogTail(path, ['Test Player'], clock=lambda: NOW)
    path.write_text(row())
    assert reader.poll() == []
    path.unlink()
    path.write_text(row() * 2)
    assert reader.poll() == []


def test_expired_rows_do_not_stay_in_context(tmp_path):
    path = tmp_path / 'combat.txt'
    path.touch()
    now = [NOW]
    reader = CombatLogTail(path, ['Test Player'], clock=lambda: now[0])
    path.write_text(row())
    assert len(reader.poll()) == 1
    now[0] += 31
    assert reader.recent() == []


def test_delayed_native_flush_retains_death_without_making_it_current(tmp_path):
    path = tmp_path / 'combat.txt'
    path.touch()
    reader = CombatLogTail(path, ['Test Player'], clock=lambda: NOW+90, started_at=NOW-1)
    path.write_text(row()+row(event='UNIT_DIED', name='nil'))
    assert [fact['event'] for fact in reader.poll()] == ['SPELL_DAMAGE','UNIT_DIED']
    assert reader.recent() == []
    assert len(reader.recent(max_age=300)) == 2
    assert reader.recent(max_age=300)[1]['age_seconds_at_read'] == 90


def test_late_flush_before_session_never_becomes_corpse_evidence(tmp_path):
    path = tmp_path / 'combat.txt'
    path.touch()
    reader = CombatLogTail(path, ['Test Player'], clock=lambda: NOW+90, started_at=NOW+30)
    path.write_text(row()+row(event='UNIT_DIED', name='nil'))
    assert reader.poll() == []
    assert reader.recent(max_age=300) == []


def test_fresh_incoming_damage_is_attributed_without_own_damage_claim(tmp_path):
    path = tmp_path / 'combat.txt'
    path.touch()
    reader = CombatLogTail(path, ['Test Player'], clock=lambda: NOW)
    path.write_text('10/2 14:15:10.000  SWING_DAMAGE,Creature-1,"Ragged Young Wolf",0xa28,0x0,Player-1,"Test Player",0x511,0x0,2\n')
    facts = reader.poll()
    assert len(facts) == 1 and facts[0]['incoming_to_player']
    assert not facts[0]['own_source'] and not facts[0]['recent_own_target']


def test_logging_setup_runs_once_after_baseline_and_then_hunting_continues(tmp_path):
    async def scenario():
        rig = Rig(tmp_path)
        try:
            await rig.start()
            rig.c.config.update(combat_log_enabled=True, combat_log_path=str(tmp_path / 'combat.txt'))
            await rig.choose('enable_client_combat_log')
            assert ('text', '/run LoggingCombat(true)') in rig.backend.events
            assert rig.c.combat_log_setup_attempted
            await rig.choose('attack_mob_level_1')
            assert sum(event == ('text', '/run LoggingCombat(true)') for event in rig.backend.events) == 1
            assert not rig.c.hunt.credited_kills
        finally:
            await rig.close()
    asyncio.run(scenario())

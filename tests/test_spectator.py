"""Spectator projection uses synthetic files only; no native or network APIs."""
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

import pytest

from sage_wow.dashboard.spectator import SpectatorFeed


class Run:
    def __init__(self, root, session='trial', start=1000):
        self.directory = root / session
        self.directory.mkdir()
        self.session = session
        self.database = self.directory / 'events.sqlite3'
        with sqlite3.connect(self.database) as c:
            c.execute('CREATE TABLE events(event_id TEXT PRIMARY KEY, occurred_at TEXT, event_type TEXT, payload_json TEXT)')
        self.count = 0
        self.add('session_started', {'session_id': session, 'mode': 'grind_only'}, start)
        self.status(start)

    def add(self, kind, payload, at=1000):
        self.count += 1
        event_id = f'{self.session}-event-{self.count}'
        with sqlite3.connect(self.database) as c:
            c.execute('INSERT INTO events VALUES(?,?,?,?)', (event_id, datetime.fromtimestamp(at, timezone.utc).isoformat(), kind, json.dumps(payload)))
        return event_id

    def status(self, now, state='PLAYING', **extra):
        (self.directory / 'status.json').write_text(json.dumps({'session_id': self.session, 'state': state, 'updated_at': now, 'player_level': 3, 'goal_level': 5, **extra}))

    def point(self, path, window=77):
        path.write_text(json.dumps({'version': 1, 'directory': str(self.directory), 'session_id': self.session, 'window_id': window}))

    def request(self, request='r1', at=1001):
        return self.add('sage_request_started', {'request_id': request}, at)

    def decision(self, request='r1', at=1002, choice='target_enemy'):
        return self.add('sage_decision', {'request_id': request, 'chosen': choice, 'request_duration_ms': 1000.25, 'server_latency_ms': 601.5}, at)


@pytest.fixture
def rig(tmp_path):
    run = Run(tmp_path)
    pointer = tmp_path / 'active.json'
    run.point(pointer)
    feed = SpectatorFeed(pointer)
    yield run, pointer, feed
    feed.close()


def test_hydration_keeps_latest_facts_but_never_replays_reactions(rig):
    run, _, feed = rig
    run.request(at=1001)
    decision = run.decision(at=1002)
    run.add('grind_progress_recovery_requested', {'signature': 'old'}, 1002)
    run.add('grind_client_combat_log', {'event': 'PARTY_KILL', 'own_source': True, 'occurred_at': 1002, 'source_guid': 'player', 'dest_guid': 'wolf'}, 1002)
    run.status(1002)
    data = feed.poll(1002)
    assert data['choice_label'] == 'TARGET ENEMY'
    assert data['decision_id'] == decision
    assert (data['response_ms'], data['server_ms']) == (1000.25, 601.5)
    assert data['native_own_kills'] == 1
    assert data['strict_credited_kills'] == 0
    assert data['reaction'] is None
    revision = data['source_revision']
    assert feed.poll(1003)['source_revision'] == revision
    assert feed.poll(1003)['reaction'] is None


def test_request_response_opener_and_receipt_have_distinct_attribution(rig):
    run, _, feed = rig
    feed.poll(1000)
    run.request(at=1001)
    run.status(1001)
    data = feed.poll(1001.5)
    assert (data['phase'], data['actor'], data['request_elapsed']) == ('thinking', 'sage', .5)
    run.decision(at=1002)
    run.status(1002)
    assert feed.poll(1002)['phase'] == 'decision'
    run.add('physical_input_attempted', {'request_id': 'r1', 'receipt_id': 'tab'}, 1002.1)
    data = feed.poll(1002.1)
    assert (data['phase'], data['actor'], data['authorization_type']) == ('executing', 'harness', 'sage_decision')
    run.add('execution_receipt', {'request_id': 'r1', 'receipt_id': 'tab', 'chosen_option': 'target_enemy', 'authorization_type': 'sage_decision', 'dispatched': True, 'completed': True}, 1002.2)
    assert feed.poll(1002.2)['phase'] == 'verifying'
    run.add('grind_target_opener_armed', {'request_id': 'r1', 'target_receipt_id': 'tab'}, 1002.3)
    run.add('physical_input_attempted', {'request_id': 'r1', 'receipt_id': 'smite'}, 1002.5)
    data = feed.poll(1002.6)
    assert data['execution_label'] == 'Sending Smite opener'
    assert data['authorization_type'] == 'user_authorized_target_opener'
    assert data['choice'] == 'target_enemy'
    assert data['request_elapsed'] is None
    assert data['response_ms'] == 1000.25
    run.add('execution_receipt', {'request_id': 'r1', 'receipt_id': 'smite', 'chosen_option': 'target_opening_smite', 'authorization_type': 'user_authorized_target_opener', 'dispatched': True, 'completed': True}, 1003)
    run.add('grind_target_opener_result', {'outcome': 'dispatched', 'target_receipt_id': 'tab'}, 1003.1)
    assert feed.poll(1003.1)['execution_label'] == 'Smite opener sent · checking result'
    assert feed.poll(1003.2)['native_own_kills'] == 0


def test_late_response_and_receipt_cannot_overwrite_current_request(rig):
    run, _, feed = rig
    feed.poll(1000)
    run.request('old', 1001)
    run.request('new', 1002)
    run.decision('old', 1002.1)
    run.add('execution_receipt', {'request_id': 'old', 'chosen_option': 'attack_mob_level_1', 'completed': True, 'dispatched': True}, 1002.2)
    run.status(1002)
    data = feed.poll(1003)
    assert data['request_id'] == 'new'
    assert data['phase'] == 'thinking'
    assert data['choice'] is None
    assert data['request_elapsed'] == 1


@pytest.mark.parametrize('kind', ['sage_request_failed', 'sage_response_discarded'])
def test_request_terminal_stops_clock_without_inventing_choice(rig, kind):
    run, _, feed = rig
    feed.poll(1000)
    run.request()
    run.add(kind, {'request_id': 'r1', 'reason': 'timeout'}, 1002)
    run.status(1002)
    data = feed.poll(1002)
    assert data['phase'] == 'observing'
    assert data['request_elapsed'] is None
    assert data['choice'] is None


def test_pause_stop_and_stale_never_keep_working_or_react(rig):
    run, _, feed = rig
    feed.poll(1000)
    run.request()
    run.status(1001)
    assert feed.poll(1001.2)['request_elapsed'] == pytest.approx(.2)
    data = feed.poll(1007)
    assert (data['state'], data['phase'], data['actor']) == ('STALE', 'stale', 'system')
    assert data['request_elapsed'] is None
    run.add('grind_paused', {'state': 'PAUSED_IDENTITY'}, 1008)
    run.status(1008, 'PAUSED_IDENTITY')
    assert feed.poll(1008)['phase'] == 'paused'
    run.decision('r1', 1008.1)
    assert feed.poll(1008.1)['decision_id'] is None
    run.add('grind_resumed', {'session_id': run.session}, 1009)
    run.status(1009)
    data = feed.poll(1009)
    assert data['phase'] == 'observing'
    assert data['request_elapsed'] is None
    run.add('session_stopped', {'reason': 'operator_stop'}, 1010)
    # A fresh-but-lagging status cannot override the terminal database event.
    run.status(1010)
    data = feed.poll(1010)
    assert data['state'] == 'STOPPED'
    assert data['reaction'] is None


def test_navigation_wait_is_idle_but_current_provider_work_wins(rig):
    run, _, feed = rig
    run.status(1001, navigation_recovery={'state': 'waiting', 'reason': 'no_action_selected'})
    data = feed.poll(1001)
    assert (data['state'], data['reason'], data['actor']) == (
        'BLOCKED', 'navigation_no_action_selected', 'system')
    assert data['request_elapsed'] is None and data['reaction'] is None
    # A new safety task may reach the database before the next status write.
    run.request('heal', 1002)
    assert feed.poll(1002)['phase'] == 'thinking'
    run.decision('heal', 1003, choice='heal_self')
    assert feed.poll(1003)['state'] == 'PLAYING'
    run.status(1003, navigation_recovery=None)
    assert feed.poll(1003)['reason'] is None


def test_fresh_navigation_wait_retires_display_of_an_old_unclosed_request(rig):
    run, _, feed = rig
    run.request('expired-recovery', 1001)
    run.status(1001)
    assert feed.poll(1001)['phase'] == 'thinking'
    run.status(1002, navigation_recovery={'state': 'waiting', 'reason': 'no_action_selected'})
    data = feed.poll(1002)
    assert data['reason'] == 'navigation_no_action_selected'
    assert data['phase'] == 'blocked' and data['request_elapsed'] is None


@pytest.mark.parametrize('state,reason,projected', [
    ('PAUSED_FOCUS', 'clean_focus_lost', 'PAUSED'),
    ('PAUSED_OPERATOR', 'operator_pause', 'PAUSED'),
    ('PAUSED_IDENTITY', 'application_identity_unavailable', 'PAUSED'),
    ('STOPPED', 'operator_stop', 'STOPPED'),
    ('BLOCKED', 'blocked_ui', 'BLOCKED'),
])
def test_navigation_debt_never_masks_machine_pause_or_other_block(rig, state, reason, projected):
    run, _, feed = rig
    run.status(1001, state, reason=reason, navigation_recovery={'state': 'waiting'})
    data = feed.poll(1001)
    assert (data['state'], data['reason']) == (projected, reason)


def test_source_switch_clears_clocks_counts_and_old_reactions(rig, tmp_path):
    run, pointer, feed = rig
    feed.poll(1000)
    run.request()
    first = feed.poll(1001)
    other = Run(tmp_path, 'other', 1002)
    other.point(pointer, window=88)
    data = feed.poll(1002)
    assert data['session_id'] == 'other'
    assert data['window_id'] == 88
    assert data['source_revision'] > first['source_revision']
    assert data['request_elapsed'] is None
    assert data['choice'] is None
    assert data['reaction'] is None


@pytest.mark.parametrize('bad', ['{', 'null', '{"version":1,"directory":"missing","window_id":77}', 'missing'])
def test_bad_or_disappearing_pointer_never_falls_back(rig, bad):
    run, pointer, original = rig
    original.close()
    feed = SpectatorFeed(pointer, run.directory, 77)
    try:
        feed.poll(1000)
        if bad == 'missing':
            pointer.unlink()
        else:
            pointer.write_text(bad)
        data = feed.poll(1001)
        assert data['state'] == 'UNAVAILABLE'
        assert data['window_id'] is None
        assert data['choice'] is None
        assert data['request_elapsed'] is None
    finally:
        feed.close()


def test_missing_source_creates_nothing(tmp_path):
    feed = SpectatorFeed(tmp_path / 'missing.json', tmp_path / 'absent', 77)
    assert feed.poll(1000)['state'] == 'UNAVAILABLE'
    assert list(tmp_path.iterdir()) == []


def test_native_kills_have_real_run_bounds_dedupe_and_delayed_attribution(rig):
    run, _, feed = rig
    feed.poll(1000)
    for source_at, own in [(999, True), (1001, False), (1200, True)]:
        run.add('grind_client_combat_log', {'event': 'PARTY_KILL', 'own_source': own, 'occurred_at': source_at, 'dest_guid': str(source_at)}, 1100)
    payload = {'event': 'PARTY_KILL', 'own_source': True, 'occurred_at': 1002, 'source_guid': 'player', 'dest_guid': 'wolf'}
    event = run.add('grind_client_combat_log', payload, 1100)
    run.add('grind_client_combat_log', payload, 1100.1)
    run.add('grind_death_observed', {'kill_attribution_known': False}, 1100.2)
    run.status(1100)
    data = feed.poll(1100.5)
    assert data['native_own_kills'] == 1
    assert data['strict_credited_kills'] == 0
    assert data['reaction'] == {'id': event, 'kind': 'kill', 'caption': 'EARLIER KILL CONFIRMED', 'at': 1100, 'source_at': 1002, 'late': True}
    assert feed.poll(1101)['reaction']['id'] == event
    assert feed.poll(1103)['reaction'] is None


def test_confirmation_reactions_do_not_fire_on_baseline_or_unconfirmed_level(rig):
    run, _, feed = rig
    feed.poll(1000)
    for level, change in [(3, 'baseline_confirmed_two_fresh_frames'), (4, 'higher_displayed_level_needs_confirmation')]:
        run.add('grind_player_level_observed', {'level': level, 'level_change_vs_same_session_observation': change}, 1001)
    run.status(1001)
    assert feed.poll(1001)['reaction'] is None
    event = run.add('grind_player_level_observed', {'level': 4, 'level_change_vs_same_session_observation': 'higher_displayed_level_confirmed_two_fresh_frames'}, 1002)
    run.status(1002, player_level=4)
    data = feed.poll(1002)
    assert data['current_level'] == 4
    assert data['reaction']['id'] == event
    assert data['reaction']['caption'] == 'LEVEL 4 CONFIRMED'


def test_confusion_reaction_dedupes_signature_and_expires(rig):
    run, _, feed = rig
    feed.poll(1000)
    event = run.add('grind_progress_recovery_requested', {'signature': 'navigation-loop'}, 1001)
    run.status(1001)
    assert feed.poll(1001)['reaction']['id'] == event
    run.add('grind_progress_recovery_requested', {'signature': 'navigation-loop'}, 1020)
    run.status(1020)
    assert feed.poll(1020)['reaction'] is None
    run.add('grind_progress_recovery_requested', {'signature': 'new-loop'}, 1021)
    assert feed.poll(1021)['reaction']['kind'] == 'confusion'


def test_status_identity_and_corruption_fail_closed(rig):
    run, _, feed = rig
    feed.poll(1000)
    run.status(1001, session_id='different')
    assert feed.poll(1001)['state'] == 'UNAVAILABLE'
    (run.directory / 'status.json').write_text('null')
    assert feed.poll(1001)['window_id'] is None


def test_blocked_status_never_displays_pending_execution_as_working(rig):
    run, _, feed = rig
    feed.poll(1000)
    run.request()
    run.status(1001, 'BLOCKED', blocked={'reason': 'loot unresolved'})
    data = feed.poll(1001.5)
    assert (data['state'], data['phase'], data['actor']) == ('BLOCKED', 'blocked', 'system')
    assert data['request_elapsed'] is None
    assert data['reaction'] is None
    assert data['reason'] == 'loot unresolved'


def test_observation_response_is_not_labeled_as_a_gameplay_command(rig):
    run, _, feed = rig
    feed.poll(1000)
    run.request()
    run.add('sage_decision', {'request_id': 'r1', 'chosen': 'player_level_3', 'selected_binding': {'type': 'observe_only'}}, 1002)
    run.status(1002)
    data = feed.poll(1002)
    assert data['choice_kind'] == 'observation'
    assert data['choice_label'] == 'SEES LEVEL 3'
    run.request('r2', 1003)
    run.add('sage_decision', {'request_id': 'r2', 'chosen': 'forward', 'selected_binding': {'type': 'keypress'}}, 1004)
    run.status(1004)
    assert feed.poll(1004)['choice_kind'] == 'action'


def test_unrelated_opener_result_does_not_change_newer_phase(rig):
    run, _, feed = rig
    feed.poll(1000)
    run.request()
    run.decision()
    run.add('grind_target_opener_armed', {'request_id': 'r1', 'target_receipt_id': 'current-tab'}, 1002.1)
    run.add('grind_target_opener_result', {'target_receipt_id': 'old-tab', 'outcome': 'dispatched'}, 1002.2)
    run.status(1002)
    data = feed.poll(1002.3)
    assert data['phase'] == 'executing'
    assert data['execution_label'] == 'Checking target for Smite opener'


def test_same_path_database_replacement_rehydrates_without_old_clock(rig, tmp_path):
    run, _, feed = rig
    run.request()
    run.status(1001)
    old = feed.poll(1001)
    replacement = Run(tmp_path, 'replacement', 1000)
    # Preserve the source identity but replace the database inode.
    with sqlite3.connect(replacement.database) as c:
        c.execute('UPDATE events SET payload_json=?', (json.dumps({'session_id': run.session}),))
    replacement.database.replace(run.database)
    data = feed.poll(1001)
    assert data['source_revision'] > old['source_revision']
    assert data['request_elapsed'] is None
    assert data['choice'] is None


def test_projector_is_read_only_and_exposes_only_matching_manifest_profile(rig):
    run, _, feed = rig
    manifest = {'session_id': run.session, 'profile_path': '/profiles/live.yaml', 'profile_sha256': 'abc'}
    (run.directory / 'launch-manifest.json').write_text(json.dumps(manifest))
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run.directory.iterdir()}
    data = feed.poll(1000)
    assert data['profile_path'] == '/profiles/live.yaml'
    assert data['profile_sha256'] == 'abc'
    assert feed._conn.execute('PRAGMA query_only').fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        feed._conn.execute("DELETE FROM events")
    feed.close()
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run.directory.iterdir()}
    assert before == after

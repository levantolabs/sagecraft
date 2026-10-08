import json

import pytest

from sage_wow.dashboard.session_source import overlay_snapshot, resolve_session_source, write_session_source
from sage_wow.models import Event
from sage_wow.storage import EventStore


def session(path, choice, *, state='STOPPED', updated=100):
    path.mkdir(parents=True)
    store = EventStore(path/'events.sqlite3')
    store.append(Event.create('session_started', {}))
    store.append(Event.create('sage_decision', {'chosen': choice}))
    store.close()
    (path/'status.json').write_text(json.dumps({'state': state, 'updated_at': updated}))
    return path


def test_running_hud_follows_new_trial_without_restarting_and_keeps_stopped_result(tmp_path):
    old = session(tmp_path/'setup08', 'old_choice')
    new = session(tmp_path/'setup16', 'inspect_npc_approach')
    pointer = tmp_path/'active.json'
    data, window = overlay_snapshot(old, 1, path=pointer)
    assert (data['choice'], window) == ('old_choice', 1)
    write_session_source(new, 77, 'setup16', path=pointer)
    data, window = overlay_snapshot(old, 1, path=pointer)
    assert (data['choice'], window, data['session_source'], data['state']) == (
        'inspect_npc_approach', 77, 'setup16', 'STOPPED')
    newest = session(tmp_path/'setup17', 'interact_quest_giver', state='PLAYING')
    write_session_source(newest, 78, 'setup17', path=pointer)
    data, window = overlay_snapshot(old, 1, path=pointer, now=102)
    assert (data['choice'], window, data['state']) == ('interact_quest_giver', 78, 'PLAYING')
    pinned, window = overlay_snapshot(old, 1, follow=False, path=pointer)
    assert (pinned['choice'], window) == ('old_choice', 1)


def test_broken_session_pointer_does_not_display_old_decisions_as_current(tmp_path):
    old = session(tmp_path/'old', 'old_choice')
    pointer = tmp_path/'active.json'
    for raw in ('{', 'null', '{"version": 1, "directory": "missing", "window_id": 77}'):
        pointer.write_text(raw)
        assert resolve_session_source(old, 1, path=pointer)['directory'] is None
        with pytest.raises(ValueError, match='Session source unavailable'):
            overlay_snapshot(old, 1, path=pointer)


def test_crashed_runner_does_not_look_like_live_thinking(tmp_path):
    data_dir = session(tmp_path/'run', 'forward', state='PLAYING')
    store = EventStore(data_dir/'events.sqlite3')
    store.append(Event.create('sage_request_started', {'request_id': 'unfinished'}))
    store.close()
    data, _ = overlay_snapshot(data_dir, 1, path=tmp_path/'absent.json', now=110)
    assert data['state'] == 'STALE'
    assert data['request_age'] is None


def test_cli_can_pin_historical_overlay():
    from sage_wow.app import build_parser
    assert build_parser().parse_args(['overlay']).pin_session is False
    assert build_parser().parse_args(['overlay', '--pin-session']).pin_session is True

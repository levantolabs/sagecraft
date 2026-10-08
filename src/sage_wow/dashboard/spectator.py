"""Read-only, incremental facts for the spectator HUD. Never owns game input.

``poll`` returns current state, not a scripted animation sequence. Reactions are
ephemeral editorial labels attached to source events; hydration never emits them.
All public timestamps are UTC epoch seconds (``decision_at_iso`` is also offered).
"""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _stamp(value):
    if (number := _number(value)) is not None:
        return number
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def _text(value, limit=160):
    return value[:limit] if isinstance(value, str) else None


def _object(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('Expected an object')
    return value


def _level(value):
    return value if type(value) is int and 1 <= value <= 100 else None


def choice_label(choice):
    if not choice:
        return 'NO SAFE CHOICE'
    labels = {
        'target_enemy': 'TARGET ENEMY', 'target_opening_smite': 'SMITE OPENER',
        'motion_no_useful_effect': 'NO USEFUL MOVEMENT', 'no_selected_frame': 'NO TARGET',
        'detour_strafe_left': 'DETOUR LEFT', 'detour_strafe_right': 'DETOUR RIGHT',
        'world_normal_confirmed': 'WORLD READY', 'damaged_alive': 'DAMAGE OBSERVED',
        'dead_after_selection_cleared': 'TARGET DOWN', 'change_search_strategy': 'CHANGE SEARCH',
        'blocked_still_unresolved': 'STILL BLOCKED', 'loot_verified': 'LOOT CHECKED',
    }
    if choice.startswith('attack_mob_level_'):
        return 'ATTACK'
    if choice.startswith('player_level_'):
        return 'SEES LEVEL ' + choice.removeprefix('player_level_')[:3]
    if choice.startswith('choose_area:'):
        return 'CHOOSE HUNT AREA'
    return labels.get(choice, choice.replace('_', ' ').upper())[:64]


class SpectatorFeed:
    """Follow a session pointer with a persistent, read-only SQLite connection.

    A missing pointer may use the explicit fallback only before any pointer has
    been observed. An invalid/disappearing pointer never revives an older run.
    No directory, database, status file, or game process is created or modified.
    """

    def __init__(self, pointer_path, fallback_directory=None, window_id=None):
        self.pointer_path = Path(pointer_path)
        self.fallback_directory = Path(fallback_directory).resolve() if fallback_directory else None
        self.fallback_window_id = window_id
        self._pointer_seen = False
        self._identity = None
        self._conn = None
        self._revision = 0
        self._reset()

    def _reset(self):
        self._cursor = 0
        self._session_id = None
        self._start_at = None
        self._stop_at = None
        self._event_paused = False
        self._pending = None
        self._decision_request = None
        self._choice = None
        self._choice_kind = 'unknown'
        self._decision_id = None
        self._decision_at = None
        self._response_ms = None
        self._server_ms = None
        self._phase = 'observing'
        self._execution_label = 'Observing the game'
        self._receipt_id = None
        self._authorization_type = None
        self._opener_request = None
        self._opener_target_receipt = None
        self._outcome = None
        self._level = None
        self._goal = None
        self._native_kills = set()
        self._credited_kills = set()
        self._reaction = None
        self._reaction_keys = set()
        self._reaction_at = None
        self._runtime_phase = None
        self._profile_path = None
        self._profile_sha256 = None

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _source(self):
        if self.pointer_path.exists():
            self._pointer_seen = True
            p = _object(self.pointer_path)
            directory = Path(p['directory'])
            window = p['window_id']
            session = _text(p.get('session_id'))
            if p.get('version') != 1 or not directory.is_absolute() or type(window) is not int or window <= 0:
                raise ValueError('Invalid session pointer')
            if not session:
                raise ValueError('Missing session identity')
            return directory.resolve(), window, session
        if self._pointer_seen or self.fallback_directory is None:
            raise ValueError('Session pointer unavailable')
        if type(self.fallback_window_id) is not int or self.fallback_window_id <= 0:
            raise ValueError('Window identity unavailable')
        return self.fallback_directory, self.fallback_window_id, None

    def _connect(self, directory, window, session):
        database = directory / 'events.sqlite3'
        st = database.stat()
        identity = (str(directory), window, session, st.st_dev, st.st_ino)
        if identity != self._identity or self._conn is None:
            self.close()
            self._reset()
            self._revision += 1
            self._identity = identity
            self._conn = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.05)
            self._conn.execute('PRAGMA query_only=ON')
            return True
        return False

    def _react(self, kind, caption, event_id, at, payload, hydrate, now, source_at=None):
        key = (kind, payload.get('signature') or event_id)
        if key in self._reaction_keys:
            return
        self._reaction_keys.add(key)
        if hydrate or now - at > 3 or at > now + 2:
            return
        if kind not in {'kill', 'level'} and self._reaction_at is not None and at - self._reaction_at < 10:
            return
        source_at = at if source_at is None else source_at
        self._reaction_at = at
        self._reaction = {'id': event_id, 'kind': kind, 'caption': caption, 'at': at,
                          'source_at': source_at, 'late': at - source_at > 5}

    def _consume(self, row, hydrate, now):
        rowid, event_id, raw_at, kind, raw = row
        self._cursor = rowid
        try:
            p = json.loads(raw)
        except (ValueError, TypeError):
            return
        at = _stamp(raw_at)
        if not isinstance(p, dict) or at is None:
            return
        request = _text(p.get('request_id'))
        if kind == 'session_started':
            self._reset()
            self._cursor = rowid
            self._session_id = _text(p.get('session_id'))
            self._start_at = at
            return
        if self._start_at is None or at < self._start_at:
            return
        if p.get('session_id') and p['session_id'] != self._session_id:
            return
        if kind == 'session_stopped':
            self._stop_at = at
            self._pending = None
            self._reaction = None
            self._outcome = _text(p.get('reason')) or 'Session stopped'
        elif kind in {'grind_paused', 'grind_focus_paused'}:
            self._event_paused = True
            self._pending = None
            self._reaction = None
            self._phase = 'observing'
        elif kind == 'grind_resumed':
            self._event_paused = False
            self._pending = None
            self._phase = 'observing'
            self._react('recovery', 'BACK IN THE GAME', event_id, at, p, hydrate, now)
        elif kind == 'sage_request_started' and request:
            self._pending = {'id': request, 'at': at}
            self._phase = 'thinking'
            self._opener_request = None
            self._opener_target_receipt = None
        elif kind == 'sage_decision':
            # A late response must not clear or replace a newer in-flight request.
            if not request or self._pending is None or request != self._pending['id']:
                return
            self._pending = None
            self._decision_request = request
            self._choice = _text(p.get('chosen'))
            binding = p.get('selected_binding')
            binding_type = binding.get('type') if isinstance(binding, dict) else None
            self._choice_kind = ('observation' if binding_type == 'observe_only' else
                                 'action' if binding_type else 'unknown')
            self._decision_id = event_id
            self._decision_at = at
            self._response_ms = _number(p.get('request_duration_ms'))
            self._server_ms = _number(p.get('server_latency_ms'))
            self._phase = 'decision' if self._choice else 'observing'
            self._execution_label = 'Choice received' if self._choice else 'No safe choice returned'
        elif kind in {'sage_request_failed', 'sage_response_discarded'}:
            if self._pending and request == self._pending['id']:
                self._pending = None
                self._phase = 'observing'
                self._execution_label = 'Request failed' if kind == 'sage_request_failed' else 'Response discarded'
        elif kind == 'grind_target_opener_armed':
            if request and request == self._decision_request:
                self._opener_request = request
                self._opener_target_receipt = _text(p.get('target_receipt_id'))
                self._phase = 'executing'
                self._execution_label = 'Checking target for Smite opener'
                self._authorization_type = 'user_authorized_target_opener'
        elif kind == 'physical_input_attempted':
            if request and request == self._decision_request and self._pending is None:
                self._receipt_id = _text(p.get('receipt_id'))
                self._phase = 'executing'
                opener = request == self._opener_request
                self._execution_label = 'Sending Smite opener' if opener else 'Sending ' + choice_label(self._choice)
                self._authorization_type = 'user_authorized_target_opener' if opener else 'sage_decision'
        elif kind == 'execution_receipt':
            if not request or request != self._decision_request or self._pending is not None:
                return
            self._receipt_id = _text(p.get('receipt_id'))
            self._authorization_type = _text(p.get('authorization_type'))
            label = choice_label(_text(p.get('chosen_option')))
            if p.get('dispatch_unknown') is True:
                self._execution_label = label + ' · dispatch unknown'
                self._phase = 'observing'
            elif p.get('possible_input') is True or p.get('dispatched') is True:
                self._execution_label = label + (' · input completed' if p.get('completed') is True else ' · partial input')
                self._phase = 'verifying'
            elif p.get('error') or p.get('completed') is False:
                self._execution_label = label + ' · input rejected'
                self._phase = 'observing'
            else:
                self._execution_label = 'Observation only'
                self._phase = 'observing'
        elif kind == 'grind_target_opener_result':
            if not self._opener_target_receipt or p.get('target_receipt_id') != self._opener_target_receipt:
                return
            self._opener_request = None
            self._opener_target_receipt = None
            if self._pending is None:
                self._phase = 'verifying' if p.get('outcome') == 'dispatched' else 'observing'
                self._execution_label = ('Smite opener sent · checking result' if p.get('outcome') == 'dispatched'
                                         else 'Opener returned to Sage')
        elif kind == 'grind_action_outcome' and p.get('purpose') != 'semantic_question':
            value = _text(p.get('outcome'))
            if value:
                self._outcome = value.replace('_', ' ')
            if self._pending is None:
                self._phase = 'observing'
        elif kind == 'grind_runtime_timing':
            self._runtime_phase = _text(p.get('phase'))
            # The composite phase contains both provider and motor work. Do not
            # fabricate a distinction that is absent from this timing sample.
            if self._pending is None and self._phase not in {'executing', 'verifying'}:
                if self._runtime_phase in {'capture', 'grind_ocr', 'grind_world_measurement', 'decision_image_composition', 'selected_target_factual_observer'}:
                    self._phase = 'observing'
                    self._execution_label = {
                        'capture': 'Capturing the game', 'grind_ocr': 'Reading the screen',
                        'grind_world_measurement': 'Measuring the scene',
                        'decision_image_composition': 'Preparing the next view',
                        'selected_target_factual_observer': 'Checking target facts',
                    }[self._runtime_phase]
        elif kind == 'grind_player_level_observed':
            level = _level(p.get('level'))
            change = _text(p.get('level_change_vs_same_session_observation')) or ''
            if level and ('confirmed' in change or change == 'same_as_confirmed_level'):
                self._level = level
                if change == 'higher_displayed_level_confirmed_two_fresh_frames':
                    self._react('level', f'LEVEL {level} CONFIRMED', event_id, at, p, hydrate, now)
                elif change == 'higher_displayed_level_confirmed_one_fresh_frame':
                    self._react('level', f'LEVEL {level} OBSERVED', event_id, at, p, hydrate, now)
        elif kind == 'grind_baseline_verified':
            self._level = _level(p.get('level')) or self._level
        elif kind == 'grind_success':
            self._level = _level(p.get('level')) or self._level
            self._outcome = 'Goal level verified'
        elif kind == 'grind_credited_kill':
            self._credited_kills.add(p.get('encounter_id') or event_id)
        elif kind == 'grind_client_combat_log' and p.get('event') == 'PARTY_KILL' and p.get('own_source') is True:
            source_at = _stamp(p.get('occurred_at'))
            if source_at is None or source_at < self._start_at or source_at > at + 2:
                return
            if self._stop_at is not None and source_at > self._stop_at:
                return
            key = (p.get('source_guid'), p.get('dest_guid'), source_at)
            if key not in self._native_kills:
                self._native_kills.add(key)
                caption = 'EARLIER KILL CONFIRMED' if at - source_at > 5 else 'OWN KILL CONFIRMED'
                self._react('kill', caption, event_id, at, p, hydrate, now, source_at)
        elif kind == 'grind_progress_recovery_requested':
            self._react('confusion', 'LOOKING FOR A TARGET', event_id, at, p, hydrate, now)

    def _snapshot(self, now, state, source=None, window=None, reason=None):
        live = state == 'PLAYING'
        phase = self._phase if live else state.lower()
        actor = 'sage' if phase in {'thinking', 'decision'} else ('harness' if live else 'system')
        reaction = self._reaction if live and self._reaction and 0 <= now - self._reaction['at'] <= 2.5 else None
        return {
            'generated_at': now, 'source_revision': self._revision,
            'session_id': self._session_id, 'source': str(source) if source else None,
            'window_id': window, 'state': state, 'phase': phase, 'actor': actor,
            'reason': reason, 'choice': self._choice, 'choice_label': choice_label(self._choice),
            'choice_kind': self._choice_kind,
            'request_id': self._pending['id'] if self._pending else self._decision_request,
            'request_started_at': self._pending['at'] if live and self._pending else None,
            'request_elapsed': max(0, now - self._pending['at']) if live and self._pending else None,
            'response_ms': self._response_ms, 'server_ms': self._server_ms,
            'decision_id': self._decision_id, 'decision_at': self._decision_at,
            'decision_at_iso': datetime.fromtimestamp(self._decision_at, timezone.utc).isoformat() if self._decision_at is not None else None,
            'execution_label': self._execution_label, 'receipt_id': self._receipt_id,
            'authorization_type': self._authorization_type, 'last_outcome': self._outcome,
            'current_level': self._level, 'goal_level': self._goal,
            'native_own_kills': len(self._native_kills), 'strict_credited_kills': len(self._credited_kills),
            'reaction': reaction, 'runtime_phase': self._runtime_phase,
            'profile_path': self._profile_path, 'profile_sha256': self._profile_sha256,
        }

    def poll(self, now=None):
        now = time.time() if now is None else float(now)
        try:
            directory, window, expected_session = self._source()
            status = _object(directory / 'status.json')
            hydrate = self._connect(directory, window, expected_session)
            # One bounded read transaction is released before returning to the
            # UI. Holding a snapshot across polls would pin the runner's WAL.
            self._conn.execute('BEGIN')
            try:
                newest = self._conn.execute("SELECT rowid,event_id,occurred_at,event_type,payload_json FROM events WHERE event_type='session_started' ORDER BY rowid DESC LIMIT 1").fetchone()
                if newest is None:
                    raise ValueError('Session has not started')
                newest_payload = json.loads(newest[4])
                if not isinstance(newest_payload, dict):
                    raise ValueError('Invalid session event')
                session_id = _text(newest_payload.get('session_id'))
                if not session_id or expected_session and session_id != expected_session:
                    raise ValueError('Session source identity mismatch')
                if status.get('session_id') != session_id:
                    raise ValueError('Status session identity mismatch')
                maximum = self._conn.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0]
                if self._session_id != session_id or maximum < self._cursor:
                    if not hydrate:
                        self._revision += 1
                    self._reset()
                    self._cursor = newest[0] - 1
                    hydrate = True
                for row in self._conn.execute('SELECT rowid,event_id,occurred_at,event_type,payload_json FROM events WHERE rowid>? AND rowid<=? ORDER BY rowid', (self._cursor, maximum)):
                    self._consume(row, hydrate, now)
                self._cursor = maximum
            finally:
                self._conn.rollback()
            if hydrate:
                try:
                    manifest = _object(directory / 'launch-manifest.json')
                    if manifest.get('session_id') == self._session_id:
                        self._profile_path = _text(manifest.get('profile_path'), 4096)
                        self._profile_sha256 = _text(manifest.get('profile_sha256'), 64)
                except (OSError, ValueError, TypeError):
                    pass
            self._level = _level(status.get('player_level')) or self._level
            self._goal = _level(status.get('goal_level')) or self._goal
            raw_state = status.get('state')
            updated = _number(status.get('updated_at'))
            navigation = status.get('navigation_recovery')
            # This is a current-cycle wait projection, never the retained
            # navigation debt. A newer provider decision wins a lagging status.
            navigation_wait = bool(isinstance(navigation, dict)
                and navigation.get('state') == 'waiting'
                and updated is not None
                and (self._pending is None or self._pending['at'] <= updated)
                and (self._decision_at is None or self._decision_at <= updated))
            if self._stop_at is not None or raw_state == 'STOPPED':
                state = 'STOPPED'
            elif updated is None or now - updated > 5 or updated > now + 2:
                state = 'STALE'
            elif isinstance(raw_state, str) and (raw_state.startswith('PAUSED') or self._event_paused):
                state = 'PAUSED'
            elif raw_state == 'BLOCKED':
                state = 'BLOCKED'
            elif raw_state == 'PLAYING':
                state = 'BLOCKED' if navigation_wait else 'PLAYING'
            else:
                state = 'UNAVAILABLE'
            if state != 'PLAYING':
                self._reaction = None
            blocked = status.get('blocked')
            reason = (_text(status.get('reason')) or
                      (_text(blocked.get('reason')) if isinstance(blocked, dict) else None))
            if raw_state == 'PLAYING' and state == 'BLOCKED' and navigation_wait:
                reason = 'navigation_no_action_selected'
            return self._snapshot(now, state, directory, window, reason)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            self.close()
            self._identity = None
            self._reset()
            return self._snapshot(now, 'UNAVAILABLE', reason='Live session telemetry unavailable')

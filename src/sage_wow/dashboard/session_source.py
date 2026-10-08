"""A local pointer for the read-only HUD; never routes input or commands."""
import json
import os
from pathlib import Path
import time
from uuid import uuid4


def pointer_path():
    return Path(os.environ.get('SAGE_WOW_SESSION_POINTER', 'data/active-session.json')).resolve()


def write_session_source(directory, window_id, session_id, *, path=None):
    path = pointer_path() if path is None else Path(path)
    payload = {'version': 1, 'directory': str(Path(directory).resolve()),
               'window_id': window_id, 'session_id': session_id,
               'published_at': time.time()}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(payload))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def resolve_session_source(directory, window_id, *, follow=True, path=None):
    fallback = {'directory': Path(directory), 'window_id': window_id,
                'label': 'Configured session', 'following': False}
    if not follow:
        return {**fallback, 'label': 'Pinned session'}
    path = pointer_path() if path is None else Path(path)
    if not path.exists():
        return fallback
    try:
        source = json.loads(path.read_text())
        target = Path(source['directory'])
        selected = source['window_id']
        if (source.get('version') != 1 or not target.is_absolute()
                or type(selected) is not int or selected <= 0
                or not (target / 'events.sqlite3').is_file()
                or not (target / 'status.json').is_file()):
            raise ValueError('Invalid HUD session source')
        label = source.get('session_id') or target.name
        if not isinstance(label, str):
            raise ValueError('Invalid HUD session label')
        return {'directory': target, 'window_id': selected,
                'label': label[:100], 'following': True}
    except (OSError, ValueError, KeyError, TypeError):
        # Never silently show an older run as if it were the new one.
        return {'directory': None, 'window_id': window_id,
                'label': 'Session source unavailable', 'following': True}


def overlay_snapshot(directory, window_id, *, follow=True, path=None, now=None):
    from sage_wow.dashboard.telemetry import live_snapshot
    from sage_wow.status import read_status

    source = resolve_session_source(directory, window_id, follow=follow, path=path)
    if source['directory'] is None:
        raise ValueError(source['label'])
    data = live_snapshot(source['directory'], now=now)
    now = time.time() if now is None else now
    status = read_status(source['directory'])
    updated = status.get('updated_at')
    if data['state'] != 'STOPPED' and (not isinstance(updated, (int, float)) or now - updated > 5):
        data['state'] = 'STALE'
        data['request_age'] = None
    controller = data.get('v2_controller') or {}
    data['session_source'] = (source['label'] if source['following'] else
        ('Pinned: ' if not follow else '') + (controller.get('session_id') or source['label']))
    return data, source['window_id']

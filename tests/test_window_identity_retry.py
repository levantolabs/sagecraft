"""Missing native identity may be re-observed once; all native calls are fake."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from types import ModuleType, SimpleNamespace
import sys

import pytest

from sage_wow.config import Profile
from sage_wow.platform.macos import window_gate


def rig(tmp_path, monkeypatch, apps, *, windows=None, foreground=True):
    window = {'window_id': 42, 'owner_pid': 7, 'owner': 'Test Game',
        'bounds': {'X': 0, 'Y': 0, 'Width': 1200, 'Height': 800}, 'onscreen': True}
    profile = Profile(tmp_path/'profile.yaml', {'client': {'window_id': 42,
        'expected_bundle_id': 'test.game', 'installation_path': '/Applications/Test.app'},
        'calibration': {'window_id': 42, 'window_bounds': dict(window['bounds']),
            'calibrated_at': 'offline fixture'}})
    calls = []; snapshots = list(windows) if windows is not None else [window]*3
    def lookup(pid):
        calls.append(pid)
        return apps.pop(0)
    def details(selected_id):
        assert selected_id == 42
        return deepcopy(snapshots.pop(0))
    appkit = ModuleType('AppKit')
    appkit.NSRunningApplication = SimpleNamespace(runningApplicationWithProcessIdentifier_=lookup)
    monkeypatch.setitem(sys.modules, 'AppKit', appkit)
    monkeypatch.setattr(window_gate.sys, 'platform', 'darwin')
    monkeypatch.setattr(window_gate, 'window_details', details)
    monkeypatch.setattr(window_gate, 'selected_window_is_frontmost', lambda _: foreground)
    return profile, calls, snapshots, window


def app(bundle='test.game', path='/Applications/Test.app'):
    return SimpleNamespace(bundleIdentifier=lambda: bundle,
        bundleURL=lambda: SimpleNamespace(path=lambda: path) if path is not None else None)


def test_missing_lookup_recovers_only_with_fresh_matching_window_and_positive_identity(tmp_path, monkeypatch):
    p, calls, snapshots, _ = rig(tmp_path, monkeypatch, [None, app()])
    gate = window_gate.current_gate(p)
    assert gate.valid
    assert gate.identity_evidence['reason'] == 'verified'
    assert gate.identity_evidence['lookups'] == [
        {'application_present': False}, {'application_present': True}]
    assert gate.identity_evidence['window_before_retry'] == gate.identity_evidence['window_after_retry']
    assert calls == [7, 7] and not snapshots


def test_normal_identity_has_no_extra_queries(tmp_path, monkeypatch):
    p, calls, snapshots, _ = rig(tmp_path, monkeypatch, [app()])
    gate = window_gate.current_gate(p)
    assert gate.valid
    evidence = json.loads(json.dumps(asdict(gate)))['identity_evidence']
    assert evidence['reason'] == 'verified'
    assert evidence['window']['owner_pid'] == 7
    assert evidence['detected_bundle_id'] == 'test.game'
    assert evidence['detected_path'] == '/Applications/Test.app'
    assert gate == replace(gate, identity_evidence=None)
    assert calls == [7] and len(snapshots) == 2


def test_second_missing_lookup_still_fails_closed(tmp_path, monkeypatch):
    p, calls, _, _ = rig(tmp_path, monkeypatch, [None, None])
    gate = window_gate.current_gate(p)
    assert not gate.client_identity_verified
    assert gate.identity_evidence['reason'] == 'application_missing'
    assert gate.identity_evidence['lookups'] == [{'application_present': False}]*2
    assert window_gate.gate_recovery_kind(gate) == 'identity'
    assert calls == [7, 7]


@pytest.mark.parametrize('retry', [False, True])
@pytest.mark.parametrize('candidate,reason', [
    (app(bundle='other.app'), 'bundle_identifier_mismatch'),
    (app(bundle=None), 'bundle_identifier_missing'),
    (app(path='/Applications/Other.app'), 'bundle_path_mismatch'),
    (app(path=None), 'bundle_path_missing')])
def test_missing_or_wrong_metadata_never_uses_another_identity(tmp_path, monkeypatch, retry, candidate, reason):
    p, calls, _, _ = rig(tmp_path, monkeypatch, ([None] if retry else [])+[candidate])
    gate = window_gate.current_gate(p)
    assert not gate.client_identity_verified
    assert gate.identity_evidence['reason'] == reason
    assert window_gate.gate_recovery_kind(gate) == ('identity' if reason.endswith('_missing') else 'invalid')
    assert calls == ([7, 7] if retry else [7])


@pytest.mark.parametrize('stage', ['before_retry', 'after_retry'])
@pytest.mark.parametrize('change', ['missing', 'owner', 'window', 'bounds', 'visibility'])
def test_changed_window_during_retry_fails_closed(tmp_path, monkeypatch, stage, change):
    p, calls, snapshots, original = rig(tmp_path, monkeypatch, [None, app()])
    altered = deepcopy(original)
    if change == 'missing': altered = None
    elif change == 'owner': altered['owner_pid'] = 8
    elif change == 'window': altered['window_id'] = 43
    elif change == 'bounds': altered['bounds']['X'] = 1
    elif change == 'visibility': altered['onscreen'] = False
    snapshots[1 if stage == 'before_retry' else 2] = altered
    gate = window_gate.current_gate(p)
    assert not gate.client_identity_verified and not gate.valid
    assert gate.identity_evidence['reason'] == 'window_changed_' + stage
    assert gate.identity_evidence['window_' + stage] == altered
    assert window_gate.gate_recovery_kind(gate) == 'invalid'
    assert calls == ([7] if stage == 'before_retry' else [7, 7])


def test_recovered_identity_does_not_override_focus_loss(tmp_path, monkeypatch):
    p, calls, _, _ = rig(tmp_path, monkeypatch, [None, app()], foreground=False)
    gate = window_gate.current_gate(p)
    assert gate.client_identity_verified and not gate.valid
    assert calls == [7, 7]


def test_positive_identity_is_not_cached_across_gate_calls(tmp_path, monkeypatch):
    p, calls, snapshots, original = rig(tmp_path, monkeypatch, [app(), None, None])
    snapshots.append(original)
    assert window_gate.current_gate(p).valid
    assert not window_gate.current_gate(p).client_identity_verified
    assert calls == [7, 7, 7]


@pytest.mark.parametrize('stage', ['application_lookup', 'bundle_url', 'bundle_identifier'])
@pytest.mark.parametrize('error_type', [AttributeError, TypeError])
def test_native_exception_retains_its_stage_without_exception_contents(tmp_path, monkeypatch, stage, error_type):
    candidate = app()
    p, calls, _, _ = rig(tmp_path, monkeypatch, [candidate])
    def failed(*args):
        raise error_type('unrelated native details must not enter diagnostics')
    if stage == 'application_lookup':
        monkeypatch.setattr(sys.modules['AppKit'].NSRunningApplication,
            'runningApplicationWithProcessIdentifier_', failed)
    else:
        setattr(candidate, 'bundleURL' if stage == 'bundle_url' else 'bundleIdentifier', failed)
    gate = window_gate.current_gate(p)
    assert not gate.client_identity_verified and not gate.valid
    evidence = gate.identity_evidence
    assert evidence['reason'] == 'identity_query_exception'
    assert evidence['stage'] == stage and evidence['error_type'] == error_type.__name__
    assert 'unrelated native' not in json.dumps(asdict(gate))


def test_missing_window_is_distinguished_from_missing_application(tmp_path, monkeypatch):
    p, calls, _, _ = rig(tmp_path, monkeypatch, [], windows=[None])
    gate = window_gate.current_gate(p)
    assert not gate.valid and not calls
    assert gate.identity_evidence == {'reason': 'window_missing', 'selected_window_id': 42}


@pytest.mark.parametrize('missing', ['expected_bundle_id', 'installation_path'])
def test_incomplete_configuration_does_not_claim_native_lookup_failed(tmp_path, monkeypatch, missing):
    p, calls, _, _ = rig(tmp_path, monkeypatch, [])
    del p.values['client'][missing]
    gate = window_gate.current_gate(p)
    assert not gate.valid and not calls
    assert gate.identity_evidence['reason'] == 'identity_configuration_missing'

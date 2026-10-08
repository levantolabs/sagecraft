from sage_wow.platform.macos import system
import pytest


@pytest.fixture(autouse=True)
def keyboard_focus(monkeypatch):
    monkeypatch.setattr(system, '_frontmost_application_pid', lambda: 7)


GAME_BOUNDS = {"X": 0, "Y": 0, "Width": 1200, "Height": 800}


def row(window_id, pid, layer=0, title="", bounds=None, onscreen=True):
    return {"window_id": window_id, "owner_pid": pid, "layer": layer, "title": title,
            "owner": "TestApp", "bounds": bounds or dict(GAME_BOUNDS), "onscreen": onscreen}


def test_foreground_uses_fresh_window_order_and_exact_selected_identity(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "darwin")
    game = {"window_id": 42, "owner_pid": 7}
    windows = [row(42, 7), row(43, 8)]
    monkeypatch.setattr(system, "_frontmost_window_snapshot", lambda: windows)
    assert system.selected_window_is_frontmost(game)
    windows.reverse()
    assert not system.selected_window_is_frontmost(game)
    windows[:] = [row(44, 7), row(42, 7)]
    assert not system.selected_window_is_frontmost(game)
    windows[:] = [row(42, 999)]
    assert not system.selected_window_is_frontmost(game)


def test_foreign_high_layer_modal_blocks_selected_game(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "darwin")
    game = {"window_id": 42, "owner_pid": 7}
    modal = row(88, 1, layer=8, title="Force Quit Applications",
                bounds={"X": 250, "Y": 120, "Width": 500, "Height": 400})
    monkeypatch.setattr(system, "_frontmost_window_snapshot", lambda: [modal, row(42, 7)])

    assert not system.selected_window_is_frontmost(game)


def test_known_clickthrough_sage_overlay_does_not_block_game(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "darwin")
    game = {"window_id": 42, "owner_pid": 7}
    hud = row(90, 14536, layer=3, title="Sage Live Decisions",
              bounds={"X": 20, "Y": 40, "Width": 455, "Height": 210})
    hud["owner"] = "Python"
    monkeypatch.setattr(system, "_frontmost_window_snapshot", lambda: [hud, row(42, 7)])

    assert system.selected_window_is_frontmost(game)


def test_foreign_modal_on_another_display_blocks_game(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "darwin")
    game = {"window_id": 42, "owner_pid": 7}
    other = row(88, 1, layer=8, title="System modal",
                bounds={"X": 1500, "Y": 120, "Width": 300, "Height": 200})
    monkeypatch.setattr(system, "_frontmost_window_snapshot", lambda: [other, row(42, 7)])

    assert not system.selected_window_is_frontmost(game)


def test_exact_windowserver_cursor_layer_does_not_block_game(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "darwin")
    game = {"window_id": 42, "owner_pid": 7}
    cursor = row(89, 0, layer=2147483630, title="Cursor",
                 bounds={"X": 600, "Y": 400, "Width": 64, "Height": 64})
    cursor["owner"] = "Window Server"
    monkeypatch.setattr(system, "_frontmost_window_snapshot", lambda: [cursor, row(42, 7)])

    assert system.selected_window_is_frontmost(game)


def test_exact_windowserver_recording_indicator_layer_does_not_block_game(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "darwin")
    game = {"window_id": 42, "owner_pid": 7}
    indicator = row(90, 0, layer=2147483630, title="StatusIndicator",
                    bounds={"X": 1465, "Y": 3, "Width": 28, "Height": 28})
    indicator["owner"] = "Window Server"
    monkeypatch.setattr(system, "_frontmost_window_snapshot", lambda: [indicator, row(42, 7)])

    assert system.selected_window_is_frontmost(game)


def test_cursor_lookalike_from_foreign_owner_does_not_get_exemption(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "darwin")
    game = {"window_id": 42, "owner_pid": 7}
    lookalike = row(89, 1, layer=2147483630, title="Cursor")
    monkeypatch.setattr(system, "_frontmost_window_snapshot", lambda: [lookalike, row(42, 7)])

    assert not system.selected_window_is_frontmost(game)


def test_missing_or_offscreen_selected_window_fails_closed(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "darwin")
    game = {"window_id": 42, "owner_pid": 7}
    windows = [row(42, 7, onscreen=False)]
    monkeypatch.setattr(system, "_frontmost_window_snapshot", lambda: windows)
    assert not system.selected_window_is_frontmost(game)
    monkeypatch.setattr(system, "_frontmost_window_snapshot", lambda: [])
    assert not system.selected_window_is_frontmost(game)


def native_scene(monkeypatch, windows, displays):
    import sys
    from types import SimpleNamespace
    from test_system_memory import _fake_quartz
    _fake_quartz(monkeypatch)
    monkeypatch.setattr(system.sys, 'platform', 'darwin')
    quartz = sys.modules['Quartz']
    quartz.CGWindowListCopyWindowInfo = lambda *_: [{
        'number': w['window_id'], 'pid': w['owner_pid'], 'owner': w['owner'],
        'title': w['title'], 'layer': w['layer'], 'bounds': w['bounds'], 'onscreen': w['onscreen']}
        for w in windows]
    quartz.CGGetActiveDisplayList = lambda count, *_: (0, tuple(range(len(displays))) if count else (), len(displays))
    quartz.CGDisplayBounds = lambda d: SimpleNamespace(
        origin=SimpleNamespace(x=displays[d][0], y=displays[d][1]),
        size=SimpleNamespace(width=displays[d][2], height=displays[d][3]))
    return quartz


DISPLAYS = [(0, 0, 1496, 967), (-1920, 0, 1920, 1080)]


def test_zero_visible_area_strips_do_not_masquerade_as_foreground_windows(monkeypatch):
    strips = [row(97131, 91968, bounds={'X': 0, 'Y': -41, 'Width': 1920, 'Height': 41}),
              row(97130, 91968, bounds={'X': -1920, 'Y': -47, 'Width': 1920, 'Height': 47})]
    native_scene(monkeypatch, strips+[row(42, 7)], DISPLAYS)
    snapshot = system._frontmost_window_snapshot()
    assert [w['onscreen'] for w in snapshot] == [False, False, True]
    assert system.selected_window_is_frontmost({'window_id': 42, 'owner_pid': 7})
    # Invisible geometry cannot establish keyboard focus for the selected app.
    monkeypatch.setattr(system, '_frontmost_application_pid', lambda: 91968)
    assert not system.selected_window_is_frontmost({'window_id': 42, 'owner_pid': 7})


@pytest.mark.parametrize('bounds,layer', [
    ({'X': 0, 'Y': -40, 'Width': 1920, 'Height': 41}, 0),
    ({'X': 0, 'Y': -.5, 'Width': 1920, 'Height': .75}, 0),
    ({'X': -1900, 'Y': 10, 'Width': 100, 'Height': 100}, 20),
    ({'X': 100, 'Y': 100, 'Width': 100, 'Height': 100}, 8),
])
def test_any_real_display_overlap_still_blocks(monkeypatch, bounds, layer):
    native_scene(monkeypatch, [row(80, 8, layer=layer, bounds=bounds), row(42, 7)], DISPLAYS)
    assert not system.selected_window_is_frontmost({'window_id': 42, 'owner_pid': 7})


def test_negative_y_is_visible_when_an_active_display_is_above_main(monkeypatch):
    strip = row(80, 8, bounds={'X': 0, 'Y': -41, 'Width': 1920, 'Height': 41})
    native_scene(monkeypatch, [strip, row(42, 7)], DISPLAYS+[(0, -900, 1920, 900)])
    assert not system.selected_window_is_frontmost({'window_id': 42, 'owner_pid': 7})


@pytest.mark.parametrize('result', [(1, (), 0), (0, (), 0)])
def test_unavailable_display_geometry_fails_closed(monkeypatch, tmp_path, result):
    from sage_wow.config import Profile
    from sage_wow.platform.macos.window_gate import current_gate
    quartz = native_scene(monkeypatch, [row(42, 7)], DISPLAYS)
    quartz.CGGetActiveDisplayList = lambda *_: result
    assert not system.selected_window_is_frontmost({'window_id': 42, 'owner_pid': 7})
    profile = Profile(tmp_path/'profile.yaml', {'client': {'window_id': 42},
        'calibration': {'window_id': 42, 'window_bounds': GAME_BOUNDS, 'calibrated_at': 'offline fixture'}})
    gate = current_gate(profile)
    assert gate.window_id is None and not gate.valid


def floating_ax_scene(monkeypatch, *, bounds=None, windows=None, frontmost=False):
    from types import SimpleNamespace
    bounds = bounds or {'X': -949., 'Y': 119., 'Width': 864., 'Height': 864.}
    popup = row(113008, 77451, layer=3, bounds=bounds)
    quartz = native_scene(monkeypatch, [popup, row(42, 7)], DISPLAYS)
    ax_window = {'pid':77451, 'AXPosition':SimpleNamespace(x=bounds['X'], y=bounds['Y']),
        'AXSize':SimpleNamespace(width=bounds['Width'], height=bounds['Height']),
        'AXRole':'AXWindow', 'AXSubrole':'AXStandardWindow', 'AXModal':False,
        'AXFocused':False, 'AXMinimized':False, 'AXMain':True, 'AXChildren':[{'AXRole':'AXButton'}]}
    app = {'AXFrontmost':frontmost, 'AXWindows':windows if windows is not None else [ax_window]}
    import sys
    from types import ModuleType
    ax=ModuleType('ApplicationServices');monkeypatch.setitem(sys.modules,'ApplicationServices',ax)
    appkit=ModuleType('AppKit');monkeypatch.setitem(sys.modules,'AppKit',appkit)
    appkit.NSRunningApplication=SimpleNamespace(runningApplicationWithProcessIdentifier_=lambda pid:
        SimpleNamespace(processIdentifier=lambda:pid,isActive=lambda:app['AXFrontmost']))
    quartz=ax
    quartz.kAXValueCGPointType = 1;quartz.kAXValueCGSizeType = 2
    quartz.AXUIElementCreateApplication = lambda pid:app if pid == 77451 else {}
    quartz.AXUIElementSetMessagingTimeout = lambda *_:0
    quartz.AXUIElementGetPid = lambda element,_:(0,element['pid'])
    quartz.AXValueGetValue = lambda value,*_:(True,value)
    quartz.AXUIElementCopyAttributeValue = lambda element,name,_:(0,element[name]) if name in element else (-25205,None)
    return quartz,ax_window,app


def test_proven_off_game_floating_window_keeps_exact_game_keyboard_focus(monkeypatch):
    quartz,window,app = floating_ax_scene(monkeypatch)
    assert system.selected_window_is_frontmost({'window_id':42,'owner_pid':7})
    # AXMain=True is not keyboard focus and must not veto an otherwise valid proof.
    assert window['AXMain'] is True
    monkeypatch.setattr(system,'_frontmost_application_pid',lambda:77451)
    assert not system.selected_window_is_frontmost({'window_id':42,'owner_pid':7})


@pytest.mark.parametrize('change', ['modal','focused','dialog','sheet','unknown_children','unknown_modal',
    'foreign_frontmost','duplicate','wrong_pid','wrong_geometry','timeout','too_many_windows','minimized','unknown_child_role'])
def test_floating_exception_requires_complete_positive_ax_proof(monkeypatch,change):
    quartz,window,app = floating_ax_scene(monkeypatch)
    if change=='modal':window['AXModal']=True
    elif change=='focused':window['AXFocused']=True
    elif change=='dialog':window['AXSubrole']='AXDialog'
    elif change=='sheet':window['AXChildren']=[{'AXRole':'AXSheet'}]
    elif change=='unknown_children':del window['AXChildren']
    elif change=='unknown_modal':del window['AXModal']
    elif change=='foreign_frontmost':app['AXFrontmost']=True
    elif change=='duplicate':app['AXWindows']=[window,dict(window)]
    elif change=='wrong_pid':window['pid']=9
    elif change=='wrong_geometry':window['AXPosition'].x+=1
    elif change=='timeout':quartz.AXUIElementCopyAttributeValue=lambda *_:(-25204,None)
    elif change=='too_many_windows':app['AXWindows']=[window]*33
    elif change=='minimized':window['AXMinimized']=True
    elif change=='unknown_child_role':window['AXChildren']=[{'AXRole':None}]
    assert not system.selected_window_is_frontmost({'window_id':42,'owner_pid':7})


@pytest.mark.parametrize('bounds', [
    {'X':-864,'Y':119,'Width':864.25,'Height':864},
    {'X':100,'Y':119,'Width':864,'Height':864},
    {'X':-949,'Y':119,'Width':864,'Height':float('nan')},
])
def test_on_game_or_fractional_or_invalid_floating_geometry_cannot_pass(monkeypatch,bounds):
    floating_ax_scene(monkeypatch,bounds=bounds)
    assert not system.selected_window_is_frontmost({'window_id':42,'owner_pid':7})


def test_focus_change_during_positive_ax_proof_vetoes(monkeypatch):
    floating_ax_scene(monkeypatch)
    pids=iter([7,77451]);monkeypatch.setattr(system,'_frontmost_application_pid',lambda:next(pids))
    assert not system.selected_window_is_frontmost({'window_id':42,'owner_pid':7})


def test_ax_budget_exhaustion_and_missing_raw_bounds_cannot_authorize(monkeypatch):
    import time
    floating_ax_scene(monkeypatch)
    assert system._nonmodal_floating_proofs(77451,deadline=time.monotonic()-1)==[]
    floating=row(88,77451,layer=3,bounds={'X':-949,'Y':119,'Width':864,'Height':864})
    monkeypatch.setattr(system,'_frontmost_window_snapshot',lambda:[floating,row(42,7)])
    assert not system.selected_window_is_frontmost({'window_id':42,'owner_pid':7})


def test_unmatched_browser_window_does_not_need_candidate_modal_metadata(monkeypatch):
    from types import SimpleNamespace
    quartz,window,app=floating_ax_scene(monkeypatch)
    app['AXWindows'].append({'pid':77451,'AXPosition':SimpleNamespace(x=-1800,y=100),
        'AXSize':SimpleNamespace(width=100,height=100)})
    assert system.selected_window_is_frontmost({'window_id':42,'owner_pid':7})

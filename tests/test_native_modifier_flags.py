"""Native adapter serialization with fake Quartz only; no native construction."""
from types import SimpleNamespace

import pytest
from sage_wow.platform.macos.input import QuartzInput, InputError


class FakeQuartz:
    kCGEventFlagMaskShift=1<<17
    kCGEventFlagMaskControl=1<<18
    kCGEventFlagMaskAlternate=1<<19
    kCGEventFlagMaskCommand=1<<20
    kCGHIDEventTap=0
    def __init__(self):
        self.events=[];self.flag_writes=[];self.fail=set();self.inherited=1
    def CGEventCreateKeyboardEvent(self,source,keycode,down):
        return {'keycode':keycode,'down':down,'flags':self.inherited}
    def CGEventGetFlags(self,event):return event['flags']
    def CGEventSetFlags(self,event,flags):
        event['flags']=flags;self.flag_writes.append((event['keycode'],event['down'],flags))
    def CGEventPost(self,tap,event):
        self.events.append(dict(event))
        if (event['keycode'],event['down']) in self.fail:raise RuntimeError('synthetic possible post failure')


def adapter(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('Native construction is forbidden')
    monkeypatch.setattr(QuartzInput,'__init__',forbidden)
    native=QuartzInput.__new__(QuartzInput)
    native.q=FakeQuartz();native._ownership_lock=SimpleNamespace(closed=False)
    native.held_keys=set();native._cleanup_keys=set();native.held_buttons={}
    return native


def stream(native):return [(event['keycode'],event['down'],event['flags']) for event in native.q.events]


def test_option_z_explicit_flags_strip_ambient_modifiers_and_keep_other_bits(monkeypatch):
    native=adapter(monkeypatch);q=native.q
    q.inherited=1|q.kCGEventFlagMaskShift|q.kCGEventFlagMaskCommand
    for code,down in ((58,True),(6,True),(6,False),(58,False)):native.key(code,down)
    assert stream(native)==[(58,True,1|q.kCGEventFlagMaskAlternate),(6,True,1|q.kCGEventFlagMaskAlternate),
        (6,False,1|q.kCGEventFlagMaskAlternate),(58,False,1)]
    assert not native.held_keys and not native._cleanup_keys


@pytest.mark.parametrize('pair,mask',[((56,60),'kCGEventFlagMaskShift'),((59,62),'kCGEventFlagMaskControl'),
    ((58,61),'kCGEventFlagMaskAlternate'),((55,54),'kCGEventFlagMaskCommand')])
def test_either_side_keeps_aggregate_modifier_after_one_side_releases(monkeypatch,pair,mask):
    native=adapter(monkeypatch);bit=getattr(native.q,mask)
    for code,down in ((pair[0],True),(pair[1],True),(pair[0],False),(6,True),(6,False),(pair[1],False)):
        native.key(code,down)
    assert [event['flags'] for event in native.q.events]==[1|bit]*5+[1]
    assert not native.held_keys


def test_ordinary_key_keeps_existing_event_flags_without_set_flags(monkeypatch):
    native=adapter(monkeypatch);native.q.inherited=123456
    native.key(13,True);native.key(13,False)
    assert not native.q.flag_writes and [event['flags'] for event in native.q.events]==[123456,123456]


@pytest.mark.parametrize('failed_key',[58,6])
def test_failed_down_tracks_only_cleanup_until_successful_release(monkeypatch,failed_key):
    native=adapter(monkeypatch)
    if failed_key==6:native.key(58,True)
    native.q.fail.add((failed_key,True))
    with pytest.raises(RuntimeError):native.key(failed_key,True)
    assert failed_key not in native.held_keys and failed_key in native._cleanup_keys
    native.release_all()
    assert not native.held_keys and not native._cleanup_keys
    releases=[event for event in native.q.events if not event['down']]
    assert [event['keycode'] for event in releases]==([6,58] if failed_key==6 else [58])
    assert releases[-1]['flags']==1


def test_release_all_attempts_every_key_and_retains_failed_letter_and_modifier_releases(monkeypatch):
    native=adapter(monkeypatch)
    native.key(58,True);native.key(6,True);native.key(56,True)
    native.q.fail={(6,False),(58,False)}
    with pytest.raises(RuntimeError):native.release_all()
    assert [event['keycode'] for event in native.q.events if not event['down']]==[6,56,58]
    assert native.held_keys=={6,58}
    native.q.fail.clear();native.release_all()
    assert not native.held_keys and not native._cleanup_keys
    assert stream(native)[-2:]==[(6,False,1|native.q.kCGEventFlagMaskAlternate),(58,False,1)]


def test_failed_down_then_failed_up_remains_cleanup_obligation(monkeypatch):
    native=adapter(monkeypatch);native.q.fail={(58,True),(58,False)}
    with pytest.raises(RuntimeError):native.key(58,True)
    with pytest.raises(RuntimeError):native.release_all()
    assert native._cleanup_keys=={58} and not native.held_keys
    native.q.fail.clear();native.release_all()
    assert not native._cleanup_keys


def test_missing_native_owner_still_rejects_before_any_event(monkeypatch):
    native=adapter(monkeypatch);native._ownership_lock.closed=True
    with pytest.raises(InputError):native.key(58,True)
    assert not native.q.events and not native.held_keys and not native._cleanup_keys

"""Status publication must not stall the native-input heartbeat."""
import asyncio
import json
from pathlib import Path
import threading
import time

import pytest

from sage_wow.agent.cycle import CycleResult
from sage_wow.control.executor import GateSnapshot, SafeExecutor
from sage_wow.grind_runner import GrindRunner
from sage_wow.platform.macos.geometry import Rect
from test_grind_focus import attached
from test_grind_only import Backend, Frames, Freeze, Sage, cleanup, profile


def test_slow_status_write_during_movement_keeps_heartbeat_alive(tmp_path, monkeypatch):
    async def scenario():
        p=profile(tmp_path);frames=Frames(tmp_path);backend=Backend();rect=Rect(0,0,500,300)
        executor=SafeExecutor(backend,lambda:GateSnapshot(7,7,True,rect,rect,True,True),
            heartbeat_timeout=.75,watchdog_interval=.01)
        writes=[];result=[];original=Path.write_text
        def slow_write(path,*args,**kwargs):
            if path.name=='status.tmp' and ('key',13,True) in backend.events and not writes:
                writes.append(threading.get_ident())
                time.sleep(1.0)  # Longer than the unchanged watchdog lease.
            return original(path,*args,**kwargs)
        monkeypatch.setattr(Path,'write_text',slow_write)
        class MovingRunner(GrindRunner):
            async def step(self):
                result.append(await self.executor.execute({'type':'keypress','keycode':13,'hold_seconds':1.4}))
                self.request_stop('offline_movement_complete')
                return CycleResult('dispatched')
        runner=MovingRunner(p,tmp_path/'run',sage=Sage(),executor=executor,
            capture=frames.capture,ocr=frames.ocr,freeze_factory=Freeze)
        await asyncio.wait_for(runner.run(),5)
        assert executor.last_watchdog_trip is None
        assert result[0]['dispatched'] and writes and writes[0]!=threading.get_ident()
        assert ('key',13,False) in backend.events and backend.closed and not executor.armed
        status=json.loads((runner.directory/'status.json').read_text())
        assert status['state']=='STOPPED' and status['reason']=='offline_movement_complete'
    asyncio.run(scenario())


def test_cancelled_status_write_drains_before_final_state(tmp_path, monkeypatch):
    async def scenario():
        runner,c,frames,sage,backend,store,executor=attached(tmp_path)
        entered=threading.Event();release=threading.Event();original=Path.write_text
        def blocked_write(path,*args,**kwargs):
            if path.name=='status.tmp' and not entered.is_set():
                entered.set();assert release.wait(3)
            return original(path,*args,**kwargs)
        monkeypatch.setattr(Path,'write_text',blocked_write)
        pending=asyncio.create_task(runner.status())
        try:
            assert await asyncio.to_thread(entered.wait,2)
            pending.cancel();await asyncio.sleep(.03)
            assert not pending.done()  # No old write can outlive final publication.
            release.set()
            with pytest.raises(asyncio.CancelledError):await pending
            runner.state='STOPPED';await runner.status()
            assert json.loads((tmp_path/'status.json').read_text())['state']=='STOPPED'
        finally:
            release.set();await asyncio.gather(pending,return_exceptions=True)
            await cleanup(executor,store)
    asyncio.run(scenario())


def test_status_write_error_releases_input_and_closes_runner(tmp_path, monkeypatch):
    async def scenario():
        p=profile(tmp_path);frames=Frames(tmp_path);backend=Backend();rect=Rect(0,0,500,300)
        executor=SafeExecutor(backend,lambda:GateSnapshot(7,7,True,rect,rect,True,True))
        original=Path.write_text
        def unavailable(path,*args,**kwargs):
            if path.name=='status.tmp':raise OSError('offline status disk failure')
            return original(path,*args,**kwargs)
        monkeypatch.setattr(Path,'write_text',unavailable)
        runner=GrindRunner(p,tmp_path/'run',sage=Sage(),executor=executor,
            capture=frames.capture,ocr=frames.ocr,freeze_factory=Freeze)
        with pytest.raises(OSError,match='offline status disk failure'):await runner.run()
        assert runner.state=='STOPPED' and not executor.armed and backend.closed
    asyncio.run(scenario())

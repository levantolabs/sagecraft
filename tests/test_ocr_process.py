"""Spawned fake OCR workers only; native Vision/input/capture remain unused."""
import asyncio
import os
import sys
import time

import pytest

from sage_wow.perception.ocr_process import IsolatedOCR
from sage_wow.control.executor import SafeExecutor
from test_executor import FakeBackend, valid_gate


FAKE_WORKER = r'''
import ctypes
import os
import socket
from pathlib import Path
from sage_wow.perception.ocr import TextObservation
from sage_wow.perception.ocr_worker import serve
def denied(*args, **kwargs):
    raise RuntimeError('offline worker network denied')
socket.getaddrinfo = socket.socket.connect = socket.socket.connect_ex = denied
assert os.environ['SAGE_WOW_DISABLE_LIVE_INPUT'] == '1'
assert os.environ['SAGE_WOW_DISABLE_LIVE_CAPTURE'] == '1'
def fake(path, language, accurate):
    if path.name == 'crash':
        os._exit(7)
    if path.name == 'failure':
        raise RuntimeError('simulated OCR failure')
    if path.name in {'slow', 'hang'}:
        Path(str(path)+'.started').write_text(str(os.getpid()))
        # PyDLL holds THIS CHILD's GIL throughout the C call. The parent
        # heartbeat must continue even though another Python thread inside
        # this child would be unable to run.
        libc = ctypes.PyDLL(None)
        libc.usleep.argtypes = [ctypes.c_uint]
        libc.usleep(1100000 if path.name == 'slow' else 5000000)
    return [TextObservation(path.name+':'+str(language)+':'+str(accurate), .9,
        {'x':1,'y':2,'width':3,'height':4}, source='offline_fake_process')]
serve(fake)
'''


def worker(**kwargs):
    return IsolatedOCR(command=[sys.executable,'-u','-c',FAKE_WORKER], **kwargs)


def test_native_style_child_gil_stall_does_not_interrupt_parent_heartbeat(tmp_path, record_property):
    async def scenario():
        provider = worker(timeout_seconds=3)
        backend = FakeBackend()
        executor = SafeExecutor(backend, valid_gate)  # Production .75s limit.
        pulses = []
        async def pulse():
            while True:
                executor.heartbeat()
                pulses.append(time.monotonic())
                await asyncio.sleep(.05)
        executor.arm()
        heartbeat = asyncio.create_task(pulse())
        try:
            slow = asyncio.create_task(asyncio.to_thread(provider,tmp_path/'slow','en',False))
            action = asyncio.create_task(executor.execute({'type':'keypress','keycode':13,'hold_seconds':1.3}))
            result = await slow
            assert result[0].text == 'slow:en:False'
            assert result[0].source == 'offline_fake_process'
            timing = provider.last_timing
            assert timing['worker_pid'] != os.getpid() and timing['worker_elapsed_seconds'] >= 1.1
            assert executor.armed and executor.last_watchdog_trip is None
            assert (await action)['dispatched']
            assert len(pulses) >= 15
            max_gap = max(right-left for left,right in zip(pulses,pulses[1:]))
            assert max_gap < executor.heartbeat_timeout
            record_property('parent_max_heartbeat_gap_seconds',max_gap)
            record_property('worker_native_style_stall_seconds',timing['worker_elapsed_seconds'])
            first_pid = timing['worker_pid']
            assert (await asyncio.to_thread(provider,tmp_path/'warm'))[0].text == 'warm:None:True'
            assert provider.last_timing['worker_pid'] == first_pid  # Warm process reused.
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat,return_exceptions=True)
            await executor.stop()
            await asyncio.to_thread(provider.close)
    asyncio.run(scenario())


@pytest.mark.parametrize('failure,match', [('crash','exited'),('failure','simulated OCR failure'),('hang','timed out')])
def test_worker_failure_or_timeout_retires_process_and_next_call_is_fresh(tmp_path, failure, match):
    provider = worker(timeout_seconds=3)
    try:
        provider(tmp_path/'warm')
        process = provider._process
        provider.timeout_seconds = .2  # Test the request deadline after cold startup.
        started = time.monotonic()
        with pytest.raises(RuntimeError,match=match):
            provider(tmp_path/failure)
        assert time.monotonic()-started < 1.5
        assert process.poll() is not None and provider._process is None
        provider.timeout_seconds = 3
        assert provider(tmp_path/'recovered')[0].text == 'recovered:None:True'
        assert provider.last_timing['worker_pid'] != process.pid
    finally:
        provider.close()


def test_closing_cancels_inflight_worker_and_seals_late_thread_requests(tmp_path):
    async def scenario():
        provider = worker(timeout_seconds=8)
        pending = asyncio.create_task(asyncio.to_thread(provider,tmp_path/'hang'))
        try:
            deadline = time.monotonic()+2
            while not (tmp_path/'hang.started').exists():
                assert time.monotonic() < deadline
                await asyncio.sleep(.01)
            process = provider._process
            started = time.monotonic()
            await asyncio.to_thread(provider.close)
            with pytest.raises((RuntimeError, ValueError, OSError)):
                await asyncio.wait_for(pending,1)
            assert time.monotonic()-started < 1.5
            assert process.poll() is not None
            with pytest.raises(RuntimeError,match='closed'):
                await asyncio.to_thread(provider,tmp_path/'late')
            assert provider._process is None
        finally:
            provider.close()
    asyncio.run(scenario())


def test_wrong_response_request_identity_is_rejected_and_worker_reaped(tmp_path):
    fake = "import json,sys; request=json.loads(sys.stdin.readline()); request['id']='wrong'; print(json.dumps({'request':request,'observations':[]})); sys.stdout.flush()"
    provider = IsolatedOCR(command=[sys.executable,'-u','-c',fake])
    try:
        with pytest.raises(RuntimeError,match='identity mismatch'):
            provider(tmp_path/'fake')
        assert provider._process is None
    finally:
        provider.close()


@pytest.mark.parametrize('injected', [True, False])
def test_runner_only_creates_and_closes_its_own_ocr_worker(tmp_path, monkeypatch, injected):
    from sage_wow.grind_runner import GrindRunner
    from test_grind_only import Backend, Frames, Freeze, Sage, profile
    async def scenario():
        frames = Frames(tmp_path)
        entered = asyncio.Event()
        async def pending_provider():
            entered.set()
            await asyncio.Event().wait()
        providers = []
        def make_provider():
            assert not injected, 'An injected OCR function must never create a worker'
            providers.append(worker())
            return providers[-1]
        monkeypatch.setattr('sage_wow.perception.ocr_process.IsolatedOCR', make_provider)
        runner = GrindRunner(profile(tmp_path),tmp_path/'run',sage=Sage(hook=pending_provider),
            executor=SafeExecutor(Backend(),valid_gate),capture=frames.capture,
            ocr=frames.ocr if injected else None,freeze_factory=Freeze)
        task = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(entered.wait(),3)
            process = None if injected else providers[0]._process
            runner.request_stop('offline_test_finished')
            await asyncio.wait_for(task,3)
            if injected:
                assert not providers and runner.ocr_worker is None
            else:
                assert runner.ocr_worker is providers[0]
                assert providers[0]._closed and providers[0]._process is None
                assert process.poll() is not None
        finally:
            if not task.done():
                runner.request_stop('offline_cleanup')
                await asyncio.wait_for(task,3)
    asyncio.run(scenario())

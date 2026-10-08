"""Bounded local OCR transport, isolating native imports/work from the input owner."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import sys
import threading
import time
from uuid import uuid4

from sage_wow.perception.ocr import TextObservation


class IsolatedOCR:
    """Synchronous OCR callable; async callers use to_thread as before.

    The child only reads supplied image files. Closing seals this instance,
    including requests queued by a cancelled to_thread call.
    """

    MAX_RESPONSE_BYTES = 2 * 1024 * 1024

    def __init__(self, *, timeout_seconds: float = 8.0, command=None):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError('OCR worker timeout must be positive and no more than 30 seconds')
        self.timeout_seconds = timeout_seconds
        self.command = command or [sys.executable, '-u', '-m', 'sage_wow.perception.ocr_worker']
        self._serial = threading.Lock()
        self._state = threading.Lock()
        self._process = None
        self._closed = False
        self.last_timing = None

    def _get_process(self):
        with self._state:
            if self._closed:
                raise RuntimeError('OCR worker is closed')
            if self._process is None:
                env = os.environ.copy()
                # This process is never an input owner or capture provider.
                env.update(SAGE_WOW_DISABLE_LIVE_INPUT='1', SAGE_WOW_DISABLE_LIVE_CAPTURE='1')
                self._process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, bufsize=0,
                    close_fds=True)
            return self._process

    @staticmethod
    def _terminate(process):
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=.5)
            else:
                process.wait(timeout=.5)
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()

    def _retire(self, process):
        with self._state:
            if self._process is not process:
                return  # close() already owns termination.
            self._process = None
        self._terminate(process)

    def close(self):
        with self._state:
            self._closed = True
            process, self._process = self._process, None
        if process is not None:
            self._terminate(process)

    def __call__(self, image_path: Path, language: str | None = None,
                 accurate: bool = True) -> list[TextObservation]:
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        if not self._serial.acquire(timeout=self.timeout_seconds):
            raise RuntimeError('OCR worker timed out waiting for the previous request')
        process = None
        try:
            process = self._get_process()
            request = {'id':str(uuid4()), 'image_path':str(Path(image_path).resolve()),
                       'language':language, 'accurate':accurate}
            wire = (json.dumps(request, allow_nan=False)+'\n').encode()
            if len(wire) > 4096:
                raise ValueError('OCR request is too large')
            if process.stdin.write(wire) != len(wire):
                raise RuntimeError('OCR worker request was incomplete')
            data = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while b'\n' not in data:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise RuntimeError('OCR worker timed out')
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError('OCR worker exited before returning its response')
                    data.extend(chunk)
                    if len(data) > self.MAX_RESPONSE_BYTES:
                        raise RuntimeError('OCR worker response exceeded its size limit')
            if not data.endswith(b'\n') or data.count(b'\n') != 1:
                raise RuntimeError('OCR worker returned an invalid response boundary')
            response = json.loads(data)
            if not isinstance(response,dict) or response.get('request') != request:
                raise RuntimeError('OCR worker response identity mismatch')
            if response.get('error'):
                raise RuntimeError('OCR worker failed: '+str(response['error'])[:500])
            observations = [TextObservation(**row) for row in response['observations']]
            with self._state:
                if self._closed or self._process is not process:
                    raise RuntimeError('OCR worker closed before the response was accepted')
                self.last_timing = {'worker_pid':response['worker_pid'], 'parent_pid':os.getpid(),
                    'elapsed_seconds':time.monotonic()-started,
                    'worker_elapsed_seconds':response['elapsed_seconds'],
                    'request_id':request['id']}
            return observations
        except Exception:
            if process is not None:
                self._retire(process)
            raise
        finally:
            self._serial.release()

"""Local OCR worker entry point. No runner, capture, input, or model imports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

from sage_wow.perception.ocr import observations_as_dict, recognize_text


def serve(recognizer=recognize_text):
    for line in sys.stdin:
        if len(line) > 4096:
            return
        started = time.monotonic()
        request = None
        try:
            request = json.loads(line)
            if (not isinstance(request,dict) or set(request) != {'id','image_path','language','accurate'}
                    or not isinstance(request['id'],str) or not request['id']
                    or not isinstance(request['image_path'],str)
                    or not (request['language'] is None or isinstance(request['language'],str))
                    or type(request['accurate']) is not bool):
                raise ValueError('Malformed OCR request')
            rows = recognizer(Path(request['image_path']), request['language'], request['accurate'])
            result = {'observations':observations_as_dict(rows)}
        except Exception as exc:
            result = {'error':type(exc).__name__+': '+str(exc)[:500]}
        response = {**result, 'request':request, 'worker_pid':os.getpid(),
                    'elapsed_seconds':time.monotonic()-started}
        sys.stdout.write(json.dumps(response,allow_nan=False)+'\n')
        sys.stdout.flush()


if __name__ == '__main__':
    serve()

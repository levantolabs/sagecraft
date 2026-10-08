"""Local immutable evidence for bounded trials; never captures or infers anything."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from uuid import uuid4

from sage_wow.models import Event


class EvidenceRetentionError(OSError):
    """Fatal for this bounded trial: no inference/input may continue unrecorded."""


class EvidenceArchive:
    def __init__(self, root, store, *, max_bytes=512*1024*1024):
        self.root = Path(root)/str(uuid4())
        self.root.mkdir(parents=True, exist_ok=False)
        self.store, self.max_bytes, self.bytes = store, max_bytes, 0
        self._frames = {}
        self._frame_paths = {}
        self.failed = None

    def retain(self, data, suffix):
        if self.failed:
            raise EvidenceRetentionError(self.failed)
        try:
            return self._retain(data, suffix)
        except (OSError, ValueError) as exc:
            self.failed = str(exc)
            raise EvidenceRetentionError(str(exc)) from exc

    def _retain(self, data, suffix):
        digest = hashlib.sha256(data).hexdigest()
        path = self.root/(digest+suffix)
        if path.exists():
            if path.read_bytes() != data:
                raise OSError('Evidence archive content mismatch')
        else:
            if self.bytes+len(data) > self.max_bytes:
                raise OSError('Bounded trial evidence capacity reached; preserve evidence and stop the trial')
            with path.open('xb') as stream:
                stream.write(data)
            path.chmod(0o400)
            self.bytes += len(data)
        return {'path': str(path), 'sha256': digest, 'bytes': len(data)}

    def frame(self, frame, epoch, generation):
        key = (epoch, frame.frame_id)
        data = Path(frame.image_path).read_bytes()
        if key in self._frames:
            if hashlib.sha256(data).hexdigest() != self._frames[key]:
                self.failed = 'A retained frame ID was reused with different image bytes'
                raise EvidenceRetentionError(self.failed)
            return self._frame_paths[key]
        evidence = self.retain(data, Path(frame.image_path).suffix or '.image')
        self.store.append(Event.create('trial_frame_retained', dict(evidence,
            frame_id=frame.frame_id, captured_at=frame.captured_at,
            session_epoch=epoch, input_generation=generation,
            source='supplied runtime frame; timestamp/generation bind post-input observations, not proof of effect')))
        self._frames[key] = evidence['sha256']
        self._frame_paths[key] = Path(evidence['path'])
        return self._frame_paths[key]

    def request_image(self, path, *, request_id, frame_id, epoch):
        evidence = self.retain(Path(path).read_bytes(), Path(path).suffix or '.image')
        self.store.append(Event.create('trial_request_image_retained', dict(evidence,
            request_id=request_id, frame_id=frame_id, session_epoch=epoch,
            source='immutable pre-encoding decision image; exact wire JPEG recorded separately by SageClient')))
        return Path(evidence['path'])

    def wire_image(self, media, metadata):
        prefix, encoded = media.split(',', 1)
        if prefix != 'data:image/jpeg;base64':
            raise ValueError('Unexpected Sage request image encoding')
        evidence = self.retain(base64.b64decode(encoded, validate=True), '.jpg')
        self.store.append(Event.create('trial_wire_image_retained', dict(evidence, **metadata,
            source='exact prepared JPEG bytes supplied in the outbound Sage request; no re-encoding')))

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class TextObservation:
    text: str
    confidence: float
    bounds: dict[str, int]
    source: str = "apple_vision_ocr"


def normalized_box_to_pixels(box: tuple[float, float, float, float], width: int, height: int) -> dict[str, int]:
    """Convert Vision's normalized, bottom-left rectangle to image pixel coordinates."""
    x, y, box_width, box_height = box
    left = round(x * width)
    top = round((1 - y - box_height) * height)
    right = round((x + box_width) * width)
    bottom = round((1 - y) * height)
    return {"x": left, "y": top, "width": max(0, right - left), "height": max(0, bottom - top)}


def recognize_text(image_path: Path, language: str | None = None, accurate: bool = True) -> list[TextObservation]:
    if sys.platform != "darwin":
        raise RuntimeError("Apple Vision OCR is available only on macOS")
    with _autorelease_pool():
        return _recognize_text(image_path, language, accurate)


@contextmanager
def _autorelease_pool() -> Iterator[None]:
    """Bound Objective-C temporary-object lifetime on persistent worker threads."""
    try:
        import objc
    except ImportError:
        # Preserve importability on non-macOS hosts; the Vision import below
        # reports unavailable dependencies through the existing error path.
        yield
        return
    with objc.autorelease_pool():
        yield


def _recognize_text(image_path: Path, language: str | None = None,
                    accurate: bool = True) -> list[TextObservation]:
    try:
        import Vision
        from Foundation import NSURL
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Install macOS OCR dependencies with `uv sync --extra macos`: " + str(exc)) from exc
    with Image.open(image_path) as image:
        width, height = image.size
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(
        Vision.VNRequestTextRecognitionLevelAccurate if accurate else Vision.VNRequestTextRecognitionLevelFast
    )
    request.setUsesLanguageCorrection_(True)
    if language:
        request.setRecognitionLanguages_([language])
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(image_path)), {})
    success, error = handler.performRequests_error_([request], None)
    if not success:
        raise RuntimeError(f"Apple Vision OCR failed: {error}")
    result = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        recognized = candidates[0]
        rect = observation.boundingBox()
        box = normalized_box_to_pixels((rect.origin.x, rect.origin.y, rect.size.width, rect.size.height), width, height)
        result.append(TextObservation(str(recognized.string()), float(recognized.confidence()), box))
    return result


def observations_as_dict(observations: list[TextObservation]) -> list[dict[str, object]]:
    return [asdict(item) for item in observations]

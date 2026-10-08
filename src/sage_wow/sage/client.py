from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import httpx
from PIL import Image

from sage_wow.sage.usage import estimate_decision_units


MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_EDGE = 8192
MAX_IMAGE_PIXELS = 4096 * 4096
MAX_IMAGE_CHOICES = 20


@dataclass(frozen=True)
class ImageBatchContent:
    image_path: Path
    text: str


@dataclass(frozen=True)
class BatchQuestion:
    """One independent Sage Choice question; batches intentionally start choice-only."""
    question_id: str
    instructions: str
    options: tuple[dict[str, str], ...]

    @property
    def kind(self) -> str:
        return "choice"


@dataclass(frozen=True)
class BatchRequestGroup:
    content: str | ImageBatchContent
    questions: tuple[BatchQuestion, ...]


@dataclass(frozen=True)
class SageBatchAnswer:
    question_id: str
    ok: bool
    kind: str | None
    result: dict[str, Any] | None
    chosen: str | None
    error: str | None
    meta: dict[str, Any]
    raw: dict[str, Any]


@dataclass(frozen=True)
class SageBatchGroupResult:
    group_index: int
    answers: tuple[SageBatchAnswer, ...]


@dataclass(frozen=True)
class SageBatchResponse:
    groups: tuple[SageBatchGroupResult, ...]
    meta: dict[str, Any]
    request_count: int
    question_count: int
    preparation_ms: float
    request_ms: float
    parse_ms: float
    total_ms: float
    raw: dict[str, Any]

    @property
    def answers(self) -> tuple[SageBatchAnswer, ...]:
        return tuple(answer for group in self.groups for answer in group.answers)


class SageError(RuntimeError):
    """Base class for decision API errors."""


class SageConfigurationError(SageError):
    pass


class SageTransientError(SageError):
    pass


class SageAuthenticationError(SageError):
    pass


class SageAllowanceError(SageError):
    pass


class SageRequestError(SageError):
    pass


class SageResponseError(SageError):
    pass


@dataclass(frozen=True)
class ChoiceOption:
    option: str
    description: str
    binding: dict[str, Any]


@dataclass(frozen=True)
class CandidateSet:
    version: str
    options: tuple[ChoiceOption, ...]

    def __post_init__(self) -> None:
        ids = [option.option for option in self.options]
        if len(ids) < 2 or len(ids) > MAX_IMAGE_CHOICES:
            raise ValueError(f"Image Choice needs 2..{MAX_IMAGE_CHOICES} options; got {len(ids)}")
        if len(ids) != len(set(ids)):
            raise ValueError("Candidate option IDs must be unique")


@dataclass(frozen=True)
class DecisionEnvelope:
    request_id: str
    captured_frame_id: str
    session_epoch: str
    candidate_set_version: str
    candidate_options: tuple[dict[str, Any], ...]
    action_bindings: dict[str, dict[str, Any]]

    @classmethod
    def create(cls, captured_frame_id: str, session_epoch: str, candidates: CandidateSet) -> "DecisionEnvelope":
        options_snapshot = tuple({"option": item.option, "description": item.description} for item in candidates.options)
        return cls(str(uuid4()), captured_frame_id, session_epoch, candidates.version, options_snapshot,
                   {option.option: dict(option.binding) for option in candidates.options})


@dataclass(frozen=True)
class SageDecision:
    envelope: DecisionEnvelope
    chosen: str | None
    selected_binding: dict[str, Any] | None
    probability: float | None
    probabilities: tuple[dict[str, Any], ...]
    model: str | None
    server_latency_ms: float | None
    reasoning: dict[str, Any] | None
    usage: dict[str, Any]
    request_duration_ms: float
    option_ids: tuple[str, ...]

    def event_payload(self) -> dict[str, Any]:
        return {
            "request_id": self.envelope.request_id,
            "captured_frame_id": self.envelope.captured_frame_id,
            "session_epoch": self.envelope.session_epoch,
            "candidate_set_version": self.envelope.candidate_set_version,
            "candidate_options": list(self.envelope.candidate_options),
            "action_bindings": self.envelope.action_bindings,
            "chosen": self.chosen,
            "selected_binding": self.selected_binding,
            "probability": self.probability,
            "probabilities": list(self.probabilities),
            "model": self.model,
            "server_latency_ms": self.server_latency_ms,
            "reasoning": self.reasoning,
            "usage": self.usage,
            "estimated_decision_units": estimate_decision_units(self.usage),
            "request_duration_ms": self.request_duration_ms,
            "option_ids": list(self.option_ids),
        }


def _prepare_image(path: Path, max_bytes: int = MAX_IMAGE_BYTES) -> tuple[str, int, int]:
    if max_bytes <= 0 or max_bytes > MAX_IMAGE_BYTES:
        raise ValueError(f"max_image_bytes must be in 1..{MAX_IMAGE_BYTES}")
    try:
        image = Image.open(path).convert("RGB")
    except (OSError, Image.DecompressionBombError) as exc:
        raise SageRequestError(f"Cannot encode image {path}: {exc}") from exc
    scale = min(1.0, MAX_IMAGE_EDGE / max(image.size), (MAX_IMAGE_PIXELS / (image.width * image.height)) ** 0.5)
    if scale < 1:
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.Resampling.LANCZOS)
    quality = 90
    encoded = b""
    while True:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        encoded = buffer.getvalue()
        if len(encoded) <= max_bytes:
            break
        if quality > 45:
            quality -= 10
        else:
            new_size = (max(1, int(image.width * 0.8)), max(1, int(image.height * 0.8)))
            if new_size == image.size:
                raise SageRequestError("Image cannot fit Sage's decoded image size limit")
            image = image.resize(new_size, Image.Resampling.LANCZOS)
            quality = 80
    media = "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
    return media, image.width, image.height


class SageClient:
    """Persistent async client for Levanto Sage image Choice decisions."""

    def __init__(
        self,
        api_key: str,
        endpoint: str = "https://sage.levanto.ai",
        timeout_seconds: float = 12,
        max_image_bytes: int = MAX_IMAGE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
        max_batch_questions: int = 20,
    ):
        if not api_key.strip():
            raise SageConfigurationError("SAGE_API_KEY is missing")
        if not endpoint.startswith("https://"):
            raise SageConfigurationError("Sage endpoint must use HTTPS")
        if timeout_seconds <= 0:
            raise SageConfigurationError("timeout_seconds must be positive")
        if max_image_bytes <= 0 or max_image_bytes > MAX_IMAGE_BYTES:
            raise SageConfigurationError(f"max_image_bytes must be in 1..{MAX_IMAGE_BYTES}")
        if isinstance(max_batch_questions, bool) or not isinstance(max_batch_questions, int) or not 1 <= max_batch_questions <= 100:
            raise SageConfigurationError("max_batch_questions must be an integer in 1..100")
        self.image_evidence_sink = None
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_image_bytes = max_image_bytes
        self.max_batch_questions = max_batch_questions
        self._client = httpx.AsyncClient(
            base_url=self.endpoint,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def decide_batch(
        self,
        groups: Iterable[BatchRequestGroup],
        *,
        reasoning: str = "off",
        latency_mode: str = "fast",
    ) -> SageBatchResponse:
        """Submit independent choice questions in documented grouped format.

        The local question bound is an operational safeguard, not a documented
        provider limit. Dependent questions belong in a later decision-graph wave.
        """
        if reasoning not in {"off", "auto", "on"}:
            raise ValueError("reasoning must be 'off', 'auto', or 'on'")
        if latency_mode not in {"fast", "quality"}:
            raise ValueError("latency_mode must be 'fast' or 'quality'")
        groups = tuple(groups)
        if not groups:
            raise SageRequestError("a batch needs at least one request group")
        stable_groups: list[BatchRequestGroup] = []
        ids: set[str] = set()
        total_questions = 0
        for group in groups:
            if not isinstance(group, BatchRequestGroup) or not group.questions:
                raise SageRequestError("each batch group needs content and at least one question")
            for question in group.questions:
                if (not isinstance(question, BatchQuestion)
                        or not isinstance(question.question_id, str) or not question.question_id.strip()):
                    raise SageRequestError("each batch question needs a non-empty question id")
                if question.question_id in ids:
                    raise SageRequestError(f"duplicate batch question id {question.question_id!r}")
                ids.add(question.question_id)
                if not isinstance(question.instructions, str) or not question.instructions.strip():
                    raise SageRequestError(f"question {question.question_id!r} needs instructions")
                options = question.options
                if not isinstance(options, (tuple, list)) or any(not isinstance(item, dict) for item in options):
                    raise SageRequestError(f"question {question.question_id!r} has malformed options")
                option_ids = [item.get("option") for item in options]
                if (not 2 <= len(options) <= MAX_IMAGE_CHOICES
                        or any(not isinstance(item.get("option"), str) or not item.get("option")
                               or not isinstance(item.get("description"), str)
                               or not item.get("description") for item in options)
                        or len(option_ids) != len(set(option_ids))):
                    raise SageRequestError(f"question {question.question_id!r} needs 2..{MAX_IMAGE_CHOICES} unique choice options")
                total_questions += 1
            stable_questions = tuple(BatchQuestion(q.question_id, q.instructions,
                                    tuple(copy.deepcopy(dict(option)) for option in q.options))
                                     for q in group.questions)
            stable_groups.append(BatchRequestGroup(copy.deepcopy(group.content), stable_questions))
        if total_questions > self.max_batch_questions:
            raise SageRequestError(
                f"batch has {total_questions} questions; configured local maximum is {self.max_batch_questions}"
            )

        started = time.perf_counter()
        preparation_started = started
        payload_groups: list[dict[str, Any]] = []
        for group in stable_groups:
            if isinstance(group.content, ImageBatchContent):
                if not isinstance(group.content.text, str):
                    raise SageRequestError("image batch content needs text")
                media, _, _ = await asyncio.to_thread(
                    _prepare_image, Path(group.content.image_path), self.max_image_bytes,
                )
                if self.image_evidence_sink:
                    self.image_evidence_sink(media, {'question_ids':[q.question_id for q in group.questions],
                        'image_source_path':str(group.content.image_path), 'context':group.content.text,
                        'questions':[{'id':q.question_id,'instructions':q.instructions,'options':q.options} for q in group.questions],
                        'reasoning':reasoning,'latency_mode':latency_mode,'request_kind':'batch'})
                content: str | dict[str, Any] = {"kind": "image", "media": media, "text": group.content.text}
            elif isinstance(group.content, str) and group.content.strip():
                content = group.content
            else:
                raise SageRequestError("batch content must be non-empty text or ImageBatchContent")
            payload_groups.append({
                "content": content,
                "questions": [{
                    "id": question.question_id,
                    "kind": "choice",
                    "instructions": question.instructions,
                    "options": [dict(option) for option in question.options],
                } for question in group.questions],
            })
        preparation_ms = (time.perf_counter() - preparation_started) * 1000
        payload = {"reasoning": reasoning, "requests": payload_groups}
        if latency_mode != "fast":
            payload["latency_mode"] = latency_mode
        request_started = time.perf_counter()
        try:
            response = await self._client.post("/decide/batch", json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise SageTransientError(f"Sage batch request failed before a response: {type(exc).__name__}") from exc
        request_ms = (time.perf_counter() - request_started) * 1000
        _raise_for_http_status(response, self._api_key)
        parse_started = time.perf_counter()
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise SageResponseError("Sage batch returned invalid JSON") from exc
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise SageResponseError("Sage batch response is missing results")
        result_groups = body["results"]
        if len(result_groups) != len(groups):
            raise SageResponseError("Sage batch group count does not match the request")
        parsed_groups: list[SageBatchGroupResult] = []
        for group_index, (request_group, result_group) in enumerate(zip(stable_groups, result_groups)):
            if not isinstance(result_group, dict) or not isinstance(result_group.get("answers"), list):
                raise SageResponseError(f"Sage batch result group {group_index} is malformed")
            answer_rows = result_group["answers"]
            if len(answer_rows) != len(request_group.questions):
                raise SageResponseError(f"Sage batch answer count differs in group {group_index}")
            parsed_answers: list[SageBatchAnswer] = []
            for question, answer in zip(request_group.questions, answer_rows):
                if not isinstance(answer, dict) or not isinstance(answer.get("ok"), bool):
                    raise SageResponseError(f"Sage batch answer for {question.question_id!r} is malformed")
                if answer["ok"] is False:
                    error_text = answer.get("error")
                    if not isinstance(error_text, str) or not error_text:
                        raise SageResponseError(f"Sage batch error answer for {question.question_id!r} is malformed")
                    parsed_answers.append(SageBatchAnswer(
                        question.question_id, False, "choice", None, None,
                        error_text, {}, copy.deepcopy(answer),
                    ))
                    continue
                result = answer.get("result")
                if (not isinstance(result, dict) or result.get("id") != question.question_id
                        or result.get("kind") != "choice"):
                    raise SageResponseError(f"Sage batch question ID/kind mismatch for {question.question_id!r}")
                decision = result.get("result")
                if not isinstance(decision, dict) or "chosen" not in decision:
                    raise SageResponseError(f"Sage batch choice result is malformed for {question.question_id!r}")
                chosen = decision["chosen"]
                allowed = {item["option"] for item in question.options}
                if chosen is not None and (not isinstance(chosen, str) or chosen not in allowed):
                    raise SageResponseError(f"Sage batch selected an unknown option for {question.question_id!r}")
                answer_meta = result.get("meta", {})
                if not isinstance(answer_meta, dict):
                    raise SageResponseError(f"Sage batch answer metadata is malformed for {question.question_id!r}")
                meta = answer_meta
                parsed_answers.append(SageBatchAnswer(
                    question.question_id, True, "choice", copy.deepcopy(result), chosen, None,
                    copy.deepcopy(meta), copy.deepcopy(answer),
                ))
            parsed_groups.append(SageBatchGroupResult(group_index, tuple(parsed_answers)))
        if "meta" in body and not isinstance(body["meta"], dict):
            raise SageResponseError("Sage batch metadata is malformed")
        meta = body.get("meta", {})
        if "request_count" in meta and (isinstance(meta["request_count"], bool)
                or not isinstance(meta["request_count"], int) or meta["request_count"] != len(groups)):
            raise SageResponseError("Sage batch metadata request_count mismatch")
        if "question_count" in meta and (isinstance(meta["question_count"], bool)
                or not isinstance(meta["question_count"], int) or meta["question_count"] != total_questions):
            raise SageResponseError("Sage batch metadata question_count mismatch")
        parse_ms = (time.perf_counter() - parse_started) * 1000
        return SageBatchResponse(tuple(parsed_groups), copy.deepcopy(meta), len(groups), total_questions,
                                 preparation_ms, request_ms, parse_ms,
                                 (time.perf_counter() - started) * 1000, copy.deepcopy(body))

    async def decide_image_choice(
        self,
        image_path: Path,
        text: str,
        instructions: str,
        candidates: CandidateSet,
        envelope: DecisionEnvelope,
        reasoning: str = "off",
    ) -> SageDecision:
        if reasoning not in {"off", "auto", "on"}:
            raise ValueError("reasoning must be 'off', 'auto', or 'on'")
        if envelope.candidate_set_version != candidates.version:
            raise SageRequestError("Candidate set changed after request envelope was created")
        candidate_bindings = {item.option: item.binding for item in candidates.options}
        if envelope.action_bindings != candidate_bindings:
            raise SageRequestError("Action bindings changed after the decision envelope was created")
        options_snapshot = tuple({"option": item.option, "description": item.description} for item in candidates.options)
        if envelope.candidate_options != options_snapshot:
            raise SageRequestError("Candidate descriptions changed after the decision envelope was created")
        media, _, _ = await asyncio.to_thread(_prepare_image, image_path, self.max_image_bytes)
        if self.image_evidence_sink:
            self.image_evidence_sink(media, {'request_id':envelope.request_id,
                'frame_id':envelope.captured_frame_id,'session_epoch':envelope.session_epoch,
                'image_source_path':str(image_path),'request_kind':'choice'})
        payload: dict[str, Any] = {
            "content": {"kind": "image", "media": media, **({"text": text} if text.strip() else {})},
            "question": {
                "id": envelope.request_id,
                "kind": "choice",
                "instructions": instructions,
                "options": [{"option": item.option, "description": item.description} for item in candidates.options],
            },
            "reasoning": reasoning,
        }
        started = time.perf_counter()
        try:
            response = await self._client.post("/decide", json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise SageTransientError(f"Sage request failed before a response: {type(exc).__name__}") from exc
        elapsed_ms = (time.perf_counter() - started) * 1000
        _raise_for_http_status(response, self._api_key)
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise SageResponseError("Sage returned invalid JSON") from exc
        if body.get("id") != envelope.request_id or body.get("kind") != "choice":
            raise SageResponseError("Sage response ID or kind does not match the submitted request")
        result = body.get("result")
        if not isinstance(result, dict) or "chosen" not in result:
            raise SageResponseError("Sage Choice response is missing result.chosen")
        chosen = result["chosen"]
        by_id = {item.option: item for item in candidates.options}
        if chosen is not None and chosen not in by_id:
            raise SageResponseError("Sage selected an option outside the submitted candidate set")
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        probability = result.get("probability")
        return SageDecision(
            envelope=envelope,
            chosen=chosen,
            selected_binding=by_id[chosen].binding if chosen is not None else None,
            probability=probability if isinstance(probability, (int, float)) else None,
            probabilities=tuple(result.get("probabilities", [])),
            model=meta.get("model"),
            server_latency_ms=meta.get("latency_ms"),
            reasoning=meta.get("reasoning"),
            usage=meta.get("usage") if isinstance(meta.get("usage"), dict) else {},
            request_duration_ms=elapsed_ms,
            option_ids=tuple(by_id),
        )

    async def decide_grounded_choice(self, text: str, instructions: str, options: list[dict[str, str]], request_id: str) -> dict:
        """Separate text-only Sage grounding. Call only after a logged Sage research choice."""
        payload = {"content": text, "question": {"id": request_id, "kind": "choice",
                   "instructions": instructions, "options": options},
                   "grounding": {"trigger": "always"}, "reasoning": "auto"}
        try:
            response = await self._client.post("/decide", json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise SageTransientError(f"Sage request failed before a response: {type(exc).__name__}") from exc
        _raise_for_http_status(response, self._api_key)
        body = response.json()
        if body.get("id") != request_id or body.get("kind") != "choice":
            raise SageResponseError("Grounded response envelope mismatch")
        chosen = body.get("result", {}).get("chosen")
        if chosen is not None and chosen not in {item['option'] for item in options}:
            raise SageResponseError("Grounded response selected an unknown option")
        return body

    async def close(self) -> None:
        await self._client.aclose()


def _error_detail(response: httpx.Response, secret: str = "") -> str:
    try:
        body = response.json()
        detail = body.get("detail", body)
    except (json.JSONDecodeError, AttributeError):
        detail = response.text[:500]
    safe_detail = str(detail).replace(secret, "[REDACTED]") if secret else str(detail)
    return f"HTTP {response.status_code}: {safe_detail}"


def _raise_for_http_status(response: httpx.Response, secret: str) -> None:
    """Share retry/auth/allowance classification across image and grounding calls."""
    status=response.status_code
    detail=_error_detail(response,secret)
    if status in {408,429} or 500<=status<600:
        raise SageTransientError(detail)
    if status==401:
        raise SageAuthenticationError(detail)
    if status==402:
        raise SageAllowanceError(detail)
    if status>=400:
        raise SageRequestError(detail)

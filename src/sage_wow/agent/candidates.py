"""Central, fail-closed compilation of Sage action choices."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from sage_wow.sage.client import MAX_IMAGE_CHOICES, CandidateSet, ChoiceOption


class CandidateConfigurationError(ValueError):
    def __init__(self, code: str, message: str, option_count: int | None = None,
                 reserved_options: int = 0, max_options: int = MAX_IMAGE_CHOICES,
                 duplicates: tuple[str, ...] = ()):
        super().__init__(message)
        self.code = code
        self.message = message
        self.option_count = option_count
        self.reserved_options = reserved_options
        self.max_options = max_options
        self.duplicates = duplicates

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "option_count": self.option_count,
            "reserved_options": self.reserved_options,
            "max_options": self.max_options,
            "duplicates": list(self.duplicates),
        }


def compile_candidates(
    candidates: Iterable[Any],
    *,
    max_options: int = MAX_IMAGE_CHOICES,
    reserved_options: int = 0,
) -> CandidateSet:
    """Snapshot and validate choices; never silently truncate an option list.

    `reserved_options` accounts for options a caller will append after this
    compilation stage. It consumes the same hard model budget without being
    added to the returned set.
    """
    items = list(candidates)
    if isinstance(max_options, bool) or not isinstance(max_options, int) or not 2 <= max_options <= MAX_IMAGE_CHOICES:
        raise CandidateConfigurationError(
            "invalid_budget", f"max_options must be an integer from 2 to {MAX_IMAGE_CHOICES}",
            len(items), reserved_options, max_options if isinstance(max_options, int) else MAX_IMAGE_CHOICES,
        )
    if (isinstance(reserved_options, bool) or not isinstance(reserved_options, int)
            or reserved_options < 0 or reserved_options >= max_options):
        raise CandidateConfigurationError(
            "invalid_reserved_budget", "reserved_options must be a non-negative integer smaller than max_options",
            len(items), reserved_options if isinstance(reserved_options, int) else 0, max_options,
        )
    options = [getattr(item, "option", None) for item in items]
    if any(not isinstance(option, str) or not option.strip() for option in options):
        raise CandidateConfigurationError("invalid_option_id", "every candidate needs a non-empty string option id",
                                          len(items), reserved_options, max_options)
    duplicates = tuple(sorted({option for option in options if options.count(option) > 1}))
    if duplicates:
        raise CandidateConfigurationError("duplicate_option_ids", "candidate option ids must be unique",
                                          len(items), reserved_options, max_options, duplicates)
    if not 2 <= len(items):
        raise CandidateConfigurationError("too_few_options", "Image Choice needs at least 2 options",
                                          len(items), reserved_options, max_options)
    if len(items) + reserved_options > max_options:
        raise CandidateConfigurationError(
            "option_budget_exceeded",
            f"{len(items)} candidates plus {reserved_options} reserved options exceeds budget {max_options}",
            len(items), reserved_options, max_options,
        )

    payload = []
    frozen_options = []
    try:
        for item in items:
            binding = json.loads(json.dumps(item.binding, sort_keys=True, separators=(",", ":"), allow_nan=False))
            description = str(item.description)
            payload.append({"option": item.option, "description": description, "binding": binding})
            frozen_options.append(ChoiceOption(item.option, description, binding))
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, AttributeError) as exc:
        raise CandidateConfigurationError("non_json_candidate", f"candidate data must be JSON-safe: {exc}",
                                          len(items), reserved_options, max_options) from exc
    version = hashlib.sha256(encoded).hexdigest()[:16]
    try:
        return CandidateSet(version, tuple(frozen_options))
    except ValueError as exc:
        raise CandidateConfigurationError("invalid_candidate_set", str(exc), len(items), reserved_options,
                                          max_options) from exc

from __future__ import annotations

import math
from typing import Any


def estimate_decision_units(usage: dict[str, Any], *, question_count: int = 1, grounding_searches: int = 0) -> int | None:
    """Estimate billed units from Sage's returned usage; never invent absent usage fields."""
    if not usage:
        return None
    billed_tokens = usage.get("billed_input_tokens")
    image_count = usage.get("image_count")
    if billed_tokens is None and image_count is None and grounding_searches == 0:
        return None
    token_units = math.ceil(int(billed_tokens) / 4000) if billed_tokens is not None else question_count
    image_units = int(image_count) if image_count is not None else 0
    return max(question_count, token_units) + image_units + grounding_searches

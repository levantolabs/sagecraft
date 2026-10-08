import pytest

from sage_wow.agent.candidates import CandidateConfigurationError, compile_candidates
from sage_wow.agent.cycle import ActionCandidate


def choices(n=2):
    return [ActionCandidate(f"option_{i}", f"Choice {i}", {"type": "wait", "seconds": i + 1})
            for i in range(n)]


def test_candidate_compiler_rejects_duplicate_ids_with_typed_details():
    items = choices()
    items[1] = ActionCandidate(items[0].option, "Duplicate", {"type": "observe_only"})
    with pytest.raises(CandidateConfigurationError) as caught:
        compile_candidates(items)
    assert caught.value.code == "duplicate_option_ids"
    assert caught.value.duplicates == ("option_0",)


def test_candidate_compiler_refuses_over_budget_instead_of_truncating():
    with pytest.raises(CandidateConfigurationError, match="exceeds budget") as caught:
        compile_candidates(choices(18), max_options=20, reserved_options=3)
    assert caught.value.code == "option_budget_exceeded"
    assert caught.value.option_count == 18
    assert caught.value.reserved_options == 3


def test_candidate_compiler_returns_stable_snapshot_and_validates_json_finiteness():
    source = choices()
    compiled = compile_candidates(source)
    source[0].binding["nested"] = {"keys": [1, 2]}
    assert "nested" not in compiled.options[0].binding
    bad = choices()
    bad[0].binding["not_json"] = float("nan")
    with pytest.raises(CandidateConfigurationError) as caught:
        compile_candidates(bad)
    assert caught.value.code == "non_json_candidate"

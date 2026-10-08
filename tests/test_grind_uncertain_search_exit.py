"""Unresolved selection sensing can yield to Sage's deliberate relocation."""
import asyncio
from copy import deepcopy

from test_grind_inspection_source_handoff import SourceRig
from test_grind_interrupted_sensing import interrupted


def test_uncertain_acquisition_can_choose_and_retain_relocation(tmp_path, monkeypatch):
    async def run():
        r = SourceRig(tmp_path / 'run')
        try:
            await interrupted(r, monkeypatch)
            await r.choose('cannot_assess')
            await r.choose('cannot_assess')
            await r.choose(None)
            await r.choose(None)
            h = r.c.hunt
            assert h.recovery_requested and h.confirmed_target_absences == 0
            lineage = r.c.target_inspection_episode['lineage']
            key = lineage['question_key']
            debt = deepcopy(h.action_failures)
            history = deepcopy(h.target_history)
            keys = list(r.physical())
            result = await r.choose('change_search_strategy')
            assert result.status == 'dispatched' and not result.receipt['possible_input']
            assert h.phase == 'choose_area' and h.planning_requested
            await r.choose(None)
            assert 'explore_visible' in r.sage.calls[-1]['options']
            assert 'change_search_strategy' not in r.sage.calls[-1]['options']
            await r.choose('explore_visible')
            assert h.phase == 'travel' and not h.planning_requested
            assert r.physical() == keys
            await r.choose('detour_backward')
            assert h.pending['family'] == 'motion' and h.pending['purpose'] == 'travel'
            assert len(r.physical()) > len(keys)
            assert r.events('dispatch_guard_checked')[-1]['approved']
            assert h.action_failures == debt and h.target_history == history
            assert h.question_debt[key] == 2 and len(lineage['request_ids']) == 2
            assert h.confirmed_target_absences == 0 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())

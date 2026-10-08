"""Real focus reconciliation and finite clear failure paths, with fake input."""
import asyncio
from copy import deepcopy
import json

from sage_wow.agent.grind_relocation import selected_disposition
from test_grind_selected_task_exit import disposed, exhaust
from test_grind_target_inspection_contract import ContractRig


def test_focus_world_confirmation_requires_fresh_explicit_selected_task_resume(tmp_path, monkeypatch):
    async def run():
        r = ContractRig(tmp_path / 'focus')
        try:
            _, intent = await disposed(r, monkeypatch)
            c, h = r.c, r.c.hunt
            await r.choose(None)
            menu = deepcopy(h.travel_policy['retry_menu'])
            source = deepcopy(intent['source'])
            episode = deepcopy(c.target_inspection_episode)
            plan = deepcopy(h.plan)
            physical = deepcopy(r.physical())
            observers = len(r.wire)
            c.pause_focus()
            assert c.paused and c.resume_focus()
            assert (await r.choose('world_normal_confirmed')).status == 'dispatched'
            frame = r.world.capture()
            target = await c.target_proposal(frame)
            assert selected_disposition(c, frame, target)['status'] == 'temporarily_unproven'
            assert intent['source'] == source
            await r.choose(None)
            offered = r.sage.calls[-1]['options']
            assert 'resume_destination' in offered and 'reject_selected_target' in offered
            assert not any(k.startswith(('attack_', 'detour_', 'turn_')) for k in offered)
            assert h.travel_policy['retry_menu'] == menu
            assert r.physical() == physical and len(r.wire) == observers
            result = await r.choose('resume_destination')
            assert result.status == 'dispatched' and not result.receipt['possible_input']
            assert intent['source']['scope']['session_epoch'] == c.cycle.session_epoch
            assert intent['original_handoff']['source'] == source
            assert h.plan['request_id'] == plan['request_id'] and h.plan['area'] == plan['area']
            assert h.travel_policy['retry_menu'] == menu
            assert c.target_inspection_episode['id'] == episode['id']
            assert c.target_inspection_episode['attempts'] == episode['attempts'] == 2
            assert r.physical() == physical and len(r.wire) == observers
            frame = r.world.capture()
            assert selected_disposition(c, frame, await c.target_proposal(frame))['status'] == 'matching'
            await r.choose(None)
            assert h.phase == 'travel'
            assert 'resume_destination' not in r.sage.calls[-1]['options']
            assert not h.progress_facts and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_two_actual_failed_selected_clears_wait_with_visible_precise_cause(tmp_path, monkeypatch):
    async def run():
        r = ContractRig(tmp_path / 'clear-exhausted')
        try:
            _, intent = await disposed(r, monkeypatch)
            h = r.c.hunt
            await exhaust(r)
            menu = deepcopy(h.travel_policy['retry_menu'])
            for attempt in (1, 2):
                assert (await r.choose('reject_selected_target')).status == 'dispatched'
                r.c.wait_until=0
                continuation=await r.c.process(r.world.capture())
                assert continuation.status=='dispatched' and continuation.decision is None
                assert h.pending['clear_pair']['total_escape_limit']==2
                assert (await r.choose('clear_failed')).status == 'dispatched'
                assert h.pending is None and h.failures['clear'] == attempt
            assert not h.allowed('reject', 'clear')
            calls = len(r.sage.calls)
            physical = deepcopy(r.physical())
            result = await r.choose()
            assert result.status == 'grind_navigation_wait'
            assert r.c.navigation_wait['reason'] == 'selected_task_clear_exhausted'
            checkpoint = r.store.connection.execute(
                "select payload_json from checkpoints where checkpoint_key='grind_only'").fetchone()
            assert json.loads(checkpoint[0])['navigation_wait'] == r.c.navigation_wait
            assert len(r.sage.calls) == calls and r.physical() == physical
            assert h.travel_policy['retry_menu'] == menu and intent['disposition'] == 'handed_off'
            assert h.failures['clear'] == 2 and not h.progress_facts and not h.credited_kills
            again = await r.choose()
            assert again.status == 'grind_navigation_wait'
            assert len(r.sage.calls) == calls and r.physical() == physical
        finally:
            await r.close()
    asyncio.run(run())


def test_unchanged_exit_null_then_auto_null_waits_without_focus_or_plan_refill(tmp_path, monkeypatch):
    async def run():
        r = ContractRig(tmp_path / 'exit-abstention')
        try:
            _, intent = await disposed(r, monkeypatch)
            c, h = r.c, r.c.hunt
            modes = []
            original = r.sage.decide_image_choice
            async def tracked(*args):
                modes.append(args[-1])
                return await original(*args)
            r.sage.decide_image_choice = tracked
            await exhaust(r)  # First actual null on the offered clear question.
            assert 'reject_selected_target' in r.sage.calls[-1]['options']
            await r.choose(None)
            assert modes[-2:] == ['off', 'auto']
            assert h.unclear == 2
            calls, observers = len(r.sage.calls), len(r.wire)
            physical = deepcopy(r.physical())
            debt = deepcopy(h.question_debt)
            menu = deepcopy(h.travel_policy['retry_menu'])
            for _ in range(2):
                result = await r.choose()
                assert result.status == 'grind_navigation_wait'
                assert c.navigation_wait['reason'] == 'selected_task_exit_unselected'
            h.plan['request_id'] = 'same-goal-observation-alias'
            assert (await r.choose()).status == 'grind_navigation_wait'
            assert len(r.sage.calls) == calls and len(r.wire) == observers
            c.pause_focus()
            assert c.resume_focus()
            assert (await r.choose('world_normal_confirmed')).status == 'dispatched'
            after_world = len(r.sage.calls)
            assert (await r.choose()).status == 'grind_navigation_wait'
            assert c.navigation_wait['reason'] == 'selected_task_exit_unselected'
            assert len(r.sage.calls) == after_world == calls + 1
            assert r.physical() == physical and len(r.wire) == observers
            assert h.question_debt == debt and h.travel_policy['retry_menu'] == menu
            assert intent['disposition'] == 'handed_off' and h.allowed('reject', 'clear')
            assert not h.progress_facts and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())

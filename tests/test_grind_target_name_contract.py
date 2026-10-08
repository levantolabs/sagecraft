"""Malformed perception is recoverable before native bindings; all IO is fake."""
import asyncio

import pytest

from sage_wow.control.executor import ExecutionRejected, SafeExecutor
from sage_wow.control.target_names import plain_target_name
from test_executor import FakeBackend, valid_gate
from test_grind_exact_band_observation import start_band
from test_grind_loot_integration import LootRig, previous_target_setup
from test_grind_product_spec import Rig
from test_grind_target_inspection_contract import ContractRig


BAD_NAMES = ("Other Creature™", "Other\\Creature", "Other\nCreature",
             "Other/Creature", " Other Creature", "Other Creature ", "A" * 65)


@pytest.mark.parametrize("name", [*BAD_NAMES, "", None, 7])
def test_plain_name_rejects_unsupported_raw_data(name):
    assert not plain_target_name(name)


@pytest.mark.parametrize("name", ["Other Creature", "Watcher O'Brien", "Long-Fang", "A" * 64])
def test_plain_name_preserves_supported_names(name):
    assert plain_target_name(name)


@pytest.mark.parametrize("name", ["Watcher O'Brien", "Long-Fang"])
def test_supported_punctuation_reaches_named_executor_unchanged(name):
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=30)
    async def run():
        executor.arm()
        try:
            result = await executor.execute({"type": "target_named", "name": name})
            assert result["name"] == name
        finally: await executor.stop()
    asyncio.run(run())
    assert ("text", "/targetexact " + name) in backend.events


@pytest.mark.parametrize("kind", ["target_named", "target_previous", "target_and_cast", "cast_burst", "cast_guarded"])
@pytest.mark.parametrize("name", BAD_NAMES)
def test_executor_rejects_invalid_name_before_any_input(kind, name):
    backend = FakeBackend()
    async def guard(*args):
        pytest.fail("Malformed binding must fail before perception or native input")
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=30,
                            batch_guard=guard, batch_post_guard=guard)
    binding = {"type": kind, "name" if kind in {"target_named", "target_and_cast"}
               else "expected_target_name": name}
    if kind in {"target_and_cast", "cast_guarded", "cast_burst"}:
        binding["spell"] = "Smite"
    if kind == "cast_burst": binding.update(count=3, interval_seconds=2.2)
    async def run():
        executor.arm()
        try:
            with pytest.raises(ExecutionRejected, match="plain"):
                await executor.execute(binding)
        finally: await executor.stop()
    asyncio.run(run())
    assert backend.events == []


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("name", ["Young Wolf™", "Young\\Wolf"])
def test_invalid_single_and_burst_attack_are_withheld_then_fresh_name_resumes(tmp_path, count, name):
    async def run():
        r = Rig(tmp_path)
        r.c.config.update(committed_combat=True, smite_burst_count=count, cast_wait_seconds=2.2)
        async def guard(*args): return {"continue": True, "frame_id": "offline-intercast"}
        async def sleep(seconds): pass
        r.executor.batch_guard = guard
        r.executor._batch_sleep = sleep
        try:
            await r.start()
            r.world.name = name
            assert (await r.c.target_proposal(r.world.capture()))["name"] == name
            generation = r.c.cycle.input_generation
            await r.choose(None)
            options = r.sage.calls[-1]["options"]
            assert not any(key.startswith("attack_mob_level_") for key in options)
            assert not (r.c.config.get('selected_target_observer') or {}).get('enabled')
            assert "reinspect_selected_frame" not in options
            assert 'change_search_strategy' in options
            assert r.c.cycle.input_generation == generation and not r.physical_keys()
            assert r.executor._armed and not r.c.stopped and not r.c.hunt.credited_kills
            # Fresh valid evidence, not a canonicalized or sanitized old name.
            r.world.name = "Young Wolf"
            result = await r.choose("attack_mob_level_1")
            assert result.receipt["selected_binding"]["expected_target_name"] == "Young Wolf"
            assert len([event for event in r.casts() if event[1].startswith("/cast ")]) == count
            assert r.executor._armed and not r.c.stopped and not r.c.hunt.credited_kills
        finally: await r.close()
    asyncio.run(run())


def test_complete_observer_facts_do_not_renew_persistent_invalid_name_budget(tmp_path):
    async def run():
        r = ContractRig(tmp_path / "persistent-invalid")
        try:
            await start_band(r, own=1)
            r.c.config["committed_combat"] = True
            r.world.name = "Other Creature™"
            r.world.target_level = 1
            r.answers["level"] = "1"
            r.drop_level = False
            # Level/type/life are complete; only the fused raw name is invalid.
            await r.choose("reinspect_selected_frame")
            for _ in range(2):
                assert (await r.observe_target()).status == "grind_reobserve"
            r.c.observer_last_at = 0
            result = await r.choose("change_search_strategy")
            assert result.status == "dispatched"
            assert len(r.wire) == 2 and not r.physical()
            assert r.c.target_inspection_episode["exhausted"]
            assert r.c.hunt.planning_requested
            options = r.sage.calls[-1]["options"]
            assert not any(key.startswith("attack_mob_level_") for key in options)
            assert "reinspect_selected_frame" not in options
            assert {"recover_now", "cannot_assess"} <= options.keys()
            assert "dead_or_unrecoverable" not in options
            r.c.observer_last_at = 0
            await r.choose("explore_visible")
            assert len(r.wire) == 2 and r.c.hunt.phase == "travel"
            assert not r.c.hunt.credited_kills and r.executor._armed
        finally: await r.close()
    asyncio.run(run())


def test_fresh_valid_source_reconciles_after_one_malformed_observation(tmp_path):
    async def run():
        r = ContractRig(tmp_path / "fresh-valid")
        try:
            await start_band(r, own=1)
            r.c.config["committed_combat"] = True
            r.world.name = "Other Creature™"
            r.world.target_level = 1
            r.answers["level"] = "1"
            r.drop_level = False
            await r.choose("reinspect_selected_frame")
            assert (await r.observe_target()).status == "grind_reobserve"
            r.world.name = "Other Creature"
            assert (await r.observe_target()).status == "grind_reobserve"
            result = await r.choose("attack_mob_level_1")
            assert result.status == "dispatched"
            assert result.receipt["selected_binding"]["expected_target_name"] == "Other Creature"
            assert len(r.wire) == 2 and r.c.target_inspection_episode is None
            assert r.executor._armed and not r.c.stopped and not r.c.hunt.credited_kills
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["creation", "restored_authority"])
def test_malformed_previous_target_authority_is_never_an_executable_choice(tmp_path, stage):
    async def run():
        r = LootRig(tmp_path)
        r.c.config.update(loot_enabled=True, committed_combat=True)
        try:
            await r.start()
            await previous_target_setup(r)
            if stage == "creation":
                # Historical retained data must be filtered before authority creation.
                r.c.hunt.loot_request["target"]["name"] = "Young Wolf™"
                r.c.hunt.recent_combat["target"]["name"] = "Young Wolf™"
            else:
                await r.loot(None)
                source = r.c.loot.data["death_sources"][0]
                assert source["previous_target_authority"]
                source["previous_target_authority"]["expected_target_name"] = "Young Wolf™"
            generation = r.c.cycle.input_generation
            keys = list(r.physical_keys())
            await r.loot(None)
            assert "target_recent_corpse" not in r.sage.calls[-1]["options"]
            assert r.c.cycle.input_generation == generation and r.physical_keys() == keys
            assert r.executor._armed and not r.c.stopped and not r.c.hunt.credited_kills
            if stage == "creation":
                assert r.c.loot.data["death_sources"][0]["previous_target_authority"] is None
        finally: await r.close()
    asyncio.run(run())

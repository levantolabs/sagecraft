

from sage_wow.agent.looting import LootSweep


def test_loot_requires_two_distinct_observations_after_interaction():
    state = {'loot_pending': True}
    sweep = LootSweep(state)
    assert 'loot_verified' not in {c.option for c in sweep.candidates()}
    sweep.observe('loot_verified', 'before')
    assert sweep.pending
    sweep.interacted()
    assert sweep.pending
    sweep.observe('loot_verified', 'one')
    sweep.observe('loot_verified', 'one')
    assert sweep.pending
    # Resume from a durable checkpoint without losing the obligation.
    sweep = LootSweep(state)
    sweep.observe('loot_verified', 'two')
    assert not sweep.pending


def test_combat_interrupt_and_additional_kills_preserve_sweep():
    sweep = LootSweep({})
    sweep.suspect_kill('kill1')
    sweep.interacted()
    sweep.observe('loot_verified', 'one')
    sweep.observe('under_attack', 'two')
    assert sweep.pending and sweep.data['confirmations'] == 0
    sweep.suspect_kill('kill2')
    sweep.suspect_kill('kill2')
    assert sweep.data['suspected_kills'] == ['kill1', 'kill2']
    assert sweep.pending
    assert sweep.data['attempts'] == 0


def test_uncertainty_requires_active_search_before_unavailable():
    sweep = LootSweep({})
    sweep.suspect_kill('kill')
    bindings = {'turn_left': {'keycode': 123, 'verified_from': 'test'}}
    sweep.observe('loot_uncertain', 'one')
    sweep.observe('loot_uncertain', 'two')
    choices = {c.option: c for c in sweep.candidates(bindings)}
    assert 'loot_uncertain' not in choices
    assert 'loot_unavailable' not in choices
    assert choices['loot_search'].binding['type'] == 'keypress'
    sweep.observe('loot_unavailable', 'premature')
    assert sweep.pending
    for _ in range(8):
        sweep.searched()
    choices = {c.option for c in sweep.candidates(bindings)}
    assert 'loot_unavailable' in choices
    assert not {'loot_search', 'loot_uncertain'} & choices
    sweep.observe('loot_unavailable', 'after_search')
    assert not sweep.pending
    assert sweep.data['outcome'] == 'unavailable_after_search_not_verified_looted'

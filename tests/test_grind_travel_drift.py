"""Receipt-linked drift regressions from a live run; synthetic IO, no live client."""
import asyncio

import pytest

from test_grind_travel_acceptance import TravelRig


@pytest.mark.parametrize('transform', [
    lambda p: p,
    lambda p: [100-p[0], p[1]],
    lambda p: [p[1], 100-p[0]],
])
def test_two_resolved_steps_reveal_drift_hidden_by_per_step_precision(tmp_path, transform):
    async def run():
        r=TravelRig(tmp_path)
        try:
            r.position=transform([30.4,72.1])
            await r.travel(transform([28.5,74.2]))
            await r.action('probe_forward')
            r.position=transform([30.3,71.9])
            await r.action('probe_forward')
            first=r.sage.calls[-1]
            assert 'Direction still current: yes' in first['prompt']
            r.position=transform([30.2,71.7])
            await r.action('turn_left')
            call=r.sage.calls[-1]
            assert 'Measured forward movement takes us farther' in call['instructions']
            assert not {'probe_forward','advance_forward','detour_forward'} & call['options'].keys()
            assert r.c.hunt.last_completed_action['progress_status']=='unresolved_precision'
            # The individual latest step remains uncertain; only the linked
            # endpoint window supports the accumulated direction/progress.
            assert r.c.hunt.last_completed_action['forward_progress_measurement']['progress_status']=='farther'
            assert 'Linked forward movement: 2 completed actions' in call['prompt']
        finally:
            await r.close()
    asyncio.run(run())


def test_measured_heading_is_not_described_as_an_unmeasured_first_probe(tmp_path):
    async def run():
        r=TravelRig(tmp_path)
        try:
            r.position=[30.4,72.1]
            await r.travel([28.5,74.2])
            await r.action('probe_forward');r.position=[30.3,71.9]
            await r.action('probe_forward')
            call=r.sage.calls[-1]
            assert 'Direction still current: yes' in call['prompt']
            assert 'have not measured where forward takes us' not in call['instructions']
            assert 'progress' in call['instructions'].lower()
        finally:
            await r.close()
    asyncio.run(run())


def test_small_but_consistent_destination_progress_reopens_advance(tmp_path):
    async def run():
        r=TravelRig(tmp_path)
        try:
            r.position=[30.2,71.7]
            await r.travel([28.5,74.2])
            await r.action('probe_forward');r.position=[30.3,71.9]
            await r.action('probe_forward');r.position=[30.4,72.1]
            await r.action('advance_forward')
            assert 'Linked forward movement: 2 completed actions' in r.sage.calls[-1]['prompt']
            assert r.c.hunt.last_completed_action['progress_status']=='unresolved_precision'
            assert r.c.hunt.last_completed_action['forward_progress_measurement']['progress_status']=='closer'
        finally:
            await r.close()
    asyncio.run(run())


def test_unreadable_movement_endpoint_breaks_accumulated_drift(tmp_path):
    async def run():
        r=TravelRig(tmp_path)
        try:
            r.position=[30.4,72.1]
            await r.travel([28.5,74.2])
            await r.action('probe_forward');r.position=[30.3,71.9]
            await r.action('probe_forward');r.position=None
            await r.action('change_destination')
            r.position=[30.3,71.9]
            r.c.hunt.choose(dict(r.c.hunt.plan['area']),r.world.capture(),'fresh-observation',1)
            await r.action('probe_forward');r.position=[30.2,71.7]
            await r.action('turn_right')
            assert 'Measured forward movement takes us farther' not in r.sage.calls[-1]['instructions']
            assert 'Linked forward movement: 1 completed actions' in r.sage.calls[-1]['prompt']
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interrupt', ['turn', 'strafe', 'focus', 'zone', 'position'])
def test_drift_window_cannot_cross_changed_orientation_scope_or_endpoint(tmp_path, interrupt):
    async def run():
        r=TravelRig(tmp_path)
        try:
            r.position=[30.4,72.1]
            await r.travel([28.5,74.2])
            await r.action('probe_forward');r.position=[30.3,71.9]
            await r.action('turn_left' if interrupt=='turn' else 'detour_strafe_left' if interrupt=='strafe' else 'change_destination')
            if interrupt=='focus':
                r.c.pause_focus();r.c.resume_focus();await r.action('world_normal_confirmed')
            elif interrupt=='zone':
                r.zone='Another Offline Valley'
                r.c.hunt.plan['area']['zone_reference']=r.zone
            elif interrupt=='position':
                r.position=[30.5,72.2]
            if r.c.hunt.phase=='choose_area':
                r.c.hunt.choose(dict(r.c.hunt.plan['area']),r.world.capture(),'offline-reselect',1)
            await r.action('probe_forward')
            before=r.position[:]
            r.position=[before[0]-.1,before[1]-.2]
            await r.action('turn_right')
            call=r.sage.calls[-1]
            assert 'Measured forward movement takes us farther' not in call['instructions']
            assert 'probe_forward' in call['options']
            assert r.c.hunt.last_completed_action['probe_measurement']['displacement']==pytest.approx(.22360679775)
        finally:
            await r.close()
    asyncio.run(run())

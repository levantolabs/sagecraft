"""Independent offline travel/clean-observation acceptance; no OS or paid provider."""
import asyncio
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from sage_wow.perception.ocr import TextObservation
from test_grind_product_spec import Rig


class TravelRig(Rig):
    def __init__(self, directory, *, clean=False):
        super().__init__(directory)
        self.c.config.update(clean_world_observation=clean, move_seconds=.2, turn_seconds=.05)
        self.controls.update(strafe_left={'keycode': 20, 'verified_from': 'offline operator'},
                             strafe_right={'keycode': 21, 'verified_from': 'offline operator'},
                             toggle_hud={'keycodes': [58, 6], 'hold_seconds': .08,
                                         'verified_from': 'offline authorized hide/restore experiment'})
        layout = self.c.profile.values['calibration']['ui_layout']['regions']
        layout.update(coordinate_primary=[400, 35, 495, 60],
                      coordinate_alternate=[395, 30, 500, 65], coordinate_magnification=[400, 35, 495, 60], local_caption=[250, 0, 395, 25],
                      minimap=[400, 0, 500, 80])
        self.position = [10., 10.]; self.zone = 'Offline Valley'; self.hud = True
        self.trace = []; self.frames = {}; self.fail_toggle = None; self.toggle_count = 0
        self.after_hidden = None; self.pressed = set(); self.world.name = ''
        base_capture, base_ocr, base_key = self.world.capture, self.world.ocr, self.backend.key
        def capture():
            frame = base_capture()
            with Image.open(frame.image_path) as image:
                draw = ImageDraw.Draw(image)
                if self.hud:
                    draw.rectangle((0, 0, 149, 25), fill='#e0e0e0')
                    draw.text((40, 5), 'Test Player', fill='black')
                    draw.rectangle((400, 0, 499, 79), fill='#4477bb')
                    draw.text((260, 5), self.zone, fill='white')
                    if self.position is not None:
                        draw.text((405, 40), f'{self.position[0]:.1f}, {self.position[1]:.1f}', fill='white')
                else:
                    for box in (layout['player_frame'], layout['minimap'], layout['target_overlay']):
                        draw.rectangle(box, fill='#263746')
                draw.rectangle((160, 270, 190, 295), fill='#bd37e4')  # Clean terrain sentinel.
                image.save(frame.image_path)
            self.frames[frame.image_path] = {'hud': self.hud, 'position': self.position[:] if self.position else None,
                                             'zone': self.zone, 'frame': frame}
            self.trace.append(('capture', self.hud, frame.frame_id))
            if not self.hud and self.after_hidden: self.after_hidden()
            return frame
        def ocr(path):
            path = Path(path)
            point = self.position
            row = lambda text, x, y, w, h: TextObservation(text, .99, {'x': x, 'y': y, 'width': w, 'height': h})
            if path.name == 'coordinates.png':
                return [row(f'{point[0]:.1f}, {point[1]:.1f}', 0, 0, 100, 15)] if point else []
            if path.name == 'zone.png': return [row(self.zone, 0, 0, 100, 15)]
            if path.name in {Path(key).name for key, item in self.frames.items() if not item['hud']}: return []
            rows = base_ocr(path) + [row(self.zone, 260, 5, 100, 15)]
            if point: rows.append(row(f'{point[0]:.1f}, {point[1]:.1f}', 405, 40, 75, 15))
            return rows
        def key(code, down):
            self.trace.append(('key', code, down))
            if code == 6 and down and 58 in self.pressed:
                self.toggle_count += 1
                if self.fail_toggle == self.toggle_count: raise RuntimeError('offline uncertain HUD primitive')
            if code == 6 and not down and 58 in self.pressed:
                self.hud = not self.hud
                self.trace.append(('hud', self.hud))
            if down: self.pressed.add(code)
            else: self.pressed.discard(code)
            base_key(code, down)
        self.world.capture = capture; self.world.ocr = ocr
        self.c.capture = capture; self.c.ocr = ocr; self.backend.key = key
        provider = self.sage.decide_image_choice
        self.provider_views = []
        async def decide(path, *args, **kwargs):
            with Image.open(path) as image:
                self.provider_views.append({'pixels': set(image.convert('RGB').get_flattened_data()), 'size': image.size, 'world_corner': image.convert('RGB').getpixel((10, 10))})
            self.trace.append(('provider', self.hud, self.c.cycle.input_generation))
            return await provider(path, *args, **kwargs)
        self.sage.decide_image_choice = decide
    async def travel(self, destination=(20., 10.)):
        await self.start()
        area = {'id': 'offline_patch', 'label': 'Offline fixture destination', 'coordinate': list(destination),
                'zone_reference': self.zone, 'arrival_radius': .6,
                'source': {'kind': 'offline_fixture', 'claim': 'Synthetic test geometry only'}}
        self.c.hunt.choose(area, self.world.capture(), 'offline-area-choice', 1)
        self.trace.clear(); self.sage.calls.clear(); self.provider_views.clear()
    async def action(self, answer):
        result = await self.choose(answer)
        assert result.status == 'dispatched', (answer, result.status, result.detail)
        assert result.decision.chosen == answer
        return result


def travel_scenario(test):
    def wrapped(tmp_path):
        async def run():
            rig = TravelRig(tmp_path)
            try: await rig.travel(); await test(rig)
            finally: await rig.close()
        asyncio.run(run())
    return wrapped


@travel_scenario
async def test_numeric_feedback_and_two_zero_moves_change_the_actual_menu(r):
    first=await r.action('probe_forward')
    r.position=[11.,10.]
    await r.action('advance_forward')
    record=r.c.hunt.last_completed_action
    assert record['receipt_id']==first.receipt['receipt_id']
    assert record['purpose']=='probe' and record['progress']==pytest.approx(1)
    await r.action('advance_forward')  # Known straight input retains supported direction; failure debt remains.
    await r.action('detour_strafe_left')
    assert not {'probe_forward','advance_forward','detour_forward'} & r.sage.calls[-1]['options'].keys()
    assert len(r.sage.calls[-1]['options'])<=13 and not r.c.stopped
    failures=dict(r.c.hunt.travel_failures)
    await r.action('turn_left')
    await r.action('probe_forward')
    assert all(r.c.hunt.travel_failures.get(k,0)>=v for k,v in failures.items())


@travel_scenario
async def test_completed_move_facts_survive_redeclaration_turns_and_focus(r):
    await r.action('probe_forward');r.position=[10.4,10.]
    await r.action('advance_forward');r.position=[10.8,10.]
    await r.action('turn_left')
    retained=[item['receipt_id'] for item in r.c.hunt.recent_moves]
    r.c.hunt.choose(dict(r.c.hunt.plan['area']),r.world.capture(),'same-destination',1)
    assert [item['receipt_id'] for item in r.c.hunt.recent_moves]==retained
    await r.action('turn_right')
    assert r.c.hunt.last_completed_action['action']=='turn_left'
    assert r.c.hunt.last_completed_action['status']=='turn_completed'
    r.c.pause_focus()
    assert r.c.hunt.pending is None and r.c.hunt.motion.heading is None
    assert [item['receipt_id'] for item in r.c.hunt.recent_moves]==retained+[r.c.hunt.last_completed_action['receipt_id']]


@travel_scenario
async def test_eight_verified_translations_use_real_chords_without_invented_forward(r):
    result=await r.action('detour_forward_left')
    options=r.sage.calls[-1]['options']
    assert len(options)==13 and 'probe_forward' in options
    assert options['detour_forward_left'].binding['keycodes']==[r.controls['forward']['keycode'],20]
    downs=[item for item in result.receipt['input_steps'] if item['kind']=='key_down']
    ups=[item for item in result.receipt['input_steps'] if item['kind']=='key_up']
    assert len(downs)==len(ups)==2 and not r.pressed
    assert ups[0]['monotonic_at']-downs[-1]['monotonic_at']==pytest.approx(.2,abs=.08)
    r.position=[10.4,9.6]
    await r.action('detour_forward')
    assert r.c.hunt.forward_history is None  # Diagonal displacement never establishes forward.
    assert 'Current measured forward direction: unavailable' in r.sage.calls[-1]['prompt']
    assert 'no completed applicable forward measurement' in r.sage.calls[-1]['prompt']


async def clean_rig(directory):
    rig = TravelRig(directory, clean=True)
    await rig.travel()
    return rig


def test_hide_restore_precedes_provider_and_action_with_truthful_chained_receipts(tmp_path):
    async def run():
        r = await clean_rig(tmp_path)
        try:
            result = await r.action('probe_forward')
            provider_index = next(index for index, event in enumerate(r.trace) if event[0] == 'provider')
            hidden_index = next(index for index, event in enumerate(r.trace) if event[:2] == ('hud', False))
            restored_index = next(index for index, event in enumerate(r.trace) if event[:2] == ('hud', True))
            movement_index = next(index for index, event in enumerate(r.trace)
                                  if event == ('key', r.controls['forward']['keycode'], True))
            assert hidden_index < restored_index < provider_index < movement_index
            assert r.trace[provider_index][1] is True and r.hud
            tx = r.c.observation_transaction
            hide, restore = tx['hide_receipt'], tx['restore_receipt']
            assert hide['authorization_type'] == restore['authorization_type'] == 'user_authorized_observation'
            assert hide['authorization_id'] == restore['authorization_id'] == tx['transaction_id']
            assert hide['authorization_reference'] and restore['authorization_reference']
            assert hide['completed'] and restore['completed']
            assert hide['generation_after'] == restore['generation_before']
            assert restore['generation_after'] == result.receipt['generation_before']
            assert not tx['restoration_needed']
            assert (0xBD, 0x37, 0xE4) in r.provider_views[-1]['pixels']
            assert r.provider_views[-1]['world_corner'] == (0x26, 0x37, 0x46)
            context = json.dumps(r.sage.calls[-1]['context'])
            assert tx['hidden_captured_at'] in r.sage.calls[-1]['prompt']
            assert tx['restored_captured_at'] in r.sage.calls[-1]['prompt']
            assert len(r.sage.calls) == 1 and len(r.sage.calls[-1]['options']) <= 14
            assert set(r.sage.calls[-1]['options'])-{'target_enemy'} == {
                'probe_forward','detour_backward','detour_strafe_left','detour_strafe_right',
                'detour_forward_left','detour_forward_right','detour_backward_left','detour_backward_right',
                'turn_left','turn_right','begin_hunt','change_destination','urgent_state'}
            # The second measurable move has a real mapping despite observation-only generation changes.
            r.position = [10.2, 10.]
            await r.action('advance_forward')
            assert 'Current measured forward direction: approximately East' in r.sage.calls[-1]['prompt']
            assert r.c.hunt.last_completed_action['progress'] == pytest.approx(.2)
            # No resolved displacement preserves still-supported facing, not useful-motion credit.
            await r.action('turn_left')
            assert 'Current measured forward direction: approximately East' in r.sage.calls[-1]['prompt']
            assert r.c.hunt.last_completed_action['status']=='unresolved_precision'
            r.position = [10.4, 10.]
            await r.action('probe_forward')
            await r.action('turn_left')
            await r.action('detour_backward')
            assert 'Current measured forward direction: unavailable' in r.sage.calls[-1]['prompt']
            assert 'reason: turn completed' in r.sage.calls[-1]['prompt']
        finally: await r.close()
    asyncio.run(run())


def test_original_clean_source_expiry_vetoes_action_despite_fresh_restored_anchor(tmp_path, monkeypatch):
    async def run():
        r = await clean_rig(tmp_path)
        try:
            from sage_wow.agent import cycle
            from types import SimpleNamespace
            import time
            def expired_clock(): return time.time() + 13
            async def age_only_clean_evidence():
                monkeypatch.setattr(cycle, 'time', SimpleNamespace(time=expired_clock,
                                                                             monotonic=time.monotonic))
            r.sage.hook = age_only_clean_evidence
            result = await r.choose('probe_forward')
            assert result.status != 'dispatched'
            assert r.hud and r.toggle_count == 2
            assert r.controls['forward']['keycode'] not in r.physical_keys()
            tx = r.c.observation_transaction
            assert datetime.fromisoformat(tx['hidden_captured_at']) < datetime.fromisoformat(tx['restored_captured_at'])
            assert r.c.hunt.pending_action is None
        finally: await r.close()
    asyncio.run(run())


def test_actual_modal_preempts_hide_and_never_supplies_world_motion(tmp_path):
    async def run():
        r = await clean_rig(tmp_path)
        try:
            r.world.error = 'Loading'
            await r.action('loading_wait')
            assert r.toggle_count == 0 and not r.physical_keys()
            assert all(candidate.binding['type'] == 'observe_only'
                       for candidate in r.sage.calls[-1]['options'].values())
            assert 'probe_forward' not in r.sage.calls[-1]['options']
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['hide_unknown', 'restore_unknown', 'focus_after_hide'])
def test_ambiguous_or_interrupted_observation_has_no_world_input_or_blind_toggle(tmp_path, failure):
    async def run():
        r = await clean_rig(tmp_path)
        try:
            if failure == 'hide_unknown': r.fail_toggle = 1
            elif failure == 'restore_unknown': r.fail_toggle = 2
            else:
                current = r.executor.inspect_gate()
                gate = {'value': current}
                r.executor.inspect_gate = lambda: gate['value']
                def lose_focus(): gate['value'] = replace(current, foreground=False)
                r.after_hidden = lose_focus
            # The transaction must fail before a provider gameplay question.
            result = await r.c.process(r.world.capture())
            assert result.status != 'dispatched'
            assert not r.sage.calls
            assert r.controls['forward']['keycode'] not in r.physical_keys()
            assert r.toggle_count == (2 if failure == 'restore_unknown' else 1)
            assert r.c.observation_transaction['restoration_needed']
            assert r.c.hunt.pending_action is None
            previous = r.toggle_count
            await r.c.process(r.world.capture())
            assert r.toggle_count == previous and not r.sage.calls
        finally: await r.close()
    asyncio.run(run())

@travel_scenario
async def test_unresolved_coordinate_jitter_is_one_comparable_failed_approach(r):
    await r.action('probe_forward')
    r.position = [10.1, 10.]
    await r.action('probe_forward')
    r.position = [10., 10.]
    await r.action('detour_strafe_left')
    assert 'probe_forward' not in r.sage.calls[-1]['options']
    assert len(r.c.hunt.travel_failures) == 1
    assert next(iter(r.c.hunt.travel_failures.values())) == 2
    assert r.c.hunt.last_completed_action['suspected_collision_or_input_ineffectiveness']
    assert r.c.hunt.sector == 0 and r.c.hunt.phase == 'travel'

def test_unchanged_urgent_resume_roundtrips_do_not_reset_on_hud_child_inputs(tmp_path):
    async def run():
        r = await clean_rig(tmp_path)
        try:
            for count in (1, 2):
                await r.action('urgent_state')
                await r.action('recovered_resume')
                assert r.c.hunt.travel_urgent_debt['completed_roundtrips'] == count
            await r.action('urgent_state')
            assert not r.c.hunt.blocked and r.c.hunt.recovery_requested
            assert r.toggle_count == 6 and r.c.hunt.last_gameplay_receipt_id is None
            await r.choose(None)
            assert not r.c.hunt.planning_requested and r.toggle_count == 6
            assert set(r.sage.calls[-1]['options'])=={'target_enemy','turn_left','turn_right','forward'}
            assert r.c.hunt.travel_urgent_debt['completed_roundtrips']==2
            assert r.c.hunt.selected_presence is None
            assert not r.casts() and r.controls['forward']['keycode'] not in r.physical_keys()
            # Relevant own-resource change retires the false-urgency debt;
            # real Loading still preempts hide and world movement afterward.
            r.world.player_health = 'red'
            await r.action('recover_now')
            assert r.c.hunt.travel_urgent_debt is None
            r.world.error = 'Loading'
            await r.action('loading_wait')
            assert r.toggle_count == 6
            assert all(option.binding['type'] == 'observe_only' for option in r.sage.calls[-1]['options'].values())
        finally:
            await r.close()
    asyncio.run(run())

def test_restored_hud_with_changed_health_reobserves_without_false_restoration_failure(tmp_path):
    async def run():
        r = await clean_rig(tmp_path)
        try:
            first = await r.action('probe_forward')
            r.position = [11., 10.]
            r.after_hidden = lambda: setattr(r.world, 'player_health', 'red')
            calls = len(r.sage.calls)
            result = await r.c.process(r.world.capture())
            assert result.status == 'grind_reobserve'
            assert r.c.observation_transaction['status'] == 'restored_verified'
            assert not r.c.observation_transaction['restoration_needed'] and r.hud
            assert len(r.sage.calls) == calls and r.c.hunt.phase == 'recover'
            assert r.c.hunt.last_completed_action['receipt_id'] == first.receipt['receipt_id']
            assert r.c.hunt.last_completed_action['progress'] == pytest.approx(1)
            assert r.physical_keys().count(r.controls['forward']['keycode']) == 1
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interruption', ['capture_exception', 'capture_cancellation'])
def test_known_hide_capture_interruption_restores_only_under_valid_observation_authority(tmp_path, interruption):
    async def run():
        r = await clean_rig(tmp_path)
        try:
            def fail_capture():
                if interruption == 'capture_cancellation': raise asyncio.CancelledError()
                raise RuntimeError('offline clean capture failure')
            r.after_hidden = fail_capture
            if interruption == 'capture_cancellation':
                with pytest.raises(asyncio.CancelledError):
                    await r.c.process(r.world.capture())
            else:
                with pytest.raises(RuntimeError,match='offline clean capture failure'):
                    await r.c.process(r.world.capture())
            tx = r.c.observation_transaction
            assert tx['hide_receipt']['completed'] and tx['restore_receipt']['completed']
            assert tx['hide_receipt']['generation_after'] == tx['restore_receipt']['generation_before']
            assert not tx['restoration_needed'] and r.hud and r.toggle_count == 2
            assert not r.sage.calls and r.controls['forward']['keycode'] not in r.physical_keys()
        finally:
            await r.close()
    asyncio.run(run())

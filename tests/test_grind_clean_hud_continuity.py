"""Synthetic HUD animations exercise real clean-observation guards, offline only."""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from sage_wow.agent.grind_observation import hud_continuity
from sage_wow.agent.grind_only import same_patch
from sage_wow.perception.ocr import TextObservation
from test_grind_travel_acceptance import TravelRig


class AnimatedRig(TravelRig):
    def __init__(self, directory):
        super().__init__(directory, clean=True)
        self.world.name = 'Young Wolf'; self.world.target_level = 1
        self.player_label = 'Test Player'; self.player_level = 1
        self.life = 'alive'; self.mana = 'blue'; self.animate = True
        self.visible_samples = 0
        self.c.config.update(player_mana_box=[40, 44, 140, 52], target_name_box=[210, 100, 350, 115])
        capture, ocr = self.world.capture, self.c.ocr
        def animated_capture():
            frame = capture()
            if self.hud:
                self.visible_samples += 1
                with Image.open(frame.image_path) as image:
                    draw = ImageDraw.Draw(image)
                    draw.rectangle((40, 44, 139, 51), fill=self.mana)
                    draw.rectangle((25, 55, 44, 79), fill='#263746')
                    draw.text((28, 60), str(self.player_level), fill='white')
                    if self.animate:
                        # Hidden settle samples must not accidentally cycle the
                        # restored portrait back to the identical source color.
                        color = ['#ff2000', '#0050ff', '#ddaa00'][self.visible_samples % 3]
                        # Portrait/decorative regions exclude calibrated names,
                        # badges, bars and the static HUD visibility proof.
                        draw.rectangle((0, 32, 23, 54), fill=color)
                        if self.world.name: draw.rectangle((390, 90, 418, 160), fill=color)
                        else: draw.rectangle((200, 80, 490, 240), fill=color)  # Moving scenery without a target HUD.
                    if self.world.name and self.life == 'dead': draw.text((275, 125), 'Dead', fill='white')
                    image.save(frame.image_path)
            return frame
        def rows(path):
            result = ocr(path)
            result = [replace(row, text=self.player_label) if row.text == 'Test Player' else row for row in result]
            if self.hud and self.world.name and self.life == 'dead' and Path(path).name.startswith('acceptance-'):
                result.append(TextObservation('Dead', .99, {'x': 275, 'y': 125, 'width': 30, 'height': 10}))
            return result
        self.world.capture = self.c.capture = animated_capture
        self.world.ocr = self.c.ocr = rows


@pytest.mark.parametrize('selection', ['alive', 'dead', 'absent'])
def test_animated_portraits_keep_clean_travel_and_dispatch_available(tmp_path, selection):
    async def run():
        r = AnimatedRig(tmp_path)
        try:
            if selection == 'dead': r.life = 'dead'; r.world.health = 'black'
            if selection == 'absent': r.world.name = ''; r.world.target_level = None
            await r.travel()
            result = await r.action('probe_forward')
            assert result.receipt['completed'] and not result.receipt.get('dispatch_unknown')
            assert r.controls['forward']['keycode'] in r.physical_keys()
            assert r.c.hunt.phase == 'travel' and not r.c.stopped
            tx = r.c.observation_transaction
            source, restored = tx['source_frame']['image_path'], tx['restored_frame']['image_path']
            assert not same_patch(source, restored, (0, 0, 150, 90))
            assert not same_patch(source, restored, (200, 80, 490, 240))
        finally: await r.close()
    asyncio.run(run())


def change(r, kind):
    if kind == 'player_health': r.world.player_health = 'red'
    elif kind == 'mana': r.mana = 'black'
    elif kind == 'target_health': r.world.health = 'yellow'
    elif kind == 'target_name': r.world.name = 'Other Wolf'
    elif kind == 'target_absent': r.world.name = ''; r.world.target_level = None
    elif kind == 'target_level': r.world.target_level = 2
    elif kind == 'player_level': r.player_level = 2
    elif kind == 'player_name': r.player_label = 'Other Player'
    elif kind == 'target_dead': r.life = 'dead'; r.world.health = 'black'
    elif kind == 'modal': r.world.error = 'Game Menu'


@pytest.mark.parametrize('kind', ['player_health', 'mana', 'target_health', 'target_name',
    'target_absent', 'target_level', 'player_level', 'player_name', 'target_dead', 'modal'])
def test_actual_hud_changes_after_sage_still_reject_clean_dispatch(tmp_path, kind):
    async def run():
        r = AnimatedRig(tmp_path)
        try:
            await r.travel()
            async def mutate(): change(r, kind)
            r.sage.hook = mutate
            result = await r.choose('probe_forward')
            assert result.status == 'dispatch_guard_rejected', (kind, result.status, result.detail)
            assert r.controls['forward']['keycode'] not in r.physical_keys()
            assert not r.c.hunt.pending
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['player_health', 'mana', 'target_health', 'target_name', 'target_level', 'modal'])
def test_actual_change_during_hide_restore_reobserves_before_provider(tmp_path, kind):
    async def run():
        r = AnimatedRig(tmp_path)
        try:
            await r.travel(); r.after_hidden = lambda: change(r, kind)
            r.sage.answers.append('probe_forward')
            result = await r.c.process(r.world.capture())
            assert result.status == 'grind_reobserve' and r.c.hunt.phase == 'recover'
            assert not r.sage.calls and r.controls['forward']['keycode'] not in r.physical_keys()
            assert r.c.observation_transaction['status'] == 'restored_verified'
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['absent', 'unknown', 'unknown_badge', 'unknown_dead', 'empty_name_presence', 'empty_name_life'])
def test_unobserved_travel_does_not_erase_changed_or_ambiguous_selected_hud(tmp_path, kind):
    async def run():
        r = AnimatedRig(tmp_path)
        try:
            r.world.name = ''; r.world.target_level = None
            before = r.world.capture(); after = r.world.capture()
            old = await r.c.target_proposal(before); new = await r.c.target_proposal(after)
            # Scene motion under the place where a missing HUD would be.
            with Image.open(after.image_path) as image:
                ImageDraw.Draw(image).rectangle((200, 80, 490, 240), fill='#cc8844')
                image.save(after.image_path)
            if not kind.startswith('unknown'):
                old['visual_observation'] = {'selected_hud': 'absent'}
                new['visual_observation'] = {'selected_hud': 'absent'}
            if kind == 'unknown_badge': old['levels'] = new['levels'] = [1]
            if kind == 'unknown_dead': old['invalid_text'] = new['invalid_text'] = True
            if kind == 'empty_name_presence': new['visual_observation']['selected_hud'] = 'present'
            if kind == 'empty_name_life':
                old['visual_observation'] = {'selected_hud': 'present', 'life_state': 'dead'}
                new['visual_observation'] = {'selected_hud': 'present', 'life_state': 'alive'}
            evidence = hud_continuity(r.c, before, after, old, new, await r.c.rows(after))
            assert bool(evidence['failed_predicates']) == (kind not in {'absent', 'unknown'}), evidence
            if kind == 'unknown':
                assert evidence['selection_observation'] == {'before': 'unknown', 'current': 'unknown', 'legacy_unobserved_target': True}
            if kind in {'unknown_badge', 'unknown_dead'}: assert 'unknown_target_region' in evidence['failed_predicates']
            if kind == 'empty_name_presence': assert 'same_selected_presence' in evidence['failed_predicates']
            if kind == 'empty_name_life': assert 'same_selected_life' in evidence['failed_predicates']
        finally: await r.close()
    asyncio.run(run())

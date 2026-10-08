"""Owned restoration with changing world pixels around opaque HUD support.

All captures, OCR, provider answers and inputs use the existing offline rig.
These fixtures establish visibility only; gameplay still needs fresh guards.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace

from PIL import Image, ImageDraw
import pytest

from sage_wow.agent.grind_observation import differences, digest
from sage_wow.models import Frame
from test_grind_travel_acceptance import TravelRig


WORLD = '#263746'
CHANGED_WORLD = '#aec4de'


def changing_world_corners(r):
    """A round minimap and transparent player-frame margin reveal scenery."""
    capture = r.c.capture

    def rendered():
        frame = capture()
        with Image.open(frame.image_path) as original:
            image = original.convert('RGB')
            color = CHANGED_WORLD if r.hud and r.toggle_count >= 2 else WORLD
            # This strip is outside the name and resource/badge pixels.
            ImageDraw.Draw(image).rectangle((0, 0, 19, 29), fill=color)
            mask = Image.new('L', (100, 80), 0)
            ImageDraw.Draw(mask).ellipse((5, 5, 94, 74), fill=255)
            minimap = Image.new('RGB', mask.size, color)
            minimap.paste(image.crop((400, 0, 500, 80)), (0, 0), mask)
            image.paste(minimap, (400, 0))
            image.save(frame.image_path)
        return frame

    r.c.capture = r.world.capture = rendered


async def observed_transaction(r):
    await r.travel()
    changing_world_corners(r)
    result = await r.choose(None)  # No gameplay receipt after the HUD pair.
    assert result.status == 'needs_more_evidence'
    t = r.c.observation_transaction
    assert t['status'] == 'restored_verified' and not t['restoration_needed']
    assert r.toggle_count == 2 and r.hud
    return t


def test_changed_world_corners_allow_full_restoration_then_guarded_travel(tmp_path):
    async def run():
        r = TravelRig(tmp_path, clean=True)
        try:
            await r.travel()
            changing_world_corners(r)
            result = await r.action('probe_forward')
            t = r.c.observation_transaction
            source, restored = Frame(**t['source_frame']), Frame(**t['restored_frame'])
            assert min(differences(r.c, source, restored).values()) > 2
            accepted, evidence = r.c.observation.restored_support(t, restored)
            assert accepted, evidence
            assert t['hide_verified'] and len(t['hidden_witnesses']) == 2
            assert t['status'] == 'restored_verified' and not t['restoration_needed']
            assert result.receipt['completed'] and r.toggle_count == 2 and r.hud
            assert r.controls['forward']['keycode'] in r.physical_keys()
            assert all(entry[1] for entry in r.trace if entry[0] == 'provider')
        finally:
            await r.close()
    asyncio.run(run())


def test_restored_support_reconciles_without_another_toggle_or_gameplay(tmp_path):
    async def run():
        r = TravelRig(tmp_path, clean=True)
        try:
            t = await observed_transaction(r)
            # Model a retained, unresolved restore obligation with its original
            # completed receipts and no intervening input.
            t.update(status='observation_unresolved', restoration_needed=True)
            calls = len(r.sage.calls)
            result = await r.c.process(r.world.capture())
            assert result.status == 'grind_reobserve'
            assert t['status'] == 'restoration_reconciled' and not t['restoration_needed']
            assert r.toggle_count == 2 and not t.get('correction_receipt')
            assert len(r.sage.calls) == calls and not r.casts()
            assert r.controls['forward']['keycode'] not in r.physical_keys()
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['missing_player', 'missing_minimap', 'half_minimap',
    'hidden_changed_world', 'black'])
def test_partial_or_absent_hud_cannot_match_restored_support(tmp_path, kind):
    async def run():
        r = TravelRig(tmp_path, clean=True)
        try:
            t = await observed_transaction(r)
            fresh = r.world.capture()
            with Image.open(fresh.image_path) as image:
                if kind == 'missing_player': image.paste(WORLD, (0, 0, 150, 90))
                elif kind == 'missing_minimap': image.paste(WORLD, (400, 0, 500, 80))
                elif kind == 'half_minimap': image.paste(WORLD, (450, 0, 500, 80))
                elif kind == 'black': image.paste('black', (0, 0, 500, 300))
                else:
                    with Image.open(t['hidden_frame']['image_path']) as hidden:
                        image.paste(hidden)
                    ImageDraw.Draw(image).rectangle((0, 0, 19, 29), fill=CHANGED_WORLD)
                    ImageDraw.Draw(image).rectangle((480, 0, 499, 79), fill=CHANGED_WORLD)
                image.save(fresh.image_path)
            assert not r.c.observation.restored_support(t, fresh)[0]
            assert not await r.c.observation.visible_candidate(t, fresh)
            assert r.toggle_count == 2 and not r.casts()
            if kind == 'hidden_changed_world':
                # This candidate is outside the original strict hidden match.
                # Restoration-only support must not grant another toggle.
                assert r.c.observation.visibility(t, fresh)[0] == 'ambiguous'
                t.update(status='observation_unresolved', restoration_needed=True)
                calls = len(r.sage.calls)
                result = await r.c.process(fresh)
                assert result.status == 'grind_blocked' and t['restoration_needed']
                assert r.toggle_count == 2 and not t.get('correction_receipt')
                assert len(r.sage.calls) == calls
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['unverified', 'unverified_source', 'missing_witness',
    'same_witness', 'same_time', 'source_hash', 'hidden_hash', 'source_bytes',
    'hidden_bytes', 'wrong_geometry'])
def test_restored_support_requires_immutable_distinct_verified_witnesses(tmp_path, kind):
    async def run():
        r = TravelRig(tmp_path, clean=True)
        try:
            original = await observed_transaction(r)
            t = deepcopy(original)
            fresh = r.world.capture()
            if kind == 'unverified': t['hide_verified'] = False
            elif kind == 'unverified_source': t['source_visible_proof']['verified'] = False
            elif kind == 'missing_witness': t['hidden_witnesses'] = t['hidden_witnesses'][:1]
            elif kind == 'same_witness': t['hidden_witnesses'][1] = deepcopy(t['hidden_witnesses'][0])
            elif kind == 'same_time':
                sample = t['hidden_witnesses'][1]
                at = t['hidden_witnesses'][0]['captured_at']
                sample['captured_at'] = sample['frame']['captured_at'] = at
            elif kind == 'source_hash': t['source_sha256'] = '0' * 64
            elif kind == 'hidden_hash': t['hidden_witnesses'][0]['sha256'] = '0' * 64
            elif kind in {'source_bytes', 'hidden_bytes'}:
                reference = t['source_frame'] if kind == 'source_bytes' else t['hidden_witnesses'][0]['frame']
                with Image.open(reference['image_path']) as image:
                    image.putpixel((1, 1), (255, 0, 0))
                    image.save(reference['image_path'])
            else: fresh = replace(fresh, source='another-window')
            # Malformed immutable references may raise or conservatively
            # return unavailable; neither response may authorize restoration.
            try:
                accepted, _ = r.c.observation.restored_support(t, fresh)
            except ValueError:
                accepted = False
            assert not accepted
            assert r.toggle_count == 2 and not r.casts()
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['tiny', 'single_quadrant', 'unstable_hidden'])
def test_tiny_localized_or_unstable_pixel_support_is_not_a_visibility_witness(tmp_path, kind):
    async def run():
        r = TravelRig(tmp_path, clean=True)
        try:
            t = await observed_transaction(r)
            fresh = r.world.capture()
            source = Frame(**t['source_frame'])
            # Independent synthetic reference replacement: retained hashes are
            # updated deliberately to test support adequacy, not tampering.
            for index, sample in enumerate(t['hidden_witnesses']):
                path = tmp_path / f'weak-hidden-{index}.png'
                with Image.open(sample['frame']['image_path']) as original:
                    image = original.convert('RGB')
                with Image.open(source.image_path) as original:
                    image.paste(original.crop((400, 0, 500, 80)), (400, 0))
                draw = ImageDraw.Draw(image)
                if kind == 'tiny': draw.rectangle((420, 10, 422, 12), fill='black')
                elif kind == 'single_quadrant': draw.rectangle((400, 0, 449, 39), fill='black')
                else: draw.rectangle((400, 0, 499, 79), fill='black' if index else 'white')
                image.save(path)
                frame = replace(Frame(**sample['frame']), image_path=str(path))
                sample.update(frame=frame.__dict__, sha256=digest(frame))
            latest = t['hidden_witnesses'][-1]
            t.update(hidden_frame=latest['frame'], hidden_frame_id=latest['frame_id'],
                     hidden_sha256=latest['sha256'])
            accepted, evidence = r.c.observation.restored_support(t, fresh)
            assert not accepted
            assert evidence['anchors']['player_frame']['accepted']
            assert not evidence['anchors']['minimap']['accepted']
            assert r.toggle_count == 2 and not r.casts()
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['identity', 'loading'])
def test_pixel_support_does_not_replace_current_identity_or_loading_checks(tmp_path, kind):
    async def run():
        r = TravelRig(tmp_path, clean=True)
        try:
            t = await observed_transaction(r)
            if kind == 'loading': r.world.error = 'Loading'
            else:
                ocr = r.c.ocr
                r.c.ocr = lambda path: [replace(row, text='Other Player')
                    if row.text == 'Test Player' else row for row in ocr(path)]
            fresh = r.world.capture()
            assert r.c.observation.restored_support(t, fresh)[0]
            assert not await r.c.observation.visible_candidate(t, fresh)
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['health', 'modal'])
def test_restored_support_never_overrides_current_gameplay_continuity(tmp_path, kind):
    async def run():
        r = TravelRig(tmp_path, clean=True)
        try:
            await r.travel()
            changing_world_corners(r)
            r.after_hidden = lambda: setattr(r.world,
                'player_health' if kind == 'health' else 'error',
                'red' if kind == 'health' else 'Game Menu')
            r.sage.answers.append('probe_forward')
            result = await r.c.process(r.world.capture())
            t = r.c.observation_transaction
            assert t['status'] == 'restored_verified' and not t['restoration_needed']
            assert result.status == 'grind_reobserve' and r.c.hunt.phase == 'recover'
            assert not r.sage.calls and r.controls['forward']['keycode'] not in r.physical_keys()
            assert r.toggle_count == 2 and r.hud
        finally:
            await r.close()
    asyncio.run(run())

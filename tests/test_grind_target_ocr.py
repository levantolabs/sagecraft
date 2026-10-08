"""Bounded alternate sensing, with fake OCR, provider and input only."""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from sage_wow.agent.grind_only import proposal
from sage_wow.agent.grind_target_ocr import reread
from sage_wow.perception.ocr import TextObservation
from test_grind_exact_band_observation import start_band
from test_grind_target_inspection_contract import ContractRig


def row(text, *, confidence=1., x=3, y=3, width=80, height=20):
    return TextObservation(text, confidence, {'x': x, 'y': y, 'width': width, 'height': height})


@pytest.mark.parametrize('raw_name', ['Burly Rockjaw Trogg®', 'Burly Rockjaw Trogg™', None])
def test_missing_or_unusable_text_gets_independent_exact_crop_read(tmp_path, raw_name):
    async def run():
        r = ContractRig(tmp_path / 'reread')
        try:
            frame = r.world.capture()
            raw = proposal(r.p, r.c.config, frame, r.world.ocr(frame.image_path))
            raw.update(name=raw_name, name_row=None, levels=[])
            calls = []
            def ocr(path):
                calls.append(path.name)
                return [row('Burly Rockjaw Trogg' if path.stem == 'name' else '2')]
            result = reread(r.p, r.c.config, frame, [], raw, ocr)
            assert result['name'] == 'Burly Rockjaw Trogg' and result['levels'] == [2]
            assert result['name_row'].source == 'calibrated_target_crop_ocr'
            assert result['focused_ocr']['raw_name'] == raw_name
            assert result['focused_ocr']['raw_levels'] == []
            assert calls == ['name.png', 'level.png']
            assert raw['name'] == raw_name and raw['levels'] == []
            assert not r.physical()
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('answers', [[], [row('Trogg®')], [row(' Trogg')],
    [row('Trogg', confidence=.5)], [row('Trogg', x=-1)],
    [row('Trogg', width=10000)], [row('Trogg'), row('Other')],
    [row('Trogg'), row('Other', confidence=.1)]])
def test_unresolved_crop_never_sanitizes_or_guesses_a_name(tmp_path, answers):
    async def run():
        r = ContractRig(tmp_path / 'negative')
        try:
            frame = r.world.capture()
            raw = proposal(r.p, r.c.config, frame, r.world.ocr(frame.image_path))
            raw.update(name='Trogg®', levels=[2])
            result = reread(r.p, r.c.config, frame, [], raw, lambda path: answers)
            assert result['name'] == 'Trogg®'
            assert not result['focused_ocr']['fields']['name']['accepted']
            assert not r.physical()
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('number', ['0', '100', '2?', ' 2', 'Level 2', 'unknown'])
def test_unreadable_badge_is_not_inferred(tmp_path, number):
    async def run():
        r = ContractRig(tmp_path / 'number')
        try:
            frame = r.world.capture()
            raw = proposal(r.p, r.c.config, frame, r.world.ocr(frame.image_path))
            raw['levels'] = []
            result = reread(r.p, r.c.config, frame, [], raw, lambda path: [row(number)])
            assert result['levels'] == []
        finally: await r.close()
    asyncio.run(run())


def test_existing_valid_facts_are_not_overwritten(tmp_path):
    async def run():
        r = ContractRig(tmp_path / 'valid')
        try:
            frame = r.world.capture()
            raw = proposal(r.p, r.c.config, frame, r.world.ocr(frame.image_path))
            def unexpected(path): pytest.fail('Usable full-frame fields do not need another reading')
            assert reread(r.p, r.c.config, frame, [], raw, unexpected) is raw
        finally: await r.close()
    asyncio.run(run())


def test_ambiguous_valid_full_frame_names_cannot_be_erased(tmp_path):
    async def run():
        r = ContractRig(tmp_path / 'conflict')
        try:
            frame = r.world.capture()
            raw = proposal(r.p, r.c.config, frame, r.world.ocr(frame.image_path))
            raw.update(name=None, levels=[2])
            result = reread(r.p, r.c.config, frame,
                [row('Other Creature', x=210, y=100, width=100, height=15)], raw,
                lambda path: [row('Burly Rockjaw Trogg')])
            assert result['name'] is None
            assert result['focused_ocr']['fields']['name']['conflicting_full_frame_names'] == ['Other Creature']
        finally: await r.close()
    asyncio.run(run())


def test_mutated_source_is_rejected(tmp_path):
    async def run():
        r = ContractRig(tmp_path / 'mutation')
        try:
            frame = r.world.capture()
            raw = proposal(r.p, r.c.config, frame, r.world.ocr(frame.image_path))
            raw['levels'] = []
            def ocr(path):
                Image.new('RGB', (500, 300), 'red').save(frame.image_path)
                return [row('2')]
            with pytest.raises(ValueError, match='Immutable target OCR source'):
                reread(r.p, r.c.config, frame, [], raw, ocr)
            assert not r.physical()
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('box', [[-1, 0, 20, 20], [420, 170, 501, 200], [450, 170, 420, 200]])
def test_invalid_calibration_cannot_supply_target_text(tmp_path, box):
    async def run():
        r = ContractRig(tmp_path / 'geometry')
        try:
            frame = r.world.capture()
            raw = proposal(r.p, r.c.config, frame, r.world.ocr(frame.image_path))
            raw['levels'] = []
            with pytest.raises(ValueError, match='contained calibrated region'):
                reread(r.p, {**r.c.config, 'target_level_box': box}, frame, [], raw, lambda path: [row('2')])
            assert not r.physical()
        finally: await r.close()
    asyncio.run(run())


def test_fresh_self_name_stays_ineligible(tmp_path):
    async def run():
        r = ContractRig(tmp_path / 'self')
        try:
            frame = r.world.capture()
            raw = proposal(r.p, r.c.config, frame, r.world.ocr(frame.image_path))
            raw.update(name=None, levels=[2])
            result = reread(r.p, r.c.config, frame, [], raw, lambda path: [row('Test Player')])
            assert result['self_target'] and result['name'] == 'Test Player'
            assert not r.physical()
        finally: await r.close()
    asyncio.run(run())


def test_source_ocr_fusion_uses_reread_not_old_observer_and_caches_per_frame(tmp_path):
    async def run():
        r = ContractRig(tmp_path / 'composition')
        try:
            await start_band(r)
            r.c.config['committed_combat'] = True
            original = r.c.ocr
            calls = []
            def ocr(path):
                if Path(path).parent.name.startswith('sage-target-ocr-'):
                    calls.append(Path(path).stem)
                    return [row('Burly Rockjaw Trogg' if Path(path).stem == 'name' else '2')]
                return [replace(item, text=item.text + '®') if item.text == 'Burly Rockjaw Trogg'
                        else item for item in original(path)]
            r.c.ocr = ocr
            frame = r.world.capture()
            r.c._analysis_cache = {}
            first = await r.c.raw_target_proposal(frame)
            second = await r.c.raw_target_proposal(frame)
            assert first is second and calls == ['name', 'level']
            r.c._analysis_cache = None
            r.c.hunt.compact_stage = 'reinspect'
            assert (await r.observe_target()).status == 'grind_reobserve'
            source = r.c.target_observation['source_ocr']
            assert source['name'] == 'Burly Rockjaw Trogg' and source['levels'] == [2]
            assert source['focused_ocr']['raw_name'] == 'Burly Rockjaw Trogg®'
            assert r.c.target_observation['result']['observation']['name'] is None
            result = await r.choose('attack_mob_level_2')
            assert result.status == 'dispatched'
            assert result.receipt['selected_binding']['expected_target_name'] == 'Burly Rockjaw Trogg'
            assert not r.c.target_inspection_episode and len(r.wire) == 1
            assert not r.c.hunt.credited_kills
        finally: await r.close()
    asyncio.run(run())

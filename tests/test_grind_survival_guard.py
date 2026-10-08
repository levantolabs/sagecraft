"""Offline own-survival boundaries; synthetic frames and fake native IO only."""
import asyncio
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw

from sage_wow.agent.grind_only import same_patch
from sage_wow.perception.ocr import TextObservation
from test_grind_product_spec import Rig


def row(text, x, y, width=75, height=12, confidence=.99):
    return TextObservation(text, confidence,
                           {"x": x, "y": y, "width": width, "height": height})


class SurvivalRig(Rig):
    def __init__(self, directory):
        super().__init__(directory)
        self.health = .20
        self.own_name = "Test Player"
        self.ui = None
        self.alternate_badge = False
        self.snapshots = {}
        capture, ocr = self.world.capture, self.world.ocr

        def capture_survival():
            frame = capture()
            with Image.open(frame.image_path) as source:
                image = source.convert("RGB")
            draw = ImageDraw.Draw(image)
            draw.rectangle((40, 30, 139, 39), fill="black")
            if self.health > 0:
                draw.rectangle((40, 30, 39 + round(100 * self.health), 39), fill=(0, 200, 0))
            if self.alternate_badge:
                # Change badge surroundings while preserving its displayed glyph.
                draw.rectangle((25, 55, 44, 79), outline=(210 if self.world.count % 2 else 70, 0, 0), width=3)
            if self.ui == "death":
                draw.rectangle((195, 50, 365, 89), fill="#201710", outline="#a08050")
                draw.text((205, 70), "Release Spirit", fill="#ffcc70")
                draw.text((310, 70), "Recap", fill="#ffcc70")
            image.save(frame.image_path)
            self.snapshots[str(frame.image_path)] = (self.own_name, self.ui)
            self.latest_frame = frame
            return frame

        def ocr_survival(path):
            own, ui = self.snapshots.get(str(path), (self.own_name, self.ui))
            rows = [replace(item, text=own) if item.text == "Test Player" else item
                    for item in ocr(path)]
            if ui == "death":
                rows += [row("Release Spirit", 205, 70, 85), row("Recap", 310, 70, 40)]
            elif ui == "chat":
                rows += [row("[You died.]", 10, 250), row("Release Spirit", 10, 270, 85),
                         row("Recap", 110, 270, 40)]
            elif ui == "lone_button": rows += [row("Release Spirit", 205, 70, 85)]
            elif ui == "misaligned":
                rows += [row("Release Spirit", 205, 50, 85), row("Recap", 310, 140, 40)]
            elif ui == "low_confidence":
                rows += [row("Release Spirit", 205, 70, 85, confidence=.4), row("Recap", 310, 70, 40)]
            elif ui == "duplicate":
                rows += [row("Release Spirit", 205, 70, 85), row("Release Spirit", 205, 100, 85),
                         row("Recap", 310, 70, 40)]
            elif ui == "menu": rows += [row("Game Menu", 205, 60)]
            return rows

        self.world.capture = self.c.capture = capture_survival
        self.c.ocr = ocr_survival

    def enable_resources(self):
        self.c.config.update(encounter_resources=True, preserve_target_heal=True)
        self.c.profile.values["character"].update(name="Test", surname="Player")


def test_self_heal_accepts_changed_badge_pixels_and_preserves_enemy(tmp_path):
    async def run():
        r = SurvivalRig(tmp_path)
        try:
            await r.start()
            r.enable_resources()
            r.alternate_badge = True
            result = await r.choose("heal_self")
            assert result.status == "dispatched"
            assert ("text", "/cast [@player] Lesser Heal") in r.casts()
            assert r.world.name == "Young Wolf" and r.c.heal_pending
            assert not r.c.hunt.credited_kills and not r.c.stopped
            guard = next(e["payload"] for e in r.store.recent(30)
                         if e["event_type"] == "dispatch_guard_checked")
            assert guard["approved"] and guard["evidence"]
            # A passing test must actually exercise the old false-negative boundary.
            frames = list(r.snapshots)[-2:]
            assert not same_patch(*frames, tuple(r.c.config["player_level_box"]), badge=True)
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["identity", "surname_missing", "menu", "death", "health_empty",
                                      "epoch", "generation", "source_hash", "capture_source", "stale"])
def test_self_heal_rejects_changed_authority_or_unconfirmed_living_player(tmp_path, mutation):
    async def run():
        r = SurvivalRig(tmp_path)
        try:
            await r.start()
            r.enable_resources()
            async def change():
                if mutation == "identity": r.own_name = "Other Player"
                elif mutation == "surname_missing": r.own_name = "Test"
                elif mutation == "menu": r.ui = "menu"
                elif mutation == "death": r.ui = "death"; r.health = 0
                elif mutation == "health_empty": r.health = 0
                elif mutation == "epoch": r.c.cycle.session_epoch = "changed-offline-epoch"
                elif mutation == "generation": r.c.cycle._input_generation += 1
                elif mutation == "source_hash":
                    with Image.open(r.latest_frame.image_path) as source:
                        image = source.copy()
                    image.putpixel((499, 299), (255, 0, 255)); image.save(r.latest_frame.image_path)
                else:
                    original = r.c.capture
                    def changed_capture():
                        frame = original()
                        return replace(frame, **({"source": "other-window"} if mutation == "capture_source"
                                      else {"captured_at": "2020-01-01T00:00:00+00:00"}))
                    r.c.capture = changed_capture
            r.sage.hook = change
            result = await r.choose("heal_self")
            assert result.status != "dispatched"
            assert not r.physical_keys() and not r.casts() and not r.c.heal_pending
            assert not r.c.hunt.credited_kills
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize("route", ["level", "loot", "travel"])
def test_current_death_modal_preempts_all_routing_without_physical_input(tmp_path, route):
    async def run():
        r = SurvivalRig(tmp_path)
        try:
            await r.start()
            r.ui = "death"; r.health = 0
            if route == "level": r.c.level.last_attempt_at = None
            elif route == "loot":
                r.c.config["loot_enabled"] = True
                r.c.hunt.loot_request = {"frame_id": "old-fight", "target": {"name": "Young Wolf", "levels": [1]}}
            elif route == "travel": r.c.hunt.phase = "travel"
            before = len(r.sage.calls)
            result = await r.c.process(r.world.capture())
            assert r.c.stopped and "death" in r.c.reason
            assert result.status == "grind_stopped"
            assert len(r.sage.calls) == before
            assert not r.physical_keys() and not r.casts()
            assert not r.c.hunt.credited_kills
        finally: await r.close()
    asyncio.run(run())


def test_current_death_stops_real_hud_reconciliation_before_any_more_input(tmp_path):
    from test_grind_travel_acceptance import TravelRig
    from test_grind_hud_no_effect import failed_hide, ignore_chords

    async def run():
        r = TravelRig(tmp_path, clean=True)
        try:
            await r.travel()
            ignore_chords(r)
            transaction = await failed_hide(r)
            assert r.c.observation.known_chain(transaction)
            prior_keys = list(r.physical_keys())
            prior_calls = len(r.sage.calls)
            prior_generation = r.c.cycle.input_generation
            ocr = r.c.ocr
            r.c.ocr = lambda path: ocr(path) + [row("Release Spirit", 205, 70, 85),
                                              row("Recap", 310, 70, 40)]
            result = await r.c.process(r.world.capture())
            assert result.status == "grind_stopped" and r.c.reason == "own_death_modal_observed"
            assert r.c.stopped and transaction["restoration_needed"]
            assert transaction["reconciliation_attempts"] == 1
            assert not transaction.get("restore_receipt") and not transaction.get("correction_receipt")
            assert r.physical_keys() == prior_keys
            assert r.c.cycle.input_generation == prior_generation
            assert len(r.sage.calls) == prior_calls and not r.casts()
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize("ui", [None, "chat", "lone_button", "misaligned", "low_confidence", "duplicate"])
def test_empty_bar_or_unconfirmed_death_text_does_not_establish_death(tmp_path, ui):
    async def run():
        r = SurvivalRig(tmp_path)
        try:
            await r.start()
            r.ui = ui; r.health = 0
            await r.choose(None)
            assert not r.c.stopped
            assert not r.physical_keys() and not r.casts()
            assert not r.c.hunt.credited_kills
        finally: await r.close()
    asyncio.run(run())

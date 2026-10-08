# Live setup

This guide takes you from a fresh clone to a live run. Expect about an hour the first time, most of it calibrating screen regions. Read the [disclaimer](../README.md) first: automated play may break the game's terms of use.

## What you need

- **macOS** (Apple Silicon or Intel), Python 3.12–3.14 and [uv](https://docs.astral.sh/uv/).
- **World of Warcraft in English.** Spells are cast by typing slash commands such as `/cast Smite`, so spell names must match.
- **A priest, ideally a dwarf or gnome in Coldridge Valley.** The harness uses Smite and Lesser Heal, and the included hunting-area catalog covers that zone.
- **A Sage API key** from [levanto.ai](https://levanto.ai). Budget roughly 800–900 Sage decisions per hour of active play.

## 1. Install

```sh
uv sync --extra macos --extra dev
cp .env.example .env        # then add SAGE_API_KEY to .env
```

## 2. Grant permissions

In **System Settings → Privacy & Security**, allow the app you launch the runner from (Terminal, iTerm, VS Code …):

| Permission | Why |
|---|---|
| Screen Recording | Capture the game window |
| Accessibility | Send keyboard and mouse input |
| Input Monitoring | The global stop hotkey (Ctrl+Alt+Escape) |

Restart that app after granting them.

## 3. Prepare the game

- Use a **windowed** game at a fixed size, and keep it in the same place for every run. Calibration is in exact pixels.
- Keep the default **Enter** key for opening chat. The harness types commands into chat.
- Bind **Tab** to *Target Nearest Enemy*, and set the movement and turning keys you will declare below (defaults: W/S and the arrow keys).
- Turn the minimap coordinates display on. Navigation reads the player's zone coordinates under the minimap.
- If you change the UI scale or move frames later, recalibrate (step 5).

## 4. Create your profile

```sh
mkdir -p data
cp profiles/template.yaml data/my-profile.yaml
uv run sage-wow --profile data/my-profile.yaml doctor
```

`doctor` lists visible windows and checks permissions. Find the game window's ID, then:

```sh
uv run sage-wow --profile data/my-profile.yaml configure \
  --window-id <ID> \
  --expected-bundle-id com.blizzard.worldofwarcraft \
  --installation-path "/Applications/World of Warcraft/<edition folder>/World of Warcraft.app" \
  --name "<your character name>"
```

The character name stops the harness from mistaking your own character for a target. Keep `data/` out of git (it already is in `.gitignore`).

## 5. Calibrate

```sh
uv run sage-wow --profile data/my-profile.yaml calibrate
```

This saves a screenshot under `calibration/` and records the window geometry. Open that screenshot in an image editor that shows pixel coordinates, and fill in `calibration.ui_layout` in your profile. Each region is `[left, top, right, bottom]` in screenshot pixels:

| Region | Covers |
|---|---|
| `player_frame` | Your portrait, name, health and mana bars and level badge |
| `player_health` | Just your health bar (inside `player_frame`) |
| `target_frame` | The selected target's frame |
| `target_health` | Just the target's health bar (inside `target_frame`) |
| `target_overlay` | The target frame plus its level badge (contains `target_frame`) |
| `minimap` | The minimap |
| `coordinate_primary` | The coordinate text under the minimap |
| `coordinate_alternate` | A slightly larger box around it (contains `coordinate_primary`) |
| `coordinate_magnification` | A box for an enlarged read of the coordinates (contains `coordinate_primary`) |
| `local_caption` | The zone name text near the minimap |

Related regions must be calibrated together. Also set these, inside the frames above:

- `grind_only.player_level_box`: your level badge, inside `player_frame`;
- `grind_only.target_level_box`: the target's level badge, inside `target_overlay`;
- optionally `grind_only.target_name_box` and `grind_only.player_mana_box`.

Set `image_width` and `image_height` to the screenshot's size, and give the layout an `id` and a short `provenance` note (for example, "calibrated 2026-10-08 from calibration/…png").

## 6. Declare your key bindings

In `controls.bindings`, every binding needs a macOS **virtual keycode** and a `verified_from` note saying where you checked it (for example, "Key Bindings menu, 2026-10-08"). Required: `target_enemy`, `forward`, `backward`, `turn_left`, `turn_right`, `smite`. If looting is on, `interact_target` is required too. Common keycodes: Tab 48, W 13, S 1, ← 123, → 124, Escape 53. You can also set them with `configure --binding name=keycode`.

`toggle_hud` (Option+Z, keycodes `[58, 6]`) hides the interface for clean screenshots while travelling. If you don't want that, set `grind_only.clean_world_observation: false`.

## 7. Choose the policy

The template mirrors the final campaign's settings. The main knobs:

| Setting | Meaning |
|---|---|
| `goal_level` | Stop after two fresh readings of this level |
| `duration_seconds` | Maximum run length (default 2 hours) |
| `target_bands_by_player_level` | Which creature levels may be attacked, by your level |
| `hunting_progression_by_player_level` | Which catalog areas and creatures to hunt, by your level |
| `fixed_hunting_area` | Restrict to one catalog area (the final campaign used `coldridge_southeast_troggs`) |
| `smite_burst_count` | Casts per attack decision (1–3) |
| `target_opening_cast` | Cast one Smite right after Sage chooses to target an enemy, if it's eligible |
| `heal_health_fraction` | Health below which Sage is offered a self-heal (0.15–0.40) |
| `combat_log_enabled` | Read kills from the game's combat log (run `/combatlog` in game first) |
| `selected_target_observer` | Sage observations of the selected target; keep `backend: sage` |
| `session_id` | A new, unique name for every run |

Hunting areas live in [`profiles/hunting-areas.yaml`](../profiles/hunting-areas.yaml). Each entry is a starting location from public game databases, not a verified route.

## 8. Run

```sh
git status                     # the runner refuses uncommitted changes under src/
uv run sage-wow --profile data/my-profile.yaml doctor
uv run sage-wow --profile data/my-profile.yaml run
```

Keep the game window in front. The first run on a new character expects level 1. Each run writes `events.sqlite3`, `status.json` and retained evidence to `data/runs/<timestamp>/`.

**To pause or stop:** press **Ctrl+Alt+Escape**, press Ctrl+C in the terminal, or run `uv run sage-wow pause | resume | stop`. If the window loses focus, the harness releases all keys and pauses until the game is back in front. A stopped run can't be resumed; start a new one with a new `session_id`.

**To see what's happening:** `uv run sage-wow status` shows the run state and Sage usage. `uv run sage-wow --profile data/my-profile.yaml overlay` shows a small always-on-top panel with Sage's latest decision.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `UI layout … expects WxH screenshot pixels` | The window size or display scale changed. Recalibrate. |
| `Verified <name> binding required` | That binding is missing a keycode or `verified_from`. |
| Pauses immediately | The game isn't the frontmost window, or Accessibility permission is missing. |
| Stop hotkey doesn't work | Input Monitoring isn't granted to the host app. |
| Sage requests fail with 401/402 | Check `SAGE_API_KEY` and your Levanto plan. |
| Targets are never attacked | Check `target_overlay`, `target_level_box` and the level bands. Run `sage-wow decide-frame` on a saved screenshot to see what Sage reads. |

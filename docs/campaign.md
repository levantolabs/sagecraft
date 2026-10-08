# The level-five campaign

**Character:** a dwarf priest, starting at level 1 in Coldridge Valley.
**Dates:** October 5–6, 2026.
**Model:** `levanto-sage-v1.3`, which every returned decision reported.
**Result:** level 5, confirmed by two fresh readings of the level badge.

This page gives the full picture: what Sage did, what the harness did, and what people did. The numbers come from the run databases and the game's own combat log; per-session data is in [`campaign-metrics.json`](../assets/campaign-metrics.json).

![Campaign timeline: 32 sessions over 16½ hours, with level-ups at sessions 1, 2, 6, 13 and 32](../assets/campaign-timeline.png)

## Results

| | |
|---|---:|
| Level | **1 → 5** |
| Sage decisions returned | **4,573** (plus 280 target observations, also Sage) |
| Median Sage response time | **0.8 s** (request time only; capture and input not included) |
| Kills in the game's combat log | **128 or more** (deduplicated, attributed to our character) |
| Keyboard/mouse commands sent | **1,869** (1,864 completed) |
| Active play time | **5h 29m** |
| Elapsed time, first to last session | **16h 31m** |
| Sessions | **32** |
| Deaths | **3** (two in play, one caused during a manual recovery) |

"Decisions" includes everything Sage was asked: what it saw (its own level, whether a target is selected) as well as what to do. Active time includes waiting, sensing and stalled stretches, not only combat.

## What it took

This was a **supervised development campaign**. The harness was being finished while it ran.

- **32 sessions, 31 of them stopped before the goal.** 17 were stopped by us because the character wasn't making progress. 10 were stopped by the harness's own safety checks (focus lost, heartbeat missed, an input that couldn't be confirmed). 2 ended in death, 1 hit the evidence-storage cap, 1 ended for a strategy change, and 1 reached level 5.
- **16 harness fixes between sessions.** Examples: chat blocking combat, navigation waiting forever, retries that stopped the character from relocating, repeated ineffective casts. These changed the harness, not the model.
- **1 strategy change.** After session 22 we fixed the hunting area to the Rockjaw trogg grounds, instead of letting Sage choose between areas.
- **8 recoveries with the runner stopped:** three resurrections, terrain escapes, one Hearthstone and small movement fixes. Most were done by an AI desktop agent assisting the operators; one resurrection was finished by hand. Sage never resurrected the character on its own.
- **External interruptions:** a game maintenance window and client update (about 47 minutes), a host machine running out of memory, and 5 transient API errors (none ended a session).

## The final session

| | |
|---|---:|
| Start | Late level 4 (about 85% of the way to 5) |
| Duration | 18 min 5 s |
| Kills in the combat log | 11 or more |
| Sage decisions | 278 |
| Commands sent | 111, all completed |
| Human gameplay input, code changes, restarts | **0** |
| Finish | Two fresh level-5 readings; the runner stopped itself |

Two transient API errors happened in this session; the run carried on.

The [final-run excerpt](../examples/final-run-excerpt.jsonl) shows the last minute: a route that ran out of options, a broader question, a fresh target check, a six-option combat decision, and the two level-5 readings. Load it with `sage-wow replay` (see the [README](../README.md#try-it-offline)).

## What this does and doesn't show

**It shows** a decision model using screenshots to make thousands of gameplay judgments that took a character to a verified goal, inside a harness that constrains and checks every action.

**It doesn't show:**
- a fully autonomous run from level 1 to 5;
- play anywhere other than this zone and class;
- recovery from death;
- what the harness alone, or a simpler policy, would have achieved with the same menus.

That last comparison is the natural next experiment.

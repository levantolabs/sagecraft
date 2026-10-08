# Architecture

SageCraft is a loop: capture the game window, ask Sage a multiple-choice question about it, carry out the answer under strict checks, and look again. This page maps that loop to the code.

## Who does what

| Component | Responsibility | Limit |
|---|---|---|
| **Sage** | Visual judgments and gameplay choices | Chooses only from the options it is given, or returns no choice |
| **Perception** | Capture the window, crop and scale images, read text (Apple Vision OCR) and bar fill (pixels) | Measurements can be wrong; they inform options but never pick one |
| **Controller** | Keep the goal and history, build each question and its options, escalate stalled tasks | The menus shape what Sage can do, and can themselves contain bugs |
| **Executor** | Check the exact window, focus and input ownership, then send keys or clicks | A receipt proves input was sent, not that it worked |
| **Combat-log reader** | Read kills and deaths from the game's own combat log file | Separate evidence; not game-memory access |

## One decision cycle

```mermaid
sequenceDiagram
    participant W as Game window
    participant H as Harness
    participant S as Sage
    participant E as Executor
    H->>W: Capture the calibrated window
    H->>H: Read HUD, recall task and recent outcomes
    H->>S: Screenshot + context + 2–20 options
    S-->>H: One option, or no choice
    alt Valid choice and the scene is unchanged
        H->>E: Chosen action
        E->>W: Guarded keys or clicks
        E-->>H: Completed / incomplete / unknown
        H->>W: Fresh capture to see the effect
    else No choice, error, stale scene or lost focus
        H->>H: Send nothing; observe again, pause or stop
    end
```

The request, source frame, option list, choice and input receipt are logged together in SQLite. One choice can authorise a short fixed sequence, such as a burst of three casts, with each step re-checked. Window cleanup, input release and scheduling are deterministic and need no model call.

## Nested loops

| Scale | Looks at | Can decide | Escalates when |
|---|---|---|---|
| Action | Current frame, selected target, last input's effect | Cast, short move, inspect, correct | No visible effect, stale context |
| Encounter | Target identity, level and health; cast history | Engage, switch target, recover, leave | Repeated ineffective casts, unsuitable target, survival risk |
| Navigation | Destination, measured movement, failed approaches | Probe, detour, turn, scout, change destination | Retry menu exhausted |
| Hunting | Target levels, candidate areas, recent opportunities | Keep or change area | Local search can't make progress |
| Goal | Own level | Continue, or stop | Two fresh level-5 readings, death or a safety stop |

Escalating keeps the failure history, so renaming a task or picking the same destination again doesn't reset retry budgets.

Hunting areas come from [`profiles/hunting-areas.yaml`](profiles/hunting-areas.yaml), a small catalog of locations sourced from public game databases. A catalog entry is a starting hypothesis, not a verified route or a promise of creatures.

## Code map

| Path | What's there |
|---|---|
| [`app.py`](src/sage_wow/app.py) | The `sage-wow` CLI: doctor, configure, calibrate, run, stop, replay, decide-frame |
| [`grind_runner.py`](src/sage_wow/grind_runner.py) | Run lifecycle: launch checks, pause/stop, safety gates, checkpoints |
| [`agent/grind_only.py`](src/sage_wow/agent/grind_only.py) | Main controller: builds questions and options, dispatches decisions |
| [`agent/grind_combat.py`](src/sage_wow/agent/grind_combat.py) | Target acquisition and combat choices |
| [`agent/grind_search.py`](src/sage_wow/agent/grind_search.py) | Hunting plans, movement history, progress accounting |
| [`agent/grind_navigation_recovery.py`](src/sage_wow/agent/grind_navigation_recovery.py) | Broader questions when navigation stalls |
| [`agent/selected_target_vision.py`](src/sage_wow/agent/selected_target_vision.py) | Sage observations of the selected target |
| [`agent/cycle.py`](src/sage_wow/agent/cycle.py) | One observe → decide → act cycle |
| [`sage/client.py`](src/sage_wow/sage/client.py) | Sage API client |
| [`control/executor.py`](src/sage_wow/control/executor.py) | Input ownership and guarded commands |
| [`platform/macos/`](src/sage_wow/platform/macos) | Screen capture, input, window and focus checks |
| [`dashboard/`](src/sage_wow/dashboard) | Read-only live overlay data |
| [`storage.py`](src/sage_wow/storage.py), [`replay.py`](src/sage_wow/replay.py) | SQLite event log and JSONL replay |

## Safety rules

These hold for any change:

- Input goes only to the exact, verified, foreground game window, from one process at a time.
- Losing focus releases all held keys and pauses.
- Partial or unknown input, a failed release, or a broken integrity check stops the run.
- Ctrl+Alt+Escape, Ctrl+C and `sage-wow stop` always work.
- The runner records a hash of its own source and profile at launch, and refuses to start from uncommitted source changes.

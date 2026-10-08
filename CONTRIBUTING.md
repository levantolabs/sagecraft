# Contributing to SageCraft

Thanks for your interest. You can work on most of SageCraft without a game account or a Sage API key.

## Set up

Python 3.12–3.14 and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --extra dev
uv run pytest -n auto
uv run sage-wow --data-dir data/demo replay examples/final-run-excerpt.jsonl
```

The test suite runs with live input and screen capture disabled, blocks network access and never starts a live runner. Keep it that way: don't remove a guard in `tests/conftest.py` to make a test pass. On macOS, add `--extra macos` for the native capture and input code.

Start with [ARCHITECTURE.md](ARCHITECTURE.md) and the [final-run excerpt](examples/final-run-excerpt.jsonl).

## Good first contributions

- **Turn a failure into a test:** take a small event sequence where the controller made a poor menu or got stuck, and add a deterministic regression test.
- **Better traces:** improve tooling so a reader can follow screenshot → options → Sage's choice → input → effect.
- **One recovery boundary:** find a case where stale target, travel or cast history blocks a sensible next action.
- **Calibration helpers:** measuring the HUD rectangles in a profile is manual today.
- **Portability notes:** list what ties the harness to macOS, this client or this UI layout before proposing another platform.

For larger changes, open an issue first describing the problem and the behaviour you propose.

## Reporting a bug

Include the commit, Python and macOS versions, what you expected, what happened, and the smallest way to reproduce it. For a decision problem, include the options offered, Sage's choice, the input receipt and what happened next. Say whether the evidence is live, replayed or mocked.

Before sharing logs, remove API keys, account and character details, other players' names and chat, and local paths. Don't attach raw `data/` directories, databases or recordings.

## Pull requests

Fork, branch, and open a PR against `main`. Explain the problem, the new behaviour, how you tested it and any remaining limits. CI must pass.

Keep three things distinct in code and docs: what Sage chose, what the harness did deterministically, and what a person did. A passing test or a completed keypress is not proof that something worked in the game.

By contributing you agree that your contributions are licensed under the [Apache 2.0 License](LICENSE).

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from sage_wow.config import DEFAULT_PROFILE, Profile, load_profile, save_profile, session_data_dir
from sage_wow.platform.macos.capture import CaptureError, capture_window
from sage_wow.platform.macos.hotkey import parse_hotkey
from sage_wow.platform.macos.system import dependencies, host_checks, list_windows, selected_window_is_frontmost, window_details
from sage_wow.platform.macos.window_gate import current_gate
from sage_wow.perception.ocr import observations_as_dict, recognize_text
from sage_wow.replay import ReplaySession
from sage_wow.status import read_status, write_command
from sage_wow.sage.client import CandidateSet, ChoiceOption, DecisionEnvelope, SageClient
from sage_wow.storage import EventStore


def _profile(path: str) -> Profile:
    return load_profile(Path(path))


def doctor(args: argparse.Namespace) -> int:
    profile = _profile(args.profile)
    checks = host_checks() + dependencies()
    window = window_details(profile.window_id) if profile.window_id is not None else None
    if window:
        checks.append(_check("selected_client_window", "ok", f"id={window['window_id']} owner={window['owner']} title={window['title']!r}"))
        foreground = selected_window_is_frontmost(window)
        checks.append(_check("selected_window_foreground", "ok" if foreground else "needs_attention",
                             "frontmost application matches selected window owner" if foreground else "bring selected window to foreground"))
        expected = profile.values.get("client", {}).get("expected_bundle_id")
        expected_path = profile.values.get("client", {}).get("installation_path")
        identity = _app_identity(int(window["owner_pid"]))
        bundle = identity.get("bundle_id")
        if expected and expected_path:
            detected_path = os.path.realpath(identity["path"]) if identity.get("path") else None
            expected_real_path = os.path.realpath(expected_path)
            identity_matches = bundle == expected and detected_path == expected_real_path
            checks.append(_check("client_identity", "ok" if identity_matches else "mismatch",
                                 f"expected bundle={expected}, path={expected_real_path}; detected bundle={bundle or 'unknown'}, path={detected_path or 'unknown'}"))
        else:
            checks.append(_check("client_identity", "unverified", f"observed bundle={bundle or 'unknown'}, path={identity.get('path') or 'unknown'}; configure the verified bundle ID and exact client app path"))
    elif profile.window_id is not None:
        checks.append(_check("selected_client_window", "needs_attention", f"window id {profile.window_id} is not visible"))
    else:
        checks.append(_check("selected_client_window", "not_configured", "select an exact window id with `sage-wow configure --window-id ID`"))
    checks.append(_check("game_environment", "configured", f"edition={profile.edition}, environment={profile.environment}, build={profile.values['game'].get('client_build') or 'unknown'}"))
    calibrated = bool(profile.calibration.get("calibrated_at"))
    checks.append(_check("calibration", "ok" if calibrated else "not_configured", "calibration preview saved" if calibrated else "run `sage-wow calibrate` after selecting the game window"))
    input_gate = current_gate(profile)
    checks.append(_check("live_control_gate", "ready" if input_gate.valid else "disarmed",
                         "exact foreground game window, verified app identity and matching calibration" if input_gate.valid else "requires the same selected window to be foreground, calibrated, and matched to an explicitly verified bundle ID"))
    checks.append(_check("sage_api", "configured" if os.getenv("SAGE_API_KEY") else "not_configured", "key present in process environment" if os.getenv("SAGE_API_KEY") else "SAGE_API_KEY not set; replay is available and API calls require a local key"))
    print(json.dumps({"profile": str(profile.path), "checks": [vars(c) for c in checks], "visible_windows": list_windows()}, indent=2))
    return 0


def _check(name: str, status: str, detail: str):
    from sage_wow.platform.macos.system import Check
    return Check(name, status, detail)


def _app_identity(pid: int) -> dict[str, str | None]:
    if sys.platform != "darwin":
        return {"bundle_id": None, "path": None}
    try:
        from AppKit import NSRunningApplication
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        bundle = str(app.bundleIdentifier()) if app and app.bundleIdentifier() else None
        url = app.bundleURL() if app else None
        path = str(url.path()) if url and url.path() else None
        return {"bundle_id": bundle, "path": path}
    except (ImportError, AttributeError):
        return {"bundle_id": None, "path": None}


def configure(args: argparse.Namespace) -> int:
    path = Path(args.profile)
    profile = load_profile(path)
    values = dict(profile.values)
    if args.window_id is not None:
        candidate = window_details(args.window_id)
        if candidate is None:
            print(f"Window id {args.window_id} is not currently visible; use `sage-wow doctor` to inspect exact window IDs.", file=sys.stderr)
            return 2
        values["client"]["window_id"] = args.window_id
        if values.get("calibration", {}).get("window_id") != args.window_id:
            previous_ui_layout = values.get("calibration", {}).get("ui_layout")
            values["calibration"] = {"window_id": None, "window_bounds": None, "content_bounds": None,
                                      "display_scale": None, "calibrated_at": None}
            if previous_ui_layout is not None:
                values["calibration"]["ui_layout"] = previous_ui_layout
        print(f"Selected window id={args.window_id}, owner={candidate['owner']}, title={candidate['title']!r}")
    if args.expected_bundle_id is not None:
        values["client"]["expected_bundle_id"] = args.expected_bundle_id
    if args.installation_path is not None:
        values["client"]["installation_path"] = args.installation_path
    if args.environment:
        if args.environment not in {"beta", "live"}:
            print("environment must be beta or live", file=sys.stderr)
            return 2
        values["game"]["environment"] = args.environment
    if args.region is not None:
        values["game"]["region"] = args.region
    if args.ruleset is not None:
        values["game"]["ruleset"] = args.ruleset
    if args.locale is not None:
        values["game"]["locale"] = args.locale
    if args.realm is not None:
        values["game"]["beta_test_realm"] = args.realm
    if args.name is not None:
        values["character"]["name"] = args.name
    if args.surname is not None:
        values["character"]["surname"] = args.surname
    if args.race is not None:
        values["character"]["race"] = args.race
    if args.faction is not None:
        values["character"]["faction"] = args.faction
    if args.client_version is not None:
        values["game"]["client_version"] = args.client_version
    if args.client_build is not None:
        values["game"]["client_build"] = args.client_build
    if args.content_phase is not None:
        values["game"]["content_phase"] = args.content_phase
    if args.level_cap is not None:
        if args.level_cap < 1:
            print("active level cap must be positive", file=sys.stderr)
            return 2
        values["game"]["active_level_cap"] = args.level_cap
    if args.visual_mode is not None:
        values["ui"]["visual_mode"] = args.visual_mode
    if args.ui_scale is not None:
        if args.ui_scale <= 0:
            print("UI scale must be positive", file=sys.stderr)
            return 2
        values["ui"]["ui_scale"] = args.ui_scale
    if args.cooldown_manager is not None:
        values["ui"]["cooldown_manager_enabled"] = args.cooldown_manager == "enabled"
    for entry in args.binding or []:
        try:
            name, raw_keycode = entry.split("=", 1)
            keycode = int(raw_keycode)
            if not name.strip() or keycode < 0:
                raise ValueError
        except ValueError:
            print("bindings must use PANEL=MACOS_KEYCODE with a non-negative keycode", file=sys.stderr)
            return 2
        values["controls"].setdefault("bindings", {})[name.strip()] = {"keycode": keycode}
    if args.duration is not None:
        if args.duration <= 0:
            print("duration must be positive; omit it for an unbounded session", file=sys.stderr)
            return 2
        values["session"]["duration_seconds"] = args.duration
    if args.stop_hotkey is not None:
        try:
            parse_hotkey(args.stop_hotkey)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        values["controls"]["stop_hotkey"] = args.stop_hotkey
    save_profile(Profile(path, values))
    print(f"Updated {path}. Unknown or unprovided fields remain null.")
    return 0


def calibrate(args: argparse.Namespace) -> int:
    profile = _profile(args.profile)
    if profile.window_id is None:
        print("Choose the exact game client window first: `sage-wow doctor`, then `sage-wow configure --window-id ID`.", file=sys.stderr)
        return 2
    try:
        frame = capture_window(profile.window_id, Path(args.output_dir))
    except CaptureError as exc:
        print(f"Calibration paused: {exc}", file=sys.stderr)
        return 2
    window = window_details(profile.window_id)
    if not window:
        print("Selected window disappeared during calibration", file=sys.stderr)
        return 2
    bounds = window["bounds"]
    values = dict(profile.values)
    previous_ui_layout = values.get("calibration", {}).get("ui_layout")
    values["calibration"] = {
        "window_id": profile.window_id,
        "window_bounds": bounds,
        "content_bounds": {"x": 0, "y": 0, "width": int(bounds["Width"]), "height": int(bounds["Height"])},
        "captured_image_size": {"width": frame.width, "height": frame.height},
        "display_scale": round(frame.width / int(bounds["Width"]), 3),
        "ui_scale": values.get("ui", {}).get("ui_scale"),
        "calibrated_at": frame.captured_at,
        "preview_frame": frame.image_path,
    }
    if previous_ui_layout is not None:
        values["calibration"]["ui_layout"] = previous_ui_layout
    save_profile(Profile(profile.path, values))
    print(f"Saved capture baseline: {frame.image_path}\nWindow bounds: {bounds}\nImage: {frame.width}x{frame.height}; display scale: {values['calibration']['display_scale']}x")
    print("Inspect this baseline and compare the game content edges before enabling any live input.")
    return 0


def observe(args: argparse.Namespace) -> int:
    profile = _profile(args.profile)
    if profile.window_id is None:
        print("No exact game window selected. Run `sage-wow doctor` and select an ID with `sage-wow configure --window-id ID`.", file=sys.stderr)
        return 2
    try:
        frame = capture_window(profile.window_id, Path(args.output_dir))
    except CaptureError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"frame_id": frame.frame_id, "captured_at": frame.captured_at, "source": frame.source,
                      "width": frame.width, "height": frame.height, "image_path": frame.image_path}, indent=2))
    return 0


def ocr(args: argparse.Namespace) -> int:
    try:
        observations = recognize_text(Path(args.image), language=args.language, accurate=not args.fast)
    except (RuntimeError, OSError) as exc:
        print(f"OCR unavailable: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"source": "apple_vision_ocr", "image": str(Path(args.image)),
                      "observations": observations_as_dict(observations)}, indent=2))
    return 0


def replay(args: argparse.Namespace) -> int:
    source = Path(args.session)
    session = ReplaySession(source)
    frames = list(session.frames())
    events = list(session.events())
    store = EventStore(Path(args.data_dir) / "replay.sqlite3")
    try:
        for event in events:
            store.append(event)
    finally:
        store.close()
    decisions = [{"at": event.occurred_at, "chosen": event.payload.get("chosen"),
                  "from": [option.get("option") for option in event.payload.get("candidate_options", [])],
                  "model": event.payload.get("model")}
                 for event in events if event.event_type == "sage_decision"]
    print(json.dumps({"mode": "replay", "source": str(source), "frame_count": len(frames), "event_count": len(events),
                      "last_frame": vars(frames[-1]) if frames else None,
                      "event_types": [event.event_type for event in events],
                      "sage_decisions": decisions}, indent=2))
    return 0


async def decide_frame_async(args: argparse.Namespace) -> int:
    api_key = os.getenv("SAGE_API_KEY", "")
    profile = _profile(args.profile)
    try:
        raw = json.loads(Path(args.candidates).read_text())
        candidates = CandidateSet(
            version=str(raw["version"]),
            options=tuple(ChoiceOption(str(item["option"]), str(item["description"]), dict(item.get("binding", {}))) for item in raw["options"]),
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
        print(f"Invalid candidate set: {exc}", file=sys.stderr)
        return 2
    envelope = DecisionEnvelope.create(args.frame_id, args.session_epoch, candidates)
    sage_config = profile.values.get("sage", {})
    client = SageClient(
        api_key=api_key,
        endpoint=str(sage_config.get("endpoint", "https://sage.levanto.ai")),
        timeout_seconds=float(sage_config.get("request_timeout_seconds", 12)),
        max_image_bytes=int(sage_config.get("max_image_bytes", 4 * 1024 * 1024)),
    )
    try:
        decision = await client.decide_image_choice(
            Path(args.image), args.text, args.instructions, candidates, envelope, reasoning=args.reasoning,
        )
    finally:
        await client.close()
    store = EventStore(session_data_dir(Path(args.data_dir), profile) / "events.sqlite3")
    try:
        from sage_wow.models import Event
        store.append(Event.create("sage_decision", decision.event_payload()))
    finally:
        store.close()
    print(json.dumps(decision.event_payload(), indent=2))
    return 0


def decide_frame(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(decide_frame_async(args))
    except Exception as exc:
        from sage_wow.sage.client import SageError
        if isinstance(exc, SageError):
            print(f"Sage decision unavailable: {exc}", file=sys.stderr)
            return 2
        raise


def run(args: argparse.Namespace) -> None:
    from datetime import datetime, timezone
    from sage_wow.grind_runner import launch
    profile = _profile(args.profile)
    run_dir = Path(args.run_dir) if args.run_dir else Path(args.data_dir) / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"Run directory: {run_dir}")
    asyncio.run(launch(profile, run_dir))


def _active_run_dir(args: argparse.Namespace) -> Path:
    """An explicit --run-dir, else the run the live runner last published."""
    if getattr(args, "run_dir", None):
        return Path(args.run_dir)
    from sage_wow.dashboard.session_source import pointer_path
    try:
        return Path(json.loads(pointer_path().read_text())["directory"])
    except (OSError, KeyError, ValueError) as exc:
        raise ValueError("No active run found; pass --run-dir") from exc


def status(args: argparse.Namespace) -> int:
    data_dir = _active_run_dir(args)
    result = read_status(data_dir)
    try:
        store = EventStore(data_dir / "events.sqlite3")
        try:
            decisions = [row for row in store.connection.execute("SELECT payload_json FROM events WHERE event_type='sage_decision'").fetchall()]
            payloads = [json.loads(row[0]) for row in decisions]
            known = [int(item["estimated_decision_units"]) for item in payloads if item.get("estimated_decision_units") is not None]
            result["sage_usage"] = {
                "decision_count": len(payloads),
                "estimated_decision_units": sum(known),
                "unestimated_decision_count": len(payloads) - len(known),
                "basis": "Sage response usage metadata; units are estimates",
            }
        finally:
            store.close()
    except OSError:
        result["sage_usage"] = {"decision_count": 0, "estimated_decision_units": 0, "unestimated_decision_count": 0}
    print(json.dumps(result, indent=2))
    return 0


def control(args: argparse.Namespace) -> int:
    data_dir = _active_run_dir(args)
    write_command(data_dir, args.command)
    print(f"Requested {args.command}; current state: {read_status(data_dir).get('state')}")
    return 0


def overlay(args: argparse.Namespace) -> None:
    from sage_wow.platform.macos.overlay import run_overlay
    profile=_profile(args.profile)
    if profile.window_id is None:raise ValueError('overlay requires a configured game window')
    run_overlay(session_data_dir(Path(args.data_dir).resolve(),profile),profile.window_id,args.x,args.y,
                follow=not args.pin_session)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sage-wow", description="Persistent visual agent harness for World of Warcraft")
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE), help="Profile YAML")
    parser.add_argument("--data-dir", default="data", help="Root directory for run data")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Inspect host, permissions, dependencies and selected game client").set_defaults(func=doctor)
    conf = sub.add_parser("configure", help="Set setup values and the exact client window")
    conf.add_argument("--window-id", type=int)
    conf.add_argument("--expected-bundle-id", help="Set only after confirming the client bundle identity")
    conf.add_argument("--installation-path", help="Verified client installation path")
    conf.add_argument("--environment", choices=("beta", "live"))
    conf.add_argument("--region")
    conf.add_argument("--ruleset")
    conf.add_argument("--locale")
    conf.add_argument("--realm")
    conf.add_argument("--race", choices=("Human", "Dwarf", "Night Elf", "Gnome", "Troll", "Undead"))
    conf.add_argument("--faction")
    conf.add_argument("--name")
    conf.add_argument("--surname")
    conf.add_argument("--client-version")
    conf.add_argument("--client-build")
    conf.add_argument("--content-phase")
    conf.add_argument("--level-cap", type=int)
    conf.add_argument("--visual-mode")
    conf.add_argument("--ui-scale", type=float)
    conf.add_argument("--cooldown-manager", choices=("enabled", "disabled"))
    conf.add_argument("--binding", action="append", help="Explicit panel/action keycode, e.g. --binding smite=18 (repeatable)")
    conf.add_argument("--duration", type=float)
    conf.add_argument("--stop-hotkey", help="Global stop combo, e.g. ctrl+alt+escape or ctrl+keycode:12")
    conf.set_defaults(func=configure)
    cal = sub.add_parser("calibrate", help="Capture a selected window and save a calibration baseline")
    cal.add_argument("--output-dir", default="calibration")
    cal.set_defaults(func=calibrate)
    obs = sub.add_parser("observe", help="Capture one frame from the selected foreground game window")
    obs.add_argument("--output-dir", default="data/captures")
    obs.set_defaults(func=observe)
    ocr_cmd = sub.add_parser("ocr", help="Read visible text locally with Apple Vision")
    ocr_cmd.add_argument("image")
    ocr_cmd.add_argument("--language", help="Optional BCP-47 recognition language, such as en-US")
    ocr_cmd.add_argument("--fast", action="store_true", help="Prioritize speed over accuracy")
    ocr_cmd.set_defaults(func=ocr)
    rep = sub.add_parser("replay", help="Replay a recorded JSONL fixture without a game or Sage key")
    rep.add_argument("session")
    rep.set_defaults(func=replay)
    decision = sub.add_parser("decide-frame", help="Ask Sage for a Choice decision on one explicit image file")
    decision.add_argument("image", help="Path to one screenshot/frame; only this file is sent")
    decision.add_argument("--frame-id", required=True)
    decision.add_argument("--session-epoch", default="manual")
    decision.add_argument("--candidates", required=True, help="JSON file containing candidate-set version and options")
    decision.add_argument("--text", default="", help="Compact context sent with the image")
    decision.add_argument("--instructions", default="Choose the best available action from the supplied options. If evidence is insufficient, choose null.")
    decision.add_argument("--reasoning", choices=("off", "auto"), default="off")
    decision.set_defaults(func=decide_frame)
    run_cmd = sub.add_parser("run", help="Start a live grinding run (requires a calibrated profile and SAGE_API_KEY)")
    run_cmd.add_argument("--run-dir", help="Fresh directory for this run; defaults to DATA_DIR/runs/<UTC timestamp>")
    run_cmd.set_defaults(func=run)
    stat = sub.add_parser("status", help="Show the active run's status and Sage usage")
    stat.add_argument("--run-dir", help="Run directory; defaults to the active run")
    stat.set_defaults(func=status)
    hud = sub.add_parser('overlay', help='Show a click-through live decision and timer overlay above WoW')
    hud.add_argument('--x',type=int,default=24)
    hud.add_argument('--y',type=int,default=48)
    hud.add_argument('--pin-session',action='store_true',help='Keep displaying this profile/data directory instead of following new live runs')
    hud.set_defaults(func=overlay)
    for name in ("pause", "resume", "stop"):
        cmd = sub.add_parser(name, help=f"Request the active run to {name}")
        cmd.add_argument("--run-dir", help="Run directory; defaults to the active run")
        cmd.set_defaults(func=control, command=name)
    return parser


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.func(args)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"sage-wow: {exc}\n")
    if isinstance(result, int):
        raise SystemExit(result)


if __name__ == "__main__":
    main()

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from sage_wow.config import Profile
from sage_wow.control.executor import GateSnapshot
from sage_wow.platform.macos.geometry import Rect
from sage_wow.platform.macos.system import selected_window_is_frontmost, window_details


def identity_window_key(gate: GateSnapshot) -> tuple | None:
    """Process/window continuity for a disarmed recovery, never input authority."""
    evidence = gate.identity_evidence or {}
    window = evidence.get("window") or {}
    pid = window.get("owner_pid")
    bundle, path = evidence.get("expected_bundle_id"), evidence.get("expected_path")
    if (type(pid) is not int or pid <= 0 or window.get("window_id") != gate.window_id
            or not isinstance(bundle, str) or not bundle or not isinstance(path, str) or not path):
        return None
    return (gate.window_id, pid, bundle, path)


def gate_recovery_kind(gate: GateSnapshot) -> str:
    """Missing native metadata can wait disarmed; contradictions remain terminal."""
    if gate.valid:
        return "valid"
    if replace(gate, foreground=True).valid:
        return "focus"
    if not replace(gate, foreground=True, client_identity_verified=True).valid:
        return "invalid"
    evidence = gate.identity_evidence or {}
    if identity_window_key(gate) is None:
        return "invalid"
    reason = evidence.get("reason")
    if reason not in {"application_missing", "bundle_identifier_missing", "bundle_path_missing"}:
        return "invalid"
    # Missing one field must not conceal a contradiction in the other.
    bundle, path = evidence.get("detected_bundle_id"), evidence.get("detected_path")
    if bundle is not None and bundle != evidence["expected_bundle_id"]:
        return "invalid"
    try:
        if path is not None and path != str(Path(evidence["expected_path"]).resolve()):
            return "invalid"
    except (OSError, RuntimeError, ValueError):
        return "invalid"
    lookups = evidence.get("lookups")
    if lookups == [{"application_present": False}, {"application_present": False}]:
        if reason != "application_missing" or bundle is not None or path is not None:
            return "invalid"
    elif lookups in ([{"application_present": True}],
                     [{"application_present": False}, {"application_present": True}]):
        if not ((reason == "bundle_identifier_missing" and bundle is None)
                or (reason == "bundle_path_missing" and path is None)):
            return "invalid"
    else:
        return "invalid"
    if len(lookups) == 2 and not (evidence.get("window_before_retry") == evidence["window"]
                                 == evidence.get("window_after_retry")):
        return "invalid"
    return "identity"


def current_gate(profile: Profile) -> GateSnapshot:
    calibration = profile.calibration
    selected_id = profile.window_id
    calibrated_id = calibration.get("window_id")
    calibrated_id = int(calibrated_id) if calibrated_id is not None else None
    saved = calibration.get("window_bounds") or {}
    calibrated_bounds = None
    try:
        calibrated_bounds = Rect(int(saved["X"]), int(saved["Y"]), int(saved["Width"]), int(saved["Height"]))
    except (KeyError, TypeError, ValueError):
        pass
    window = window_details(selected_id) if selected_id is not None else None
    if window is None:
        return GateSnapshot(None, calibrated_id, False, None, calibrated_bounds, False, False,
                            {"reason": "window_missing", "selected_window_id": selected_id})
    bounds = window["bounds"]
    current_bounds = Rect(int(bounds["X"]), int(bounds["Y"]), int(bounds["Width"]), int(bounds["Height"]))
    identity_ok = False
    expected_bundle = profile.values.get("client", {}).get("expected_bundle_id")
    expected_path = profile.values.get("client", {}).get("installation_path")
    evidence = {"reason": "unsupported_platform" if sys.platform != "darwin" else "identity_configuration_missing",
                "window": window, "expected_bundle_id": expected_bundle,
                "expected_path": expected_path, "lookups": []}
    if sys.platform == "darwin" and expected_bundle and expected_path:
        stage = "application_lookup"
        try:
            from AppKit import NSRunningApplication
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(window["owner_pid"]))
            evidence["lookups"].append({"application_present": app is not None})
            # A native lookup can transiently return nil for an unchanged live
            # window. Retry once with fresh matching WindowServer snapshots;
            # never reuse a prior app identity or retry a contradictory result.
            evidence["reason"] = "application_missing"
            if app is None:
                stage = "window_before_retry"
                before = window_details(selected_id)
                evidence["window_before_retry"] = before
                if before == window:
                    stage = "application_retry"
                    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(window["owner_pid"]))
                    evidence["lookups"].append({"application_present": app is not None})
                    stage = "window_after_retry"
                    after = window_details(selected_id)
                    evidence["window_after_retry"] = after
                    if after != window:
                        app = None
                        evidence["reason"] = "window_changed_after_retry"
                else:
                    evidence["reason"] = "window_changed_before_retry"
            stage = "bundle_url"
            detected_url = app.bundleURL() if app else None
            stage = "bundle_path"
            detected_path = Path(str(detected_url.path())).resolve() if detected_url else None
            evidence["detected_path"] = str(detected_path) if detected_path is not None else None
            stage = "bundle_identifier"
            detected_bundle = app.bundleIdentifier() if app else None
            evidence["detected_bundle_id"] = str(detected_bundle) if detected_bundle is not None else None
            stage = "identity_comparison"
            identity_ok = bool(app and detected_bundle == expected_bundle
                               and detected_path == Path(expected_path).resolve())
            if app:
                evidence["reason"] = ("verified" if identity_ok else
                    "bundle_identifier_missing" if detected_bundle is None else
                    "bundle_identifier_mismatch" if detected_bundle != expected_bundle else
                    "bundle_path_missing" if detected_path is None else "bundle_path_mismatch")
        except (ImportError, AttributeError, TypeError) as exc:
            identity_ok = False
            evidence.update(reason="identity_query_exception", stage=stage, error_type=type(exc).__name__)
    valid_calibration = bool(calibration.get("calibrated_at")) and calibrated_bounds == current_bounds
    return GateSnapshot(
        int(window["window_id"]), calibrated_id, selected_window_is_frontmost(window), current_bounds,
        calibrated_bounds, valid_calibration, identity_ok, evidence,
    )

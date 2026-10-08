import httpx
import pytest
import socket
import subprocess
from sage_wow.platform.macos import capture


def test_unmocked_external_http_call_is_denied_before_dns_or_network():
    with pytest.raises(RuntimeError, match="test network guard denied DNS"):
        httpx.get("https://api.sage.example/decide", timeout=.1)


def test_external_ip_connection_is_denied_before_connect():
    sock = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="test network guard denied connection"):
            sock.connect(("198.51.100.7", 443))
    finally:
        sock.close()


def test_mock_transport_remains_available_without_network():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with httpx.Client(transport=transport) as client:
        response = client.get("https://api.sage.example/decide")
    assert response.json() == {"ok": True}


def test_live_runner_subprocess_launch_is_denied():
    with pytest.raises(RuntimeError, match="denied a live Sage WoW runner launch"):
        subprocess.Popen(["python", "-m", "sage_wow.grind_runner"])


def test_real_capture_backend_is_denied_before_window_enumeration(monkeypatch):
    monkeypatch.setenv("SAGE_WOW_DISABLE_LIVE_CAPTURE", "1")
    monkeypatch.setattr(capture, "window_details", lambda *_: pytest.fail(
        "capture guard must run before Quartz window enumeration"))
    with pytest.raises(capture.CaptureError, match="SAGE_WOW_DISABLE_LIVE_CAPTURE=1"):
        capture._capture_window(123)


def test_public_capture_window_is_denied_before_platform_apis(monkeypatch):
    monkeypatch.setenv("SAGE_WOW_DISABLE_LIVE_CAPTURE", "1")
    monkeypatch.setattr(capture.sys, "platform", "darwin")
    monkeypatch.setattr(capture, "_autorelease_pool", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(capture, "window_details", lambda *_: pytest.fail(
        "public capture guard must run before Quartz window enumeration"))
    with pytest.raises(capture.CaptureError, match="SAGE_WOW_DISABLE_LIVE_CAPTURE=1"):
        capture.capture_window(123)

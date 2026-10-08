import pytest
import ipaddress
import socket
import os
import subprocess
from pathlib import Path


@pytest.fixture(autouse=True)
def disable_live_input_for_tests(monkeypatch):
    """Prevent every test and its children from dispatching real OS input."""
    monkeypatch.setenv("SAGE_WOW_DISABLE_LIVE_INPUT", "1")
    monkeypatch.setenv("SAGE_WOW_DISABLE_LIVE_CAPTURE", "1")
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_popen = subprocess.Popen

    def loopback_host(host):
        if not isinstance(host, str):
            return False
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
        except ValueError:
            return False

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not loopback_host(host):
            raise RuntimeError(f"test network guard denied DNS lookup for {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(sock, address):
        if isinstance(address, tuple) and address and not loopback_host(address[0]):
            raise RuntimeError(f"test network guard denied connection to {address[0]!r}")
        return real_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if isinstance(address, tuple) and address and not loopback_host(address[0]):
            raise RuntimeError(f"test network guard denied connection to {address[0]!r}")
        return real_connect_ex(sock, address)

    def guarded_popen(args, *pargs, **kwargs):
        if isinstance(args, (list, tuple)):
            tokens = [os.fspath(item) if isinstance(item, os.PathLike) else str(item) for item in args]
        else:
            tokens = [str(args)]
        for index, token in enumerate(tokens[:-1]):
            if token == "-m" and tokens[index + 1] in {"sage_wow.app", "sage_wow.grind_runner"}:
                raise RuntimeError("test process guard denied a live Sage WoW runner launch")
        if any(Path(token).name == "sage-wow" for token in tokens if "/" in token):
            if any(token in {"run", "agent", "start"} for token in tokens):
                raise RuntimeError("test process guard denied a live Sage WoW runner launch")
        return real_popen(args, *pargs, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)


_KNOWN_FAILURES = {line.strip() for line in (Path(__file__).parent / 'known_failures.txt').read_text().splitlines()
                   if line.strip() and not line.startswith('#')}


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.nodeid in _KNOWN_FAILURES:
            item.add_marker(pytest.mark.xfail(reason='known failure at the campaign commit; see tests/known_failures.txt', strict=False))

"""Explicit opt-in for live source tests."""

import ipaddress
import os
import socket

import pytest


@pytest.fixture(autouse=True)
def offline_network_boundary(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail unexpected real egress; explicit ASGI/mock transports still execute normally."""
    if request.node.get_closest_marker("live") is not None:
        return
    resolve, connect, connect_ex = (
        socket.getaddrinfo,
        socket.socket.connect,
        socket.socket.connect_ex,
    )

    def allowed(host: object) -> None:
        if host in (None, "", "localhost", b"localhost"):
            return
        value = host.decode() if isinstance(host, bytes) else str(host)
        try:
            if ipaddress.ip_address(value.split("%", 1)[0]).is_loopback:
                return
        except ValueError:
            pass
        raise AssertionError(f"Non-live test attempted external network access: {value}")

    def lookup(host, *args, **kwargs):
        allowed(host)
        return resolve(host, *args, **kwargs)

    def guarded_connect(sock, address):
        if isinstance(address, tuple):
            allowed(address[0])
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        if isinstance(address, tuple):
            allowed(address[0])
        return connect_ex(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--live", action="store_true", help="Enable read-only source smoke tests")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live") and not os.environ.get("CI"):
        return
    skip = pytest.mark.skip(reason="live source smoke tests require --live outside CI")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)

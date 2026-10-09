"""Test setup: fake Kodi modules, vendored packages, aiohttp shim."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "fakes"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from resources.lib import bootstrap  # noqa: E402

bootstrap.install()

import xbmc  # noqa: E402
import xbmcaddon  # noqa: E402
import xbmcgui  # noqa: E402
import xbmcvfs  # noqa: E402


@pytest.fixture(autouse=True)
def kodi(tmp_path, monkeypatch):
    """Reset the fake Kodi environment for each test."""
    xbmc._reset()
    xbmcgui._reset()
    xbmcaddon._reset()
    monkeypatch.setattr(xbmcvfs, "PROFILE_ROOT", tmp_path / "profile")
    return xbmc


@pytest.fixture(scope="session")
def tls_cert(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl CLI not available")
    from fake_tv import make_self_signed_cert

    return make_self_signed_cert(tmp_path_factory.mktemp("tls"))


@pytest.fixture
def patch_ports(monkeypatch):
    """Point aiowebostv's hard-coded ports at a FakeTv's ephemeral ports."""
    from aiowebostv import webos_client

    def apply(tv):
        # A port nothing listens on gives "connection refused" for ws.
        monkeypatch.setattr(webos_client, "WS_PORT", tv.ws_port or _closed_port())
        if tv.wss_port:
            monkeypatch.setattr(webos_client, "WSS_PORT", tv.wss_port)

    return apply


def _closed_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

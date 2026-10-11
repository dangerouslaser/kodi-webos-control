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
import xbmcplugin  # noqa: E402
import xbmcvfs  # noqa: E402


@pytest.fixture(autouse=True)
def kodi(tmp_path, monkeypatch):
    """Reset the fake Kodi environment for each test."""
    xbmc._reset()
    xbmcgui._reset()
    xbmcplugin._reset()
    xbmcaddon._reset()
    monkeypatch.setattr(xbmcvfs, "PROFILE_ROOT", tmp_path / "profile")
    return xbmc


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Tests may only open WebSockets to loopback; never to a real TV."""
    from urllib.parse import urlparse

    import aiohttp

    real = aiohttp.ClientSession.ws_connect

    async def guarded(self, url, **kwargs):
        host = urlparse(url).hostname or ""
        if not (host.startswith("127.") or host in ("localhost", "::1")):
            raise AssertionError(f"test tried to connect to non-loopback host {url}")
        return await real(self, url, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", guarded)


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
        # A port nothing listens on gives "connection refused". Always patch
        # both ports so the wss fallback can never reach a real TV on 3001.
        monkeypatch.setattr(webos_client, "WS_PORT", tv.ws_port or _closed_port())
        monkeypatch.setattr(webos_client, "WSS_PORT", tv.wss_port or _closed_port())

    return apply


def _closed_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def fast_picture_rules(monkeypatch):
    """The fake TV never switches signal; don't wait the real 8 s for it."""
    from resources.lib import automations

    monkeypatch.setattr(automations, "SIGNAL_SETTLE", 0.2)
    monkeypatch.setattr(automations, "RETRY_DELAY", 0.05)
    from resources.lib import soundrules

    monkeypatch.setattr(soundrules, "RESTORE_DELAY", 0.1)

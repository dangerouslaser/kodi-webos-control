"""The Remote Bridge TV app served from Kodi to the Homebrew Channel."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from resources.lib import bridgerepo
from resources.lib.bridgerepo import RepoServer


@pytest.fixture
def bundle(tmp_path):
    ipk = b"!<arch>\nnot really an ipk"
    (tmp_path / "app_1.2.3_all.ipk").write_bytes(ipk)
    (tmp_path / "icon.png").write_bytes(b"\x89PNG icon")
    (tmp_path / "bridge.json").write_text(json.dumps({
        "id": "com.example.app", "version": "1.2.3", "title": "App", "appDescription": "An app",
        "ipk": "app_1.2.3_all.ipk", "size": len(ipk), "sha256": hashlib.sha256(ipk).hexdigest(),
    }))
    return tmp_path, ipk


async def get(port, path):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: tv\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, body = raw.split(b"\r\n\r\n", 1)
    return head.split(b"\r\n")[0].decode(), body


async def test_serves_a_one_package_repository(bundle):
    directory, ipk = bundle
    server = RepoServer(str(directory))
    await server.start(0)
    port = server._server.sockets[0].getsockname()[1]
    server.port = port
    try:
        status, body = await get(port, "/repo.json")
        assert status == "HTTP/1.1 200 OK"
        manifest = json.loads(body)["packages"][0]["manifest"]
        base = f"http://127.0.0.1:{port}"
        assert manifest["version"] == "1.2.3"
        assert manifest["ipkUrl"] == f"{base}/app_1.2.3_all.ipk"   # the address the TV used
        assert manifest["ipkHash"] == {"sha256": hashlib.sha256(ipk).hexdigest()}
        assert manifest["rootRequired"] is True
        assert (await get(port, "/app_1.2.3_all.ipk")) == ("HTTP/1.1 200 OK", ipk)
        assert (await get(port, "/icon.png"))[1] == b"\x89PNG icon"
        assert (await get(port, "/../bridge.json"))[0] == "HTTP/1.1 404 Not Found"
    finally:
        await server.close()


async def test_nothing_served_without_a_bundle(tmp_path):
    server = RepoServer(str(tmp_path))
    await server.start(0)
    assert server._server is None


def test_versions_compare_numerically():
    assert bridgerepo.version_tuple("0.4.2") < bridgerepo.version_tuple("0.10.0")
    assert bridgerepo.version_tuple("0.5.0") == (0, 5, 0)
    assert bridgerepo.version_tuple("") < bridgerepo.version_tuple("0.1")


def test_the_add_on_carries_a_bridge():
    info = bridgerepo.bundled()
    assert info is not None, "run scripts/bundle_bridge.sh"
    data = (bridgerepo.BUNDLE_DIR + "/" + info["ipk"])
    assert hashlib.sha256(open(data, "rb").read()).hexdigest() == info["sha256"]

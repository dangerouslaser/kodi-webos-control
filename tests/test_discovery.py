"""SSDP discovery against a fake responder, and controller use of it."""

from __future__ import annotations

import asyncio

import pytest
import xbmcaddon
from fake_tv import FakeSsdp, FakeTv

from resources.lib import discovery, storage
from resources.lib.discovery import DiscoveredTv
from resources.lib.pairing import KeyStore
from resources.lib.settings import Settings
from resources.lib.tv import IDENTITY_FILE, TvController


async def wait_for(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_discover_finds_webos_and_ignores_other_devices():
    ssdp = FakeSsdp("2d7bd61a-89bd-de8c-4370-2ac893376948")
    await ssdp.start()
    try:
        found = await discovery.discover(1.0, target=("127.0.0.1", ssdp.port))
    finally:
        await ssdp.stop()
    assert found == [DiscoveredTv(
        host="127.0.0.1",
        uuid="2d7bd61a-89bd-de8c-4370-2ac893376948",
        name="[LG] webOS TV OLED77FAKE",
        model="OLED77FAKE.AUS",
    )]
    assert found[0].label == "[LG] webOS TV OLED77FAKE (OLED77FAKE.AUS) - 127.0.0.1"


async def test_discover_returns_empty_when_nothing_answers():
    from conftest import _closed_port

    assert await discovery.discover(0.5, target=("127.0.0.1", _closed_port())) == []


def test_parse_response_and_filter():
    hue = discovery.parse_response(b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\nUSN: uuid:x\r\n\r\n")
    assert hue is not None and not discovery.is_webos(hue)
    lg = discovery.parse_response(
        b"HTTP/1.1 200 OK\r\nST: urn:lge-com:service:webos-second-screen:1\r\n\r\n")
    assert discovery.is_webos(lg)
    assert discovery.parse_response(b"NOTIFY * HTTP/1.1\r\n\r\n") is None


def test_hints_round_trip():
    discovery.remember([DiscoveredTv("10.0.0.5", "abc")])
    discovery.remember([DiscoveredTv("10.0.0.6", "def")])
    assert discovery.hinted_uuid("10.0.0.5") == "abc"
    assert discovery.hinted_uuid("10.0.0.6") == "def"
    assert discovery.hinted_uuid("10.0.0.7") is None


# controller ---------------------------------------------------------------


@pytest.fixture
async def tv(patch_ports):
    tv = FakeTv()
    await tv.start()
    patch_ports(tv)
    yield tv
    await tv.stop()


def make(settings, found, **kwargs):
    hosts = []
    calls = []

    async def fake_discover():
        calls.append(1)
        return list(found)

    controller = TvController(
        settings,
        backoff_base=0.05,
        discover=fake_discover,
        on_host=hosts.append,
        notify=lambda k, m: kwargs.setdefault("notes", []).append(m),
        **{k: v for k, v in kwargs.items() if k != "notes"},
    )
    return controller, hosts, calls


def unreachable_factory(dead_host):
    """Route dead_host to a closed port; everything else to the fake TV."""
    from aiowebostv import WebOsClient
    from conftest import _closed_port


    def factory(host, **kw):
        client = WebOsClient("127.0.0.1", **kw)
        if host == dead_host:
            port = _closed_port()

            async def dead(uri, max_msg_size):
                raise ConnectionRefusedError(f"refused {port}")

            client._ws_connect = dead
        # client.host stays 127.0.0.1: tests must never touch real hosts.
        return client

    return factory


async def test_first_run_adopts_single_discovered_tv(tv):
    # Default loopback host, nothing listens there, never paired.
    found = [DiscoveredTv("192.0.2.224", tv.uuid, "[LG] webOS TV X")]
    controller, hosts, calls = make(
        Settings(host="127.0.0.1", loopback_fallback=False),
        found,
        client_factory=unreachable_factory("127.0.0.1"),
    )
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        assert hosts == ["192.0.2.224"]
        assert controller.settings.host == "192.0.2.224"
        assert tv.prompts == 1  # the pairing prompt confirms the choice
        assert KeyStore().get("192.0.2.224") in tv.valid_keys
        assert KeyStore().get_uuid(tv.uuid) in tv.valid_keys
        assert storage.read_json(IDENTITY_FILE) == {"host": "192.0.2.224", "uuid": tv.uuid}
    finally:
        await controller.stop()


async def test_first_run_does_not_guess_between_several_tvs(tv):
    found = [DiscoveredTv("192.0.2.10", "aaa"), DiscoveredTv("192.0.2.11", "bbb")]
    notes = []
    controller, hosts, calls = make(
        Settings(host="127.0.0.1", loopback_fallback=False),
        found,
        notes=notes,
        client_factory=unreachable_factory("127.0.0.1"),
    )
    await controller.start()
    try:
        await wait_for(lambda: calls)
        await asyncio.sleep(0.3)
        assert not controller.connected
        assert hosts == []
        assert tv.prompts == 0
        assert len(calls) == 1  # rate limited
    finally:
        await controller.stop()


async def test_no_first_run_discovery_once_something_was_paired(tv):
    KeyStore().set("10.9.9.9", "some-key")
    controller, hosts, calls = make(
        Settings(host="127.0.0.1", loopback_fallback=False),
        [DiscoveredTv("192.0.2.224", tv.uuid)],
        client_factory=unreachable_factory("127.0.0.1"),
    )
    await controller.start()
    try:
        await asyncio.sleep(0.3)
        assert calls == []
        assert tv.prompts == 0
    finally:
        await controller.stop()


async def test_follows_paired_tv_to_new_ip_without_prompt(tv):
    # Pair at the old address first.
    old = "192.0.2.224"
    controller, _, _ = make(Settings(host=old), [], client_factory=unreachable_factory("none"))
    await controller.start()
    await wait_for(lambda: controller.connected)
    await controller.stop()
    assert tv.prompts == 1

    # Now the old address is dead and SSDP reports the same UUID elsewhere,
    # plus another TV that must be ignored.
    found = [DiscoveredTv("192.0.2.50", "someone-elses-tv"), DiscoveredTv("192.0.2.99", tv.uuid)]
    controller, hosts, calls = make(Settings(host=old), found, client_factory=unreachable_factory(old))
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        assert hosts == ["192.0.2.99"]
        assert controller.active_host == "192.0.2.99"
        assert tv.prompts == 1  # reused key, no prompt
        assert KeyStore().get("192.0.2.99") == KeyStore().get(old)
    finally:
        await controller.stop()


async def test_does_not_follow_when_uuid_unknown_on_network(tv):
    old = "192.0.2.224"
    controller, _, _ = make(Settings(host=old), [], client_factory=unreachable_factory("none"))
    await controller.start()
    await wait_for(lambda: controller.connected)
    await controller.stop()

    found = [DiscoveredTv("192.0.2.50", "someone-elses-tv")]
    controller, hosts, calls = make(Settings(host=old), found, client_factory=unreachable_factory(old))
    await controller.start()
    try:
        await wait_for(lambda: calls)
        await asyncio.sleep(0.2)
        assert not controller.connected
        assert hosts == []
        assert tv.prompts == 1
    finally:
        await controller.stop()


async def test_auto_discover_off(tv):
    controller, hosts, calls = make(
        Settings(host="127.0.0.1", loopback_fallback=False, auto_discover=False),
        [DiscoveredTv("192.0.2.224", tv.uuid)],
        client_factory=unreachable_factory("127.0.0.1"),
    )
    await controller.start()
    try:
        await asyncio.sleep(0.3)
        assert calls == []
    finally:
        await controller.stop()


async def test_find_tv_pick_reuses_key_by_uuid(tv):
    # Paired at one address; user later picks the same TV (new IP) in Find TV.
    controller, _, _ = make(Settings(host="192.0.2.224"), [], client_factory=unreachable_factory("none"))
    await controller.start()
    await wait_for(lambda: controller.connected)
    await controller.stop()

    discovery.remember([DiscoveredTv("192.0.2.99", tv.uuid)])
    controller, _, _ = make(Settings(host="192.0.2.99"), [], client_factory=unreachable_factory("none"))
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        assert tv.prompts == 1
    finally:
        await controller.stop()


async def test_repair_forgets_uuid_key_too(tv):
    controller, _, _ = make(Settings(host="192.0.2.224"), [], client_factory=unreachable_factory("none"))
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        await controller.repair()
        await wait_for(lambda: controller.connected and tv.prompts == 2)
    finally:
        await controller.stop()


def test_ui_find_tv_sets_host(monkeypatch):
    import xbmcgui

    from resources.lib import ui

    async def fake_discover():
        return [DiscoveredTv("192.0.2.224", "u1", "[LG] webOS TV A"),
                DiscoveredTv("192.0.2.225", "u2", "[LG] webOS TV B")]

    monkeypatch.setattr(discovery, "discover", fake_discover)
    xbmcgui.select_answers.append(1)
    ui.find_tv()
    assert xbmcaddon.settings["host"] == "192.0.2.225"
    assert discovery.hinted_uuid("192.0.2.225") == "u2"
    assert xbmcgui.selects[-1][1] == ["[LG] webOS TV A - 192.0.2.224", "[LG] webOS TV B - 192.0.2.225"]

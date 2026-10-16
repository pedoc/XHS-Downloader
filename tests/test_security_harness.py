import asyncio
import json
import socket
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from curl_cffi import AsyncCurl, Curl
from curl_cffi import requests as curl_requests

OFFLINE_URL = "https://example.invalid/offline-test"
BLOCKED_NETWORK = "Unmocked network access"


@pytest.mark.integration
@pytest.mark.parametrize(
    "cookie",
    ["", "security_test=offline-only", "security_test=中文; quoted='\""],
)
def test_plaintext_settings_round_trip_uses_temporary_volume(
    source_modules: SimpleNamespace,
    tmp_volume: Path,
    cookie: str,
) -> None:
    settings = source_modules.settings.Settings(root=tmp_volume)
    values = deepcopy(settings.run()) | {"cookie": cookie}

    settings.update(values)
    restored = source_modules.settings.Settings(root=tmp_volume).run()

    assert settings.path == tmp_volume / "settings.json"
    assert restored == values
    with settings.path.open(encoding=settings.encode) as file:
        assert json.load(file)["cookie"] == cookie


@pytest.mark.unit
def test_source_volume_bindings_are_isolated(
    source_modules: SimpleNamespace,
    tmp_volume: Path,
) -> None:
    assert source_modules.static.VOLUME == tmp_volume
    assert source_modules.app.VOLUME == tmp_volume
    assert source_modules.settings.VOLUME == tmp_volume


@pytest.mark.unit
def test_tui_settings_instance_uses_temporary_volume(tmp_volume: Path) -> None:
    from source.TUI.app import XHSDownloader

    assert XHSDownloader.SETTINGS.path == tmp_volume / "settings.json"


@pytest.mark.integration
def test_manager_factory_creates_only_temporary_directories(
    manager_factory: Callable[..., Any],
    tmp_volume: Path,
) -> None:
    manager = manager_factory()

    assert manager.root == tmp_volume
    assert manager.path == tmp_volume / "work"
    assert manager.folder == tmp_volume / "work" / "Download"
    assert manager.temp == tmp_volume / "Temp"
    assert manager.folder.is_dir()
    assert manager.temp.is_dir()
    manager.request_client.request.assert_not_awaited()
    manager.download_client.request.assert_not_awaited()


@pytest.mark.unit
def test_fake_manager_client_can_return_an_explicit_offline_response(
    manager_factory: Callable[..., Any],
) -> None:
    manager = manager_factory()
    expected = SimpleNamespace(status_code=200, text="offline response")
    manager.request_client.get = AsyncMock(return_value=expected)

    response = asyncio.run(manager.request_client.get(OFFLINE_URL))

    assert response is expected
    manager.request_client.get.assert_awaited_once_with(OFFLINE_URL)


@pytest.mark.unit
@pytest.mark.parametrize("entry_point", ["function", "session"])
def test_unmocked_synchronous_curl_requests_are_blocked(entry_point: str) -> None:
    with pytest.raises(AssertionError, match=BLOCKED_NETWORK):
        if entry_point == "function":
            curl_requests.get(OFFLINE_URL)
        else:
            with curl_requests.Session() as client:
                client.get(OFFLINE_URL)


@pytest.mark.unit
@pytest.mark.parametrize("entry_point", ["get", "stream"])
def test_unmocked_asynchronous_curl_requests_are_blocked(entry_point: str) -> None:
    async def request() -> None:
        async with curl_requests.AsyncSession(async_curl=Mock()) as client:
            with pytest.raises(AssertionError, match=BLOCKED_NETWORK):
                if entry_point == "get":
                    await client.get(OFFLINE_URL)
                else:
                    async with client.stream("GET", OFFLINE_URL):
                        pytest.fail("An unmocked stream must not be entered")

    asyncio.run(request())


@pytest.mark.unit
def test_unmocked_synchronous_httpx_requests_are_blocked() -> None:
    with httpx2.Client(trust_env=False) as client:
        with pytest.raises(AssertionError, match=BLOCKED_NETWORK):
            client.get(OFFLINE_URL)


@pytest.mark.unit
def test_unmocked_asynchronous_httpx_requests_are_blocked() -> None:
    async def request() -> None:
        async with httpx2.AsyncClient(trust_env=False) as client:
            with pytest.raises(AssertionError, match=BLOCKED_NETWORK):
                await client.get(OFFLINE_URL)

    asyncio.run(request())


@pytest.mark.unit
def test_unmocked_native_curl_perform_is_blocked() -> None:
    client = Curl()
    try:
        with pytest.raises(AssertionError, match=BLOCKED_NETWORK):
            client.perform()
    finally:
        client.close()


@pytest.mark.unit
def test_unmocked_native_async_curl_entry_point_is_blocked() -> None:
    with pytest.raises(AssertionError, match=BLOCKED_NETWORK):
        AsyncCurl.add_handle(None, None)


@pytest.mark.unit
def test_unmocked_dns_lookups_are_blocked() -> None:
    with pytest.raises(AssertionError, match=BLOCKED_NETWORK):
        socket.getaddrinfo("example.invalid", 443)

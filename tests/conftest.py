import socket
import sys
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from curl_cffi import AsyncCurl, Curl
from curl_cffi import requests as curl_requests

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REAL_VOLUME = REPOSITORY_ROOT / "Volume"


def deny_network(*args: Any, **kwargs: Any) -> NoReturn:
    raise AssertionError("Unmocked network access is forbidden in offline tests")


async def deny_async_network(*args: Any, **kwargs: Any) -> NoReturn:
    deny_network(*args, **kwargs)


@pytest.fixture(autouse=True)
def offline_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # Native libcurl does not use Python's socket/DNS functions.
    monkeypatch.setattr(Curl, "perform", deny_network)
    monkeypatch.setattr(AsyncCurl, "add_handle", deny_network)
    monkeypatch.setattr(curl_requests.Session, "request", deny_network)
    monkeypatch.setattr(curl_requests.AsyncSession, "request", deny_async_network)
    monkeypatch.setattr(httpx2.Client, "send", deny_network)
    monkeypatch.setattr(httpx2.AsyncClient, "send", deny_async_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)


@pytest.fixture
def tmp_volume(tmp_path: Path) -> Path:
    volume = tmp_path / "sandbox" / "Volume"
    volume.mkdir(parents=True)
    return volume


@pytest.fixture(autouse=True)
def source_modules(
    monkeypatch: pytest.MonkeyPatch,
    tmp_volume: Path,
    offline_network: None,
) -> SimpleNamespace:
    # Keep source imports inside this fixture: static.py creates Volume on import.
    original_mkdir = Path.mkdir

    def isolated_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path != REAL_VOLUME:
            original_mkdir(path, *args, **kwargs)

    with monkeypatch.context() as bootstrap:
        bootstrap.setattr(Path, "mkdir", isolated_mkdir)
        import_module("source")

    modules = SimpleNamespace(
        static=import_module("source.module.static"),
        settings=import_module("source.module.settings"),
        manager=import_module("source.module.manager"),
        app=import_module("source.application.app"),
        cleaner=import_module("source.expansion.cleaner"),
    )
    for name, module in tuple(sys.modules.items()):
        if name.startswith("source.") and hasattr(module, "VOLUME"):
            monkeypatch.setattr(module, "VOLUME", tmp_volume)
    settings_class = modules.settings.Settings
    monkeypatch.setattr(settings_class, "default", deepcopy(settings_class.default))
    monkeypatch.setattr(settings_class.__init__, "__defaults__", (tmp_volume,))
    tui_app = import_module("source.TUI.app")
    monkeypatch.setattr(tui_app.XHSDownloader, "SETTINGS", settings_class(tmp_volume))
    monkeypatch.setattr(modules.app.XHS, "_XHS__INSTANCE", None)
    return modules


class FakeSession:
    def __init__(self, **options: Any) -> None:
        self.options = options
        self.headers = curl_requests.Headers(options.get("headers"))
        self.cookies = curl_requests.Cookies(options.get("cookies"))
        self.proxies = {"all": options["proxy"]} if options.get("proxy") else {}
        self.request = AsyncMock(side_effect=deny_async_network)
        self.get = AsyncMock(side_effect=deny_async_network)
        self.head = AsyncMock(side_effect=deny_async_network)
        self.stream = Mock(side_effect=deny_network)
        self.close = AsyncMock()

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


@pytest.fixture
def manager_factory(
    monkeypatch: pytest.MonkeyPatch,
    source_modules: SimpleNamespace,
    tmp_volume: Path,
) -> Callable[..., Any]:
    monkeypatch.setattr(source_modules.manager, "AsyncSession", FakeSession)
    excluded_settings = {
        "mapping_data",
        "work_path",
        "folder_name",
        "max_retry",
        "language",
        "disclaimer_accepted",
    }

    def create_manager(**overrides: Any) -> Any:
        defaults = deepcopy(source_modules.settings.Settings.default)
        parameters = {
            key: value for key, value in defaults.items() if key not in excluded_settings
        } | {
            "root": tmp_volume,
            "path": str(tmp_volume / "work"),
            "folder": defaults["folder_name"],
            "retry": 0,
            "proxy": None,
            "script_server": False,
            "cleaner": source_modules.cleaner.Cleaner(),
            "print_object": Mock(return_value=Mock()),
        }
        return source_modules.manager.Manager(**(parameters | overrides))

    return create_manager


@pytest.fixture
def html_context(
    manager_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    module = import_module("source.application.request")
    manager = manager_factory(cookie="web_session=offline-session")

    async def content(size: int | None = None) -> AsyncIterator[bytes]:
        yield response.text.encode(response.encoding)

    response = SimpleNamespace(
        text="offline note html", url="https://www.xiaohongshu.com/explore/offline-note",
        status_code=200, headers=curl_requests.Headers(), raise_for_status=Mock(),
        atext=AsyncMock(return_value="offline note html"),
        aiter_content=content, encoding="utf-8",
        quit_now=None, astream_task=None, aclose=AsyncMock(),
    )
    manager.request_client.get = AsyncMock(return_value=response)
    manager.public_client.get = AsyncMock(return_value=response)
    sync_get = Mock(return_value=response)
    monkeypatch.setattr(module, "get", sync_get, raising=False)
    monkeypatch.setattr(module, "sleep_time", AsyncMock())

    def create_client(**options: Any) -> FakeSession:
        client = FakeSession(**options)
        client.get = AsyncMock(return_value=response, side_effect=cookie_factory.get_error)
        cookie_factory.instances = (*cookie_factory.instances, client)
        return client

    cookie_factory = Mock(side_effect=create_client)
    cookie_factory.instances = ()
    cookie_factory.get_error = None
    monkeypatch.setattr(module, "AsyncSession", cookie_factory, raising=False)
    network = import_module("source.module.network")
    monkeypatch.setattr(network, "AsyncSession", cookie_factory)
    monkeypatch.setattr(network, "public_addresses", AsyncMock(return_value=("8.8.8.8",)))
    return SimpleNamespace(
        module=module, html=module.Html(manager), manager=manager, response=response,
        public_client=manager.public_client, sync_get=sync_get,
        cookie_factory=cookie_factory,
    )

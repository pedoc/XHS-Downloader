import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def mcp_startup(
    source_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    tools: dict[str, Callable[..., Any]] = {}

    def register(*, name: str, **options: Any) -> Callable[..., Any]:
        def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
            tools[name] = function
            return function

        return decorate

    server = SimpleNamespace(tool=register, run_async=AsyncMock())
    monkeypatch.setattr(source_modules.app, "FastMCP", Mock(return_value=server))
    core = object.__new__(source_modules.app.XHS)
    return SimpleNamespace(core=core, server=server, tools=tools)


@pytest.mark.unit
def test_mcp_startup_does_not_trigger_banner_version_checks(
    mcp_startup: SimpleNamespace,
) -> None:
    asyncio.run(mcp_startup.core.run_mcp_server())

    mcp_startup.server.run_async.assert_awaited_once()
    assert mcp_startup.server.run_async.await_args.kwargs["show_banner"] is False


@pytest.mark.unit
def test_disabling_the_banner_preserves_tools_and_explicit_http_binding(
    mcp_startup: SimpleNamespace,
) -> None:
    asyncio.run(mcp_startup.core.run_mcp_server(host="127.0.0.1", port=8123))

    assert set(mcp_startup.tools) == {"get_detail_data", "download_detail"}
    options = mcp_startup.server.run_async.await_args.kwargs
    assert options["transport"] == "streamable-http"
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 8123

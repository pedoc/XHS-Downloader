import asyncio
from collections.abc import Callable
from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.unit
@pytest.mark.parametrize("proxy", [
    "http://127.0.0.1:7890", "https://offline.invalid:8443",
    "socks5://127.0.0.1:1080", "socks5h://[::1]:1080",
    "http://offline-user:offline-password@127.0.0.1:7890",
])
def test_configuring_a_proxy_does_not_probe_the_network(
    manager_factory: Callable[..., Any], source_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch, proxy: str,
) -> None:
    probe = Mock(side_effect=AssertionError("Proxy configuration must not send requests"))
    monkeypatch.setattr(source_modules.manager, "get", probe, raising=False)

    manager = manager_factory(proxy=proxy)

    assert manager.proxy == proxy
    assert manager.request_client.options["proxy"] == proxy
    probe.assert_not_called()
    assert "offline-password" not in str(manager.proxy_tip)


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, ""])
def test_empty_proxy_configuration_means_direct_connection(value: str | None) -> None:
    module = import_module("source.module.proxy")
    assert module.validate_proxy(value) is None


@pytest.mark.unit
def test_local_proxy_shorthand_is_normalized_without_connecting() -> None:
    module = import_module("source.module.proxy")
    assert module.validate_proxy("127.0.0.1:7890") == "http://127.0.0.1:7890"


@pytest.mark.unit
@pytest.mark.parametrize("value", [
    True, 7890, {}, "http://", "file:///offline", "socks://offline.invalid:1080",
    "https://offline.invalid:0",
    "http://offline.invalid:99999", "http://offline.invalid/path",
    "http://offline.invalid?value=1", "http://offline.invalid#fragment",
    "http://offline.invalid\n", "http://offline.invalid\\other",
])
def test_invalid_proxy_configuration_is_rejected_instead_of_silently_going_direct(
    value: Any,
) -> None:
    module = import_module("source.module.proxy")
    with pytest.raises(ValueError):
        module.validate_proxy(value)


@pytest.mark.unit
def test_proxy_parse_errors_do_not_copy_credentials_into_tracebacks() -> None:
    import traceback

    module = import_module("source.module.proxy")
    marker = "offline-private-proxy-password"
    value = f"http://offline-user:{marker}@offline.invalid：7890"
    try:
        module.validate_proxy(value)
    except ValueError as error:
        assert marker not in str(error)
        assert marker not in traceback.format_exc()
    else:
        pytest.fail("Malformed proxy authority must be rejected")


@pytest.mark.integration
def test_gui_rejects_an_invalid_proxy_before_persisting_settings(
    source_modules: SimpleNamespace,
) -> None:
    backend_module = import_module("source.GUI.backend")
    backend = backend_module.GuiBackend()
    backend.settings = deepcopy(source_modules.settings.Settings.default)
    update = Mock()
    backend.settings_manager = SimpleNamespace(update=update)
    backend._close_xhs = AsyncMock()
    backend._create_xhs = AsyncMock()

    result = asyncio.run(backend.save_settings({"proxy": "file:///offline"}))

    assert result["ok"] is False
    update.assert_not_called()
    backend._close_xhs.assert_not_awaited()
    backend._create_xhs.assert_not_awaited()

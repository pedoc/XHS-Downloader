import asyncio
from collections.abc import Callable
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

NOTE_URL = "https://www.xiaohongshu.com/explore/offline-note"


@pytest.fixture
def request_context(
    manager_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    module = import_module("source.application.request")
    manager = manager_factory()
    response = SimpleNamespace(text="offline html", url=NOTE_URL, raise_for_status=Mock())
    manager.request_client.get = AsyncMock(return_value=response)
    manager.public_client.get = AsyncMock(return_value=response)
    sync_get = Mock(return_value=response)
    monkeypatch.setattr(module, "get", sync_get)
    monkeypatch.setattr(module, "sleep_time", AsyncMock())
    return SimpleNamespace(
        module=module,
        manager=manager,
        html=module.Html(manager),
        sync_get=sync_get,
    )


@pytest.mark.unit
@pytest.mark.parametrize("name", ["request_client", "public_client", "download_client"])
def test_all_http_clients_verify_tls(
    manager_factory: Callable[..., Any], name: str
) -> None:
    client = getattr(manager_factory(), name)

    assert client.options.get("verify", True) is True


@pytest.mark.unit
def test_explicit_cookie_request_verifies_tls(request_context: SimpleNamespace) -> None:
    result = asyncio.run(
        request_context.html.request_url(NOTE_URL, cookie="web_session=offline-only")
    )

    assert result == "offline html"
    assert request_context.sync_get.call_args.kwargs["verify"] is True


@pytest.mark.unit
@pytest.mark.parametrize("purpose", ["html", "short", "update"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"verify": False},
        {"allow_redirects": True},
        {"cookies": {"web_session": "offline-only"}},
        {"proxies": {"all": "http://untrusted.invalid"}},
        {"curl_options": {64: 0}},
    ],
)
def test_request_options_cannot_bypass_fixed_security_settings(
    request_context: SimpleNamespace,
    purpose: str,
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(TypeError):
        asyncio.run(request_context.html.request_url(NOTE_URL, purpose=purpose, **overrides))

    request_context.manager.request_client.get.assert_not_awaited()
    request_context.manager.public_client.get.assert_not_awaited()
    request_context.sync_get.assert_not_called()


@pytest.mark.unit
def test_public_request_still_accepts_an_explicit_timeout(
    request_context: SimpleNamespace,
) -> None:
    asyncio.run(request_context.html.request_url(NOTE_URL, purpose="update", timeout=8))

    assert request_context.manager.public_client.get.await_args.kwargs["timeout"] == 8


@pytest.mark.unit
def test_certificate_failure_never_uses_an_insecure_fallback(
    request_context: SimpleNamespace,
) -> None:
    request_context.manager.request_client.get.side_effect = (
        request_context.module.RequestException("offline certificate verification failed")
    )

    result = asyncio.run(request_context.html.request_url(NOTE_URL))

    assert result == ""
    request_context.manager.request_client.get.assert_awaited_once()
    assert request_context.manager.request_client.get.await_args.args[0] == NOTE_URL
    assert request_context.manager.request_client.options.get("verify", True) is True
    request_context.sync_get.assert_not_called()

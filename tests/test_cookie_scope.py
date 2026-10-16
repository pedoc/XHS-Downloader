import asyncio
from collections.abc import Callable
from importlib import import_module
from inspect import unwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from curl_cffi.requests import Cookies, Headers, Request

XHS_HOST = "www.xiaohongshu.com"
REDNOTE_HOST = "www.rednote.com"
LOGIN_COOKIE = "web_session=offline-session"
PREFIXED_COOKIE = (
    f"{LOGIN_COOKIE}; __Host-offline=host-only; __Secure-offline=secure-only"
)
COOKIE_VALUES = {
    "web_session": "offline-session",
    "__Host-offline": "host-only",
    "__Secure-offline": "secure-only",
}
NOTE_URL = f"https://{XHS_HOST}/explore/offline-note?xsec_token=offline"
SHORT_URL = "https://xhslink.com/a/offline"
RELEASE_URL = "https://github.com/JoeanAmier/XHS-Downloader/releases/tag/v9.9"


def assert_host_only_secure_cookies(
    cookies: Cookies,
    host: str,
    values: dict[str, str],
) -> None:
    assert isinstance(cookies, Cookies)
    entries = tuple(cookies.jar)
    assert {cookie.name: cookie.value for cookie in entries} == values
    for cookie in entries:
        assert cookie.domain == host
        assert cookie.domain_specified is False
        assert cookie.domain_initial_dot is False
        assert cookie.path == "/"
        assert cookie.secure is True


def assert_public_lookup(lookup: AsyncMock, url: str, purpose: str) -> None:
    lookup.assert_awaited_once()
    args, kwargs = lookup.await_args
    requested_url = args[0] if args else kwargs.get("url")
    content = args[1] if len(args) > 1 else kwargs.get("content", True)
    assert requested_url == url
    assert content is False
    assert kwargs.get("purpose") == purpose


@pytest.mark.unit
@pytest.mark.parametrize("host", [XHS_HOST, REDNOTE_HOST])
def test_scoped_cookies_preserve_values_in_secure_host_only_entries(host: str) -> None:
    cookie_module = import_module("source.module.cookies")

    cookies = cookie_module.scoped_cookies(PREFIXED_COOKIE, host=host)

    assert_host_only_secure_cookies(cookies, host, COOKIE_VALUES)


@pytest.mark.unit
@pytest.mark.parametrize(
    "host",
    [
        "github.com",
        "www.xiaohongshu.com.attacker.invalid",
        "www.rednote.com.attacker.invalid",
    ],
)
def test_scoped_cookies_reject_unknown_hosts_and_lookalike_suffixes(host: str) -> None:
    cookie_module = import_module("source.module.cookies")

    with pytest.raises(ValueError, match="known note sites"):
        cookie_module.scoped_cookies(LOGIN_COOKIE, host=host)


@pytest.mark.unit
def test_empty_cookie_string_creates_an_empty_cookie_jar() -> None:
    cookie_module = import_module("source.module.cookies")

    cookies = cookie_module.scoped_cookies("")

    assert isinstance(cookies, Cookies)
    assert tuple(cookies.jar) == ()


@pytest.mark.unit
@pytest.mark.parametrize("host", [XHS_HOST, REDNOTE_HOST])
def test_curl_cookie_export_never_rebinds_login_cookies_to_the_request_host(
    host: str,
) -> None:
    cookie_module = import_module("source.module.cookies")
    cookies = cookie_module.scoped_cookies(PREFIXED_COOKIE, host=host)
    destinations = (
        NOTE_URL,
        f"https://{REDNOTE_HOST}/explore/offline-note",
        RELEASE_URL,
        SHORT_URL,
        "https://sns-img-qc.xhscdn.com/offline.jpg",
        "https://www.xiaohongshu.com.attacker.invalid/offline",
        "https://sub.www.xiaohongshu.com/offline",
    )

    for url in destinations:
        request = Request(url=url, headers=Headers(), method="GET")
        morsels = cookies.get_cookies_for_curl(request)

        assert {morsel.name: morsel.value for morsel in morsels} == COOKIE_VALUES
        assert all(morsel.hostname == host for morsel in morsels), url
        assert all(morsel.subdomains is False for morsel in morsels), url
        assert all(morsel.secure is True for morsel in morsels), url


@pytest.mark.integration
def test_manager_login_cookie_jar_is_scoped_only_to_the_xhs_host(
    manager_factory: Callable[..., Any],
) -> None:
    manager = manager_factory(cookie=LOGIN_COOKIE)

    assert_host_only_secure_cookies(
        manager.request_client.cookies,
        XHS_HOST,
        {"web_session": "offline-session"},
    )


@pytest.mark.integration
@pytest.mark.parametrize("client_name", ["public_client", "download_client"])
def test_public_and_download_clients_have_no_login_cookie_and_discard_responses(
    manager_factory: Callable[..., Any],
    client_name: str,
) -> None:
    manager = manager_factory(cookie=LOGIN_COOKIE)
    client = getattr(manager, client_name)

    assert tuple(client.cookies.jar) == ()
    assert not client.headers.get("cookie")
    assert not client.headers.get("authorization")
    assert client.options.get("discard_cookies") is True


@pytest.mark.integration
def test_manager_closes_the_public_client_alongside_its_other_clients(
    manager_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = manager_factory(cookie=LOGIN_COOKIE)
    public_client = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(manager, "public_client", public_client, raising=False)

    asyncio.run(manager.close())

    manager.request_client.close.assert_awaited_once()
    manager.download_client.close.assert_awaited_once()
    public_client.close.assert_awaited_once()


@pytest.mark.unit
def test_normal_note_html_keeps_using_the_authenticated_client(
    html_context: SimpleNamespace,
) -> None:
    result = asyncio.run(html_context.html.request_url(NOTE_URL))

    assert result == "offline note html"
    html_context.cookie_factory.assert_called_once()
    assert_host_only_secure_cookies(
        html_context.cookie_factory.call_args.kwargs["cookies"], XHS_HOST,
        {"web_session": "offline-session"},
    )
    html_context.manager.request_client.get.assert_not_awaited()
    html_context.sync_get.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("host", [XHS_HOST, REDNOTE_HOST])
def test_explicit_note_cookie_is_scoped_to_its_initial_host_without_changing_defaults(
    html_context: SimpleNamespace,
    host: str,
) -> None:
    url = f"https://{host}/explore/offline-note"

    result = asyncio.run(
        html_context.html.request_url(url, cookie="web_session=offline-explicit")
    )

    assert result == "offline note html"
    call = (
        html_context.sync_get.call_args
        or html_context.manager.request_client.get.await_args
        or html_context.cookie_factory.call_args
    )
    assert call is not None
    assert_host_only_secure_cookies(
        call.kwargs.get("cookies"),
        host,
        {"web_session": "offline-explicit"},
    )
    assert_host_only_secure_cookies(
        html_context.manager.request_client.cookies,
        XHS_HOST,
        {"web_session": "offline-session"},
    )


@pytest.mark.unit
@pytest.mark.parametrize("purpose,url", [("short", SHORT_URL), ("update", RELEASE_URL)])
@pytest.mark.parametrize("cookie", [None, LOGIN_COOKIE])
def test_public_lookups_ignore_business_cookies_and_use_the_public_client(
    html_context: SimpleNamespace,
    purpose: str,
    url: str,
    cookie: str | None,
) -> None:
    html_context.response.url = url

    result = asyncio.run(
        html_context.html.request_url(url, False, purpose=purpose, cookie=cookie)
    )

    assert result == url
    html_context.cookie_factory.assert_called_once()
    html_context.manager.request_client.get.assert_not_awaited()
    html_context.sync_get.assert_not_called()
    client = html_context.cookie_factory.instances[0]
    client.get.assert_awaited_once()
    assert client.get.await_args.args[0] == url
    assert tuple(client.cookies.jar) == ()
    assert client.options["discard_cookies"] is True
    headers = {key.lower(): value for key, value in client.headers.items()}
    assert "cookie" not in headers
    assert "authorization" not in headers
    if purpose == "update":
        assert "referer" not in headers


@pytest.mark.integration
def test_short_link_expansion_requests_the_public_short_purpose(
    source_modules: SimpleNamespace,
) -> None:
    lookup = AsyncMock(return_value=NOTE_URL)
    core = object.__new__(source_modules.app.XHS)
    core.html = SimpleNamespace(request_url=lookup)

    result = asyncio.run(core.extract_links(SHORT_URL))

    assert result == [NOTE_URL]
    assert_public_lookup(lookup, SHORT_URL, "short")


@pytest.mark.integration
@pytest.mark.parametrize("iteration", [1, 2], ids=["first-import", "cached-import"])
def test_gui_update_uses_a_public_lookup_instead_of_the_login_client_stream(
    tmp_volume: Path,
    iteration: int,
) -> None:
    backend_module = import_module("source.GUI.backend")
    lookup = AsyncMock(return_value=RELEASE_URL)
    html = SimpleNamespace(
        request_url=lookup,
        client=SimpleNamespace(
            stream=Mock(side_effect=AssertionError("Login stream must not check updates"))
        ),
    )
    backend = backend_module.GuiBackend()
    assert backend.settings_manager.root == tmp_volume, f"GUI import round {iteration}"
    backend.xhs = SimpleNamespace(html=html)

    result = asyncio.run(backend.check_update())

    assert_public_lookup(lookup, backend_module.RELEASES, "update")
    assert result["status"] == "ok"
    html.client.stream.assert_not_called()


@pytest.mark.integration
def test_tui_update_requests_the_public_update_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update_module = import_module("source.TUI.update")
    lookup = AsyncMock(return_value=RELEASE_URL)
    screen = update_module.Update(SimpleNamespace(html=SimpleNamespace(request_url=lookup)))
    dismiss = Mock()
    monkeypatch.setattr(screen, "dismiss", dismiss)

    asyncio.run(unwrap(update_module.Update.check_update)(screen))

    assert_public_lookup(lookup, update_module.RELEASES, "update")
    dismiss.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("fails", [False, True])
def test_update_lookup_does_not_retry_or_apply_business_throttling(
    html_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    fails: bool,
) -> None:
    request_module = import_module("source.application.request")
    html_context.html.retry = 5
    if fails:
        html_context.cookie_factory.get_error = request_module.RequestException(
            "offline timeout"
        )
    throttle = AsyncMock()
    monkeypatch.setattr(request_module, "sleep_time", throttle)

    result = asyncio.run(
        html_context.html.request_url(RELEASE_URL, False, purpose="update", timeout=8)
    )

    assert result == ("" if fails else str(html_context.response.url))
    html_context.cookie_factory.assert_called_once()
    html_context.cookie_factory.instances[0].get.assert_awaited_once()
    throttle.assert_not_awaited()

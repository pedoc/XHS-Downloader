import asyncio
from collections.abc import AsyncIterator, Callable
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

NOTE_URL = "https://www.xiaohongshu.com/explore/offline-note"


@pytest.mark.integration
@pytest.mark.parametrize("purpose,url", [
    ("html", NOTE_URL), ("short", "https://xhslink.com/a/offline"),
    ("update", "https://github.com/JoeanAmier/XHS-Downloader/releases/latest"),
])
def test_http_status_failures_keep_business_retry_throttling(
    html_context: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    purpose: str, url: str,
) -> None:
    html_context.html.retry = 2
    html_context.response.raise_for_status.side_effect = (
        html_context.module.RequestException("offline HTTP 403")
    )
    throttle = AsyncMock()
    monkeypatch.setattr(html_context.module, "sleep_time", throttle)

    result = asyncio.run(html_context.html.request_url(url, purpose=purpose))

    expected = 1 if purpose == "update" else 3
    assert result == ""
    assert html_context.cookie_factory.call_count == expected
    assert throttle.await_count == (0 if purpose == "update" else expected)


@pytest.mark.integration
@pytest.mark.parametrize("purpose,url", [
    ("html", NOTE_URL),
    ("update", "https://github.com/JoeanAmier/XHS-Downloader/releases/latest"),
])
def test_html_deadline_includes_dns_before_a_transport_is_created(
    html_context: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, purpose: str, url: str,
) -> None:
    network = import_module("source.module.network")

    async def scenario() -> None:
        cancelled = asyncio.Event()

        async def blocked_dns(host: str) -> tuple[str, ...]:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return ("8.8.8.8",)

        monkeypatch.setattr(network, "public_addresses", blocked_dns)
        result = await asyncio.wait_for(
            html_context.html.request_url(url, purpose=purpose, timeout=0.02), timeout=1,
        )
        assert result == ""
        assert cancelled.is_set()
        html_context.cookie_factory.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.integration
def test_metadata_requests_reject_non_vendor_targets_before_any_transport(
    html_context: SimpleNamespace,
) -> None:
    network = import_module("source.module.network")
    with pytest.raises(network.UnsafeURLError):
        asyncio.run(html_context.html.request_url("https://untrusted.invalid/explore/note"))
    html_context.manager.request_client.get.assert_not_awaited()
    html_context.cookie_factory.assert_not_called()


@pytest.mark.integration
def test_initial_http_metadata_url_is_upgraded_before_transport(
    html_context: SimpleNamespace,
) -> None:
    asyncio.run(html_context.html.request_url(NOTE_URL.replace("https:", "http:")))

    html_context.cookie_factory.assert_called_once()
    client = html_context.cookie_factory.instances[0]
    assert client.get.await_args.args[0] == NOTE_URL
    assert client.options["verify"] is True
    assert client.options["allow_redirects"] is False


@pytest.mark.integration
def test_api_callers_cannot_override_the_locally_configured_proxy(
    source_modules: SimpleNamespace, manager_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI, HTTPException

    core = object.__new__(source_modules.app.XHS)
    core.manager = manager_factory()
    core.extract_links = AsyncMock(return_value=[NOTE_URL])
    monkeypatch.setattr(core, "_XHS__deal_extract", AsyncMock(return_value={}))
    app = FastAPI()
    core.setup_routes(app)
    endpoint = next(route.endpoint for route in app.routes if route.path == "/xhs/detail")
    params = source_modules.app.ExtractParams(
        url=NOTE_URL, proxy="http://untrusted.invalid:8080",
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint(params))

    assert error.value.status_code == 400
    core.extract_links.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.parametrize("requested", [None, "127.0.0.1:7890", "http://127.0.0.1:7890"])
def test_api_keeps_the_explicit_local_proxy_and_reports_policy_failures(
    source_modules: SimpleNamespace, manager_factory: Callable[..., Any], requested: str | None,
) -> None:
    from fastapi import FastAPI

    core = object.__new__(source_modules.app.XHS)
    core.manager = manager_factory(proxy="http://127.0.0.1:7890")
    core.extract_links = AsyncMock(return_value=[NOTE_URL])
    deal = AsyncMock(return_value={"message": "offline rejected target", "error": True})
    core._XHS__deal_extract = deal
    app = FastAPI()
    core.setup_routes(app)
    endpoint = next(route.endpoint for route in app.routes if route.path == "/xhs/detail")
    params = source_modules.app.ExtractParams(url=NOTE_URL, proxy=requested)

    result = asyncio.run(endpoint(params))

    assert core.extract_links.await_args.kwargs["proxy"] == core.manager.proxy
    assert deal.await_args.kwargs["proxy"] == core.manager.proxy
    assert result.message == source_modules.app._("获取小红书作品数据失败")
    assert result.data["error"] is True


@pytest.mark.integration
def test_download_rejects_invalid_targets_before_opening_a_transport(
    source_modules: SimpleNamespace, manager_factory: Callable[..., Any],
) -> None:
    module = import_module("source.application.download")
    manager = manager_factory()
    manager.download_client.stream = Mock(side_effect=AssertionError("Unsafe transport"))
    downloader = module.Download(manager)
    network = import_module("source.module.network")

    with pytest.raises(network.UnsafeURLError):
        asyncio.run(downloader.run(
            ["https://untrusted.invalid/offline.mp4"], [None], None,
            manager.folder, "offline", module._("视频"), None,
        ))

    manager.download_client.stream.assert_not_called()
    assert tuple(manager.temp.iterdir()) == ()


@pytest.fixture
def media_context(
    manager_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch,
    proxy_download: bool,
) -> SimpleNamespace:
    from curl_cffi import CurlOpt
    from curl_cffi.requests import Headers

    network = import_module("source.module.network")
    module = import_module("source.application.download")
    manager = manager_factory(
        cookie="web_session=offline-owner", proxy="http://127.0.0.1:7890",
        proxy_download=proxy_download,
    )
    payload = b"\x00\x00\x00\x18ftypisom" + b"offline media"

    async def chunks(size: int) -> AsyncIterator[bytes]:
        yield payload

    response = SimpleNamespace(
        status_code=200, headers=Headers({"content-length": str(len(payload))}),
        raise_for_status=Mock(), aiter_content=chunks, quit_now=None,
        astream_task=None, aclose=AsyncMock(),
    )
    sessions = []

    def factory(**options: Any) -> Any:
        client = type(manager.download_client)(**options)
        client.get = AsyncMock(return_value=response)
        sessions.append(client)
        return client

    dns = AsyncMock(return_value=("8.8.8.8",))
    monkeypatch.setattr(network, "AsyncSession", factory)
    monkeypatch.setattr(network, "public_addresses", dns)
    return SimpleNamespace(
        module=module, manager=manager, response=response, payload=payload,
        sessions=sessions, dns=dns, curl_opt=CurlOpt,
    )


@pytest.mark.integration
@pytest.mark.parametrize("proxy_download", [False, True])
def test_media_transport_keeps_range_scope_and_explicit_proxy_policy(
    media_context: SimpleNamespace, proxy_download: bool,
) -> None:
    module, manager = media_context.module, media_context.manager
    sessions, dns, payload = media_context.sessions, media_context.dns, media_context.payload
    curl_opt = media_context.curl_opt
    result = asyncio.run(module.Download(manager).run(
        ["http://sns-video-bd.xhscdn.com/offline.mp4"], [None], None,
        manager.folder, "offline", module._("视频"), None,
    ))

    assert result == [True]
    assert (manager.folder / "offline.mp4").read_bytes() == payload
    assert tuple(manager.temp.iterdir()) == ()
    assert len(sessions) == 1
    client = sessions[0]
    assert client.get.await_args.args[0].startswith("https://")
    assert tuple(client.cookies.jar) == ()
    assert client.headers["Range"] == "bytes=0-"
    if proxy_download:
        assert client.options["proxy"] == manager.proxy
        assert client.options["curl_options"][curl_opt.NOPROXY] == ""
        dns.assert_not_awaited()
    else:
        assert client.options["curl_options"][curl_opt.PROXY] == ""
        assert curl_opt.RESOLVE in client.options["curl_options"]
        dns.assert_awaited_once()
    media_context.response.aclose.assert_awaited_once()
    client.close.assert_awaited_once()

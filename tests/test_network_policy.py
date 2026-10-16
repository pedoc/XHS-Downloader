import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import nullcontext
from functools import partial
from importlib import import_module
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession, Cookies, Headers, Response

HTML = "https://www.xiaohongshu.com/"
REDNOTE = "https://www.rednote.com/explore/1"
SHORT = "https://xhslink.com/abc"
MEDIA = "https://sns-img.xhscdn.com/image"
UPDATE = "https://github.com/JoeanAmier/XHS-Downloader/releases/latest"
ADDRESSES = ("93.184.216.34", "2606:4700:4700::1111")
pytestmark = pytest.mark.unit


class FakeResponse:
    def __init__(self, status: int = 200, location: str | None = None) -> None:
        self.status_code = status
        self.headers = Headers({"Location": location} if location is not None else {})
        self.closed, self.quit_now, self.astream_task = False, None, None

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self.closed = True

    async def atext(self) -> str:
        assert not self.closed
        return "offline body"

    async def aiter_content(self) -> AsyncIterator[bytes]:
        yield b"offline body"


class FakeSession:
    def __init__(self, state: SimpleNamespace, **options: Any) -> None:
        assert all(session.closed for session in state.sessions)
        self.state, self.options, self.closed = state, options, False
        self.cookies = Cookies(options.get("cookies"))
        state.sessions.append(self)

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *_: Any) -> None:
        assert not isinstance(getattr(self, "response", None), Response) or self.response.astream_task.done()
        self.closed = True

    async def get(self, url: str, **options: Any) -> FakeResponse | Response:
        self.state.calls.append(("GET", url, options))
        self.response = response = self.state.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture
def network() -> ModuleType:
    return import_module("source.module.network")


@pytest.fixture
def policy(network: ModuleType, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        network=network, responses=[FakeResponse()], sessions=[], calls=[],
        manager=SimpleNamespace(impersonate="chrome", timeout=17, proxy=None,
                                request_client=SimpleNamespace(cookies=Cookies())),
        dns=AsyncMock(return_value=ADDRESSES),
    )
    monkeypatch.setattr(network, "AsyncSession", partial(FakeSession, state))
    monkeypatch.setattr(network, "public_addresses", state.dns)
    return state


def read_stream(state: SimpleNamespace, url: str = HTML,
                purpose: str = "html", **options: Any) -> FakeResponse:
    async def consume() -> FakeResponse:
        async with state.network.checked_stream(state.manager, url, purpose, **options) as r:
            assert not state.sessions[-1].closed
            assert await r.atext() == "offline body"
            assert [chunk async for chunk in r.aiter_content()] == [b"offline body"]
            return r
    return asyncio.run(consume())


def cookie_jar(host: str = "www.xiaohongshu.com") -> Cookies:
    cookies = Cookies()
    cookies.set("web_session", "offline-only", domain=host, secure=True)
    return cookies


@pytest.mark.parametrize("url,purpose,expected", [
    (HTML, "html", HTML), (REDNOTE, "html", REDNOTE),
    ("https://WWW.XIAOHONGSHU.COM:443/a", "html", HTML + "a"),
    ("http://www.xiaohongshu.com/a?token=a%2Fb%3D&x=1+2", "html",
     HTML + "a?token=a%2Fb%3D&x=1+2"),
    ("www.rednote.com/a?xsec_token=a%2B%2F%3D", "html",
     "https://www.rednote.com/a?xsec_token=a%2B%2F%3D"),
    ("//xhslink.cn/a", "short", "https://xhslink.cn/a"),
    ("www.xiaohongshu.com:443/a?next=https://www.rednote.com/", "html",
     HTML + "a?next=https://www.rednote.com/"),
    ("//xhslink.com/a?next=https://www.rednote.com/", "short",
     "https://xhslink.com/a?next=https://www.rednote.com/"),
    (SHORT, "short", SHORT), (MEDIA, "media", MEDIA),
    ("https://xhscdn.com/a", "media", "https://xhscdn.com/a"),
    ("https://a.b.rednotecdn.com/a", "media", "https://a.b.rednotecdn.com/a"),
    ("http://ci.xiaohongshu.com/a?token=raw%252F", "media",
     "https://ci.xiaohongshu.com/a?token=raw%252F"),
    (UPDATE.replace("latest", "tag/v2.8"), "update", UPDATE.replace("latest", "tag/v2.8")),
])
def test_normalize_accepts_only_expected_vendor_urls(
    network: ModuleType, url: str, purpose: str, expected: str,
) -> None:
    assert network.normalize_url(url, purpose) == expected


@pytest.mark.parametrize("url,purpose", [
    ("https://www.xiaohongshu.com.evil.test/a", "html"),
    ("https://evilwww.xiaohongshu.com/a", "html"),
    ("https://www.xiaohongshu.com@127.0.0.1/a", "html"),
    ("https://user:pass@www.xiaohongshu.com/a", "html"),
    ("https://www.xiaohongshu.com\\@evil.test/a", "html"),
    ("https://www.xiaohongshu.com./a", "html"),
    ("https://www.xiaohongshu.com:/a", "html"),
    ("https://www.xiaohongshu.com:0443/a", "html"),
    ("https://www.xiaohongshu.com:444/a", "html"),
    ("http://www.xiaohongshu.com:80/a", "html"),
    ("ftp://www.xiaohongshu.com/a", "html"), ("file:///etc/passwd", "html"),
    ("https:////www.xiaohongshu.com/a", "html"),
    ("https://www.xiaohongshu.com%2f.evil.test/a", "html"),
    ("https://www.ｘiaohongshu.com/a", "html"),
    ("https://127.0.0.1/a", "html"), ("https://[::ffff:127.0.0.1]/a", "html"),
    ("\n" + HTML, "html"), (HTML + "a\tb", "html"), (HTML + "a b", "html"),
    (HTML + "\x00", "html"), (HTML + "\x7f", "html"), (HTML + "\x85", "html"),
    ("https://[not-ip]/", "html"), ("https://" + "a" * 64 + ".xhscdn.com/", "media"),
    ("https://notxhscdn.com/a", "media"), ("https://xhscdn.com.evil.test/a", "media"),
    ("https://a..xhscdn.com/a", "media"), ("https://-bad.xhscdn.com/a", "media"),
    ("https://www.xiaohongshu.com/a", "media"), (REDNOTE, "short"),
    (UPDATE.replace("JoeanAmier", "other"), "update"),
    (UPDATE.replace("latest", "latest/other"), "update"),
    (UPDATE.replace("latest", "tag/../../other"), "update"),
    (UPDATE.replace("latest", "tag/%2e%2e/%2e%2e/other"), "update"),
    (UPDATE.replace("latest", "tag/"), "update"), (HTML, "unknown"),
    ("", "html"), (None, "html"),
])
def test_normalize_rejects_unsafe_or_out_of_scope_urls(
    network: ModuleType, url: Any, purpose: str,
) -> None:
    with pytest.raises(network.UnsafeURLError):
        network.normalize_url(url, purpose)


@pytest.mark.parametrize("url", ["http://www.xiaohongshu.com/a", "www.xiaohongshu.com/a"])
def test_redirect_normalization_never_upgrades_insecure_targets(network: ModuleType, url: str) -> None:
    with pytest.raises(network.UnsafeURLError):
        network.normalize_url(url, "html", initial=False)


@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["100.64.0.1"],
    ["0.0.0.0"], ["224.0.0.1"], ["192.0.2.1"], ["::1"], ["fc00::1"],
    ["fe80::1"], ["fec0::1"], ["ff02::1"], ["2001:db8::1"],
    ["::ffff:127.0.0.1"], ["::ffff:10.0.0.1"], [ADDRESSES[0], "192.168.1.1"],
    [], ["not-an-address"], ["240.0.0.1"], ["255.255.255.255"], ["::"],
    ["2002:7f00:1::"], ["2001::1"], ["2606:4700:4700::1111%eth0"],
])
def test_dns_rejects_any_nonpublic_or_invalid_answer(
    network: ModuleType, monkeypatch: pytest.MonkeyPatch, addresses: list[str],
) -> None:
    async def resolve() -> None:
        answer = [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                   socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=answer))
        with pytest.raises(network.UnsafeURLError):
            await network.public_addresses("sns-img.xhscdn.com")
    asyncio.run(resolve())


@pytest.mark.parametrize("addresses", [ADDRESSES, ("::ffff:8.8.8.8",)])
def test_dns_pins_all_unique_public_answers_and_fails_closed(
    network: ModuleType, monkeypatch: pytest.MonkeyPatch, addresses: tuple[str, ...],
) -> None:
    async def resolve() -> None:
        answer = [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                   socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in (*addresses, addresses[0])]
        resolver = AsyncMock(return_value=answer)
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
        assert await network.public_addresses("sns-img.xhscdn.com") == addresses
        resolver.assert_awaited_once_with("sns-img.xhscdn.com", 443, type=socket.SOCK_STREAM)
        resolver.side_effect = socket.gaierror("offline failure")
        with pytest.raises(network.UnsafeURLError):
            await network.public_addresses("sns-img.xhscdn.com")
    asyncio.run(resolve())


def test_each_hop_uses_a_closed_isolated_pinned_session(
    policy: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-use.invalid:8080")
    policy.responses = [FakeResponse(302, "/next"), FakeResponse()]
    assert read_stream(policy).closed
    assert len(policy.sessions) == policy.dns.await_count == 2
    for session in policy.sessions:
        options = session.options
        assert session.closed
        assert options["impersonate"] == "chrome" and options["timeout"] == 17
        assert options["verify"] is True and options["trust_env"] is False
        assert options["allow_redirects"] is False
        assert options["curl_options"][CurlOpt.RESOLVE] == [
            "www.xiaohongshu.com:443:93.184.216.34,[2606:4700:4700::1111]"]
        assert options["curl_options"][CurlOpt.PROXY] == ""
        assert options["curl_options"][CurlOpt.PROTOCOLS_STR] == "https"
        assert options["curl_options"][CurlOpt.REDIR_PROTOCOLS_STR] == "https"
    assert [call[1] for call in policy.calls] == [HTML, HTML + "next"]
    assert all(call[2]["allow_redirects"] is False for call in policy.calls)
    assert all("curl_options" not in call[2] for call in policy.calls)
    assert policy.sessions[0].options["curl_options"] is not policy.sessions[1].options["curl_options"]


@pytest.mark.parametrize("argument,configured,expected", [
    (None, "http://trusted-proxy.invalid:8080", "http://trusted-proxy.invalid:8080"),
    ("socks5h://trusted.invalid:1080", None, "socks5h://trusted.invalid:1080"),
    ("", "http://ignored.invalid:8080", None),
])
def test_explicit_proxy_is_a_trust_boundary_not_locally_dns_pinned(
    policy: SimpleNamespace, argument: str | None, configured: str | None, expected: str | None,
) -> None:
    policy.manager.proxy = configured
    read_stream(policy, proxy=argument, timeout=3)
    options = policy.sessions[0].options
    assert options["proxy"] == expected and options["timeout"] == 3
    if expected:
        policy.dns.assert_not_awaited()
        assert CurlOpt.RESOLVE not in options["curl_options"]
        assert options["curl_options"][CurlOpt.NOPROXY] == ""
    else:
        policy.dns.assert_awaited_once()
        assert options["curl_options"][CurlOpt.PROXY] == ""


@pytest.mark.parametrize("purpose,url,location,target", [
    ("html", HTML, "next?token=a%2Fb", HTML + "next?token=a%2Fb"),
    ("short", SHORT, REDNOTE, REDNOTE),
    ("media", MEDIA, "//a.rednotecdn.com/video", "https://a.rednotecdn.com/video"),
])
def test_allowed_redirects_preserve_relative_urls_and_range(
    policy: SimpleNamespace, purpose: str, url: str, location: str, target: str,
) -> None:
    policy.responses = [FakeResponse(307, location), FakeResponse()]
    read_stream(policy, url, purpose, headers={"Range": "bytes=123-"})
    assert policy.calls[1][1] == target
    assert all(Headers(s.options["headers"])["Range"] == "bytes=123-" for s in policy.sessions)


@pytest.mark.parametrize("location", [
    None, "", " ", "https://[bad", "\n" + HTML, "http://www.xiaohongshu.com/next",
    "https://127.0.0.1/", "https://evil.test/", "https://www.xiaohongshu.com\\@evil.test/",
    "https://user@www.xiaohongshu.com/", "#fragment",
])
def test_redirects_fail_before_opening_an_unsafe_or_looped_next_hop(
    policy: SimpleNamespace, location: str | None,
) -> None:
    response = FakeResponse(302, location)
    policy.responses = [response]
    with pytest.raises(policy.network.UnsafeURLError):
        read_stream(policy)
    assert len(policy.sessions) == 1 and policy.sessions[0].closed and response.closed


@pytest.mark.parametrize("redirects", [5, 6])
def test_redirect_limit_allows_five_hops_but_not_six(policy: SimpleNamespace, redirects: int) -> None:
    policy.responses = [FakeResponse(302, f"/hop-{i}") for i in range(1, redirects + 1)] + [FakeResponse()]
    if redirects == 5:
        read_stream(policy)
    else:
        with pytest.raises(policy.network.UnsafeURLError):
            read_stream(policy)
    assert len(policy.sessions) == 6 and all(s.closed for s in policy.sessions)


@pytest.mark.parametrize("purpose,url", [("short", SHORT), ("media", MEDIA), ("update", UPDATE)])
def test_public_requests_always_discard_cookies_and_strip_raw_credentials(
    policy: SimpleNamespace, purpose: str, url: str,
) -> None:
    policy.manager.request_client.cookies = cookie_jar()
    read_stream(policy, url, purpose, cookies=Cookies({"unscoped": "ignore"}), headers={
        "Cookie": "leak=1", "Authorization": "secret", "Proxy-Authorization": "secret", "Host": "evil.test"})
    session = policy.sessions[0]
    assert not session.cookies
    assert not Headers(session.options["headers"])
    assert session.options["discard_cookies"] is True
    assert policy.calls[0][2]["discard_cookies"] is True


def test_html_cookie_scope_never_expands_or_accepts_response_cookies(policy: SimpleNamespace) -> None:
    original = cookie_jar()
    policy.manager.request_client.cookies = original
    response = FakeResponse(302, REDNOTE)
    response.headers["Set-Cookie"] = "new_login=bad; Domain=.rednote.com; Secure"
    policy.responses = [response, FakeResponse()]
    read_stream(policy, headers={"Cookie": "bad=1", "Authorization": "bad", "X-Token": "bad", "Referer": HTML})
    first, second = policy.sessions
    assert len(first.cookies) == 1 and not second.cookies
    assert all(c.domain == "www.xiaohongshu.com" and not c.domain_specified and c.secure for c in first.cookies.jar)
    assert Headers(first.options["headers"]).get("Cookie") is None
    assert not Headers(second.options["headers"])
    assert len(original) == 1 and next(iter(original.jar)).domain_specified
    assert all(s.options["discard_cookies"] for s in policy.sessions)


@pytest.mark.parametrize("host", [None, "www.xiaohongshu.com", "www.rednote.com"])
def test_cookie_sources_only_match_the_initial_rednote_site(policy: SimpleNamespace, host: str | None) -> None:
    policy.manager.request_client.cookies = cookie_jar()
    cookies = cookie_jar(host) if host else None
    if cookies is not None:
        cookies.set("other", "ignore", domain="www.xiaohongshu.com", secure=True)
    read_stream(policy, REDNOTE, cookies=cookies)
    assert len(policy.sessions[0].cookies) == (1 if host == "www.rednote.com" else 0)


@pytest.mark.parametrize("cookies", [{"domainless": "bad"}, Cookies({"domainless": "bad"})])
def test_domainless_html_cookie_is_rejected_before_network(policy: SimpleNamespace, cookies: Any) -> None:
    with pytest.raises(policy.network.UnsafeURLError):
        read_stream(policy, cookies=cookies)
    policy.dns.assert_not_awaited()
    assert not policy.sessions


@pytest.mark.parametrize("where", ["consumer", "transport"])
@pytest.mark.parametrize("error", [RuntimeError("offline failure"), asyncio.CancelledError()])
def test_session_closes_on_consumer_or_transport_errors(
    policy: SimpleNamespace, where: str, error: BaseException,
) -> None:
    async def fail() -> None:
        if where == "transport":
            policy.responses = [error]
        async with policy.network.checked_stream(policy.manager, HTML, "html"):
            raise error
    with pytest.raises(type(error)):
        asyncio.run(fail())
    assert policy.sessions[0].closed


@pytest.mark.parametrize("headers", [{"X-Safe": "ok\r\nCookie: leak"}, {"Cookie ": "leak"}])
def test_header_injection_is_rejected_before_network(policy: SimpleNamespace, headers: Any) -> None:
    with pytest.raises(policy.network.UnsafeURLError):
        read_stream(policy, headers=headers)
    policy.dns.assert_not_awaited()
    assert not policy.sessions


def test_two_url_redirect_cycle_is_rejected_without_dispatching_a_third_hop(policy: SimpleNamespace) -> None:
    policy.responses = [FakeResponse(302, "/second"), FakeResponse(302, "/")]
    with pytest.raises(policy.network.UnsafeURLError):
        read_stream(policy)
    assert len(policy.calls) == 2 and all(s.closed for s in policy.sessions)


@pytest.mark.parametrize("status,error", [(302, None), (200, None),
    (200, RuntimeError("consumer failure")), (200, asyncio.CancelledError()),
    (200, asyncio.CancelledError("during cleanup"))])
def test_real_curl_response_unconsumed_task_is_stopped(
    policy: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, status: int, error: BaseException | None,
) -> None:
    async def check() -> None:
        response = Response()
        response.status_code, response.headers = status, Headers({"Location": "/next"})
        response.quit_now, response.astream_task = asyncio.Event(), asyncio.create_task(asyncio.Event().wait())
        policy.responses = [response, FakeResponse()]
        monkeypatch.setattr(FakeSession, "stream", AsyncSession.stream, raising=False)
        monkeypatch.setattr(FakeSession, "request", AsyncMock(side_effect=list(policy.responses)), raising=False)
        async def consume() -> None:
            async with policy.network.checked_stream(policy.manager, HTML, "html"):
                if isinstance(error, asyncio.CancelledError):
                    cancel = asyncio.current_task().cancel
                    if error.args:
                        response.astream_task.add_done_callback(lambda _: cancel())
                    else:
                        asyncio.get_running_loop().call_soon(cancel)
                    await asyncio.sleep(0)
                elif error is not None:
                    raise error
        with (pytest.raises(type(error)) if error is not None else nullcontext()):
            await asyncio.wait_for(consume(), timeout=1.0)
        assert response.quit_now.is_set() and response.astream_task.cancelled()
        assert all(session.closed for session in policy.sessions)
    asyncio.run(check())

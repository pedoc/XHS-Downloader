"""Purpose-bound HTTPS streams with per-hop validation and direct DNS pinning.

An explicitly configured proxy is trusted to resolve and route the destination;
local DNS validation cannot guarantee that proxy's remote destination address.
"""

from __future__ import annotations

import asyncio
import re
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.cookiejar import Cookie
from ipaddress import IPv6Address, ip_address
from typing import TYPE_CHECKING
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession, Cookies, Headers, Response
from curl_cffi.requests.cookies import CookieTypes
from curl_cffi.requests.headers import HeaderTypes

if TYPE_CHECKING:
    from .manager import Manager

__all__ = ["UnsafeURLError", "normalize_url", "public_addresses", "checked_stream"]

_HTML_HOSTS = frozenset({"www.xiaohongshu.com", "www.rednote.com"})
_SHORT_HOSTS = frozenset({"xhslink.com", "xhslink.cn"})
_MEDIA_SUFFIXES = ("xhscdn.com", "rednotecdn.com")
_PURPOSES = ("html", "short", "media", "update")
_RELEASE_PATH = "/JoeanAmier/XHS-Downloader/releases/"
_MAX_REDIRECTS = 5
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", re.ASCII)
_BLOCKED_HEADERS = frozenset({"cookie", "cookie2", "authorization", "proxy-authorization", "host"})
_REDIRECT_HEADERS = frozenset({
    "accept", "accept-encoding", "accept-language", "cache-control",
    "if-modified-since", "if-none-match", "if-range", "range", "user-agent",
})


class UnsafeURLError(ValueError):
    """The URL, redirect or resolved destination violates the outbound policy."""


def _url_text(value: str) -> None:
    if not isinstance(value, str) or not value or any(
        char == "\\" or char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159
        for char in value
    ):
        raise UnsafeURLError("URL contains forbidden characters or is empty")


def _hostname(authority: str) -> str:
    match = re.fullmatch(r"([A-Za-z0-9.-]+)(?::443)?", authority, re.ASCII)
    if not match:
        raise UnsafeURLError("URL authority must be a hostname on HTTPS port 443")
    host = match[1].lower()
    if len(host) > 253 or not all(_LABEL.fullmatch(label) for label in host.split(".")):
        raise UnsafeURLError("URL hostname is malformed")
    try:
        ip_address(host)
    except ValueError:
        return host
    raise UnsafeURLError("IP literal URLs are not permitted")


def _allowed_host(host: str, purpose: str, initial: bool) -> bool:
    if purpose == "html":
        return host in _HTML_HOSTS
    if purpose == "short":
        return host in _SHORT_HOSTS or (not initial and host in _HTML_HOSTS)
    if purpose == "media":
        return host == "ci.xiaohongshu.com" or any(
            host == suffix or host.endswith("." + suffix) for suffix in _MEDIA_SUFFIXES
        )
    return host == "github.com"


def _release_path(path: str) -> bool:
    if path == _RELEASE_PATH + "latest":
        return True
    prefix = _RELEASE_PATH + "tag/"
    if not path.startswith(prefix):
        return False
    tag = unquote(path[len(prefix):])
    return bool(tag) and not any(char in tag for char in "\\%?#") and all(
        segment not in {"", ".", ".."} for segment in tag.split("/")
    )


def normalize_url(url: str, purpose: str, *, initial: bool = True) -> str:
    """Validate a vendor URL, preserving its query without decoding tokens."""
    _url_text(url)
    if purpose not in _PURPOSES:
        raise UnsafeURLError("Unknown outbound request purpose")
    candidate = url
    if initial and not re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", candidate):
        candidate = "https:" + candidate if candidate.startswith("//") else "https://" + candidate
    try:
        parsed = urlsplit(candidate)
    except ValueError as error:
        raise UnsafeURLError("Malformed URL") from error
    if parsed.scheme != "https" and not (initial and parsed.scheme == "http"):
        raise UnsafeURLError("Only HTTPS URLs are permitted")
    host = _hostname(parsed.netloc)
    if not _allowed_host(host, purpose, initial):
        raise UnsafeURLError("Hostname is not allowed for this request purpose")
    if purpose == "update" and not _release_path(parsed.path):
        raise UnsafeURLError("Only this repository's release pages are permitted")
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def _public_address(answer: tuple) -> str:
    address = ip_address(answer[4][0])
    target = address
    if isinstance(address, IPv6Address):
        if address.scope_id or address.is_site_local or address.sixtofour or address.teredo:
            raise UnsafeURLError("DNS returned a nonpublic or transitional address")
        target = address.ipv4_mapped or address
    if not target.is_global or target.is_multicast or target.is_reserved:
        raise UnsafeURLError("DNS returned a nonpublic unicast address")
    return str(address)


async def public_addresses(host: str) -> tuple[str, ...]:
    """Resolve once and reject the complete answer if any address is unsafe."""
    _hostname(host)
    try:
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM,
        )
        addresses = tuple(dict.fromkeys(_public_address(answer) for answer in answers))
    except (OSError, ValueError, TypeError, IndexError) as error:
        raise UnsafeURLError("DNS resolution did not produce safe public addresses") from error
    if not addresses:
        raise UnsafeURLError("DNS resolution returned no addresses")
    return addresses


def _host_cookie(cookie: Cookie, host: str) -> Cookie:
    return Cookie(
        version=cookie.version,
        name=cookie.name,
        value=cookie.value,
        port=cookie.port,
        port_specified=cookie.port_specified,
        domain=host,
        domain_specified=False,
        domain_initial_dot=False,
        path=cookie.path or "/",
        path_specified=cookie.path_specified,
        secure=True,
        expires=cookie.expires,
        discard=cookie.discard,
        comment=cookie.comment,
        comment_url=cookie.comment_url,
        rest=dict(cookie._rest),
        rfc2109=cookie.rfc2109,
    )


def _initial_cookies(source: CookieTypes | None, host: str) -> Cookies:
    result = Cookies()
    for cookie in Cookies(source).jar:
        if not cookie.domain:
            raise UnsafeURLError("HTML cookies must have an explicit hostname scope")
        domain = cookie.domain.lstrip(".").lower()
        if host == domain or (cookie.domain_specified and host.endswith("." + domain)):
            result.jar.set_cookie(_host_cookie(cookie, host))
    return result


def _safe_headers(headers: HeaderTypes | None, *, cross_host: bool = False) -> Headers:
    result = []
    for name, value in Headers(headers).multi_items():
        if not _HEADER_NAME.fullmatch(name):
            raise UnsafeURLError("Malformed HTTP header name")
        key = name.lower()
        if key in _BLOCKED_HEADERS or (cross_host and key not in _REDIRECT_HEADERS):
            continue
        if value is not None and any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
            raise UnsafeURLError("HTTP header contains forbidden control characters")
        result.append((name, value))
    return Headers(result)


async def _session_options(
    manager: Manager, host: str, proxy: str | None,
    timeout: float | tuple[float, float] | None,
) -> dict:
    options = {CurlOpt.PROTOCOLS_STR: "https", CurlOpt.REDIR_PROTOCOLS_STR: "https"}
    if proxy:
        # A trusted proxy resolves/routes remotely; do not claim local IP pinning.
        options[CurlOpt.NOPROXY] = ""
    else:
        addresses = await public_addresses(host)
        pinned = ",".join(f"[{ip}]" if ":" in ip else ip for ip in addresses)
        options |= {CurlOpt.RESOLVE: [f"{host}:443:{pinned}"], CurlOpt.PROXY: ""}
    return {
        "impersonate": manager.impersonate,
        "timeout": manager.timeout if timeout is None else timeout,
        "proxy": proxy or None,
        "verify": True,
        "trust_env": False,
        "allow_redirects": False,
        "discard_cookies": True,
        "curl_options": options,
    }


def _redirect_url(response: Response, current: str, purpose: str, hop: int) -> str:
    if hop >= _MAX_REDIRECTS:
        raise UnsafeURLError("Redirect limit exceeded")
    location = response.headers.get("Location")
    _url_text(location)
    try:
        target = urljoin(current, location)
    except ValueError as error:
        raise UnsafeURLError("Malformed redirect URL") from error
    return normalize_url(target, purpose, initial=False)


async def _close_response(response: Response) -> None:
    # curl-cffi 0.16.3 aclose waits for a transfer without stopping unread bodies.
    if response.quit_now is not None:
        response.quit_now.set()
    task = response.astream_task
    if task is None:
        await response.aclose()
        return
    if not task.done():
        task.cancel()
    result = (await asyncio.gather(task, return_exceptions=True))[0]
    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
        raise result


@asynccontextmanager
async def checked_stream(
    manager: Manager, url: str, purpose: str, *,
    headers: HeaderTypes | None = None,
    cookies: CookieTypes | None = None,
    proxy: str | None = None,
    timeout: float | tuple[float, float] | None = None,
) -> AsyncIterator[Response]:
    """Yield the final response while retaining ownership of its isolated session."""
    current = normalize_url(url, purpose)
    initial_host = urlsplit(current).netloc
    jar = Cookies()
    if purpose == "html":
        source = manager.request_client.cookies if cookies is None else cookies
        jar = _initial_cookies(source, initial_host)
    active_headers = _safe_headers(headers)
    selected_proxy = manager.proxy if proxy is None else proxy
    seen = frozenset()
    for hop in range(_MAX_REDIRECTS + 1):
        if current in seen:
            raise UnsafeURLError("Redirect loop detected")
        seen = seen | {current}
        host = urlsplit(current).netloc
        options = await _session_options(manager, host, selected_proxy, timeout)
        async with AsyncSession(
            **options, headers=active_headers,
            cookies=jar if host == initial_host else Cookies(),
        ) as session:
            response = await session.get(
                current, stream=True, allow_redirects=False, discard_cookies=True,
            )
            try:
                if response.status_code not in _REDIRECT_CODES:
                    yield response
                    return
                target = _redirect_url(response, current, purpose, hop)
                active_headers = _safe_headers(
                    active_headers, cross_host=urlsplit(target).netloc != host,
                )
                current = target
            finally:
                await _close_response(response)

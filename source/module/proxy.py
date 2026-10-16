"""Validate explicit proxy settings without making a connectivity probe."""

from urllib.parse import urlsplit

PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5", "socks5h"})


def validate_proxy(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or any(
        char.isspace() or char == "\\" or ord(char) < 32 or ord(char) == 127
        for char in value
    ):
        raise ValueError("Proxy address contains forbidden characters")
    candidate = value if "://" in value else "http://" + value
    try:
        proxy = urlsplit(candidate)
        port = proxy.port
    except ValueError:
        raise ValueError("Malformed proxy address") from None
    if (
        proxy.scheme not in PROXY_SCHEMES or not proxy.hostname
        or proxy.path not in {"", "/"} or proxy.query or proxy.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("Proxy must be an HTTP(S) or SOCKS host address")
    return candidate

from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie

from curl_cffi.requests import Cookies

__all__ = ["cookie_values", "scoped_cookies"]

COOKIE_HOSTS = frozenset({"www.xiaohongshu.com", "www.rednote.com"})


def cookie_values(raw: str) -> dict[str, str]:
    cookie = SimpleCookie()
    cookie.load(raw)
    return {key: morsel.value for key, morsel in cookie.items()}


def scoped_cookies(raw: str, host: str = "www.xiaohongshu.com") -> Cookies:
    """将手动配置的 Cookie 限定到一个站点，禁止按请求目标重新绑定域。"""

    if host not in COOKIE_HOSTS:
        raise ValueError("Cookies are only supported for known note sites")
    jar = CookieJar()
    for name, value in cookie_values(raw).items():
        # Cookies.set 会清空 __Host- Cookie 的域；直接构造 host-only 条目。
        jar.set_cookie(
            Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=host,
                domain_specified=False,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
        )
    return Cookies(jar)

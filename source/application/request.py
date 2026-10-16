from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from curl_cffi.requests import get
from curl_cffi.requests.exceptions import RequestException

from ..module import ERROR, logging, retry, sleep_time
from ..module.cookies import scoped_cookies
from ..translation import _

if TYPE_CHECKING:
    from curl_cffi.requests import AsyncSession

    from ..module import Manager

__all__ = ["Html"]


class Html:
    def __init__(
        self,
        manager: "Manager",
    ):
        self.manager = manager
        self.print = manager.print
        self.retry = manager.retry
        self.client = manager.request_client
        self.public_client = manager.public_client
        self.timeout = manager.timeout
        self.proxy = manager.proxy
        self.impersonate = manager.impersonate

    async def request_url(
        self,
        url: str,
        content=True,
        cookie: str | None = None,
        proxy: str | None = None,
        purpose: Literal["html", "short", "update"] = "html",
        **kwargs,
    ) -> str:
        request = self.__request_url if purpose == "update" else self.__retry_request_url
        return await request(url, content, cookie, proxy, purpose, **kwargs)

    @retry
    async def __retry_request_url(self, *args: Any, **kwargs: Any) -> str:
        return await self.__request_url(*args, **kwargs)

    async def __request_url(
        self,
        url: str,
        content: bool,
        cookie: str | None,
        proxy: str | None,
        purpose: Literal["html", "short", "update"],
        **kwargs,
    ) -> str:
        if purpose not in {"html", "short", "update"}:
            raise ValueError("Unknown request purpose")
        if not url.startswith("http"):
            url = f"https://{url}"
        headers = {} if purpose == "update" else self.manager.get_headers(url)
        try:
            if cookie and purpose == "html":
                response = self.__request_url_with_cookie(
                    url,
                    headers,
                    cookie,
                    proxy=proxy,
                    **kwargs,
                )
            else:
                response = await self.__request_url_get(
                    url,
                    headers,
                    client=self.client if purpose == "html" else self.public_client,
                    proxy=proxy,
                    **kwargs,
                )
            if purpose != "update":
                await sleep_time()
            response.raise_for_status()
            return response.text if content else str(response.url)
        except RequestException as error:
            logging(
                self.print,
                _("网络异常，{0} 请求失败: {1}").format(url, repr(error)),
                ERROR,
            )
            return ""

    @staticmethod
    def format_url(url: str) -> str:
        return bytes(url, "utf-8").decode("unicode_escape")

    def __request_url_with_cookie(
            self,
            url: str,
            headers: dict,
            cookie: str,
            proxy: str | None = None,
            **kwargs,
    ):
        return get(
            url,
            headers=headers,
            cookies=scoped_cookies(cookie, host=urlsplit(url).hostname or ""),
            timeout=self.timeout,
            verify=False,
            allow_redirects=True,
            proxy=self.proxy if proxy is None else proxy,
            impersonate=self.impersonate,
            **kwargs,
        )

    async def __request_url_get(
        self,
        url: str,
        headers: dict,
        proxy: str | None = None,
        client: "AsyncSession | None" = None,
        **kwargs,
    ):
        selected_client = self.client if client is None else client
        return await selected_client.get(
            url,
            headers=headers,
            proxy=self.proxy if proxy is None else proxy,
            **kwargs,
        )

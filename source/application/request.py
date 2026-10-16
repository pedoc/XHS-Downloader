from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from ..module import ERROR, logging, retry, sleep_time
from ..module.cookies import scoped_cookies
from ..translation import _

if TYPE_CHECKING:
    from curl_cffi.requests import Response

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
        content: bool = True,
        cookie: str | None = None,
        proxy: str | None = None,
        purpose: Literal["html", "short", "update"] = "html",
        timeout: float | None = None,
    ) -> str:
        request = self.__request_url if purpose == "update" else self.__retry_request_url
        return await request(url, content, cookie, proxy, purpose, timeout=timeout)

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
        timeout: float | None = None,
    ) -> str:
        if purpose not in {"html", "short", "update"}:
            raise ValueError("Unknown request purpose")
        if not url.startswith("http"):
            url = f"https://{url}"
        headers = {} if purpose == "update" else self.manager.get_headers(url)
        try:
            if cookie and purpose == "html":
                response = await self.__request_url_with_cookie(
                    url,
                    headers,
                    cookie,
                    proxy=proxy,
                    timeout=timeout,
                )
            else:
                response = await self.__request_url_get(
                    url,
                    headers,
                    client=self.client if purpose == "html" else self.public_client,
                    proxy=proxy,
                    timeout=timeout,
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

    async def __request_url_with_cookie(
        self,
        url: str,
        headers: dict,
        cookie: str,
        proxy: str | None = None,
        timeout: float | None = None,
    ) -> "Response":
        async with AsyncSession(
            headers=headers,
            cookies=scoped_cookies(cookie, host=urlsplit(url).hostname or ""),
            timeout=self.timeout if timeout is None else timeout,
            verify=True,
            allow_redirects=True,
            proxy=self.proxy if proxy is None else proxy,
            impersonate=self.impersonate,
            discard_cookies=True,
        ) as client:
            return await client.get(url)

    async def __request_url_get(
        self,
        url: str,
        headers: dict,
        proxy: str | None = None,
        client: "AsyncSession | None" = None,
        timeout: float | None = None,
    ):
        selected_client = self.client if client is None else client
        options = {} if timeout is None else {"timeout": timeout}
        return await selected_client.get(
            url,
            headers=headers,
            proxy=self.proxy if proxy is None else proxy,
            **options,
        )

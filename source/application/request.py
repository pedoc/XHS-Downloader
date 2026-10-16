from asyncio import timeout as request_deadline
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from curl_cffi.requests.exceptions import RequestException

from ..module import ERROR, logging, retry, sleep_time
from ..module.cookies import scoped_cookies
from ..module.network import checked_stream, normalize_url
from ..translation import _

if TYPE_CHECKING:
    from ..module import Manager

__all__ = ["Html"]


class Html:
    def __init__(self, manager: "Manager"):
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
        url = normalize_url(url, purpose)
        headers = {} if purpose == "update" else self.manager.get_headers(url)
        cookies = (
            scoped_cookies(cookie, host=urlsplit(url).hostname or "")
            if cookie and purpose == "html" else None
        )
        received_response = False
        try:
            async with request_deadline(self.timeout if timeout is None else timeout):
                async with checked_stream(
                    self.manager, url, purpose, headers=headers, cookies=cookies,
                    proxy=proxy, timeout=timeout,
                ) as response:
                    received_response = True
                    response.raise_for_status()
                    result = await response.atext() if content else str(response.url)
            if purpose != "update":
                await sleep_time()
            return result
        except (RequestException, TimeoutError) as error:
            if received_response and purpose != "update":
                await sleep_time()
            logging(
                self.print,
                _("网络异常，{0} 请求失败: {1}").format(url, repr(error)),
                ERROR,
            )
            return ""

    @staticmethod
    def format_url(url: str) -> str:
        return bytes(url, "utf-8").decode("unicode_escape")

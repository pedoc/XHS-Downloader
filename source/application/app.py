from asyncio import (
    CancelledError,
    Event,
    Future,
    Queue,
    QueueEmpty,
    create_task,
    gather,
    sleep,
)
from contextlib import suppress
from datetime import datetime
from re import compile
from textwrap import dedent
from types import SimpleNamespace
from typing import Annotated, Awaitable, Callable
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastmcp import FastMCP
from pydantic import Field
from pyperclip import copy, paste
from rich import print
from uvicorn import Config, Server

from ..expansion import (
    # BrowserCookie,
    Cleaner,
    Converter,
    Namespace,
    beautify_string,
)
from ..expansion.paths import ID_TOKEN, validate_component
from ..module import (
    __VERSION__,
    ERROR,
    IMPERSONATE,
    INFO,
    MASTER,
    REPOSITORY,
    VERSION_BETA,
    VERSION_MAJOR,
    VERSION_MINOR,
    VOLUME,
    WARNING,
    DataRecorder,
    ExtractData,
    ExtractParams,
    IDRecorder,
    Manager,
    Mapping,
    MapRecorder,
    NoteGenerator,
    # sleep_time,
    ScriptServer,
    logging,
)
from ..module.limits import MAX_DOWNLOAD_TIME, MAX_FILE_SIZE, MIN_FREE_SPACE
from ..module.network import UnsafeURLError, normalize_url
from ..module.proxy import validate_proxy
from ..translation import _, switch_language
from .download import Download
from .explore import Explore
from .image import Image
from .request import Html
from .video import Video

__all__ = ["XHS"]


def new_statistics(total: int = 0) -> SimpleNamespace:
    """创建一次处理调用专用的统计对象，避免复用可变默认参数。"""

    return SimpleNamespace(
        all=total,
        success=0,
        fail=0,
        skip=0,
    )


def data_cache(function):
    async def inner(
        self,
        data: dict,
    ):
        if self.manager.record_data:
            download = data["下载地址"]
            lives = data["动图地址"]
            cover = data.get("封面地址")
            await function(
                self,
                data,
            )
            data["下载地址"] = download
            data["动图地址"] = lives
            data["封面地址"] = cover

    return inner


class Print:
    def __init__(
        self,
        func: Callable = print,
    ):
        self.func = func

    def __call__(
        self,
    ):
        return self.func


class XHS:
    VERSION_MAJOR = VERSION_MAJOR
    VERSION_MINOR = VERSION_MINOR
    VERSION_BETA = VERSION_BETA
    LINK_XHS = compile(r"(?:https?://)?www\.xiaohongshu\.com/explore/\S+")
    LINK_RN = compile(r"(?:https?://)?www\.rednote\.com/explore/\S+")
    USER_XHS = compile(
        r"(?:https?://)?www\.xiaohongshu\.com/user/profile/[a-z0-9]+/\S+"
    )
    USER_RN = compile(r"(?:https?://)?www\.rednote\.com/user/profile/[a-z0-9]+/\S+")
    SHARE_XHS = compile(r"(?:https?://)?www\.xiaohongshu\.com/discovery/item/\S+")
    SHARE_RN = compile(r"(?:https?://)?www\.rednote\.com/discovery/item/\S+")
    SHORT = compile(
        r"(?:https?://)?xhslink\.(?:com|cn)/[^\s\"<>\\^`{|}，。；！？、【】《》]+"
    )
    ID = compile(r"(?:explore|item)/(\S+)?\?")
    ID_USER = compile(r"user/profile/[a-z0-9]+/(\S+)?\?")
    __INSTANCE = None
    CLEANER = Cleaner()

    def __new__(cls, *args, **kwargs):
        if not cls.__INSTANCE:
            cls.__INSTANCE = super().__new__(cls)
        return cls.__INSTANCE

    def __init__(
        self,
        mapping_data: dict | None = None,
        work_path="",
        folder_name="Download",
        name_format="发布时间 作者昵称 作品标题",
        impersonate: str = IMPERSONATE,
        cookie: str = "",
        proxy: str | None = None,
        proxy_download: bool = False,
        timeout=10,
        chunk=1024 * 1024,
        max_retry=5,
        record_data=False,
        image_format="JPEG",
        image_download=True,
        video_download=True,
        video_cover_download=False,
        live_download=False,
        video_preference="resolution",
        folder_mode=False,
        download_record=True,
        author_archive=False,
        write_mtime=False,
        language="zh_CN",
        # read_cookie: int | str = None,
        script_server: bool = False,
        note_format: str = "",
        script_host="127.0.0.1",
        script_port=5558,
        max_file_size: int = MAX_FILE_SIZE,
        max_download_time: int = MAX_DOWNLOAD_TIME,
        min_free_space: int = MIN_FREE_SPACE,
        **kwargs,
    ):
        switch_language(language)
        self.print = Print()
        self.manager = Manager(
            VOLUME,
            work_path,
            folder_name,
            name_format,
            chunk,
            impersonate,
            cookie,
            # self.read_browser_cookie(read_cookie) or cookie,
            proxy,
            proxy_download,
            timeout,
            max_retry,
            record_data,
            image_format,
            image_download,
            video_download,
            video_cover_download,
            live_download,
            video_preference,
            download_record,
            folder_mode,
            author_archive,
            write_mtime,
            script_server,
            note_format,
            self.CLEANER,
            self.print,
            max_file_size=max_file_size,
            max_download_time=max_download_time,
            min_free_space=min_free_space,
        )
        self.mapping_data = mapping_data or {}
        self.map_recorder = MapRecorder(
            self.manager,
        )
        self.mapping = Mapping(self.manager, self.map_recorder)
        self.html = Html(self.manager)
        self.image = Image()
        self.video = Video()
        self.explore = Explore()
        self.convert = Converter()
        self.downloader = Download(self.manager)
        self.id_recorder = IDRecorder(self.manager)
        self.data_recorder = DataRecorder(self.manager)
        self.note_generator = NoteGenerator(self.manager.note_format, self.manager.folder)
        self.clipboard_cache: str = ""
        self.queue = Queue()
        self.event = Event()
        self.script = None
        self.script_task_handler: Callable[..., Awaitable[object]] | None = None
        self.init_script_server(
            script_host,
            script_port,
        )

    def __extract_image(self, container: dict, data: Namespace):
        container["下载地址"], container["动图地址"] = self.image.get_image_link(
            data, self.manager.image_format
        )
        container["封面地址"] = None

    def __extract_video(
        self,
        container: dict,
        data: Namespace,
    ):
        container["下载地址"] = self.video.deal_video_link(
            data,
            self.manager.video_preference,
        )
        container["动图地址"] = [
            None,
        ]
        link, _ = self.image.get_image_link(data, self.manager.image_format)
        container["封面地址"] = link[0] if link else None

    async def __download_files(
        self,
        container: dict,
        download: bool,
        index,
        count: SimpleNamespace,
        progress_callback: Callable[[dict], None] | None = None,
        task_id: str | None = None,
    ):
        author_id = self.__validate_id(container["作者ID"])
        alias = validate_component(
            self.CLEANER.filter_name(container["作者昵称"]) or author_id
        )
        nickname = validate_component(f"{author_id}_{alias}")
        filename = self.__naming_rules(container)
        path = self.downloader.generate_path(nickname, filename)
        urls = container["下载地址"]
        result = None
        if urls and download:
            result = await self.downloader.run(
                urls,
                container["动图地址"],
                index,
                path,
                filename,
                container["作品类型"],
                container["时间戳"],
                container["封面地址"],
                progress=progress_callback,
                task_id=task_id,
            )
        await self.note_generator.save_note_info(container, path, filename)
        await self.save_data(container)
        if not urls:
            self.logging(_("提取作品文件下载地址失败"), ERROR)
            count.fail += 1
        elif download:
            if not result:
                count.skip += 1
            elif all(result):
                await self.__add_record(container["作品ID"])
                count.success += 1
            else:
                count.fail += 1

    @data_cache
    async def save_data(
        self,
        data: dict,
    ):
        data["采集时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data["下载地址"] = " ".join(data["下载地址"])
        data["动图地址"] = " ".join(i or "NaN" for i in data["动图地址"])
        data.pop("时间戳", None)
        # 数据库表结构固定，封面地址不入库，由 data_cache 装饰器在记录后恢复
        data.pop("封面地址", None)
        await self.data_recorder.add(**data)

    async def __add_record(
        self,
        id_: str,
    ) -> None:
        await self.id_recorder.add(id_)

    async def extract(
        self,
        url: str,
        download=False,
        index: list | tuple | None = None,
        check_record: bool = True,
        progress_callback: Callable[[dict], None] | None = None,
        task_id: str | None = None,
        result_callback: Callable[[dict], None] | None = None,
        proxy: str | None = None,
    ) -> list[dict]:
        if not (
            urls := await self.extract_links(
                url,
                proxy=proxy,
            )
        ):
            self.logging(_("提取小红书作品链接失败"), WARNING)
            return []
        statistics = new_statistics(len(urls))
        self.logging(_("共 {0} 个小红书作品待处理...").format(statistics.all))
        result = [
            await self.__deal_extract(
                i,
                download,
                index,
                check_record=check_record,
                proxy=proxy,
                count=statistics,
                progress_callback=progress_callback,
                task_id=task_id,
            )
            for i in urls
        ]
        self.show_statistics(
            statistics,
        )
        if result_callback:
            result_callback(
                {
                    "task_id": task_id,
                    "all": statistics.all,
                    "success": statistics.success,
                    "fail": statistics.fail,
                    "skip": statistics.skip,
                }
            )
        return result

    def show_statistics(
        self,
        statistics: SimpleNamespace,
    ) -> None:
        self.logging(
            _("共处理 {0} 个作品，成功 {1} 个，失败 {2} 个，跳过 {3} 个").format(
                statistics.all,
                statistics.success,
                statistics.fail,
                statistics.skip,
            ),
        )

    async def extract_cli(
        self,
        url: str,
        download=True,
        index: list | tuple = None,
        check_record: bool = True,
    ) -> None:
        url = await self.extract_links(
            url,
        )
        if not url:
            self.logging(_("提取小红书作品链接失败"), WARNING)
            return
        if index:
            await self.__deal_extract(
                url[0],
                download,
                index,
                check_record=check_record,
            )
        else:
            statistics = new_statistics(len(url))
            [
                await self.__deal_extract(
                    u,
                    download,
                    index,
                    check_record=check_record,
                    count=statistics,
                )
                for u in url
            ]
            self.show_statistics(
                statistics,
            )

    async def extract_links(
        self,
        url: str,
        proxy: str | None = None,
    ) -> list[str]:
        urls = []
        for i in url.split():
            if u := self.SHORT.search(i):
                try:
                    i = await self.html.request_url(
                        u.group(), False, purpose="short", proxy=proxy,
                    )
                except UnsafeURLError as error:
                    self.logging(_("链接不符合安全策略：{0}").format(error), WARNING)
                    continue
            if u := self.SHARE_XHS.search(i):
                urls.append(u.group())
            elif u := self.SHARE_RN.search(i):
                urls.append(u.group())
            elif u := self.LINK_XHS.search(i):
                urls.append(u.group())
            elif u := self.LINK_RN.search(i):
                urls.append(u.group())
            elif u := self.USER_XHS.search(i):
                urls.append(u.group())
            elif u := self.USER_RN.search(i):
                urls.append(u.group())
        safe_urls = []
        for link in urls:
            try:
                safe_urls.append(normalize_url(link, "html"))
            except UnsafeURLError as error:
                self.logging(_("链接不符合安全策略：{0}").format(error), WARNING)
        return safe_urls

    def extract_id(self, links: list[str]) -> list[str]:
        ids = []
        for i in links:
            if j := self.ID.search(i):
                ids.append(j.group(1))
            elif j := self.ID_USER.search(i):
                ids.append(j.group(1))
        return ids

    async def _get_html_data(
        self,
        url: str,
        id_: str,
        count: SimpleNamespace,
        cookie: str | None = None,
        proxy: str | None = None,
    ) -> Namespace | dict:
        self.logging(_("开始处理作品：{0}").format(id_))
        try:
            html = await self.html.request_url(url, cookie=cookie, proxy=proxy)
        except UnsafeURLError as error:
            self.logging(_("链接不符合安全策略：{0}").format(error), ERROR)
            count.fail += 1
            return {"message": str(error), "error": True}
        namespace = self.__generate_data_object(html)
        if not namespace:
            self.logging(_("{0} 获取数据失败").format(id_), ERROR)
            count.fail += 1
            return {}
        return namespace

    async def _check_existing_record(
        self,
        id_: str,
        count: SimpleNamespace,
    ) -> str | None:
        """根据作品 ID 查询下载记录，存在记录时返回跳过提示。"""

        if not await self.has_download_record(id_):
            return None
        msg = _("作品 {0} 存在下载记录，跳过处理").format(id_)
        self.logging(msg)
        count.skip += 1
        return msg

    def _extract_data(
        self,
        namespace: Namespace,
        id_: str,
        count,
    ):
        data = self.explore.run(namespace)
        if not data:
            self.logging(_("{0} 提取数据失败").format(id_), ERROR)
            count.fail += 1
            return {}
        return data

    @staticmethod
    def __safe_media_urls(data: dict) -> dict:
        return data | {
            "下载地址": [normalize_url(url, "media") for url in data["下载地址"]],
            "动图地址": [
                normalize_url(url, "media") if url else None for url in data["动图地址"]
            ],
            "封面地址": (
                normalize_url(data["封面地址"], "media") if data.get("封面地址") else None
            ),
        }

    async def _deal_download_tasks(
        self,
        data: dict,
        namespace: Namespace,
        id_: str,
        download: bool,
        index: list | tuple | None,
        count: SimpleNamespace,
        progress_callback: Callable[[dict], None] | None = None,
        task_id: str | None = None,
    ):
        try:
            self.__validate_id(data["作品ID"])
            self.__validate_id(data["作者ID"])
            if data["作品类型"] == _("视频"):
                self.__extract_video(data, namespace)
            elif data["作品类型"] in {_("图文"), _("图集")}:
                self.__extract_image(data, namespace)
            else:
                self.logging(_("未知的作品类型：{0}").format(id_), WARNING)
                data["下载地址"] = []
                data["动图地址"] = []
            data = self.__safe_media_urls(data)
            await self.update_author_nickname(data)
            await self.__download_files(
                data,
                download=download,
                index=index,
                count=count,
                progress_callback=progress_callback,
                task_id=task_id,
            )
        except (ValueError, OSError) as error:
            count.fail += 1
            self.logging(_("作品处理失败：{0}").format(error), ERROR)
            if progress_callback:
                progress_callback({"task_id": task_id, "state": "failed"})
            return {"message": str(error), "error": True}
        return data

    async def __deal_extract(
        self,
        url: str,
        download: bool,
        index: list | tuple | None,
        check_record: bool,
        cookie: str | None = None,
        proxy: str | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        task_id: str | None = None,
        count: SimpleNamespace | None = None,
    ):
        """提取并处理一个作品；记录只在进入流程时检查一次。"""

        if count is None:
            count = new_statistics()
        id_ = self.extract_link_id(url)
        if check_record and (msg := await self._check_existing_record(id_, count)):
            return {"message": msg}
        namespace = await self._get_html_data(
            url,
            id_=id_,
            count=count,
            cookie=cookie,
            proxy=proxy,
        )
        if not isinstance(namespace, Namespace):
            return namespace
        if not (
            data := self._extract_data(
                namespace,
                id_,
                count,
            )
        ):
            return data
        data = await self._deal_download_tasks(
            data
            | {
                "作品链接": url,
            },
            namespace,
            id_,
            download=download,
            index=index,
            count=count,
            progress_callback=progress_callback,
            task_id=task_id,
        )
        self.logging(_("作品处理完成：{0}").format(id_))
        return data

    async def deal_script_tasks(
        self,
        data: dict,
        index: list | tuple | None,
        count: SimpleNamespace | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        task_id: str | None = None,
        result_callback: Callable[[dict], None] | None = None,
    ):
        if count is None:
            count = new_statistics(1)
        namespace = self.json_to_namespace(data)
        id_ = namespace.safe_extract("noteId", "")
        if msg := await self._check_existing_record(id_, count):
            result = {"message": msg}
        elif not (
            data := self._extract_data(
                namespace,
                id_,
                count,
            )
        ):
            result = data
        else:
            result = await self._deal_download_tasks(
                data,
                namespace,
                id_,
                download=True,
                index=index,
                count=count,
                progress_callback=progress_callback,
                task_id=task_id,
            )
        if result_callback:
            result_callback(
                {
                    "task_id": task_id,
                    "all": count.all,
                    "success": count.success,
                    "fail": count.fail,
                    "skip": count.skip,
                }
            )
        return result

    async def process_script_task(self, **kwargs):
        if self.script_task_handler:
            return await self.script_task_handler(**kwargs)
        return await self.deal_script_tasks(**kwargs)

    @staticmethod
    def json_to_namespace(data: dict) -> Namespace:
        return Namespace(data)

    async def update_author_nickname(
        self,
        container: dict,
    ):
        i = self.__validate_id(container["作者ID"])
        alias = self.CLEANER.filter_name(self.mapping_data.get(i, ""))
        if not alias:
            alias = self.CLEANER.filter_name(
                self.manager.filter_name(container["作者昵称"])
            )
        container["作者昵称"] = validate_component(alias or self.__validate_id(i))
        await self.mapping.update_cache(i, container["作者昵称"])

    @staticmethod
    def extract_link_id(url: str) -> str:
        """从已提取的作品链接中获取作品 ID。"""

        link = urlparse(url)
        return link.path.rstrip("/").split("/")[-1]

    def __generate_data_object(self, html: str) -> Namespace:
        data = self.convert.run(html)
        return Namespace(data)

    @staticmethod
    def __validate_id(value: str) -> str:
        validate_component(value)
        if not ID_TOKEN.fullmatch(value):
            raise ValueError(f"Invalid metadata ID: {value!r}")
        return value

    def __naming_rules(self, data: dict) -> str:
        author_id = self.__validate_id(data["作者ID"])
        note_id = self.__validate_id(data["作品ID"])
        fallback = validate_component(f"{author_id}_{note_id}")
        keys = self.manager.name_format.split()
        values = []
        for key in keys:
            match key:
                case "发布时间":
                    values.append(self.__get_name_time(data))
                case "作品标题":
                    values.append(self.__get_name_title(data))
                case _:
                    values.append(data[key])
        return validate_component(
            beautify_string(
                self.CLEANER.filter_name(
                    self.manager.SEPARATE.join(values),
                    default=fallback,
                ),
                length=128,
            )
        )

    @staticmethod
    def __get_name_time(data: dict) -> str:
        return data["发布时间"].replace(":", ".")

    def __get_name_title(self, data: dict) -> str:
        return (
            beautify_string(
                self.manager.filter_name(data["作品标题"]),
                64,
            )
            or self.__validate_id(data["作品ID"])
        )

    async def monitor(
        self,
        delay=1,
        download=True,
        check_record: bool = True,
    ) -> None:
        self.logging(
            _(
                "程序会自动读取并提取剪贴板中的小红书作品链接，并自动下载链接对应的作品文件，如需关闭，请点击关闭按钮，或者向剪贴板写入 “close” 文本！"
            ),
            style=MASTER,
        )
        self.event.clear()
        copy("")
        await gather(
            self.__get_link(delay),
            self.__receive_link(
                delay,
                download=download,
                index=None,
                check_record=check_record,
            ),
        )

    async def __get_link(self, delay: int):
        while not self.event.is_set():
            if (t := paste()).lower() == "close":
                self.stop_monitor()
            elif t != self.clipboard_cache:
                self.clipboard_cache = t
                create_task(self.__push_link(t))
            await sleep(delay)

    async def __push_link(
        self,
        content: str,
    ):
        await gather(
            *[
                self.queue.put(i)
                for i in await self.extract_links(
                    content,
                )
            ]
        )

    async def __receive_link(self, delay: int, *args, **kwargs):
        while not self.event.is_set() or self.queue.qsize() > 0:
            with suppress(QueueEmpty):
                await self.__deal_extract(self.queue.get_nowait(), *args, **kwargs)
            await sleep(delay)

    def stop_monitor(self):
        self.event.set()

    async def has_download_record(self, id_: str) -> bool:
        return bool(await self.id_recorder.select(id_))

    async def __aenter__(self):
        await self.id_recorder.__aenter__()
        await self.data_recorder.__aenter__()
        await self.map_recorder.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.id_recorder.__aexit__(exc_type, exc_value, traceback)
        await self.data_recorder.__aexit__(exc_type, exc_value, traceback)
        await self.map_recorder.__aexit__(exc_type, exc_value, traceback)
        await self.close()

    async def close(self):
        await self.stop_script_server()
        await self.manager.close()

    # @staticmethod
    # def read_browser_cookie(value: str | int) -> str:
    #     return (
    #         BrowserCookie.get(
    #             value,
    #             domains=[
    #                 "xiaohongshu.com",
    #             ],
    #         )
    #         if value
    #         else ""
    #     )

    async def run_api_server(
        self,
        host="127.0.0.1",
        port=5556,
        log_level="info",
    ):
        api = FastAPI(
            debug=self.VERSION_BETA,
            title="XHS-Downloader",
            version=__VERSION__,
        )
        self.setup_routes(api)
        config = Config(
            api,
            host=host,
            port=port,
            log_level=log_level,
        )
        server = Server(config)
        await server.serve()

    def setup_routes(
        self,
        server: FastAPI,
    ):
        @server.get(
            "/",
            summary=_("跳转至项目 GitHub 仓库"),
            description=_("重定向至项目 GitHub 仓库主页"),
            tags=["API"],
        )
        async def index():
            return RedirectResponse(url=REPOSITORY)

        @server.post(
            "/xhs/detail",
            summary=_("获取作品数据及下载地址"),
            description=_(
                dedent("""
                **参数**:
                        
                - **url**: 小红书作品链接，自动提取，不支持多链接；必需参数
                - **download**: 是否下载作品文件；设置为 true 将会耗费更多时间；可选参数
                - **index**: 下载指定序号的图片文件，仅对图文作品生效；download 参数设置为 false 时不生效；可选参数
                - **cookie**: 请求数据时使用的 Cookie；可选参数
                - **proxy**: 可选，仅允许与本机已配置代理一致，不能覆盖本机出站策略
                - **check_record**: 是否跳过已有下载记录的作品；可选参数
                """)
            ),
            tags=["API"],
            response_model=ExtractData,
        )
        async def handle(extract: ExtractParams):
            if extract.proxy is not None:
                try:
                    requested_proxy = validate_proxy(extract.proxy)
                except ValueError:
                    raise HTTPException(status_code=400, detail=_("代理地址格式无效")) from None
                if requested_proxy != self.manager.proxy:
                    raise HTTPException(
                        status_code=400, detail=_("API 请求不能覆盖本机配置的代理"),
                    )
            data = None
            url = await self.extract_links(
                extract.url,
                proxy=self.manager.proxy,
            )
            if not url:
                msg = _("提取小红书作品链接失败")
            else:
                if data := await self.__deal_extract(
                    url[0],
                    extract.download,
                    extract.index,
                    check_record=extract.check_record,
                    cookie=extract.cookie,
                    proxy=self.manager.proxy,
                ):
                    msg = _("获取小红书作品数据失败") if data.get("error") else _("获取小红书作品数据成功")
                else:
                    msg = _("获取小红书作品数据失败")
            return ExtractData(message=msg, params=extract, data=data)

    async def run_mcp_server(
        self,
        transport="streamable-http",
        host="127.0.0.1",
        port=5556,
        log_level="INFO",
    ):
        mcp = FastMCP(
            "XHS-Downloader",
            instructions=dedent("""
                本服务器提供两个 MCP 接口，分别用于获取小红书作品信息数据和下载小红书作品文件，二者互不依赖，可独立调用。
                
                支持的作品链接格式：
                - https://www.xiaohongshu.com/explore/...
                - https://www.xiaohongshu.com/discovery/item/...
                - https://xhslink.com/...
                
                get_detail_data
                功能：输入小红书作品链接，返回该作品的信息数据，不会下载文件。
                参数：
                - url（必填）：小红书作品链接
                返回：
                - message：结果提示
                - data：作品信息数据
                
                download_detail
                功能：输入小红书作品链接，下载作品文件，默认不返回作品信息数据。
                参数：
                - url（必填）：小红书作品链接
                - index（选填）：根据用户指定的图片序号（如用户说“下载第1和第3张”时，index应为 [1, 3]），生成由所需图片序号组成的列表；如果用户未指定序号，则该字段为 None
                - return_data（可选）：是否返回作品信息数据；如需返回作品信息数据，设置此参数为 true，默认值为 false
                返回：
                - message：结果提示
                - data：作品信息数据，不需要返回作品信息数据时固定为 None
                """),
            version=__VERSION__,
        )

        @mcp.tool(
            name="get_detail_data",
            description=dedent("""
                功能：输入小红书作品链接，返回该作品的信息数据，不会下载文件。
                
                参数：
                url（必填）：小红书作品链接，格式如：
                - https://www.xiaohongshu.com/explore/...
                - https://www.xiaohongshu.com/discovery/item/...
                - https://xhslink.com/...
                
                返回：
                - message：结果提示
                - data：作品信息数据
                """),
            tags={
                "小红书",
                "XiaoHongShu",
                "RedNote",
            },
            annotations={
                "title": "获取小红书作品信息数据",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": True,
            },
        )
        async def get_detail_data(
            url: Annotated[str, Field(description=_("小红书作品链接"))],
        ) -> dict:
            msg, data = await self.deal_detail_mcp(
                url,
                False,
                None,
            )
            return {
                "message": msg,
                "data": data,
            }

        @mcp.tool(
            name="download_detail",
            description=dedent("""
                功能：输入小红书作品链接，下载作品文件，默认不返回作品信息数据。
                
                参数：
                url（必填）：小红书作品链接，格式如：
                - https://www.xiaohongshu.com/explore/...
                - https://www.xiaohongshu.com/discovery/item/...
                - https://xhslink.com/...
                index（选填）：根据用户指定的图片序号（如用户说“下载第1和第3张”时，index应为 [1, 3]），生成由所需图片序号组成的列表；如果用户未指定序号，则该字段为 None
                return_data（可选）：是否返回作品信息数据；如需返回作品信息数据，设置此参数为 true，默认值为 false
                
                返回：
                - message：结果提示
                - data：作品信息数据，不需要返回作品信息数据时固定为 None
                """),
            tags={
                "小红书",
                "XiaoHongShu",
                "RedNote",
                "Download",
                "下载",
            },
            annotations={
                "title": "下载小红书作品文件，可以返回作品信息数据",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": True,
            },
        )
        async def download_detail(
            url: Annotated[str, Field(description=_("小红书作品链接"))],
            index: Annotated[
                list[str | int] | None,
                Field(default=None, description=_("指定需要下载的图文作品序号")),
            ],
            return_data: Annotated[
                bool,
                Field(default=False, description=_("是否需要返回作品信息数据")),
            ],
        ) -> dict:
            msg, data = await self.deal_detail_mcp(
                url,
                True,
                index,
            )
            match (
                bool(data) and not data.get("error", False),
                return_data,
            ):
                case (True, True):
                    return {
                        "message": msg + ", " + _("作品文件下载任务执行完毕"),
                        "data": data,
                    }
                case (True, False):
                    return {
                        "message": _("作品文件下载任务执行完毕"),
                        "data": None,
                    }
                case (False, True):
                    return {
                        "message": msg + ", " + _("作品文件下载任务未执行"),
                        "data": None,
                    }
                case (False, False):
                    return {
                        "message": msg + ", " + _("作品文件下载任务未执行"),
                        "data": None,
                    }
                case _:
                    raise ValueError

        await mcp.run_async(
            transport=transport,
            show_banner=False,
            host=host,
            port=port,
            log_level=log_level,
        )

    async def deal_detail_mcp(
        self,
        url: str,
        download: bool,
        index: list[str | int] | None,
    ):
        data = None
        url = await self.extract_links(
            url,
        )
        if not url:
            msg = _("提取小红书作品链接失败")
        elif data := await self.__deal_extract(
            url[0],
            download,
            index,
            check_record=True,
        ):
            msg = _("获取小红书作品数据失败") if data.get("error") else _("获取小红书作品数据成功")
        else:
            msg = _("获取小红书作品数据失败")
        return msg, data

    def init_script_server(
        self,
        host="127.0.0.1",
        port=5558,
    ):
        if self.manager.script_server:
            self.run_script_server(host, port)

    async def switch_script_server(
        self,
        host="127.0.0.1",
        port=5558,
        switch: bool = None,
    ):
        if switch is None:
            switch = self.manager.script_server
        if switch:
            self.run_script_server(
                host,
                port,
            )
        else:
            await self.stop_script_server()

    def run_script_server(
        self,
        host="127.0.0.1",
        port=5558,
    ):
        if not self.script:
            self.script = create_task(self._run_script_server(host, port))

    async def _run_script_server(
        self,
        host="127.0.0.1",
        port=5558,
    ):
        async with ScriptServer(self, host, port):
            await Future()

    async def stop_script_server(self):
        if self.script:
            self.script.cancel()
            with suppress(CancelledError):
                await self.script
            self.script = None

    async def _script_server_debug(self):
        await self.switch_script_server(
            switch=self.manager.script_server,
        )

    def logging(self, text, style=INFO):
        logging(
            self.print,
            text,
            style,
        )

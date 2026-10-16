from contextlib import suppress
from os import utime
from pathlib import Path
from re import compile, sub
from shutil import move, rmtree
from typing import TYPE_CHECKING, get_args

from curl_cffi.requests import AsyncSession, BrowserTypeLiteral

from ..expansion.paths import contained_path, validate_component

from ..translation import _
from .cookies import cookie_values, scoped_cookies
from .proxy import validate_proxy
from .static import HEADERS, IMPERSONATE, ROOT, WARNING
from .tools import get_site_referer, logging

if TYPE_CHECKING:
    from ..expansion import Cleaner

__all__ = ["Manager"]


class Manager:
    NAME = compile(r"[^\u4e00-\u9fffa-zA-Z0-9-_！？，。；：“”（）《》]")
    NAME_KEYS = (
        "收藏数量",
        "评论数量",
        "分享数量",
        "点赞数量",
        "作品标签",
        "作品ID",
        "作品标题",
        "作品描述",
        "作品类型",
        "发布时间",
        "最后更新时间",
        "作者昵称",
        "作者ID",
    )
    NO_PROXY = {
        "http://": None,
        "https://": None,
    }
    SEPARATE = "_"
    WEB_ID = r"(?:^|; )webId=[^;]+"
    WEB_SESSION = r"(?:^|; )web_session=[^;]+"

    def __init__(
        self,
        root: Path,
        path: str,
        folder: str,
        name_format: str,
        chunk: int,
        impersonate: str,
        cookie: str,
        proxy: str | None,
        proxy_download: bool,
        timeout: int,
        retry: int,
        record_data: bool,
        image_format: str,
        image_download: bool,
        video_download: bool,
        video_cover_download: bool,
        live_download: bool,
        video_preference: str,
        download_record: bool,
        folder_mode: bool,
        author_archive: bool,
        write_mtime: bool,
        script_server: bool,
        note_format: str,
        cleaner: "Cleaner",
        print_object,
    ):
        self.print = print_object
        self.root = root.resolve()
        self.cleaner = cleaner
        self.temp = contained_path(self.root, self.root.joinpath("Temp"))
        self.path = self.__check_path(path)
        self.folder = self.__check_folder(folder)
        self.compatible()
        self.blank_headers = HEADERS.copy()
        self.impersonate = self.__check_impersonate(impersonate)
        self.retry = self.__check_integer(retry, 5, 0)
        self.chunk = self.__check_integer(chunk, 2 * 1024 * 1024, 1024 * 1024)
        self.name_format = self.__check_name_format(name_format)
        self.record_data = self.check_bool(record_data, False)
        self.image_format = self.__check_image_format(image_format)
        self.folder_mode = self.check_bool(folder_mode, False)
        self.download_record = self.check_bool(download_record, True)
        self.timeout = self.__check_integer(timeout, 10, 1)
        self.proxy_tip = None
        self.proxy_download = self.check_bool(proxy_download, False)
        self.proxy = self.__check_proxy(proxy)
        self.print_proxy_tip()
        self.request_client = AsyncSession(
            headers=self.blank_headers,
            cookies=scoped_cookies(cookie),
            timeout=self.timeout,
            verify=True,
            allow_redirects=True,
            proxy=self.proxy,
            impersonate=self.impersonate,
        )
        self.public_client = AsyncSession(
            headers=self.blank_headers,
            timeout=self.timeout,
            verify=True,
            allow_redirects=True,
            proxy=self.proxy,
            impersonate=self.impersonate,
            discard_cookies=True,
        )
        self.download_client = AsyncSession(
            timeout=self.timeout,
            verify=True,
            allow_redirects=True,
            proxy=self.proxy if self.proxy_download else None,
            impersonate=self.impersonate,
            discard_cookies=True,
        )
        self.image_download = self.check_bool(image_download, True)
        self.video_download = self.check_bool(video_download, True)
        self.video_cover_download = self.check_bool(video_cover_download, False)
        self.video_preference = self.check_video_preference(video_preference)
        self.live_download = self.check_bool(live_download, True)
        self.author_archive = self.check_bool(author_archive, False)
        self.write_mtime = self.check_bool(write_mtime, False)
        self.script_server = self.check_bool(script_server, False)
        self.note_format = self.__check_note_format(note_format)
        self.create_folder()

    def __check_path(self, path: str) -> Path:
        if not path:
            return self.root
        if (r := Path(path).resolve()).is_dir():
            return r
        return r if (r := self.__check_root_again(r)) else self.root

    def __check_folder(self, folder: str) -> Path:
        folder = validate_component(self.cleaner.filter_name(folder, default="Download"))
        return contained_path(self.path, self.path.joinpath(folder))

    @staticmethod
    def __check_root_again(root: Path) -> None | Path:
        if root.parent.is_dir():
            root.mkdir(exist_ok=True)
            return root
        return None

    @staticmethod
    def __check_image_format(image_format) -> str:
        if (i := image_format.lower()) in {
            "auto",
            "png",
            "webp",
            "jpeg",
            "heic",
            "avif",
        }:
            return i
        return "jpeg"

    @staticmethod
    def is_exists(path: Path) -> bool:
        return path.exists()

    def download_path(self, path: Path) -> Path:
        root = contained_path(self.path, self.folder)
        return contained_path(root, path)

    def temp_path(self, path: Path) -> Path:
        root = contained_path(self.root, self.temp)
        return contained_path(root, path)

    def delete(self, path: Path) -> None:
        path = self.temp_path(path)
        if path.exists():
            path.unlink()

    @staticmethod
    def archive(root: Path, name: str, folder_mode: bool) -> Path:
        validate_component(name)
        return root.joinpath(name) if folder_mode else root

    def move(
        self,
        temp: Path,
        path: Path,
        mtime: int | None = None,
        rewrite: bool = False,
    ) -> None:
        temp = self.temp_path(temp)
        path = self.download_path(path)
        if path.is_dir():
            raise ValueError(f"Media destination is a directory: {path}")
        move(temp, path)
        if rewrite and mtime:
            self.update_mtime(path, mtime)

    def update_mtime(self, file: Path, mtime: int) -> None:
        utime(self.download_path(file), (mtime, mtime))

    def __clean(self) -> None:
        temp = contained_path(self.root, self.temp)
        self.__check_tree(temp)
        rmtree(temp)

    @staticmethod
    def __check_tree(root: Path) -> None:
        pending = [root]
        visited = set()
        while pending:
            folder = pending.pop()
            if folder.parts in visited:
                continue
            visited.add(folder.parts)
            for child in folder.iterdir():
                child = contained_path(root, child)
                if child.is_dir():
                    pending.append(child)

    @classmethod
    def __remove_empty_directories(cls, root: Path) -> None:
        if not root.exists():
            return
        cls.__check_tree(root)
        for parent, directories, files in root.walk(top_down=False):
            if parent == root or directories or files:
                continue
            if any(part.startswith((".", "_")) for part in parent.relative_to(root).parts):
                continue
            with suppress(OSError):
                contained_path(root, parent).rmdir()

    def filter_name(self, name: str) -> str:
        name = self.NAME.sub("_", name)
        return sub(r"_+", "_", name).strip("_")

    @staticmethod
    def check_bool(value: bool, default: bool) -> bool:
        return value if isinstance(value, bool) else default

    @staticmethod
    def __check_integer(value: int, default: int, minimum: int) -> int:
        """将配置中的整数参数限制在下载器可接受的范围内。"""

        try:
            return max(minimum, value)
        except (TypeError, ValueError):
            return default

    async def close(self):
        await self.request_client.close()
        await self.public_client.close()
        await self.download_client.close()
        # self.__clean()
        folder = contained_path(self.path, self.folder)
        contained_path(self.root, self.temp)
        self.__remove_empty_directories(self.root)
        self.__remove_empty_directories(folder)

    def __check_name_format(self, format_: str) -> str:
        keys = format_.split()
        return next(
            ("发布时间 作者昵称 作品标题" for key in keys if key not in self.NAME_KEYS),
            format_,
        )

    @staticmethod
    def check_video_preference(preference: str) -> str:
        if preference in {"resolution", "bitrate", "size"}:
            return preference
        return "resolution"

    @staticmethod
    def __check_note_format(note_format: str) -> str:
        if note_format in {"txt", "md", "all"}:
            return note_format
        return ""

    def __check_proxy(self, proxy: str | None) -> str | None:
        configured = validate_proxy(proxy)
        if configured:
            self.proxy_tip = (_("已配置网络代理，将在实际请求时使用"),)
        return configured

    def get_headers(
        self,
        url: str = "",
    ) -> dict:
        headers = self.blank_headers.copy()
        headers["referer"] = get_site_referer(url)
        return headers

    def __check_impersonate(self, impersonate: str) -> str:
        if impersonate in get_args(BrowserTypeLiteral):
            return impersonate
        logging(
            self.print,
            _("impersonate 参数错误，使用默认值: {0}").format(IMPERSONATE),
            WARNING,
        )
        return IMPERSONATE

    def print_proxy_tip(
        self,
    ) -> None:
        if self.proxy_tip:
            logging(self.print, *self.proxy_tip)

    @classmethod
    def clean_cookie(cls, cookie_string: str) -> str:
        return cls.delete_cookie(
            cookie_string,
            (
                cls.WEB_ID,
                cls.WEB_SESSION,
            ),
        )

    @classmethod
    def delete_cookie(cls, cookie_string: str, patterns: list | tuple) -> str:
        for pattern in patterns:
            # 使用空字符串替换匹配到的部分
            cookie_string = sub(pattern, "", cookie_string)
        # 去除多余的分号和空格
        cookie_string = sub(r";\s*$", "", cookie_string)  # 删除末尾的分号和空格
        cookie_string = sub(r";\s*;", ";", cookie_string)  # 删除中间多余分号后的空格
        return cookie_string.strip("; ")

    def create_folder(self) -> None:
        contained_path(self.path, self.folder).mkdir(exist_ok=True)
        contained_path(self.root, self.temp).mkdir(exist_ok=True)

    def compatible(self) -> None:
        # 仅默认应用目录中的固定旧位置是兼容迁移源，不能信任任意 root.parent。
        if (
            self.root.parts != ROOT.joinpath("Volume").resolve().parts
            or self.path.parts != self.root.parts
            or self.folder.parts != self.root.joinpath("Download").parts
        ):
            return
        destination = contained_path(self.path, self.folder)
        if destination.exists():
            return
        old = ROOT.joinpath("Download")
        if old.is_symlink() or old.is_junction():
            raise ValueError("Unsafe legacy Download link; copy legacy files manually")
        old = contained_path(ROOT, old)
        if old.exists():
            try:
                self.__check_tree(old)
            except ValueError as error:
                raise ValueError("Unsafe legacy Download tree; copy files manually") from error
            move(old, destination)

    @staticmethod
    def cookie_str_to_dict(cookie_str: str) -> dict:
        return cookie_values(cookie_str)

from asyncio import (
    CancelledError, Semaphore, current_task, gather, get_running_loop, sleep, timeout,
)
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from aiofiles import open
from curl_cffi.requests.exceptions import RequestException

from ..expansion import CacheError
from ..expansion.paths import contained_path, validate_component
from ..module import ERROR, FILE_SIGNATURES, FILE_SIGNATURES_LENGTH, MAX_WORKERS, logging
from ..module import retry as re_download
from ..module.limits import (
    WRITE_BUFFER_SIZE,
    TransferLimitError,
    cache_owner,
    check_disk_space,
    check_size,
    content_total,
    preserve_transfer_failure,
)
from ..module.network import checked_stream, normalize_url
from ..translation import _

if TYPE_CHECKING:
    from curl_cffi.requests import AsyncSession, Response

    from ..module import Manager

__all__ = ["Download"]


class _CommitError(OSError):
    """A complete cache must be retained when publishing its destination fails."""


class Download:
    SEMAPHORE = Semaphore(MAX_WORKERS)
    WRITE_BUFFER_SIZE = WRITE_BUFFER_SIZE
    CONTENT_TYPE_MAP = {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/webp": "webp",
        "video/mp4": "mp4",
        "video/quicktime": "mov",
        "audio/mp4": "m4a",
        "audio/mpeg": "mp3",
    }

    def __init__(
        self,
        manager: "Manager",
    ):
        self.manager = manager
        self.print = manager.print
        self.folder = manager.folder
        self.temp = manager.temp
        self.chunk = manager.chunk
        self.client: "AsyncSession" = manager.download_client
        self.headers = manager.blank_headers
        self.retry = manager.retry
        self.max_file_size = manager.max_file_size
        self.max_download_time = manager.max_download_time
        self.min_free_space = manager.min_free_space
        self.folder_mode = manager.folder_mode
        self.video_format = "mp4"
        self.live_format = "mp4"
        self.image_format = manager.image_format
        self.image_format_list = (
            "jpeg",
            "png",
            "webp",
            "avif",
            "heic",
        )
        self.image_download = manager.image_download
        self.video_download = manager.video_download
        self.video_cover_download = manager.video_cover_download
        self.live_download = manager.live_download
        self.author_archive = manager.author_archive
        self.write_mtime = manager.write_mtime

    async def run(
        self,
        urls: list,
        lives: list,
        index: list | tuple | None,
        path: Path,
        filename: str,
        type_: str,
        mtime: int,
        cover: str | None = None,
        progress: Callable[[dict], None] | None = None,
        task_id: str | None = None,
    ) -> list[Any]:
        validate_component(filename)
        contained_path(self.manager.path, self.folder)
        path = contained_path(self.manager.path, path)
        if type_ == _("视频"):
            tasks = self.__ready_download_video(urls, path, filename)
            tasks += self.__ready_download_cover(cover, path, filename)
        elif type_ in {
            _("图文"),
            _("图集"),
        }:
            tasks = self.__ready_download_image(
                urls,
                lives,
                index,
                path,
                filename,
            )
        else:
            raise ValueError
        tasks = [(normalize_url(url, "media"), name, format_) for url, name, format_ in tasks]
        tasks = [
            self.__download(
                url,
                path,
                name,
                format_,
                mtime,
                progress,
                task_id,
            )
            for url, name, format_ in tasks
        ]
        tasks = await gather(*tasks)
        return tasks

    def generate_path(self, nickname: str, filename: str) -> Path:
        validate_component(nickname)
        validate_component(filename)
        folder = contained_path(self.manager.path, self.folder)
        if self.author_archive:
            folder = self.manager.download_path(folder.joinpath(nickname))
            folder.mkdir(exist_ok=True)
        path = self.manager.archive(folder, filename, self.folder_mode)
        if self.folder_mode:
            path = self.manager.download_path(path)
        path.mkdir(exist_ok=True)
        return path

    def __ready_download_video(
        self,
        urls: list[str],
        path: Path,
        name: str,
    ) -> list:
        if not self.video_download:
            logging(self.print, _("视频作品下载功能已关闭，跳过下载"))
            return []
        if self.__check_exists_path(
            path,
            f"{name}.{self.video_format}",
        ):
            return []
        return [(urls[0], name, self.video_format)]

    def __ready_download_cover(
        self,
        url: str | None,
        path: Path,
        name: str,
    ) -> list:
        if not self.video_cover_download or not url:
            return []
        if not any(
            self.__check_exists_path(
                path,
                f"{name}.{s}",
            )
            for s in self.image_format_list
        ):
            return [(url, name, self.image_format)]
        return []

    def __ready_download_image(
        self,
        urls: list[str],
        lives: list[str],
        index: list | tuple | None,
        path: Path,
        name: str,
    ) -> list:
        tasks = []
        if not self.image_download:
            logging(self.print, _("图文作品下载功能已关闭，跳过下载"))
            return tasks
        for i, j in enumerate(zip(urls, lives), start=1):
            if index and i not in index:
                continue
            file = f"{name}_{i}"
            if not any(
                self.__check_exists_path(
                    path,
                    f"{file}.{s}",
                )
                for s in self.image_format_list
            ):
                tasks.append([j[0], file, self.image_format])
            if (
                not self.live_download
                or not j[1]
                or self.__check_exists_path(
                    path,
                    f"{file}.{self.live_format}",
                )
            ):
                continue
            tasks.append([j[1], file, self.live_format])
        return tasks

    def __check_exists_path(
        self,
        path: Path,
        name: str,
    ) -> bool:
        file = self.manager.download_path(path.joinpath(validate_component(name)))
        if file.exists():
            logging(self.print, _("{0} 文件已存在，跳过下载").format(name))
            return True
        return False

    async def __download(
        self, url: str, path: Path, name: str, format_: str, mtime: int,
        progress: Callable[[dict], None] | None, task_id: str | None,
    ) -> bool:
        filename = validate_component(f"{name}.{format_}")
        temp = self.manager.temp_path(self.temp.joinpath(filename))
        path = self.manager.download_path(path.joinpath(filename)).parent
        async with cache_owner(temp), self.SEMAPHORE:
            def report(state: str, completed: int = 0, total: int | None = None) -> None:
                if progress:
                    progress({
                        "task_id": task_id, "filename": filename,
                        "completed_bytes": completed, "total_bytes": total,
                        "state": state,
                    })

            return await self.__bounded_transfer(url, path, name, format_, mtime, temp, report)

    async def __bounded_transfer(
        self, url: str, path: Path, name: str, format_: str, mtime: int,
        temp: Path, report: Callable[..., None],
    ) -> bool:
        try:
            # Waiting for cache ownership or a download slot does not spend this budget.
            async with timeout(self.max_download_time) as deadline:
                loop, task = get_running_loop(), current_task()

                def check_deadline() -> None:
                    if task is not None and task.cancelling():
                        raise CancelledError
                    when = deadline.when()
                    if when is not None and loop.time() >= when:
                        raise TimeoutError("Download total deadline exceeded")

                return await self.__transfer(
                    url, path, name, format_, mtime, temp, report, check_deadline,
                )
        except CancelledError:
            self.__discard_temp(temp)
            report("failed")
            raise
        except _CommitError as error:
            report("failed")
            logging(self.print, str(error), ERROR)
            return False
        except (TransferLimitError, TimeoutError, OSError) as error:
            self.__discard_temp(temp)
            report("failed")
            logging(self.print, _("文件 {0} 下载失败：{1}").format(name, error), ERROR)
            return False

    def __discard_temp(self, temp: Path) -> None:
        try:
            self.manager.delete(temp)
        except (OSError, ValueError) as error:
            logging(
                self.print,
                _("缓存 {0} 未能清理，未记录下载成功；请恢复存储条件后续传或手动处理：{1}")
                .format(temp, error),
                ERROR,
            )

    @re_download
    async def __transfer(
        self, url: str, path: Path, name: str, format_: str, mtime: int,
        temp: Path, report: Callable[..., None], check_deadline: Callable[[], None],
    ) -> bool:
        headers = self.headers.copy()
        completed = self.__update_headers_range(headers, temp)
        check_size(completed, self.max_file_size)
        self.__check_space(temp, path, total=completed)
        try:
            async with preserve_transfer_failure(checked_stream(
                self.manager, url, "media", headers=headers,
                proxy=self.manager.proxy if self.manager.proxy_download else "",
                timeout=self.max_download_time,
            )) as response:
                if response.status_code == 416:
                    raise CacheError(_("文件 {0} 缓存异常，重新下载").format(temp.name))
                response.raise_for_status()
                total = content_total(response.headers, completed, self.max_file_size)
                remaining = total - completed if total is not None else 0
                self.__check_space(temp, path, remaining, total or completed)
                report("downloading", completed, total)
                completed = await self.__write_file(response, temp, path, completed, total, report)
            real = await self.__suffix_with_file(temp, path, name, format_)
            completed = check_size(self.manager.temp_path(temp).stat().st_size, self.max_file_size)
            self.__check_space(temp, path, total=completed)
            try:
                self.manager.move(
                    temp, real, mtime, self.write_mtime, check_deadline=check_deadline,
                )
            except TimeoutError:
                raise
            except OSError as error:
                raise _CommitError(
                    _("文件提交失败；请检查缓存 {0} 与目标 {1}：{2}").format(temp, real, error)
                ) from error
            check_deadline()
            report("completed", completed, total)
            logging(self.print, _("文件 {0} 下载成功").format(real.name))
            return True
        except (RequestException, CacheError) as error:
            report("failed", completed)
            if isinstance(error, CacheError):
                self.manager.delete(temp)
            logging(self.print, _("文件 {0} 下载失败：{1}").format(name, error), ERROR)
            return False

    async def __write_file(
        self, response: "Response", temp: Path, path: Path, completed: int,
        total: int | None, report: Callable[..., None],
    ) -> int:
        buffer = bytearray()
        temp = self.manager.temp_path(temp)
        async with open(temp, "ab") as file:
            async for chunk in response.aiter_content(self.chunk):
                completed = check_size(completed + len(chunk), self.max_file_size)
                view = memoryview(chunk)
                while view:
                    size = min(self.WRITE_BUFFER_SIZE - len(buffer), len(view))
                    buffer.extend(view[:size])
                    view = view[size:]
                    if len(buffer) == self.WRITE_BUFFER_SIZE:
                        await self.__flush_buffer(file, buffer, temp, path, completed)
                        buffer.clear()
                report("downloading", completed, total)
                await sleep(0)
            if buffer:
                await self.__flush_buffer(file, buffer, temp, path, completed)
        return completed

    def __check_space(
        self, temp: Path, path: Path, pending: int = 0, total: int = 0,
    ) -> None:
        temporary = self.manager.temp_path(temp).parent
        destination = self.manager.download_path(path.joinpath(temp.name)).parent
        check_disk_space(temporary, self.min_free_space, pending)
        # A move across filesystems copies the full file; same-volume rename does not.
        needed = pending if temporary.stat().st_dev == destination.stat().st_dev else total
        check_disk_space(destination, self.min_free_space, needed)

    async def __flush_buffer(
        self, file: Any, buffer: bytearray, temp: Path, path: Path, total: int,
    ) -> None:
        self.__check_space(temp, path, len(buffer), total)
        await file.write(bytes(buffer))

    def __get_resume_byte_position(self, file: Path) -> int:
        file = self.manager.temp_path(file)
        return file.stat().st_size if file.is_file() else 0

    def __update_headers_range(self, headers: dict[str, str], file: Path) -> int:
        headers["Range"] = f"bytes={(position := self.__get_resume_byte_position(file))}-"
        return position

    async def __suffix_with_file(
        self, temp: Path, path: Path, name: str, default_suffix: str,
    ) -> Path:
        temp = self.manager.temp_path(temp)
        validate_component(name)
        suffix = validate_component(default_suffix)
        try:
            async with open(temp, "rb") as file:
                file_start = await file.read(FILE_SIGNATURES_LENGTH)
            for offset, signature, detected in FILE_SIGNATURES:
                if file_start[offset : offset + len(signature)] == signature:
                    suffix = detected
                    break
        except Exception as error:
            logging(
                self.print,
                _("文件 {0} 格式判断失败，错误信息：{1}").format(temp.name, repr(error)),
                ERROR,
            )
        return self.manager.download_path(path.joinpath(validate_component(f"{name}.{suffix}")))

from asyncio import CancelledError, Queue, QueueEmpty, QueueFull, Task, create_task
from contextlib import suppress
from json import loads
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, Field
from websockets import ConnectionClosed, serve
from websockets.typing import Origin

from ..translation import _
from .static import ERROR, WARNING

if TYPE_CHECKING:
    from websockets.asyncio.server import Server, ServerConnection

    from ..application import XHS

MAX_PENDING_TASKS = 32
MAX_INDEX_ITEMS = 256
MAX_MESSAGE_SIZE = 1024 * 1024
MAX_CONNECTION_QUEUE = 16
CLOSE_TIMEOUT = 5

ImageIndex = Annotated[int, Field(strict=True, gt=0)]


class ScriptTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    data: dict[str, Any]
    index: list[ImageIndex] | None = Field(default=None, max_length=MAX_INDEX_ITEMS)


class ScriptServer:
    ORIGINS = (
        Origin("https://www.xiaohongshu.com"),
        Origin("https://www.rednote.com"),
    )

    def __init__(
        self,
        core: "XHS",
        host: str = "127.0.0.1",
        port: int = 5558,
    ) -> None:
        self.core = core
        self.host = host
        self.port = port
        self.server: "Server | None" = None
        self.queue: Queue[dict[str, Any]] = Queue(maxsize=MAX_PENDING_TASKS)
        self.worker: Task[None] | None = None

    async def handler(self, websocket: "ServerConnection") -> None:
        with suppress(ConnectionClosed):
            async for message in websocket:
                try:
                    task = ScriptTask.model_validate(loads(message)).model_dump()
                except (ValueError, TypeError, RecursionError) as error:
                    self.core.logging(
                        _("脚本消息无效，已忽略（{0}）").format(type(error).__name__),
                        WARNING,
                    )
                    continue
                try:
                    self.queue.put_nowait(task)
                except QueueFull:
                    self.core.logging(_("脚本任务队列已满，已拒绝新任务"), WARNING)
                    await websocket.close(code=1013, reason=_("任务队列已满，请稍后重试"))
                    return

    async def worker_loop(self) -> None:
        while True:
            task = await self.queue.get()
            try:
                await self.core.process_script_task(**task)
            except Exception as error:
                self.core.logging(
                    _("脚本任务执行失败，已跳过（{0}）").format(type(error).__name__),
                    ERROR,
                )
            finally:
                self.queue.task_done()

    async def start(self) -> None:
        """启动服务器。"""
        self.server = await serve(
            self.handler,
            self.host,
            self.port,
            origins=self.ORIGINS,
            max_size=MAX_MESSAGE_SIZE,
            max_queue=MAX_CONNECTION_QUEUE,
            close_timeout=CLOSE_TIMEOUT,
        )
        self.worker = create_task(self.worker_loop())

    async def stop(self) -> None:
        """关闭连接，取消当前任务并丢弃尚未处理的队列。"""
        try:
            if self.server:
                self.server.close()
                await self.server.wait_closed()
        finally:
            self.server = None
            try:
                if self.worker:
                    self.worker.cancel()
                    with suppress(CancelledError):
                        await self.worker
            finally:
                self.worker = None
                self._discard_pending_tasks()

    def _discard_pending_tasks(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except QueueEmpty:
                return
            self.queue.task_done()

    async def __aenter__(self) -> "ScriptServer":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.stop()

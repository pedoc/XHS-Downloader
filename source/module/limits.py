"""Application transfer quotas, not a sandbox for native transport queues or OS I/O."""

from __future__ import annotations

import shutil
from _thread import LockType
from asyncio import CancelledError, current_task, sleep
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from os.path import normcase
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from curl_cffi.requests import Response

MAX_FILE_SIZE = 2 * 1024 ** 3
MAX_DOWNLOAD_TIME = 30 * 60
# Windows curl timeout options accept signed 32-bit milliseconds.
MAX_NATIVE_TIMEOUT_SECONDS = (2 ** 31 - 1) // 1000
MIN_FREE_SPACE = 256 * 1024 ** 2
WRITE_BUFFER_SIZE = 4 * 1024 ** 2
MAX_METADATA_SIZE = 16 * 1024 ** 2
_CACHE_OWNER_POLL_INTERVAL = 0.01
_CACHE_OWNER_GUARD = Lock()
_CACHE_OWNERS: dict[str, tuple[LockType, int]] = {}


@asynccontextmanager
async def cache_owner(temp: Path) -> AsyncIterator[None]:
    """Own a canonical cache across Download instances, threads and event loops."""
    key = normcase(str(temp.resolve()))
    with _CACHE_OWNER_GUARD:
        lock, users = _CACHE_OWNERS.get(key) or (Lock(), 0)
        _CACHE_OWNERS[key] = (lock, users + 1)
    owned = False
    try:
        while not owned:
            owned = lock.acquire(blocking=False)
            if not owned:
                await sleep(_CACHE_OWNER_POLL_INTERVAL)
        yield
    finally:
        if owned:
            lock.release()
        with _CACHE_OWNER_GUARD:
            _, users = _CACHE_OWNERS[key]
            if users == 1:
                del _CACHE_OWNERS[key]
            else:
                _CACHE_OWNERS[key] = (lock, users - 1)


class TransferLimitError(ValueError):
    """The transfer violates a local resource policy and must not be retried."""


def positive_limit(value: Any, default: int, maximum: int | None = None) -> int:
    if type(value) is not int or value <= 0:
        return default
    return value if maximum is None or value <= maximum else default


def check_size(size: int, maximum: int) -> int:
    if size > maximum:
        raise TransferLimitError(f"Transfer exceeds the {maximum}-byte size limit")
    return size


def content_total(headers: Mapping[str, str], completed: int, maximum: int) -> int | None:
    check_size(completed, maximum)
    length = headers.get("Content-Length")
    if length is None:
        return None
    try:
        remaining = int(length)
    except (TypeError, ValueError) as error:
        raise TransferLimitError("Invalid Content-Length") from error
    if remaining < 0:
        raise TransferLimitError("Negative Content-Length")
    return check_size(completed + remaining, maximum)


def check_disk_space(root: Path, minimum: int, pending: int = 0) -> None:
    if shutil.disk_usage(root).free - pending < minimum:
        raise TransferLimitError(f"Free disk space would fall below {minimum} bytes")


async def read_text(response: Response, maximum: int = MAX_METADATA_SIZE) -> str:
    """Count decoded transport bytes before buffering; keep the response charset."""
    body = bytearray()
    async for chunk in response.aiter_content():
        check_size(len(body) + len(chunk), maximum)
        body.extend(chunk)
        await sleep(0)
    try:
        return body.decode(response.encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8-sig", errors="replace")


@asynccontextmanager
async def preserve_transfer_failure(
    stream: AbstractAsyncContextManager[Response],
) -> AsyncIterator[Response]:
    """Do not let stream cleanup turn quotas, I/O failure or cancellation into retries."""
    task = current_task()
    cancellations = task.cancelling() if task is not None else 0
    primary: BaseException | None = None
    try:
        async with stream as response:
            try:
                yield response
            except (TransferLimitError, CancelledError, OSError) as error:
                primary = error
                raise
    except Exception as error:
        if task is not None and task.cancelling() > cancellations:
            raise CancelledError from error
        if primary is not None and error is not primary:
            raise primary from error
        raise

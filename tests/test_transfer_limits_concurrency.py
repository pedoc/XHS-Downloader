import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from test_transfer_limits import (
    MEDIA_URL,
    assert_failed_and_clean,
    make_response,
    run_download,
    transfer_context as transfer_context,
)

TEST_TIMEOUT = 30
pytestmark = pytest.mark.integration


async def run_one(downloader: Any, path: Path, filename: str = "offline") -> list[bool]:
    return await downloader.run(
        [MEDIA_URL], [None], None, path, filename, downloader_module(downloader)._("视频"),
        1700000000,
    )


def downloader_module(downloader: Any) -> Any:
    return import_module(type(downloader).__module__)


async def settle_tasks() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def stop_tasks(*tasks: asyncio.Task[Any] | None) -> None:
    active = [task for task in tasks if task is not None]
    for task in active:
        if not task.done():
            task.cancel()
    await asyncio.gather(*active, return_exceptions=True)


@pytest.fixture
def visible_cache(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch,
) -> Any:
    @asynccontextmanager
    async def open_file(path: Path, mode: str) -> AsyncIterator[Any]:
        with path.open(mode, buffering=0) as file:
            yield SimpleNamespace(
                read=AsyncMock(side_effect=file.read), write=AsyncMock(side_effect=file.write),
            )

    monkeypatch.setattr(transfer_context.module, "open", open_file)
    return transfer_context


@pytest.fixture
def concurrent_transfer(
    visible_cache: Any, monkeypatch: pytest.MonkeyPatch,
) -> Any:
    transfer_context = visible_cache
    context = SimpleNamespace(
        base=transfer_context, entered=[], responses=[], first_written=asyncio.Event(),
        release=asyncio.Event(), before_enter=None,
    )

    @asynccontextmanager
    async def stream(*args: Any, **options: Any) -> AsyncIterator[Any]:
        index = len(context.entered)
        if context.before_enter:
            context.before_enter(index)
        context.entered.append(options["headers"]["Range"])
        response = make_response(())
        context.responses.append(response)

        async def chunks(size: int | None = None) -> AsyncIterator[bytes]:
            if index == 0:
                yield b"abcd"
                context.first_written.set()
            await context.release.wait()
            yield b"efgh" if index == 0 else b"ABCDEFGH"

        response.aiter_content = chunks
        try:
            yield response
        finally:
            await response.aclose()

    monkeypatch.setattr(transfer_context.module, "checked_stream", stream)
    return context


@pytest.mark.parametrize("separate_instance", [False, True])
@pytest.mark.parametrize("different_destination", [False, True])
def test_same_cache_is_owned_across_runs_instances_and_destinations(
    concurrent_transfer: Any, separate_instance: bool, different_destination: bool,
) -> None:
    context, base = concurrent_transfer, concurrent_transfer.base
    first_path = base.manager.folder / "first"
    second_path = base.manager.folder / "second" if different_destination else first_path
    first_path.mkdir()
    second_path.mkdir(exist_ok=True)
    second = base.module.Download(base.manager) if separate_instance else base.downloader
    second.WRITE_BUFFER_SIZE = 4

    async def scenario() -> None:
        first_task = asyncio.create_task(run_one(base.downloader, first_path))
        second_task = None
        try:
            await asyncio.wait_for(context.first_written.wait(), TEST_TIMEOUT)
            second_task = asyncio.create_task(run_one(second, second_path))
            await settle_tasks()
            assert context.entered == ["bytes=0-"]
            assert len(base.deadlines) == 1
            context.release.set()
            assert await asyncio.wait_for(asyncio.gather(first_task, second_task), TEST_TIMEOUT) == [[True], [True]]
        finally:
            context.release.set()
            await stop_tasks(first_task, second_task)

    asyncio.run(scenario())
    assert context.entered == ["bytes=0-", "bytes=0-"]
    assert (second_path / "offline.mp4").read_bytes() == b"ABCDEFGH"
    if different_destination:
        assert (first_path / "offline.mp4").read_bytes() == b"abcdefgh"
    assert tuple(base.manager.temp.iterdir()) == ()


def test_cancelled_waiter_does_not_delete_the_owners_partial_cache(
    concurrent_transfer: Any,
) -> None:
    context, base = concurrent_transfer, concurrent_transfer.base

    async def scenario() -> None:
        first = asyncio.create_task(run_one(base.downloader, base.manager.folder))
        waiter = None
        try:
            await asyncio.wait_for(context.first_written.wait(), TEST_TIMEOUT)
            waiter = asyncio.create_task(run_one(base.downloader, base.manager.folder))
            await settle_tasks()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert (base.manager.temp / "offline.mp4").read_bytes() == b"abcd"
            assert context.entered == ["bytes=0-"]
            context.release.set()
            assert await asyncio.wait_for(first, TEST_TIMEOUT) == [True]
        finally:
            context.release.set()
            await stop_tasks(first, waiter)

    asyncio.run(scenario())
    assert (base.manager.folder / "offline.mp4").read_bytes() == b"abcdefgh"


def test_cancelled_holder_cleans_cache_before_the_next_owner_enters(
    concurrent_transfer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, base = concurrent_transfer, concurrent_transfer.base
    cleanup = []
    original_delete = base.manager.delete

    def delete(path: Path) -> None:
        original_delete(path)
        cleanup.append(path)

    def before_enter(index: int) -> None:
        if index:
            assert cleanup
            assert not (base.manager.temp / "offline.mp4").exists()

    monkeypatch.setattr(base.manager, "delete", delete)
    context.before_enter = before_enter

    async def scenario() -> None:
        first = asyncio.create_task(run_one(base.downloader, base.manager.folder))
        waiter = None
        try:
            await asyncio.wait_for(context.first_written.wait(), TEST_TIMEOUT)
            waiter = asyncio.create_task(run_one(base.downloader, base.manager.folder))
            await settle_tasks()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            context.release.set()
            assert await asyncio.wait_for(waiter, TEST_TIMEOUT) == [True]
        finally:
            context.release.set()
            await stop_tasks(first, waiter)

    asyncio.run(scenario())
    assert context.entered == ["bytes=0-", "bytes=0-"]
    assert (base.manager.folder / "offline.mp4").read_bytes() == b"ABCDEFGH"


def test_unrelated_cache_keys_can_enter_transfers_concurrently(concurrent_transfer: Any) -> None:
    context, base = concurrent_transfer, concurrent_transfer.base

    async def scenario() -> None:
        first = asyncio.create_task(run_one(base.downloader, base.manager.folder))
        second = None
        try:
            await asyncio.wait_for(context.first_written.wait(), TEST_TIMEOUT)
            second = asyncio.create_task(run_one(base.downloader, base.manager.folder, "different"))
            await settle_tasks()
            assert len(context.entered) == 2
            context.release.set()
            assert await asyncio.wait_for(asyncio.gather(first, second), TEST_TIMEOUT) == [[True], [True]]
        finally:
            context.release.set()
            await stop_tasks(first, second)

    asyncio.run(scenario())
    assert (base.manager.folder / "offline.mp4").read_bytes() == b"abcdefgh"
    assert (base.manager.folder / "different.mp4").read_bytes() == b"ABCDEFGH"


def test_actual_cache_size_is_rechecked_before_publication(transfer_context: Any) -> None:
    context = transfer_context
    context.events = []
    context.progress = context.events.append

    async def chunks(size: int | None = None) -> AsyncIterator[bytes]:
        yield b"abcd"
        with (context.manager.temp / "offline.mp4").open("ab") as file:
            file.write(b"EXTERNAL")
        yield b"efgh"

    context.response.aiter_content = chunks

    assert run_download(context) == [False]
    assert_failed_and_clean(context)


async def wait_for_thread_event(event: threading.Event) -> None:
    async with asyncio.timeout(TEST_TIMEOUT):
        while not event.is_set():
            await asyncio.sleep(0.001)


def thread_stream(
    first_written: threading.Event, release: threading.Event,
    entries: list[str], entry_guard: Any,
) -> Any:
    @asynccontextmanager
    async def stream(*args: Any, **options: Any) -> AsyncIterator[Any]:
        with entry_guard:
            index = len(entries)
            entries.append(options["headers"]["Range"])
        response = make_response(())

        async def chunks(size: int | None = None) -> AsyncIterator[bytes]:
            if index == 0:
                yield b"abcd"
                first_written.set()
            await wait_for_thread_event(release)
            yield b"efgh" if index == 0 else b"ABCDEFGH"

        response.aiter_content = chunks
        try:
            yield response
        finally:
            await response.aclose()

    return stream


def thread_worker(
    downloader: Any, path: Path, outcomes: Queue[Any], attempted: threading.Event | None,
) -> None:
    async def operation() -> list[bool]:
        task = asyncio.create_task(run_one(downloader, path))
        if attempted is not None:
            await settle_tasks()
            attempted.set()
        return await asyncio.wait_for(task, TEST_TIMEOUT)

    try:
        outcomes.put(asyncio.run(operation()))
    except BaseException as error:
        outcomes.put(error)


@pytest.mark.parametrize("separate_instance", [False, True])
def test_same_cache_is_owned_across_threads_with_different_event_loops(
    visible_cache: Any, monkeypatch: pytest.MonkeyPatch, separate_instance: bool,
) -> None:
    context = visible_cache
    first_written, release, attempted = threading.Event(), threading.Event(), threading.Event()
    entries: list[str] = []
    entry_guard = threading.Lock()
    outcomes: Queue[Any] = Queue()
    first_path, second_path = context.manager.folder / "first", context.manager.folder / "second"
    first_path.mkdir()
    second_path.mkdir()
    second = context.module.Download(context.manager) if separate_instance else context.downloader
    second.WRITE_BUFFER_SIZE = 4

    monkeypatch.setattr(
        context.module, "checked_stream", thread_stream(first_written, release, entries, entry_guard),
    )
    first = threading.Thread(
        target=thread_worker, args=(context.downloader, first_path, outcomes, None), daemon=True,
    )
    second_thread = threading.Thread(
        target=thread_worker, args=(second, second_path, outcomes, attempted), daemon=True,
    )
    first.start()

    async def scenario() -> None:
        await wait_for_thread_event(first_written)
        second_thread.start()
        await wait_for_thread_event(attempted)
        with entry_guard:
            assert entries == ["bytes=0-"]
        assert (context.manager.temp / "offline.mp4").read_bytes() == b"abcd"

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        first.join(TEST_TIMEOUT)
        if second_thread.ident is not None:
            second_thread.join(TEST_TIMEOUT)
    assert not first.is_alive() and not second_thread.is_alive()
    assert [outcomes.get_nowait(), outcomes.get_nowait()] == [[True], [True]]
    assert (first_path / "offline.mp4").read_bytes() == b"abcdefgh"
    assert (second_path / "offline.mp4").read_bytes() == b"ABCDEFGH"


@pytest.mark.unit
@pytest.mark.parametrize("alias", ["parent", "case", "unicode"])
def test_cache_owner_normalizes_aliases_and_recovers_after_waiter_cancellation(
    tmp_path: Path, alias: str,
) -> None:
    limits = import_module("source.module.limits")
    path = tmp_path / "cache" / "offline.mp4"
    path.parent.mkdir()
    if alias == "case":
        other = Path(str(path).swapcase())
    elif alias == "unicode":
        path = path.parent / "é.mp4"
        other = path.parent / "é.mp4"
    else:
        nested = path.parent / "nested"
        nested.mkdir()
        other = nested / ".." / path.name

    async def scenario() -> None:
        entered = asyncio.Event()

        async def wait() -> None:
            async with limits.cache_owner(other):
                entered.set()

        async with limits.cache_owner(path):
            for _ in range(3):
                waiter = asyncio.create_task(wait())
                await settle_tasks()
                assert not entered.is_set()
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
        async with limits.cache_owner(other):
            entered.set()
        assert entered.is_set()

    asyncio.run(scenario())

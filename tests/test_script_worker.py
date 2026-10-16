import asyncio
from importlib import import_module
from json import dumps
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

TEST_TIMEOUT = 2
PRIVATE_MARKER = "offline-cookie-and-note-must-not-be-logged"


@pytest.fixture
def script_module(
    source_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> ModuleType:
    monkeypatch.setattr(
        asyncio.BaseEventLoop,
        "create_server",
        AsyncMock(side_effect=AssertionError("Real listeners are forbidden")),
    )
    return import_module("source.module.script")


def make_server(module: ModuleType, process: AsyncMock | None = None) -> Any:
    core = SimpleNamespace(
        process_script_task=process if process is not None else AsyncMock(),
        logging=Mock(),
    )
    return module.ScriptServer(core)


async def cancel_tasks(*tasks: asyncio.Task[Any] | None) -> None:
    active = [task for task in tasks if task is not None]
    for task in active:
        task.cancel()
    await asyncio.gather(*active, return_exceptions=True)


@pytest.mark.unit
@pytest.mark.parametrize("result", [None, False])
def test_successful_jobs_are_acknowledged_and_queue_join_finishes(
    script_module: ModuleType,
    result: None | bool,
) -> None:
    async def run_worker() -> None:
        process = AsyncMock(return_value=result)
        server = make_server(script_module, process)
        job = {"data": {"noteId": "offline"}, "index": None}
        server.queue.put_nowait(job)
        worker = asyncio.create_task(server.worker_loop())
        try:
            await asyncio.wait_for(server.queue.join(), TEST_TIMEOUT)
            process.assert_awaited_once_with(**job)
            assert not worker.done()
            server.core.logging.assert_not_called()
        finally:
            await cancel_tasks(worker)

    asyncio.run(run_worker())


@pytest.mark.integration
def test_a_failed_job_is_logged_and_the_next_job_still_completes(
    script_module: ModuleType,
) -> None:
    async def run_worker() -> None:
        completed = asyncio.Event()

        async def process(data: dict[str, Any], index: Any) -> None:
            if data["noteId"] == "failed":
                raise RuntimeError(PRIVATE_MARKER)
            completed.set()

        server = make_server(script_module, AsyncMock(side_effect=process))
        jobs = [
            {"data": {"noteId": "failed"}, "index": None},
            {"data": {"noteId": "next"}, "index": [1]},
        ]
        for job in jobs:
            server.queue.put_nowait(job)
        worker = asyncio.create_task(server.worker_loop())
        try:
            await asyncio.wait_for(completed.wait(), TEST_TIMEOUT)
            await asyncio.wait_for(server.queue.join(), TEST_TIMEOUT)
            assert not worker.done()
            assert server.core.process_script_task.await_count == 2
            server.core.logging.assert_called_once()
            assert PRIVATE_MARKER not in str(server.core.logging.call_args)
        finally:
            await cancel_tasks(worker)

    asyncio.run(run_worker())


@pytest.mark.unit
def test_cancellation_propagates_without_logging_or_losing_the_active_job_ack(
    script_module: ModuleType,
) -> None:
    async def run_worker() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def process(**kwargs: Any) -> None:
            entered.set()
            await release.wait()

        server = make_server(script_module, AsyncMock(side_effect=process))
        server.queue.put_nowait({"data": {}, "index": None})
        worker = asyncio.create_task(server.worker_loop())
        try:
            await asyncio.wait_for(entered.wait(), TEST_TIMEOUT)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            await asyncio.wait_for(server.queue.join(), TEST_TIMEOUT)
            server.core.logging.assert_not_called()
        finally:
            await cancel_tasks(worker)

    asyncio.run(run_worker())


@pytest.mark.integration
def test_start_passes_explicit_frame_and_receive_queue_limits_to_fake_serve(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def start_and_stop() -> None:
        network_server = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        serve = AsyncMock(return_value=network_server)
        monkeypatch.setattr(script_module, "serve", serve)
        server = make_server(script_module)
        try:
            await server.start()
            serve.assert_awaited_once()
            args, kwargs = serve.await_args
            assert args == (server.handler, "127.0.0.1", 5558)
            assert kwargs["origins"] == server.ORIGINS
            assert 0 < kwargs["max_size"] <= 2 * 1024 * 1024
            assert 0 < kwargs["max_queue"] <= 32
        finally:
            await server.stop()
        network_server.close.assert_called_once_with()
        network_server.wait_closed.assert_awaited_once_with()

    asyncio.run(start_and_stop())


@pytest.mark.integration
def test_stop_cancels_the_worker_discards_pending_jobs_and_is_idempotent(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def start_and_stop() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def process(**kwargs: Any) -> None:
            entered.set()
            await release.wait()

        network_server = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        monkeypatch.setattr(script_module, "serve", AsyncMock(return_value=network_server))
        server = make_server(script_module, AsyncMock(side_effect=process))
        worker = joiner = None
        try:
            await server.start()
            worker = server.worker
            for _ in range(3):
                server.queue.put_nowait({"data": {}, "index": None})
            await asyncio.wait_for(entered.wait(), TEST_TIMEOUT)
            joiner = asyncio.create_task(server.queue.join())

            await asyncio.wait_for(server.stop(), TEST_TIMEOUT)
            await asyncio.wait_for(joiner, TEST_TIMEOUT)
            assert worker.cancelled()
            assert server.worker is None
            assert server.server is None
            assert server.queue.empty()
            assert server.core.process_script_task.await_count == 1
            await server.stop()
            network_server.close.assert_called_once_with()
            network_server.wait_closed.assert_awaited_once_with()
        finally:
            await cancel_tasks(worker, joiner)

    asyncio.run(start_and_stop())


@pytest.mark.integration
def test_stop_still_cancels_and_discards_work_when_server_close_fails(
    script_module: ModuleType,
) -> None:
    async def stop_after_failure() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def process(**kwargs: Any) -> None:
            entered.set()
            await release.wait()

        server = make_server(script_module, AsyncMock(side_effect=process))
        server.server = SimpleNamespace(
            close=Mock(), wait_closed=AsyncMock(side_effect=RuntimeError("close failed"))
        )
        for _ in range(2):
            server.queue.put_nowait({"data": {}, "index": None})
        worker = asyncio.create_task(server.worker_loop())
        server.worker = worker
        try:
            await asyncio.wait_for(entered.wait(), TEST_TIMEOUT)
            with pytest.raises(RuntimeError, match="close failed"):
                await asyncio.wait_for(server.stop(), TEST_TIMEOUT)
            await asyncio.wait_for(server.queue.join(), TEST_TIMEOUT)
            assert worker.cancelled()
            assert server.worker is None
            assert server.server is None
            assert server.queue.empty()
        finally:
            await cancel_tasks(worker)

    asyncio.run(stop_after_failure())


@pytest.mark.integration
def test_full_handler_and_server_close_do_not_wait_on_each_other(
    script_module: ModuleType,
) -> None:
    async def close_while_full() -> None:
        server = make_server(script_module)
        assert 1 <= server.queue.maxsize <= 64
        for _ in range(server.queue.maxsize):
            server.queue.put_nowait({"data": {}, "index": None})
        websocket = MagicMock()
        websocket.__aiter__.return_value = [dumps({"data": {"noteId": "extra"}})]
        websocket.close = AsyncMock()
        handler = asyncio.create_task(server.handler(websocket))

        async def wait_closed() -> None:
            await handler

        server.server = SimpleNamespace(close=Mock(), wait_closed=AsyncMock(side_effect=wait_closed))
        try:
            await asyncio.wait_for(server.stop(), TEST_TIMEOUT)
            await asyncio.wait_for(handler, TEST_TIMEOUT)
            await asyncio.wait_for(server.queue.join(), TEST_TIMEOUT)
            websocket.close.assert_awaited_once()
            assert websocket.close.await_args.kwargs["code"] == 1013
            assert server.queue.empty()
        finally:
            await cancel_tasks(handler)

    asyncio.run(close_while_full())


@pytest.mark.integration
def test_context_manager_starts_and_closes_only_the_fake_server(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def enter_and_exit() -> None:
        network_server = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        monkeypatch.setattr(script_module, "serve", AsyncMock(return_value=network_server))
        server = make_server(script_module)

        async with server as entered:
            assert entered is server
            assert server.worker is not None

        assert server.worker is None
        assert server.server is None
        network_server.close.assert_called_once_with()
        network_server.wait_closed.assert_awaited_once_with()

    asyncio.run(enter_and_exit())

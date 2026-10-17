import asyncio
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib import import_module
from os import stat_result
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from curl_cffi.requests import Headers

MEDIA_URL = "https://sns-video-bd.xhscdn.com/offline.mp4"
NOTE_URL = "https://www.xiaohongshu.com/explore/offline-note"
RELEASE_URL = "https://github.com/JoeanAmier/XHS-Downloader/releases/latest"
TEST_TIMEOUT = 30
pytestmark = pytest.mark.integration


def make_response(chunks: tuple[bytes, ...] = (b"abc", b"de")) -> Any:
    response = SimpleNamespace(
        status_code=200, headers=Headers(), url=NOTE_URL, encoding="utf-8",
        chunks=chunks, reads=0, hang=False, on_hang=None, started=asyncio.Event(),
        raise_for_status=Mock(), quit_now=None, astream_task=None,
        aclose=AsyncMock(),
    )

    async def iterate(size: int | None = None) -> AsyncIterator[bytes]:
        for chunk in response.chunks:
            response.reads += 1
            yield chunk
        response.started.set()
        if response.hang:
            if response.on_hang:
                response.on_hang()
            await asyncio.Event().wait()

    async def read_all() -> str:
        body = bytearray()
        async for chunk in iterate():
            body.extend(chunk)
        return body.decode(response.encoding, errors="replace")

    response.aiter_content = iterate
    response.atext = AsyncMock(side_effect=read_all)
    return response


def expire_deadline(context: Any) -> None:
    assert len(context.deadlines) == 1
    context.deadlines[0].reschedule(asyncio.get_running_loop().time() - 1)


def track_files(context: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def tracked(path: Path, mode: str) -> AsyncIterator[Any]:
        context.opens.append((path, mode))
        # Tiny temporary files; never race a real executor against a 20 ms timer.
        with path.open(mode) as file:
            if mode != "ab":
                yield SimpleNamespace(read=AsyncMock(side_effect=file.read))
                return

            async def write(data: bytes) -> int:
                context.writes.append(bytes(data))
                context.write_started.set()
                if context.write_hang:
                    expire_deadline(context)
                    await asyncio.Event().wait()
                return file.write(data)

            yield SimpleNamespace(write=write)

    monkeypatch.setattr(context.module, "open", tracked)


def track_transport(context: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    network = import_module("source.module.network")

    def budget(seconds: float) -> Any:
        assert seconds == context.downloader.max_download_time
        # Preserve real timeout cancellation, but expire it at a controlled stage.
        deadline = asyncio.timeout(None)
        context.deadlines.append(deadline)
        return deadline

    def factory(**options: Any) -> Any:
        client = type(context.manager.download_client)(**options)

        async def get(*args: Any, **kwargs: Any) -> Any:
            if context.expire_get_after and len(context.sessions) > context.expire_get_after:
                expire_deadline(context)
                await asyncio.Event().wait()
            if context.get_error:
                raise context.get_error
            return context.responses.pop(0) if context.responses else context.response

        client.get = AsyncMock(side_effect=get)
        context.sessions.append(client)
        return client

    monkeypatch.setattr(context.module, "timeout", budget, raising=False)
    monkeypatch.setattr(network, "AsyncSession", factory)
    monkeypatch.setattr(network, "public_addresses", context.dns)


@pytest.fixture
def transfer_context(
    manager_factory: Callable[..., Any], monkeypatch: pytest.MonkeyPatch,
) -> Any:
    module = import_module("source.application.download")
    request = import_module("source.application.request")
    manager = manager_factory(retry=2, write_mtime=True)
    manager.max_file_size, manager.max_download_time, manager.min_free_space = 8, 1, 4
    downloader = module.Download(manager)
    downloader.WRITE_BUFFER_SIZE = 4
    context = SimpleNamespace(
        module=module, request=request, manager=manager, downloader=downloader,
        response=make_response(), sessions=[], responses=[], opens=[], writes=[],
        disk=SimpleNamespace(free=1000), disk_paths=[], write_hang=False,
        write_started=asyncio.Event(), expire_get_after=0, get_error=None, deadlines=[],
    )

    def usage(path: Path) -> Any:
        context.disk_paths.append(Path(path))
        return context.disk

    context.dns = AsyncMock(return_value=("8.8.8.8",))
    track_transport(context, monkeypatch)
    monkeypatch.setattr(shutil, "disk_usage", usage)
    monkeypatch.setattr(request, "sleep_time", AsyncMock())
    monkeypatch.setattr(request, "MAX_METADATA_SIZE", 8, raising=False)
    track_files(context, monkeypatch)
    return context


async def download(context: Any) -> list[bool]:
    return await context.downloader.run(
        [MEDIA_URL], [None], None, context.manager.folder, "offline",
        context.module._("视频"), 1700000000,
        progress=context.progress if hasattr(context, "progress") else None,
    )


def run_download(context: Any) -> list[bool]:
    async def bounded() -> list[bool]:
        return await asyncio.wait_for(download(context), timeout=TEST_TIMEOUT)

    return asyncio.run(bounded())


def assert_failed_and_clean(context: Any) -> None:
    assert tuple(context.manager.temp.iterdir()) == ()
    assert tuple(context.manager.folder.iterdir()) == ()
    assert not any(
        event["state"] == "completed" for event in getattr(context, "events", ())
    )


@pytest.mark.parametrize("length", ["9", "2147483649", "-1", "not-a-number"])
def test_rejects_declared_size_before_opening_or_reading_body(
    transfer_context: Any, length: str,
) -> None:
    context = transfer_context
    context.response.headers["Content-Length"] = length
    context.events = []
    context.progress = context.events.append

    assert run_download(context) == [False]
    assert context.opens == [] and context.writes == []
    assert context.response.reads == 0
    assert len(context.sessions) == 1  # A policy rejection must not be retried.
    context.response.aclose.assert_awaited_once()
    context.sessions[0].close.assert_awaited_once()
    assert_failed_and_clean(context)


@pytest.mark.parametrize("declared", [None, "1"])
def test_unknown_or_false_length_stops_at_actual_quota_and_cleans_partial(
    transfer_context: Any, declared: str | None,
) -> None:
    context = transfer_context
    context.response.chunks = (b"abcd", b"efgh", b"i", b"never read")
    if declared is not None:
        context.response.headers["Content-Length"] = declared

    assert run_download(context) == [False]
    assert context.response.reads == 3
    assert b"".join(context.writes) == b"abcdefgh"
    assert len(context.sessions) == 1
    context.response.aclose.assert_awaited_once()
    assert_failed_and_clean(context)


def test_resume_checks_completed_bytes_before_dns(transfer_context: Any) -> None:
    context = transfer_context
    (context.manager.temp / "offline.mp4").write_bytes(b"overquota")

    assert run_download(context) == [False]
    context.dns.assert_not_awaited()
    assert context.sessions == [] and context.opens == []
    assert_failed_and_clean(context)


@pytest.mark.parametrize("declared", [None, "6"])
def test_resume_counts_partial_plus_new_body(transfer_context: Any, declared: str | None) -> None:
    context = transfer_context
    (context.manager.temp / "offline.mp4").write_bytes(b"abc")
    context.response.status_code = 206
    context.response.chunks = (b"defgh", b"i", b"never read")
    if declared:
        context.response.headers["Content-Length"] = declared

    assert run_download(context) == [False]
    assert context.sessions[0].headers["Range"] == "bytes=3-"
    assert context.response.reads == (0 if declared else 2)
    assert_failed_and_clean(context)


@pytest.mark.parametrize("low_root", ["temp", "download"])
def test_low_disk_is_rejected_before_network(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch, low_root: str,
) -> None:
    context = transfer_context
    root = context.manager.temp if low_root == "temp" else context.manager.folder

    def usage(path: Path) -> Any:
        context.disk_paths.append(Path(path))
        return SimpleNamespace(free=3 if Path(path) == root else 1000)

    monkeypatch.setattr(shutil, "disk_usage", usage)
    assert run_download(context) == [False]
    assert root in context.disk_paths
    context.dns.assert_not_awaited()
    assert context.sessions == [] and context.opens == []
    assert_failed_and_clean(context)


def test_known_length_checks_space_before_opening_file(transfer_context: Any) -> None:
    context = transfer_context
    context.disk.free = 8  # Five bytes plus a four-byte reserve do not fit.
    context.response.headers["Content-Length"] = "5"

    assert run_download(context) == [False]
    assert context.response.reads == 0 and context.opens == []
    assert len(context.sessions) == 1
    assert_failed_and_clean(context)


def test_disk_reserve_is_rechecked_before_every_flush(transfer_context: Any) -> None:
    context = transfer_context

    async def chunks(size: int | None = None) -> AsyncIterator[bytes]:
        yield b"abcd"
        context.disk.free = 7  # Below reserve + the next four-byte write.
        yield b"efgh"
        pytest.fail("Disk policy must stop consumption")

    context.response.aiter_content = chunks
    assert run_download(context) == [False]
    assert context.writes == [b"abcd"]
    assert set(context.disk_paths) == {context.manager.temp, context.manager.folder}
    assert_failed_and_clean(context)


@pytest.mark.parametrize("stage", ["dns", "body", "write"])
def test_download_deadline_covers_dns_body_and_file_writes(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    context = transfer_context
    context.response.chunks = (b"abcd",)
    if stage == "dns":
        async def stalled_dns(host: str) -> tuple[str, ...]:
            expire_deadline(context)
            await asyncio.Event().wait()
            return ()

        monkeypatch.setattr(import_module("source.module.network"), "public_addresses", stalled_dns)
    elif stage == "body":
        context.response.hang = True
        context.response.on_hang = lambda: expire_deadline(context)
    else:
        context.write_hang = True

    assert run_download(context) == [False]
    assert context.deadlines[0].expired()
    if context.sessions:
        context.response.aclose.assert_awaited_once()
        context.sessions[0].close.assert_awaited_once()
    assert_failed_and_clean(context)


def test_all_redirects_share_one_download_deadline(transfer_context: Any) -> None:
    context = transfer_context
    context.expire_get_after = 1
    first = make_response(())
    first.status_code = 302
    first.headers["Location"] = "https://sns-video-bd.xhscdn.com/final.mp4"
    context.responses = [first, context.response]

    assert run_download(context) == [False]
    first.aclose.assert_awaited_once()
    assert context.response.reads == 0
    assert len(context.sessions) == 2 and context.deadlines[0].expired()
    assert all(client.close.await_count == 1 for client in context.sessions)
    assert_failed_and_clean(context)


def test_retries_do_not_reset_total_download_deadline(transfer_context: Any) -> None:
    context = transfer_context
    context.expire_get_after = 1
    context.get_error = context.module.RequestException("offline transfer failure")

    assert run_download(context) == [False]
    assert len(context.sessions) == 2 and context.deadlines[0].expired()
    assert all(client.close.await_count == 1 for client in context.sessions)
    assert_failed_and_clean(context)


def test_waiting_for_download_slot_does_not_spend_the_deadline(transfer_context: Any) -> None:
    context = transfer_context
    gate = asyncio.Semaphore(0)
    context.downloader.SEMAPHORE = gate

    async def scenario() -> list[bool]:
        task = asyncio.create_task(download(context))
        await asyncio.sleep(0)
        assert not task.done() and not context.sessions and not context.deadlines
        gate.release()
        return await asyncio.wait_for(task, timeout=TEST_TIMEOUT)

    assert asyncio.run(scenario()) == [True]


@pytest.mark.parametrize("partial", [b"", b"abc"])
def test_small_media_and_resume_keep_bytes_range_mtime_and_media_timeout(
    transfer_context: Any, partial: bytes,
) -> None:
    context = transfer_context
    if partial:
        (context.manager.temp / "offline.mp4").write_bytes(partial)
        context.response.status_code = 206
    context.manager.timeout = 0.001  # HTML timeout must not be imposed on media.
    context.response.headers["Content-Length"] = "5"
    context.events = []
    context.progress = context.events.append

    assert run_download(context) == [True]
    file = context.manager.folder / "offline.mp4"
    assert file.read_bytes() == partial + b"abcde"
    assert int(file.stat().st_mtime) == 1700000000
    assert context.sessions[0].headers["Range"] == f"bytes={len(partial)}-"
    assert context.sessions[0].options["timeout"] == context.downloader.max_download_time
    assert max(map(len, context.writes)) <= 4
    assert context.events[-1]["state"] == "completed"
    assert tuple(context.manager.temp.iterdir()) == ()
    context.response.aclose.assert_awaited_once()


def test_one_native_chunk_is_split_into_bounded_writes(transfer_context: Any) -> None:
    context = transfer_context
    context.response.chunks = (b"abcdefgh",)
    assert run_download(context) == [True]
    assert context.writes == [b"abcd", b"efgh"]


def test_cancellation_propagates_after_stream_closure_and_partial_cleanup(transfer_context: Any) -> None:
    context = transfer_context
    context.response.chunks, context.response.hang = (b"abcd",), True

    async def scenario() -> None:
        task = asyncio.create_task(download(context))
        await asyncio.wait_for(context.response.started.wait(), timeout=TEST_TIMEOUT)
        assert context.writes == [b"abcd"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    context.response.aclose.assert_awaited_once()
    context.sessions[0].close.assert_awaited_once()
    assert_failed_and_clean(context)


def test_cancel_keeps_original_error_when_cache_cleanup_is_denied(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = transfer_context
    context.response.chunks, context.response.hang = (b"abcd",), True
    monkeypatch.setattr(context.manager, "delete", Mock(side_effect=PermissionError("offline locked cache")))
    log = Mock()
    monkeypatch.setattr(context.module, "logging", log)

    async def scenario() -> None:
        task = asyncio.create_task(download(context))
        await asyncio.wait_for(context.response.started.wait(), timeout=TEST_TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    cache = context.manager.temp / "offline.mp4"
    assert cache.read_bytes() == b"abcd"
    assert any(str(cache) in call.args[1] for call in log.call_args_list)
    context.response.aclose.assert_awaited_once()
    assert not tuple(context.manager.folder.iterdir())


@pytest.mark.e2e
@pytest.mark.parametrize("cross_volume", [False, True])
def test_small_media_uses_real_aiofiles_with_temporary_bytes(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch, cross_volume: bool,
) -> None:
    from aiofiles import open as async_open

    context = transfer_context
    if cross_volume:
        fake_cross_volume(context, monkeypatch)
    monkeypatch.setattr(context.module, "open", async_open)
    assert run_download(context) == [True]
    assert (context.manager.folder / "offline.mp4").read_bytes() == b"abcde"


@pytest.mark.e2e
def test_policy_failure_cannot_create_successful_download_record(
    transfer_context: Any, source_modules: Any,
) -> None:
    context = transfer_context
    context.response.headers["Content-Length"] = "9"
    core = object.__new__(source_modules.app.XHS)
    core.manager, core.downloader = context.manager, context.downloader
    core.id_recorder = SimpleNamespace(add=AsyncMock())
    core.note_generator = SimpleNamespace(save_note_info=AsyncMock())
    core.save_data, core.logging = AsyncMock(), Mock()
    data = {
        "作者ID": "author", "作者昵称": "offline", "作品ID": "note",
        "作品类型": context.module._("视频"), "时间戳": 1700000000,
        "下载地址": [MEDIA_URL], "动图地址": [None], "封面地址": None,
    }
    core._XHS__naming_rules = Mock(return_value="offline")
    count = SimpleNamespace(success=0, fail=0, skip=0)
    asyncio.run(core._XHS__download_files(data, True, None, count))
    core.id_recorder.add.assert_not_awaited()
    assert (count.success, count.fail, count.skip) == (0, 1, 0)
    assert_failed_and_clean(context)


@pytest.mark.parametrize("compressed", [False, True])
def test_metadata_counts_decoded_chunks_and_never_uses_unbounded_atext(
    transfer_context: Any, compressed: bool,
) -> None:
    context = transfer_context
    context.response.chunks = (b"abcd", b"efgh", b"i", b"never read")
    if compressed:
        context.response.headers.update({"Content-Encoding": "gzip", "Content-Length": "1"})
    html = context.request.Html(context.manager)

    assert asyncio.run(html.request_url(NOTE_URL)) == ""
    assert context.response.reads == 3
    assert len(context.sessions) == 1
    context.response.atext.assert_not_awaited()
    context.response.aclose.assert_awaited_once()


@pytest.mark.parametrize("encoding,text", [("utf-8", "中文"), ("gb18030", "中文"), ("latin-1", "café")])
def test_metadata_respects_response_encoding_across_chunk_boundaries(
    transfer_context: Any, encoding: str, text: str,
) -> None:
    context = transfer_context
    payload = text.encode(encoding)
    context.response.encoding = encoding
    context.response.chunks = (payload[:1], payload[1:])
    html = context.request.Html(context.manager)

    assert asyncio.run(html.request_url(NOTE_URL)) == text
    context.response.atext.assert_not_awaited()
    context.response.aclose.assert_awaited_once()


@pytest.mark.parametrize("retries", [0, 2])
def test_metadata_stall_is_bounded_by_existing_html_budget(transfer_context: Any, retries: int) -> None:
    context = transfer_context
    context.manager.retry, context.manager.timeout = retries, 0.02
    context.response.chunks, context.response.hang = (b"a",), True
    html = context.request.Html(context.manager)

    async def scenario() -> str:
        return await asyncio.wait_for(html.request_url(NOTE_URL), timeout=TEST_TIMEOUT)

    assert asyncio.run(scenario()) == ""
    context.response.aclose.assert_awaited_once()
    context.sessions[0].close.assert_awaited_once()


def test_continuous_empty_metadata_chunks_still_observe_html_deadline(transfer_context: Any) -> None:
    context = transfer_context
    context.manager.retry, context.manager.timeout = 0, 0.02

    async def continuous(size: int | None = None) -> AsyncIterator[bytes]:
        while True:
            yield b""
            await asyncio.sleep(0)

    context.response.aiter_content = continuous
    context.response.atext = AsyncMock(side_effect=lambda: "old unbounded path")
    html = context.request.Html(context.manager)
    assert asyncio.run(html.request_url(NOTE_URL)) == ""
    context.response.aclose.assert_awaited_once()


def test_metadata_cancellation_is_not_swallowed(transfer_context: Any) -> None:
    context = transfer_context
    context.response.chunks, context.response.hang = (), True
    html = context.request.Html(context.manager)

    async def scenario() -> None:
        task = asyncio.create_task(html.request_url(NOTE_URL))
        await asyncio.wait_for(context.response.started.wait(), timeout=TEST_TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    context.response.aclose.assert_awaited_once()
    context.sessions[0].close.assert_awaited_once()


@pytest.mark.parametrize("content", [False, True])
def test_public_update_url_only_does_not_consume_body(transfer_context: Any, content: bool) -> None:
    context = transfer_context
    context.response.url, context.response.hang = RELEASE_URL, True
    context.response.chunks = (b"metadata need not be read",)
    html = context.request.Html(context.manager)

    assert asyncio.run(html.request_url(RELEASE_URL, content, purpose="update")) == RELEASE_URL
    assert context.response.reads == 0
    context.response.atext.assert_not_awaited()
    context.response.aclose.assert_awaited_once()


@pytest.mark.unit
def test_limit_defaults_and_json_values_are_forwarded_to_manager(
    source_modules: Any, manager_factory: Callable[..., Any],
) -> None:
    defaults = source_modules.settings.Settings.default
    assert defaults["max_file_size"] == 2 * 1024 ** 3
    assert defaults["max_download_time"] == 30 * 60
    assert defaults["min_free_space"] == 256 * 1024 ** 2
    module = import_module("source.application.download")
    assert module.Download.WRITE_BUFFER_SIZE == 4 * 1024 ** 2
    assert import_module("source.module.limits").MAX_METADATA_SIZE == 16 * 1024 ** 2
    manager_factory()  # Installs the existing offline AsyncSession stub.
    core = source_modules.app.XHS(max_file_size=123, max_download_time=45, min_free_space=67)
    assert (core.manager.max_file_size, core.manager.max_download_time, core.manager.min_free_space) == (123, 45, 67)
    assert (core.downloader.max_file_size, core.downloader.max_download_time, core.downloader.min_free_space) == (123, 45, 67)


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, True, 0, -1, 1.5, "8", float("inf")])
def test_invalid_json_limits_use_safe_defaults(
    manager_factory: Callable[..., Any], source_modules: Any, value: Any,
) -> None:
    manager = manager_factory(max_file_size=value, max_download_time=value, min_free_space=value)
    defaults = source_modules.settings.Settings.default
    for name in ("max_file_size", "max_download_time", "min_free_space"):
        assert getattr(manager, name) == defaults[name]


@pytest.mark.unit
@pytest.mark.parametrize("completed,length,expected", [(0, None, None), (3, "5", 8), (8, "0", 8)])
def test_content_total_accepts_absent_exact_and_zero_lengths(completed: int, length: str | None, expected: int | None) -> None:
    limits = import_module("source.module.limits")
    headers = Headers({"Content-Length": length}) if length is not None else Headers()
    assert limits.content_total(headers, completed, 8) == expected


@pytest.mark.unit
@pytest.mark.parametrize("encoding", ["utf-8", "not-a-codec"])
def test_bounded_text_reader_handles_empty_and_unknown_encoding(encoding: str) -> None:
    limits = import_module("source.module.limits")
    response = make_response((b"\xef\xbb\xbfOK",) if encoding == "not-a-codec" else ())
    response.encoding = encoding
    assert asyncio.run(limits.read_text(response, 8)) == ("OK" if encoding == "not-a-codec" else "")
    assert issubclass(limits.TransferLimitError, ValueError)


def fake_cross_volume(context: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.stat

    def stat(path: Path, *args: Any, **kwargs: Any) -> Any:
        result = original(path, *args, **kwargs)
        if path == context.manager.folder:
            fields = list(result)
            fields[2] += 1
            return stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", stat)


def fail_copy(source: Any, target: Any, *args: Any) -> None:
    if isinstance(target, Path):
        target.write_bytes(b"ab")
    else:
        target.write(b"ab")
    raise OSError("offline disk full during destination copy")


def test_failed_cross_volume_commit_keeps_complete_cache_and_no_final_partial(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = transfer_context
    manager_module = import_module("source.module.manager")
    fake_cross_volume(context, monkeypatch)
    monkeypatch.setattr(manager_module, "move", fail_copy)
    monkeypatch.setattr(manager_module, "copyfileobj", fail_copy, raising=False)

    assert run_download(context) == [False]
    cache = context.manager.temp / "offline.mp4"
    assert cache.read_bytes() == b"abcde"
    assert tuple(context.manager.folder.iterdir()) == ()
    assert len(context.sessions) == 1
    assert not context.downloader._Download__check_exists_path(context.manager.folder, "offline.mp4")


def test_failed_cross_volume_commit_does_not_damage_existing_finished_file(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = transfer_context
    module = import_module("source.module.manager")
    source, destination = context.manager.temp / "source.mp4", context.manager.folder / "old.mp4"
    source.write_bytes(b"complete source")
    destination.write_bytes(b"finished media")
    fake_cross_volume(context, monkeypatch)
    monkeypatch.setattr(module, "move", fail_copy)
    monkeypatch.setattr(module, "copyfileobj", fail_copy, raising=False)
    with pytest.raises(OSError):
        context.manager.move(source, destination)
    assert source.read_bytes() == b"complete source"
    assert destination.read_bytes() == b"finished media"
    assert set(context.manager.folder.iterdir()) == {destination}


def test_cross_volume_copy_rechecks_disk_reserve_before_each_buffer_write(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = transfer_context
    module = import_module("source.module.manager")
    fake_cross_volume(context, monkeypatch)

    def copy_with_pressure(source: Any, target: Any, size: int) -> None:
        target.write(source.read(2))
        context.disk.free = 4
        target.write(source.read(3))

    monkeypatch.setattr(module, "copyfileobj", copy_with_pressure)
    assert run_download(context) == [False]
    assert len(context.sessions) == 1
    assert_failed_and_clean(context)


def stall_first_get(context: Any, monkeypatch: pytest.MonkeyPatch, expire: bool) -> None:
    network = import_module("source.module.network")
    original = network.AsyncSession

    def factory(**options: Any) -> Any:
        client = original(**options)
        if len(context.sessions) == 1:
            async def get(*args: Any, **kwargs: Any) -> Any:
                context.responses.pop(0)
                context.response.started.set()
                if expire:
                    expire_deadline(context)
                await asyncio.Event().wait()

            client.get = AsyncMock(side_effect=get)
            client.close = AsyncMock(side_effect=context.module.RequestException("offline close failure"))
        return client

    monkeypatch.setattr(network, "AsyncSession", factory)


@pytest.mark.parametrize("kind,failure", [
    ("media", "quota"), ("html", "quota"), ("media", "cancel"), ("html", "cancel"),
    ("media", "deadline"), ("media", "enter-cancel"), ("html", "enter-cancel"),
    ("media", "enter-deadline"),
])
def test_cleanup_failure_cannot_override_quota_cancel_or_expired_deadline(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch, kind: str, failure: str,
) -> None:
    context = transfer_context
    first, second = context.response, make_response()
    first.chunks = (b"abcdefghi",) if failure == "quota" else (b"abcd",)
    first.hang = failure != "quota"
    if kind == "media" and failure == "quota":
        first.headers["Content-Length"] = "9"
    context.responses = [first, second]
    if failure.startswith("enter-"):
        stall_first_get(context, monkeypatch, expire=failure == "enter-deadline")
    elif failure == "deadline":
        first.on_hang = lambda: expire_deadline(context)

    async def scenario() -> Any:
        if not failure.startswith("enter-"):
            first.astream_task = asyncio.get_running_loop().create_future()
            first.astream_task.set_exception(context.module.RequestException("offline failed stream task"))
        operation = download(context) if kind == "media" else context.request.Html(context.manager).request_url(NOTE_URL)
        if "cancel" in failure:
            task = asyncio.create_task(operation)
            await asyncio.wait_for(first.started.wait(), timeout=TEST_TIMEOUT)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return None
        return await asyncio.wait_for(operation, timeout=TEST_TIMEOUT)

    result = asyncio.run(scenario())
    if "cancel" not in failure:
        assert result == ([False] if kind == "media" else "")
    assert len(context.sessions) == 1 and second.reads == 0
    if "deadline" in failure:
        assert context.deadlines[0].expired()
    context.sessions[0].close.assert_awaited_once()
    assert_failed_and_clean(context)


def test_cleanup_cancellation_takes_priority_over_a_quota_failure() -> None:
    limits = import_module("source.module.limits")

    @asynccontextmanager
    async def close_cancelled() -> AsyncIterator[Any]:
        try:
            yield make_response()
        finally:
            raise asyncio.CancelledError

    async def scenario() -> None:
        async with limits.preserve_transfer_failure(close_cancelled()):
            raise limits.TransferLimitError("offline quota")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())


def test_tui_save_preserves_json_only_limits_without_new_controls(source_modules: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    setting_module = import_module("source.TUI.setting")
    data = source_modules.settings.Settings.default | {
        "max_file_size": 123, "max_download_time": 45, "min_free_space": 67,
        "future_setting": "preserve", "folder_name": "original",
    }
    setting = setting_module.Setting(data)
    values = data | {"folder_name": "changed"}
    query = Mock(side_effect=lambda selector: SimpleNamespace(value=values[selector[1:]]))
    dismiss = Mock()
    monkeypatch.setattr(setting, "query_one", query)
    monkeypatch.setattr(setting, "dismiss", dismiss)
    setting.save_settings()
    saved = dismiss.call_args.args[0]
    assert saved["folder_name"] == "changed" and data["folder_name"] == "original"
    for key in ("max_file_size", "max_download_time", "min_free_space", "future_setting"):
        assert saved[key] == data[key]
        assert all(call.args != (f"#{key}",) for call in query.call_args_list)


def test_metadata_exact_size_limit_is_accepted(transfer_context: Any) -> None:
    context = transfer_context
    context.response.chunks = (b"abcd", b"efgh")
    html = context.request.Html(context.manager)

    assert asyncio.run(html.request_url(NOTE_URL)) == "abcdefgh"
    context.response.atext.assert_not_awaited()
    context.response.aclose.assert_awaited_once()


@pytest.mark.parametrize("declared", [None, "5"])
def test_exact_disk_reserve_allows_media(
    transfer_context: Any, declared: str | None,
) -> None:
    context = transfer_context
    context.disk.free = 9  # Five media bytes leave exactly the four-byte reserve.
    if declared is not None:
        context.response.headers["Content-Length"] = declared

    assert run_download(context) == [True]
    assert (context.manager.folder / "offline.mp4").read_bytes() == b"abcde"
    assert set(context.disk_paths) == {context.manager.temp, context.manager.folder}

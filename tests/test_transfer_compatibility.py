import asyncio
import os
import shutil
from importlib import import_module
from pathlib import Path
from stat import S_IMODE
from typing import Any

import pytest
from curl_cffi import Curl, CurlOpt

from test_transfer_limits import (
    expire_deadline,
    fake_cross_volume,
    make_response,
    run_download,
    transfer_context as transfer_context,
)

MAX_NATIVE_TIMEOUT_SECONDS = 2147483
SOURCE_MTIME = 946684800
pytestmark = pytest.mark.integration


@pytest.mark.unit
@pytest.mark.parametrize("seconds,expected", [
    (1, 1), (MAX_NATIVE_TIMEOUT_SECONDS, MAX_NATIVE_TIMEOUT_SECONDS),
    (MAX_NATIVE_TIMEOUT_SECONDS + 1, 1800), (10 ** 400, 1800),
])
def test_media_timeout_rejects_values_beyond_native_millisecond_range(
    manager_factory: Any, seconds: int, expected: int,
) -> None:
    manager = manager_factory(max_download_time=seconds)
    downloader = import_module("source.application.download").Download(manager)

    assert manager.max_download_time == expected
    assert downloader.max_download_time == expected


@pytest.mark.unit
@pytest.mark.parametrize("value", [MAX_NATIVE_TIMEOUT_SECONDS + 1, 10 ** 400])
def test_byte_limits_are_not_restricted_by_the_native_timeout_range(
    manager_factory: Any, value: int,
) -> None:
    manager = manager_factory(max_file_size=value, min_free_space=value)

    assert manager.max_file_size == value
    assert manager.min_free_space == value


@pytest.mark.unit
def test_largest_supported_timeout_is_accepted_without_network_access(
    manager_factory: Any,
) -> None:
    manager = manager_factory(max_download_time=MAX_NATIVE_TIMEOUT_SECONDS)
    handle = Curl()
    try:
        handle.setopt(CurlOpt.CONNECTTIMEOUT_MS, manager.max_download_time * 1000)
    finally:
        handle.close()

    async def enter_budget() -> None:
        async with asyncio.timeout(manager.max_download_time):
            pass

    asyncio.run(enter_budget())


def test_cross_volume_move_preserves_source_mtime_and_permissions(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = transfer_context
    module = import_module("source.module.manager")
    source = context.manager.temp / "source.mp4"
    destination = context.manager.folder / "finished.mp4"
    source.write_bytes(b"complete media")
    os.utime(source, (SOURCE_MTIME, SOURCE_MTIME))
    if os.name != "nt":
        source.chmod(0o640)
    source_mode = S_IMODE(source.stat().st_mode)
    copied = []

    def copy_metadata(origin: Path, staging: Path) -> None:
        assert origin == source and staging != destination
        assert staging.parent == destination.parent and staging.name.startswith(".xhs-")
        copied.append(staging)
        shutil.copystat(origin, staging)

    fake_cross_volume(context, monkeypatch)
    monkeypatch.setattr(module, "copystat", copy_metadata, raising=False)

    context.manager.move(source, destination, rewrite=False)

    assert len(copied) == 1
    assert destination.read_bytes() == b"complete media"
    assert int(destination.stat().st_mtime) == SOURCE_MTIME
    if os.name != "nt":
        assert S_IMODE(destination.stat().st_mode) == source_mode
    assert not source.exists()


def preserve_finished_file_during_copy(
    context: Any, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    module = import_module("source.module.manager")
    destination = context.manager.folder / "offline.mp4"
    original = module.copyfileobj

    def copy(source: Any, target: Any, size: int) -> None:
        original(source, target, size)
        destination.write_bytes(b"existing finished media")

    monkeypatch.setattr(module, "copyfileobj", copy)
    return destination


def test_cross_volume_metadata_failure_retains_cache_and_existing_destination(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = transfer_context
    module = import_module("source.module.manager")
    fake_cross_volume(context, monkeypatch)
    destination = preserve_finished_file_during_copy(context, monkeypatch)
    context.events = []
    context.progress = context.events.append

    def fail_metadata(source: Path, staging: Path) -> None:
        assert staging != destination
        raise OSError("offline metadata copy failure")

    monkeypatch.setattr(module, "copystat", fail_metadata, raising=False)

    assert run_download(context) == [False]
    assert (context.manager.temp / "offline.mp4").read_bytes() == b"abcde"
    assert destination.read_bytes() == b"existing finished media"
    assert set(context.manager.folder.iterdir()) == {destination}
    assert len(context.sessions) == 1
    assert not any(event["state"] == "completed" for event in context.events)


def test_deadline_expiring_during_metadata_copy_cannot_replace_a_finished_file(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = transfer_context
    module = import_module("source.module.manager")
    fake_cross_volume(context, monkeypatch)
    destination = preserve_finished_file_during_copy(context, monkeypatch)
    context.events = []
    context.progress = context.events.append

    def expire_after_metadata(source: Path, staging: Path) -> None:
        shutil.copystat(source, staging)
        expire_deadline(context)

    monkeypatch.setattr(module, "copystat", expire_after_metadata, raising=False)

    assert run_download(context) == [False]
    assert tuple(context.manager.temp.iterdir()) == ()
    assert destination.read_bytes() == b"existing finished media"
    assert set(context.manager.folder.iterdir()) == {destination}
    assert len(context.sessions) == 1
    assert not any(event["state"] == "completed" for event in context.events)


def test_repeated_actual_quota_failures_release_cache_ownership_for_a_later_success(
    transfer_context: Any,
) -> None:
    context = transfer_context
    context.response.headers["Content-Length"] = "1"
    context.response.chunks = (b"abcd", b"efgh", b"i", b"never read")

    for _ in range(5):
        assert run_download(context) == [False]
        assert tuple(context.manager.temp.iterdir()) == ()
    context.response = make_response()
    assert run_download(context) == [True]
    assert (context.manager.folder / "offline.mp4").read_bytes() == b"abcde"

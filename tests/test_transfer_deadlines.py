import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from test_transfer_limits import (
    assert_failed_and_clean,
    expire_deadline,
    fake_cross_volume,
    run_download,
    transfer_context as transfer_context,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("stage", ["before_write", "between_writes", "after_copy"])
def test_cross_volume_copy_cannot_publish_after_total_deadline(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    context = transfer_context
    module = import_module("source.module.manager")
    fake_cross_volume(context, monkeypatch)
    context.events = []
    context.progress = context.events.append

    def copy_with_expiry(source: Any, target: Any, size: int) -> None:
        if stage == "before_write":
            expire_deadline(context)
        target.write(source.read(2))
        if stage == "between_writes":
            expire_deadline(context)
        target.write(source.read())
        if stage == "after_copy":
            expire_deadline(context)

    monkeypatch.setattr(module, "copyfileobj", copy_with_expiry)

    assert run_download(context) == [False]
    assert len(context.sessions) == 1
    context.response.aclose.assert_awaited_once()
    context.sessions[0].close.assert_awaited_once()
    assert_failed_and_clean(context)


def test_expired_copy_keeps_a_finished_file_created_before_publication(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = transfer_context
    module = import_module("source.module.manager")
    fake_cross_volume(context, monkeypatch)
    destination = context.manager.folder / "offline.mp4"
    context.events = []
    context.progress = context.events.append

    def copy_with_expiry(source: Any, target: Any, size: int) -> None:
        target.write(source.read(2))
        destination.write_bytes(b"finished media")
        expire_deadline(context)
        target.write(source.read())

    monkeypatch.setattr(module, "copyfileobj", copy_with_expiry)

    assert run_download(context) == [False]
    assert destination.read_bytes() == b"finished media"
    assert set(context.manager.folder.iterdir()) == {destination}
    assert tuple(context.manager.temp.iterdir()) == ()
    assert not any(event["state"] == "completed" for event in context.events)


def test_deadline_expiring_after_suffix_check_prevents_same_volume_publication(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = transfer_context
    suffix = context.downloader._Download__suffix_with_file
    context.events = []
    context.progress = context.events.append

    async def expire_after_suffix(*args: Any) -> Any:
        destination = await suffix(*args)
        expire_deadline(context)
        return destination

    monkeypatch.setattr(context.downloader, "_Download__suffix_with_file", expire_after_suffix)

    assert run_download(context) == [False]
    assert_failed_and_clean(context)


def test_copy_observes_requested_cancellation_before_publication(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = transfer_context
    module = import_module("source.module.manager")
    fake_cross_volume(context, monkeypatch)
    context.events = []
    context.progress = context.events.append

    def copy_then_cancel(source: Any, target: Any, size: int) -> None:
        target.write(source.read(2))
        asyncio.current_task().cancel()
        target.write(source.read())

    monkeypatch.setattr(module, "copyfileobj", copy_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        run_download(context)
    assert_failed_and_clean(context)


def copy_with_primary_failure(
    context: Any, failure: str, source: Any, target: Any, size: int,
) -> None:
    target.write(source.read(2))
    if failure == "cancel":
        raise asyncio.CancelledError
    if failure == "timeout":
        expire_deadline(context)
        raise TimeoutError("offline expired commit")
    raise context.module.TransferLimitError("offline commit quota")


@pytest.mark.parametrize("failure", ["timeout", "quota", "cancel"])
@pytest.mark.parametrize("cleanup", ["close", "unlink"])
def test_commit_cleanup_preserves_primary_policy_or_cancellation_failure(
    transfer_context: Any, monkeypatch: pytest.MonkeyPatch,
    failure: str, cleanup: str,
) -> None:
    context = transfer_context
    module = import_module("source.module.manager")
    fake_cross_volume(context, monkeypatch)
    original_file, original_unlink = module.NamedTemporaryFile, Path.unlink
    staging = []
    context.events = []
    context.progress = context.events.append
    log = Mock()
    monkeypatch.setattr(module, "logging", log)

    @contextmanager
    def output(**options: Any) -> Iterator[Any]:
        with original_file(**options) as file:
            staging.append(Path(file.name))
            try:
                yield file
            finally:
                if cleanup == "close":
                    raise OSError("offline staging close failure")

    def unlink(path: Path, *args: Any, **options: Any) -> None:
        if path in staging:
            raise OSError("offline staging unlink failure")
        original_unlink(path, *args, **options)

    monkeypatch.setattr(module, "NamedTemporaryFile", output)
    monkeypatch.setattr(module, "copyfileobj", partial(copy_with_primary_failure, context, failure))
    if cleanup == "unlink":
        monkeypatch.setattr(Path, "unlink", unlink)

    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            run_download(context)
    else:
        assert run_download(context) == [False]
    assert tuple(context.manager.temp.iterdir()) == ()
    assert not (context.manager.folder / "offline.mp4").exists()
    assert not any(event["state"] == "completed" for event in context.events)
    assert len(context.sessions) == 1
    assert any(str(staging[0]) in call.args[1] for call in log.call_args_list)

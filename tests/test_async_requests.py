import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

NOTE_URL = "https://www.xiaohongshu.com/explore/offline-note"


def gated_cookie_clients(context: SimpleNamespace, expected: int = 1) -> SimpleNamespace:
    entered = asyncio.Event()
    release = asyncio.Event()
    create = context.cookie_factory.side_effect
    started = 0

    async def receive(*args: Any, **kwargs: Any) -> SimpleNamespace:
        nonlocal started
        started += 1
        if started == expected:
            entered.set()
        await release.wait()
        return context.response

    def factory(**options: Any) -> Any:
        client = create(**options)
        client.get = AsyncMock(side_effect=receive)
        return client

    context.cookie_factory.side_effect = factory
    return SimpleNamespace(entered=entered, release=release)


@pytest.mark.unit
def test_explicit_cookie_request_uses_and_closes_an_isolated_async_client(
    html_context: SimpleNamespace,
) -> None:
    result = asyncio.run(
        html_context.html.request_url(
            NOTE_URL, cookie="web_session=offline-private", timeout=8,
        )
    )

    assert result == html_context.response.text
    html_context.sync_get.assert_not_called()
    html_context.cookie_factory.assert_called_once()
    client = html_context.cookie_factory.instances[0]
    assert client.cookies.get("web_session") == "offline-private"
    assert client.options["verify"] is True
    assert client.options["timeout"] == 8
    client.get.assert_awaited_once()
    assert client.get.await_args.args[0] == NOTE_URL
    assert client.get.await_args.kwargs["stream"] is True
    client.close.assert_awaited_once()
    assert html_context.manager.request_client.cookies.get("web_session") == "offline-session"


@pytest.mark.integration
def test_waiting_for_a_cookie_response_does_not_block_other_async_work(
    html_context: SimpleNamespace,
) -> None:
    async def scenario() -> None:
        controls = gated_cookie_clients(html_context)
        task = asyncio.create_task(
            html_context.html.request_url(NOTE_URL, cookie="web_session=offline-private")
        )
        try:
            await asyncio.wait_for(controls.entered.wait(), timeout=1)
            assert not task.done()
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), timeout=1)
            controls.release.set()
            assert await task == html_context.response.text
        finally:
            controls.release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.integration
def test_concurrent_explicit_cookies_do_not_share_or_replace_default_credentials(
    html_context: SimpleNamespace,
) -> None:
    async def scenario() -> None:
        controls = gated_cookie_clients(html_context, expected=2)
        tasks = [
            asyncio.create_task(html_context.html.request_url(NOTE_URL, cookie=cookie))
            for cookie in ("web_session=offline-a", "web_session=offline-b")
        ]
        try:
            await asyncio.wait_for(controls.entered.wait(), timeout=1)
            assert len(html_context.cookie_factory.instances) == 2
            assert all(not task.done() for task in tasks)
            controls.release.set()
            assert await asyncio.gather(*tasks) == [html_context.response.text] * 2
        finally:
            controls.release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())

    assert [client.cookies.get("web_session") for client in html_context.cookie_factory.instances] == [
        "offline-a", "offline-b",
    ]
    for client in html_context.cookie_factory.instances:
        client.close.assert_awaited_once()
    assert html_context.manager.request_client.cookies.get("web_session") == "offline-session"
    html_context.sync_get.assert_not_called()


@pytest.mark.unit
def test_cancelled_cookie_request_closes_its_client_and_propagates_cancellation(
    html_context: SimpleNamespace,
) -> None:
    async def scenario() -> None:
        controls = gated_cookie_clients(html_context)
        task = asyncio.create_task(
            html_context.html.request_url(NOTE_URL, cookie="web_session=offline-private")
        )
        try:
            await asyncio.wait_for(controls.entered.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            controls.release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(html_context.cookie_factory.instances) == 1
    html_context.cookie_factory.instances[0].close.assert_awaited_once()

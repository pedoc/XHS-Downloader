import asyncio
from collections.abc import Iterator
from importlib import import_module
from json import dumps
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

TEST_TIMEOUT = 2
MAX_TEST_INDEX_ITEMS = 256
PRIVATE_MARKER = "offline-cookie-and-note-must-not-be-logged"
VALID_TASK = {
    "data": {"noteId": "offline-note", "title": "中文 ' OR 1=1; 😀"},
    "index": None,
}


class FakeWebSocket:
    def __init__(
        self,
        messages: list[str | bytes],
        receive_error: Exception | None = None,
    ) -> None:
        self.messages: Iterator[str | bytes] = iter(messages)
        self.receive_error = receive_error
        self.received = 0
        self.close = AsyncMock()

    def __aiter__(self) -> "FakeWebSocket":
        return self

    async def __anext__(self) -> str | bytes:
        try:
            message = next(self.messages)
        except StopIteration:
            if self.receive_error:
                raise self.receive_error
            raise StopAsyncIteration from None
        self.received += 1
        return message


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


@pytest.fixture
def script_server(script_module: ModuleType) -> Any:
    core = SimpleNamespace(process_script_task=AsyncMock(), logging=Mock())
    return script_module.ScriptServer(core)


def take_queued_tasks(server: Any) -> list[dict[str, Any]]:
    tasks = []
    while not server.queue.empty():
        tasks.append(server.queue.get_nowait())
        server.queue.task_done()
    return tasks


INVALID_FRAMES = [
    pytest.param("{", id="malformed-json"),
    pytest.param(b"\xff", id="invalid-utf8"),
    pytest.param("null", id="null"),
    pytest.param("[]", id="array"),
    pytest.param('"text"', id="string"),
    pytest.param("true", id="boolean"),
    pytest.param("1", id="number"),
    pytest.param(dumps({}), id="missing-data"),
    pytest.param(dumps({"data": None}), id="null-data"),
    pytest.param(dumps({"data": []}), id="array-data"),
    pytest.param(dumps({"data": PRIVATE_MARKER}), id="string-data"),
    pytest.param(dumps({"data": 1}), id="number-data"),
    pytest.param(dumps({"data": False}), id="boolean-data"),
    pytest.param(dumps({"data": {}, "index": "1 2"}), id="string-index"),
    pytest.param(dumps({"data": {}, "index": 1}), id="scalar-index"),
    pytest.param(dumps({"data": {}, "index": {"1": 1}}), id="object-index"),
    pytest.param(dumps({"data": {}, "index": ["1"]}), id="coerced-index"),
    pytest.param(dumps({"data": {}, "index": [True]}), id="boolean-index"),
    pytest.param(dumps({"data": {}, "index": [1.0]}), id="float-index"),
    pytest.param(dumps({"data": {}, "index": [0]}), id="zero-index"),
    pytest.param(dumps({"data": {}, "index": [-1]}), id="negative-index"),
    pytest.param(dumps({"data": {}, "index": [None]}), id="null-index-item"),
    pytest.param(
        dumps({"data": {}, "index": [1] * (MAX_TEST_INDEX_ITEMS + 1)}),
        id="too-many-indices",
    ),
    pytest.param(
        dumps({"data": {}, "result_callback": PRIVATE_MARKER}),
        id="callback-kwargs",
    ),
    pytest.param(
        dumps({"data": {}, "progress_callback": PRIVATE_MARKER}),
        id="progress-kwargs",
    ),
    pytest.param(dumps({"data": {}, "unknown": PRIVATE_MARKER}), id="extra-field"),
]


@pytest.mark.unit
@pytest.mark.parametrize("invalid_frame", INVALID_FRAMES)
def test_invalid_messages_are_logged_then_the_next_valid_message_is_accepted(
    script_server: Any,
    invalid_frame: str | bytes,
) -> None:
    websocket = FakeWebSocket([invalid_frame, dumps(VALID_TASK)])

    asyncio.run(script_server.handler(websocket))

    assert take_queued_tasks(script_server) == [VALID_TASK]
    script_server.core.logging.assert_called_once()
    assert PRIVATE_MARKER not in str(script_server.core.logging.call_args)
    websocket.close.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"data": VALID_TASK["data"]},
        VALID_TASK,
        {"data": VALID_TASK["data"], "index": [3, 1, 3]},
        {"data": {}, "index": []},
        {"data": {}, "index": [1] * MAX_TEST_INDEX_ITEMS},
    ],
    ids=["missing-index", "null-index", "ordered-indices", "empty-index", "max-index"],
)
def test_legacy_script_tasks_preserve_note_data_and_image_indices(
    script_server: Any,
    payload: dict[str, Any],
) -> None:
    websocket = FakeWebSocket([dumps(payload, ensure_ascii=False)])

    asyncio.run(script_server.handler(websocket))

    assert take_queued_tasks(script_server) == [
        {"data": payload["data"], "index": payload.get("index")}
    ]
    script_server.core.logging.assert_not_called()
    websocket.close.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize("error_type", [RecursionError, ValueError])
def test_parser_limit_errors_do_not_stop_the_next_message_or_leak_input(
    script_server: Any,
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    original_loads = script_module.loads

    def limited_loads(message: str | bytes) -> Any:
        if message == PRIVATE_MARKER:
            raise error_type(PRIVATE_MARKER)
        return original_loads(message)

    monkeypatch.setattr(script_module, "loads", limited_loads)
    websocket = FakeWebSocket([PRIVATE_MARKER, dumps(VALID_TASK)])

    asyncio.run(script_server.handler(websocket))

    assert take_queued_tasks(script_server) == [VALID_TASK]
    script_server.core.logging.assert_called_once()
    assert PRIVATE_MARKER not in str(script_server.core.logging.call_args)


@pytest.mark.unit
def test_a_closed_websocket_does_not_escape_the_handler(
    script_server: Any,
    script_module: ModuleType,
) -> None:
    websocket = FakeWebSocket(
        [], receive_error=script_module.ConnectionClosed(None, None)
    )

    asyncio.run(script_server.handler(websocket))

    assert script_server.queue.empty()
    script_server.core.logging.assert_not_called()


@pytest.mark.unit
def test_application_queue_has_a_finite_small_capacity(script_server: Any) -> None:
    assert 1 <= script_server.queue.maxsize <= 64


@pytest.mark.integration
def test_full_queue_rejects_the_message_and_closes_without_waiting_for_space(
    script_server: Any,
) -> None:
    capacity = script_server.queue.maxsize
    assert 1 <= capacity <= 64
    accepted = [{"data": {"noteId": str(i)}, "index": None} for i in range(capacity)]
    for task in accepted:
        script_server.queue.put_nowait(task)
    websocket = FakeWebSocket(
        [dumps({"data": {"noteId": PRIVATE_MARKER}}), dumps(VALID_TASK)]
    )

    asyncio.run(asyncio.wait_for(script_server.handler(websocket), TEST_TIMEOUT))

    assert take_queued_tasks(script_server) == accepted
    websocket.close.assert_awaited_once()
    assert websocket.close.await_args.kwargs["code"] == 1013
    assert websocket.received == 1
    assert PRIVATE_MARKER not in str(script_server.core.logging.call_args_list)
    assert PRIVATE_MARKER not in str(websocket.close.await_args)


@pytest.mark.integration
def test_concurrent_handlers_share_the_same_bounded_queue(script_server: Any) -> None:
    async def receive() -> None:
        capacity = script_server.queue.maxsize
        assert 1 <= capacity <= 64
        for _ in range(capacity - 1):
            script_server.queue.put_nowait(VALID_TASK)
        websockets = [FakeWebSocket([dumps(VALID_TASK)]) for _ in range(2)]

        await asyncio.wait_for(
            asyncio.gather(*(script_server.handler(ws) for ws in websockets)),
            TEST_TIMEOUT,
        )

        assert script_server.queue.qsize() == capacity
        assert sum(ws.close.await_count for ws in websockets) == 1
        assert len(take_queued_tasks(script_server)) == capacity

    asyncio.run(receive())

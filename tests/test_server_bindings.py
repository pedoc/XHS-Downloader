import asyncio
import runpy
import sys
from importlib import import_module
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

MAIN_SCRIPT = Path(__file__).resolve().parents[1] / "main.py"
HOST_CASES = [({}, "127.0.0.1"), ({"host": "0.0.0.0"}, "0.0.0.0")]


@pytest.fixture
def server_modules(
    monkeypatch: pytest.MonkeyPatch,
    source_modules: SimpleNamespace,
) -> SimpleNamespace:
    gui = SimpleNamespace(launch=Mock())
    monkeypatch.setitem(sys.modules, "source.GUI", gui)
    main = import_module("main")
    monkeypatch.setattr(main, "launch", gui.launch)
    monkeypatch.setattr(
        asyncio.BaseEventLoop,
        "create_server",
        AsyncMock(side_effect=AssertionError("Real listeners are forbidden")),
    )
    script = import_module("source.module.script")
    return SimpleNamespace(
        main=main,
        app=source_modules.app,
        XHS=source_modules.app.XHS,
        script=script,
        ScriptServer=script.ScriptServer,
        gui=gui,
    )


@pytest.fixture
def script_entry(
    monkeypatch: pytest.MonkeyPatch,
    server_modules: SimpleNamespace
) -> SimpleNamespace:
    dispatched = Mock()

    def capture(coroutine: Any) -> None:
        try:
            dispatched(coroutine.cr_code.co_name, dict(coroutine.cr_frame.f_locals))
        finally:
            coroutine.close()

    source = import_module("source")
    cli = Mock()
    forbidden = Mock(side_effect=AssertionError("Server body must not execute"))
    monkeypatch.setattr(source, "Settings", forbidden)
    monkeypatch.setattr(source, "XHS", forbidden)
    monkeypatch.setattr(source, "cli", cli)
    monkeypatch.setattr(asyncio, "run", capture)

    def execute(mode: str | None, *options: str) -> None:
        arguments = [str(MAIN_SCRIPT)] + ([mode] if mode else []) + list(options)
        monkeypatch.setattr(sys, "argv", arguments)
        runpy.run_path(str(MAIN_SCRIPT), run_name="__main__")

    return SimpleNamespace(
        execute=execute,
        dispatched=dispatched,
        cli=cli,
        launch=server_modules.gui.launch,
        forbidden=forbidden,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "owner,method,parameter",
    [
        ("main", "api_server", "host"),
        ("main", "mcp_server", "host"),
        ("XHS", "__init__", "script_host"),
        ("XHS", "run_api_server", "host"),
        ("XHS", "run_mcp_server", "host"),
        ("XHS", "init_script_server", "host"),
        ("XHS", "switch_script_server", "host"),
        ("XHS", "run_script_server", "host"),
        ("XHS", "_run_script_server", "host"),
        ("ScriptServer", "__init__", "host"),
    ],
)
def test_server_parameters_default_to_ipv4_loopback(
    server_modules: SimpleNamespace,
    owner: str,
    method: str,
    parameter: str,
) -> None:
    function = getattr(getattr(server_modules, owner), method)

    assert signature(function).parameters[parameter].default == "127.0.0.1"


@pytest.mark.unit
@pytest.mark.parametrize("host", [None, "0.0.0.0"])
def test_xhs_constructor_forwards_script_binding_with_only_fake_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    server_modules: SimpleNamespace,
    host: str | None,
) -> None:
    app = server_modules.app
    manager = SimpleNamespace(note_format="markdown")
    monkeypatch.setattr(app, "Manager", Mock(return_value=manager))
    for name in (
        "switch_language",
        "Print",
        "MapRecorder",
        "Mapping",
        "Html",
        "Image",
        "Video",
        "Explore",
        "Converter",
        "Download",
        "IDRecorder",
        "DataRecorder",
        "NoteGenerator",
    ):
        monkeypatch.setattr(app, name, Mock())
    initialize = Mock()
    monkeypatch.setattr(app.XHS, "init_script_server", initialize)
    core = object.__new__(app.XHS)
    options = {"script_host": host} if host is not None else {}

    app.XHS.__init__(core, **options, script_port=8123)

    initialize.assert_called_once_with(host or "127.0.0.1", 8123)


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["api", "mcp"])
@pytest.mark.parametrize("options,host", HOST_CASES)
def test_main_server_wrappers_forward_bindings_without_reading_settings(
    monkeypatch: pytest.MonkeyPatch,
    server_modules: SimpleNamespace,
    mode: str,
    options: dict[str, Any],
    host: str,
) -> None:
    core = SimpleNamespace(run_api_server=AsyncMock(), run_mcp_server=AsyncMock())
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=core)
    context.__aexit__ = AsyncMock(return_value=None)
    settings = Mock(return_value=SimpleNamespace(run=Mock(return_value={})))
    monkeypatch.setattr(server_modules.main, "Settings", settings)
    monkeypatch.setattr(server_modules.main, "XHS", Mock(return_value=context))

    asyncio.run(getattr(server_modules.main, f"{mode}_server")(**options, port=8123))

    if mode == "api":
        core.run_api_server.assert_awaited_once_with(host, 8123, "info")
    else:
        core.run_mcp_server.assert_awaited_once_with(
            transport="streamable-http", host=host, port=8123, log_level="INFO"
        )


@pytest.mark.integration
@pytest.mark.parametrize("options,host", HOST_CASES)
def test_api_passes_binding_to_fake_uvicorn_config(
    monkeypatch: pytest.MonkeyPatch,
    server_modules: SimpleNamespace,
    options: dict[str, Any],
    host: str,
) -> None:
    app = server_modules.app
    api = Mock()
    config = Mock()
    server = SimpleNamespace(serve=AsyncMock())
    config_factory = Mock(return_value=config)
    server_factory = Mock(return_value=server)
    monkeypatch.setattr(app, "FastAPI", Mock(return_value=api))
    monkeypatch.setattr(app, "Config", config_factory)
    monkeypatch.setattr(app, "Server", server_factory)
    core = SimpleNamespace(VERSION_BETA=False, setup_routes=Mock())

    asyncio.run(app.XHS.run_api_server(core, **options, port=8123))

    config_factory.assert_called_once_with(api, host=host, port=8123, log_level="info")
    server_factory.assert_called_once_with(config)
    server.serve.assert_awaited_once_with()


@pytest.mark.integration
@pytest.mark.parametrize("options,host", HOST_CASES)
def test_mcp_passes_binding_to_fake_fastmcp_runner(
    monkeypatch: pytest.MonkeyPatch,
    server_modules: SimpleNamespace,
    options: dict[str, Any],
    host: str,
) -> None:
    mcp = SimpleNamespace(
        tool=Mock(return_value=lambda function: function), run_async=AsyncMock()
    )
    monkeypatch.setattr(server_modules.app, "FastMCP", Mock(return_value=mcp))

    asyncio.run(
        server_modules.XHS.run_mcp_server(SimpleNamespace(), **options, port=8123)
    )

    mcp.run_async.assert_awaited_once_with(
        transport="streamable-http", host=host, port=8123, log_level="INFO"
    )


@pytest.mark.integration
@pytest.mark.parametrize("options,host", HOST_CASES)
def test_script_server_passes_binding_to_fake_websocket_serve(
    monkeypatch: pytest.MonkeyPatch,
    server_modules: SimpleNamespace,
    options: dict[str, Any],
    host: str,
) -> None:
    serve = AsyncMock(return_value=Mock())
    task = Mock(side_effect=lambda coroutine: coroutine.close())
    monkeypatch.setattr(server_modules.script, "serve", serve)
    monkeypatch.setattr(server_modules.script, "create_task", task)
    server = server_modules.ScriptServer(Mock(), **options, port=8123)

    asyncio.run(server.start())

    serve.assert_awaited_once()
    arguments, keywords = serve.await_args
    assert arguments == (server.handler, host, 8123)
    assert keywords["origins"] == server.ORIGINS
    task.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("method", ["init_script_server", "switch_script_server"])
@pytest.mark.parametrize("options,host", HOST_CASES)
def test_script_activation_forwards_binding(
    server_modules: SimpleNamespace,
    method: str,
    options: dict[str, Any],
    host: str,
) -> None:
    core = SimpleNamespace(
        manager=SimpleNamespace(script_server=True), run_script_server=Mock()
    )
    result = getattr(server_modules.XHS, method)(core, **options, port=8123)
    if method == "switch_script_server":
        asyncio.run(result)

    core.run_script_server.assert_called_once_with(host, 8123)


@pytest.mark.unit
@pytest.mark.parametrize("options,host", HOST_CASES)
def test_script_task_creation_forwards_binding_without_starting_task(
    monkeypatch: pytest.MonkeyPatch,
    server_modules: SimpleNamespace,
    options: dict[str, Any],
    host: str,
) -> None:
    core = SimpleNamespace(script=None, _run_script_server=AsyncMock())
    create_task = Mock(side_effect=lambda coroutine: coroutine.close())
    monkeypatch.setattr(server_modules.app, "create_task", create_task)

    server_modules.XHS.run_script_server(core, **options, port=8123)

    core._run_script_server.assert_called_once_with(host, 8123)
    core._run_script_server.assert_not_awaited()
    create_task.assert_called_once()


@pytest.mark.integration
@pytest.mark.parametrize("options,host", HOST_CASES)
def test_script_context_forwards_binding_without_listening(
    monkeypatch: pytest.MonkeyPatch,
    server_modules: SimpleNamespace,
    options: dict[str, Any],
    host: str,
) -> None:
    factory = Mock(return_value=MagicMock())
    wait = AsyncMock()
    monkeypatch.setattr(server_modules.app, "ScriptServer", factory)
    monkeypatch.setattr(server_modules.app, "Future", wait)
    core = SimpleNamespace()

    asyncio.run(server_modules.XHS._run_script_server(core, **options, port=8123))

    factory.assert_called_once_with(core, host, 8123)
    wait.assert_awaited_once_with()


@pytest.mark.e2e
@pytest.mark.parametrize("mode", ["api", "McP"])
@pytest.mark.parametrize(
    "options,host,port",
    [
        ([], "127.0.0.1", 5556),
        (["--host", "0.0.0.0", "--port", "8123"], "0.0.0.0", 8123),
        (["--port", "1"], "127.0.0.1", 1),
        (["--host=::1", "--port=65535"], "::1", 65535),
    ],
)
def test_main_script_api_mcp_arguments_reach_server_coroutine(
    script_entry: SimpleNamespace,
    mode: str,
    options: list[str],
    host: str,
    port: int,
) -> None:
    script_entry.execute(mode, *options)

    script_entry.dispatched.assert_called_once()
    name, parameters = script_entry.dispatched.call_args.args
    assert name == f"{mode.lower()}_server"
    assert parameters["host"] == host
    assert parameters["port"] == port
    script_entry.forbidden.assert_not_called()
    script_entry.launch.assert_not_called()
    script_entry.cli.assert_not_called()


@pytest.mark.e2e
@pytest.mark.parametrize("mode", ["API", "mcp"])
@pytest.mark.parametrize(
    "options",
    [["--port", value] for value in ["0", "65536", "-1", "invalid", "5.5"]]
    + [["--port"], ["--host"], ["--unknown-option"]],
)
def test_main_script_rejects_bad_server_arguments_before_starting(
    script_entry: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    options: list[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        script_entry.execute(mode, *options)

    assert error.value.code == 2
    assert "error:" in capsys.readouterr().err
    script_entry.dispatched.assert_not_called()
    script_entry.forbidden.assert_not_called()
    script_entry.launch.assert_not_called()
    script_entry.cli.assert_not_called()


@pytest.mark.e2e
@pytest.mark.parametrize("mode", [None, "tUi", "--help"])
def test_main_script_keeps_gui_tui_and_click_dispatch_unchanged(
    script_entry: SimpleNamespace,
    mode: str | None,
) -> None:
    script_entry.execute(mode)

    if mode is None:
        script_entry.launch.assert_called_once_with()
    elif mode == "tUi":
        script_entry.dispatched.assert_called_once_with("tui", {})
    else:
        script_entry.cli.assert_called_once_with()
    script_entry.forbidden.assert_not_called()

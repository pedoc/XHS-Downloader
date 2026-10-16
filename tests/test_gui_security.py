from importlib import import_module
from inspect import signature
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

GUI_ROOT = Path(__file__).resolve().parents[1] / "static" / "GUI"
ENTRY_URI = (GUI_ROOT / "index.html").as_uri()


@pytest.fixture
def security() -> ModuleType:
    return import_module("source.GUI.security")


@pytest.fixture
def guarded_window(security: ModuleType) -> SimpleNamespace:
    current_url = Mock(return_value=ENTRY_URI)
    window = SimpleNamespace(
        gui=SimpleNamespace(get_current_url=current_url), uid="offline-window",
        destroy=Mock(),
    )
    guard = security.TrustedGuiBridge(window, ENTRY_URI)
    return SimpleNamespace(window=window, guard=guard, current_url=current_url)


@pytest.mark.unit
@pytest.mark.parametrize("fragment", ["", "#settings", "#monitor"])
def test_only_the_entry_document_and_its_fragments_are_trusted(
    security: ModuleType, fragment: str,
) -> None:
    assert security.is_trusted_page(ENTRY_URI + fragment, ENTRY_URI)


@pytest.mark.unit
@pytest.mark.parametrize("url", [
    None, "", "about:blank", "https://example.invalid/", "data:text/html,offline",
    "javascript:offline", ENTRY_URI + "?remote=1",
    ENTRY_URI.replace("index.html", "other.html"),
    "file://offline.invalid/share/index.html", "file:////offline.invalid/share/index.html",
])
def test_remote_or_other_local_pages_are_not_trusted(security: ModuleType, url: str | None) -> None:
    assert not security.is_trusted_page(url, ENTRY_URI)


@pytest.mark.unit
def test_file_page_comparison_handles_unicode_without_filesystem_access(
    security: ModuleType,
) -> None:
    entry = "file:///C:/offline/%E4%B8%AD%E6%96%87%20folder/index.html"
    assert security.is_trusted_page(
        "file:///c:/offline/中文%20folder/index.html#settings", entry, windows=True,
    )
    assert not security.is_trusted_page(
        "file:///C:/offline/K/index.html", "file:///C:/offline/K/index.html", windows=True,
    )


@pytest.mark.unit
@pytest.mark.parametrize("name", ["_backend", "__class__", "__dict__", "get_state"])
def test_object_api_sentinel_rejects_every_attribute(security: ModuleType, name: str) -> None:
    sentinel = security.NoObjectApi()
    assert dir(sentinel) == []
    with pytest.raises(AttributeError):
        getattr(sentinel, name)


@pytest.mark.unit
def test_guard_preserves_a_flat_callable_signature_and_valid_local_result(
    guarded_window: SimpleNamespace,
) -> None:
    def read(query: str = "") -> dict[str, str]:
        return {"query": query}

    call = guarded_window.guard.wrap("get_state", read)
    assert signature(call) == signature(read)
    assert call("offline") == {"query": "offline"}
    guarded_window.window.destroy.assert_not_called()


@pytest.mark.unit
def test_untrusted_navigation_permanently_revokes_application_api(
    guarded_window: SimpleNamespace,
) -> None:
    implementation = Mock(return_value="offline-private")
    call = guarded_window.guard.wrap("get_state", implementation)
    guarded_window.current_url.return_value = "https://example.invalid/"

    with pytest.raises(PermissionError):
        call()
    guarded_window.current_url.return_value = ENTRY_URI
    with pytest.raises(PermissionError):
        call()

    implementation.assert_not_called()
    guarded_window.window.destroy.assert_called_once()


@pytest.mark.unit
def test_navigation_during_a_call_cannot_return_private_results(
    guarded_window: SimpleNamespace,
) -> None:
    def read() -> str:
        guarded_window.current_url.return_value = "https://example.invalid/"
        return "offline-private"

    call = guarded_window.guard.wrap("get_state", read)
    with pytest.raises(PermissionError):
        call()


@pytest.mark.unit
def test_navigation_during_an_error_cannot_return_private_error_details(
    guarded_window: SimpleNamespace,
) -> None:
    import traceback

    marker = "offline-private-error-value"

    def fail() -> None:
        guarded_window.current_url.return_value = "https://example.invalid/"
        raise ValueError(marker)

    call = guarded_window.guard.wrap("get_state", fail)
    try:
        call()
    except PermissionError as error:
        assert marker not in str(error)
        assert marker not in traceback.format_exc()
    else:
        pytest.fail("An untrusted page must not receive the original business exception")


@pytest.mark.unit
def test_local_business_errors_are_not_hidden(guarded_window: SimpleNamespace) -> None:
    def fail() -> None:
        raise ValueError("offline local error")

    call = guarded_window.guard.wrap("get_state", fail)
    with pytest.raises(ValueError, match="offline local error"):
        call()
    guarded_window.window.destroy.assert_not_called()


@pytest.mark.unit
def test_navigation_hook_uses_the_nonblocking_native_url_reader(
    guarded_window: SimpleNamespace,
) -> None:
    guarded_window.window.get_current_url = Mock(
        side_effect=AssertionError("The loaded-waiting API must not run in before_load")
    )
    guarded_window.guard.before_load()
    guarded_window.window.get_current_url.assert_not_called()
    guarded_window.current_url.return_value = "https://example.invalid/"
    guarded_window.guard.before_load()
    guarded_window.window.destroy.assert_called_once()


@pytest.mark.unit
def test_unregistered_private_and_dotted_names_cannot_reach_a_call_thread(
    security: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    util = import_module("webview.util")
    thread = Mock()
    monkeypatch.setattr(util, "Thread", thread)
    window = SimpleNamespace(_functions={}, _js_api=security.NoObjectApi())
    for name in ("missing", "_backend.snapshot", "__class__.__base__.__subclasses__",
                 "get_state.__globals__", "missing.__class__"):
        util.js_bridge_call(window, name, [], "offline-call")
    thread.assert_not_called()


@pytest.mark.unit
def test_only_explicit_flat_application_methods_are_exposed(
    security: ModuleType, guarded_window: SimpleNamespace,
) -> None:
    from webview.event import Event
    from source.GUI.backend import GuiApi

    window = guarded_window.window
    window.events = SimpleNamespace(before_load=Event(window, True))
    window.expose = Mock()
    api = GuiApi(Mock())
    bridge = security.bind_trusted_api(window, api, ENTRY_URI)
    functions = window.expose.call_args.args

    assert {function.__name__ for function in functions} == set(security.APP_METHODS)
    assert len(functions) == len(security.APP_METHODS)
    by_name = {function.__name__: function for function in functions}
    assert signature(by_name["browse_directory"]) == signature(api.browse_directory)
    assert window.events.before_load._items == [bridge.before_load]


@pytest.mark.unit
def test_launcher_does_not_pass_the_implementation_object_to_pywebview(
    security: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = import_module("source.GUI.launcher")
    backend = SimpleNamespace(start=Mock(), stop=Mock())
    api = SimpleNamespace(_bind_window=Mock())
    window = SimpleNamespace()
    create = Mock(return_value=window)
    bind = Mock()
    monkeypatch.setattr(launcher, "GuiBackend", Mock(return_value=backend))
    monkeypatch.setattr(launcher, "GuiApi", Mock(return_value=api))
    monkeypatch.setattr(launcher.webview, "create_window", create)
    monkeypatch.setattr(launcher.webview, "start", Mock())
    monkeypatch.setattr(launcher, "bind_trusted_api", bind)

    launcher.launch()

    assert type(create.call_args.kwargs["js_api"]) is security.NoObjectApi
    bind.assert_called_once_with(window, api, ENTRY_URI)
    backend.stop.assert_called_once()


@pytest.mark.unit
def test_gui_csp_restricts_network_and_privileged_content() -> None:
    from html.parser import HTMLParser

    class Parser(HTMLParser):
        policy = ""

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            values = dict(attrs)
            if tag == "meta" and (values.get("http-equiv") or "").lower() == "content-security-policy":
                self.policy = values.get("content") or ""

    parser = Parser()
    parser.feed((GUI_ROOT / "index.html").read_text("utf-8"))
    directives = {parts[0]: parts[1:] for segment in parser.policy.split(";")
                  if (parts := segment.split())}
    for name in ("default-src", "connect-src", "object-src", "base-uri", "form-action", "frame-src"):
        assert directives[name] == ["'none'"]
    assert "'self'" in directives["script-src"]
    assert "'unsafe-inline'" not in directives["script-src"]
    assert not any("http" in value for value in directives["script-src"])

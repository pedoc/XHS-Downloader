"""Keep application capabilities confined to the bundled GUI document."""

import sys
from collections.abc import Callable
from functools import wraps
from inspect import signature
from logging import getLogger
from threading import Lock
from typing import Any, NoReturn
from urllib.parse import unquote, urlsplit

LOGGER = getLogger(__name__)
ASCII_CASE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
APP_METHODS = (
    "get_state", "paste_content", "browse_directory", "create_tasks", "cancel_task",
    "clear_finished", "start_monitor", "stop_monitor", "get_history_page",
    "delete_history_records", "get_settings", "get_translations", "get_disclaimer",
    "accept_disclaimer", "decline_disclaimer", "save_settings", "check_update",
    "open_url", "open_download_folder",
)


def is_trusted_page(
    candidate: str | None, entry_uri: str, *, windows: bool = sys.platform == "win32",
) -> bool:
    """Compare file URLs without probing arbitrary files, shares or symlinks."""
    if not isinstance(candidate, str) or not candidate or any(
        ord(char) < 32 or char == "\\" for char in candidate
    ):
        return False
    try:
        page, entry = urlsplit(candidate), urlsplit(entry_uri)
        if page.scheme != "file" or page.netloc or page.query:
            return False
        actual = unquote(page.path, errors="strict")
        expected = unquote(entry.path, errors="strict")
    except (ValueError, UnicodeError):
        return False
    if actual.startswith("//"):
        return False
    if windows:
        # Do not use Unicode lower/casefold as a Windows filesystem identity check.
        actual, expected = actual.translate(ASCII_CASE), expected.translate(ASCII_CASE)
    return actual == expected


class NoObjectApi:
    """Prevent pywebview's fallback dotted attribute lookup on application objects."""

    __slots__ = ()

    def __dir__(self) -> list[str]:
        return []

    def __getattribute__(self, name: str) -> NoReturn:
        raise AttributeError(name)


class TrustedGuiBridge:
    def __init__(self, window: Any, entry_uri: str) -> None:
        self.window = window
        self.entry_uri = entry_uri
        self._lock = Lock()
        self._revoked = False
        self._seen_entry = False

    def _current_url(self) -> str | None:
        try:
            # The public reader waits for `loaded`, so it deadlocks in before_load.
            return self.window.gui.get_current_url(self.window.uid)
        except Exception as error:
            LOGGER.warning("Cannot read GUI document URL: %s", type(error).__name__)
            return None

    def _revoke(self) -> None:
        with self._lock:
            if self._revoked:
                return
            self._revoked = True
        try:
            self.window.destroy()
        except Exception as error:
            LOGGER.warning("Cannot close untrusted GUI document: %s", type(error).__name__)

    def _allowed(self) -> bool:
        with self._lock:
            if self._revoked:
                return False
        if not is_trusted_page(self._current_url(), self.entry_uri):
            self._revoke()
            return False
        with self._lock:
            if self._revoked:
                return False
            self._seen_entry = True
        return True

    def before_load(self) -> None:
        current = self._current_url()
        with self._lock:
            initial_blank = not self._seen_entry and current in {None, "about:blank"}
        if not initial_blank and not self._allowed():
            self._revoke()

    def wrap(self, name: str, method: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(method)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            if not self._allowed():
                raise PermissionError("Application API is restricted to the local GUI")
            try:
                return method(*args, **kwargs)
            finally:
                if name != "decline_disclaimer" and not self._allowed():
                    raise PermissionError("GUI document changed before the API returned") from None

        guarded.__name__ = name
        setattr(guarded, "__signature__", signature(method))
        return guarded


def bind_trusted_api(window: Any, api: Any, entry_uri: str) -> TrustedGuiBridge:
    bridge = TrustedGuiBridge(window, entry_uri)
    window.expose(*(bridge.wrap(name, getattr(api, name)) for name in APP_METHODS))
    window.events.before_load += bridge.before_load
    return bridge

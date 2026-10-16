"""Offline checks for private files excluded from the Docker build context."""

from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_MODULES = tuple(
    path.relative_to(ROOT).as_posix()
    for path in sorted((ROOT / "source").rglob("*.py"))
    if path.is_file()
)


@pytest.fixture
def ignore_patterns() -> tuple[str, ...]:
    ignore_file = ROOT / ".dockerignore"
    assert ignore_file.is_file(), "Docker builds must exclude local private state"
    return tuple(
        line.strip().strip("/")
        for line in ignore_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#") and line.strip() != "."
    )


def matches_parts(pattern: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    """Match the component globs and recursive ** used by this ignore file."""
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return matches_parts(pattern[1:], parts) or (
            bool(parts) and matches_parts(pattern, parts[1:])
        )
    return (
        bool(parts)
        and fnmatchcase(parts[0], pattern[0])
        and matches_parts(pattern[1:], parts[1:])
    )


def is_ignored(path: str, patterns: tuple[str, ...]) -> bool:
    """An excluded directory also excludes its descendants; no negations needed."""
    assert all(not pattern.startswith("!") for pattern in patterns)
    parts = PurePosixPath(path).parts
    return any(
        matches_parts(tuple(pattern.split("/")), parts[:depth])
        for pattern in patterns
        for depth in range(1, len(parts) + 1)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "patterns", "expected"),
    [
        ("Volume/settings.json", ("Volume",), True),
        ("nested/Volume/settings.json", ("Volume",), False),
        (".env", ("**/.env",), True),
        ("nested/deep/.env", ("**/.env",), True),
        ("source/settings.py", ("**/.env",), False),
        ("source/__pycache__/settings.pyc", ("**/__pycache__",), True),
        ("static/GUI/app.js", ("**/*.py[cod]",), False),
    ],
)
def test_ignore_matcher_respects_root_and_recursive_patterns(
    path: str, patterns: tuple[str, ...], expected: bool
) -> None:
    assert is_ignored(path, patterns) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "Volume/settings.json",
        "Volume/Download/notes.xlsx",
        ".git",
        ".git/config",
        ".git/objects/example",
        ".venv/Scripts/python.exe",
        "venv/bin/python",
        ".claude/settings.local.json",
        ".claude/worktrees/private/source.py",
        ".idea/workspace.xml",
        ".vscode/settings.json",
        "build/exe/settings.json",
        "dist/archive.zip",
        "output/release.zip",
        "__pycache__/main.cpython-312.pyc",
        "source/module/__pycache__/settings.cpython-312.pyc",
        "source/module/stale.pyc",
        ".pytest_cache/v/cache/nodeids",
        ".ruff_cache/0.1/cache",
        ".mypy_cache/3.12/cache.json",
        ".coverage",
        ".coverage.machine.123",
        "htmlcov/index.html",
        ".env",
        ".env.local",
        ".env.production",
        "source/.env",
        "nested/.env.development",
        ".netrc",
        "nested/.netrc",
        ".pypirc",
        ".npmrc",
        ".ssh/id_ed25519",
        ".aws/credentials",
        "nested/.aws/config",
        ".azure/accessTokens.json",
        ".config/gcloud/application_default_credentials.json",
        "credentials.json",
        "nested/credentials.yaml",
        "secrets.json",
        "nested/secrets/token.txt",
        "cookies.txt",
        "nested/cookies.json",
        "nested/cookies/session.json",
        "nested/cookies.sqlite",
        "nested/cookies.sqlite3",
        "nested/cookies.db",
        "private.pem",
        "nested/client.key",
        "client.p12",
        "client.pfx",
        "id_rsa",
        "nested/id_ed25519.pub",
        "logs/session.log",
    ],
)
def test_private_paths_are_not_sent_to_builder(
    path: str, ignore_patterns: tuple[str, ...]
) -> None:
    assert is_ignored(path, ignore_patterns), f"Private context path is exposed: {path}"


@pytest.mark.unit
@pytest.mark.parametrize("path", ["settings.json", "Download", "Download/offline.jpg"] + [
    name + suffix
    for name in ("ExploreID.db", "ExploreData.db", "MappingData.db")
    for suffix in ("", "-journal", "-wal", "-shm")
])
def test_legacy_application_data_is_not_sent_to_builder(
    path: str, ignore_patterns: tuple[str, ...],
) -> None:
    assert is_ignored(path, ignore_patterns)


@pytest.mark.integration
@pytest.mark.parametrize(
    "path",
    [
        "Dockerfile",
        "requirements.txt",
        "main.py",
        "LICENSE",
        "source",
        "source/GUI/backend.py",
        "source/module/settings.py",
        "source/module/cookies.py",
        "locale",
        "locale/en_US/LC_MESSAGES/xhs.mo",
        "locale/zh_CN/LC_MESSAGES/xhs.mo",
        "static",
        "static/GUI/index.html",
        "static/GUI/app.js",
        "static/GUI/navigation.js",
        "static/GUI/styles.css",
        "static/XHS-Downloader.tcss",
        "static/XHS-Downloader.ico",
        "static/XHS-Downloader.icns",
        "static/XHS-Downloader.png",
    ],
)
def test_required_build_inputs_remain_available(
    path: str, ignore_patterns: tuple[str, ...]
) -> None:
    assert (ROOT / path).exists(), f"Missing required build input: {path}"
    assert not is_ignored(path, ignore_patterns), f"Required input is excluded: {path}"


@pytest.mark.integration
@pytest.mark.parametrize("path", SOURCE_MODULES)
def test_all_published_source_modules_remain_in_the_build_context(
    path: str, ignore_patterns: tuple[str, ...]
) -> None:
    assert not is_ignored(path, ignore_patterns), f"Application code is excluded: {path}"

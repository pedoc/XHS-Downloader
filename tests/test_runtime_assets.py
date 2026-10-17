"""Inspect real packaging options with cx_Freeze replaced before setup.py runs."""

import re
import runpy
import shlex
import sys
from gettext import GNUTranslations
from html.parser import HTMLParser
from importlib.metadata import distribution
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUI_ROOT = ROOT / "static" / "GUI"
CATALOGS = (
    "locale/en_US/LC_MESSAGES/xhs.mo",
    "locale/zh_CN/LC_MESSAGES/xhs.mo",
)
CORE_ASSETS = frozenset(
    (
        "LICENSE",
        "static/XHS-Downloader.tcss",
        "static/XHS-Downloader.ico",
        "static/XHS-Downloader.icns",
        "static/XHS-Downloader.png",
        *CATALOGS,
    )
)
GUI_ASSETS = frozenset(
    path.relative_to(ROOT).as_posix()
    for path in GUI_ROOT.rglob("*")
    if path.is_file()
)
RUNTIME_ASSETS = CORE_ASSETS | GUI_ASSETS
UNSHIPPED_FILES = (
    "static/other/20250619.js",
    "static/other/自动滚动页面.js",
    "static/other/Bloome_AD.png",
    "static/other/DartNode_AD.png",
    "static/XHS-Downloader.js",
    "static/XHS-Downloader.jpg",
    "static/Release_Notes.md",
    "static/screenshot/程序运行截图CN1.png",
    "static/微信赞助二维码.png",
    "locale/po_to_mo.py",
    "locale/generate_path.py",
    "locale/xhs.pot",
    "locale/en_US/LC_MESSAGES/xhs.po",
)


@pytest.fixture(params=("win32", "linux", "darwin"))
def freeze_build(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> SimpleNamespace:
    setup_call = Mock(name="fake_setup")
    executable_call = Mock(name="fake_executable", side_effect=dict)
    monkeypatch.setitem(
        sys.modules, "cx_Freeze",
        SimpleNamespace(setup=setup_call, Executable=executable_call),
    )
    with monkeypatch.context() as platform_context:
        platform_context.setattr(sys, "platform", request.param)
        namespace = runpy.run_path(str(ROOT / "setup.py"))
    setup_call.assert_called_once()
    return SimpleNamespace(
        arguments=setup_call.call_args.kwargs,
        namespace=namespace,
        platform=request.param,
    )


def include_pairs(build: SimpleNamespace) -> tuple[tuple[Path, PurePosixPath], ...]:
    files = build.arguments["options"]["build_exe"]["include_files"]
    assert all(isinstance(pair, tuple) and len(pair) == 2 for pair in files)
    return tuple((Path(source), PurePosixPath(target)) for source, target in files)


def expand_resource(source: Path, target: PurePosixPath) -> tuple[tuple[str, Path], ...]:
    absolute_source = (ROOT / source).resolve()
    metadata = Path(distribution("opentelemetry-api")._path).resolve()
    allowed_roots = (ROOT / "static", ROOT / "locale", metadata)
    assert absolute_source == ROOT / "LICENSE" or any(
        absolute_source.is_relative_to(allowed) for allowed in allowed_roots
    ), "Only repository assets and installed OpenTelemetry metadata may be inspected"
    assert not target.is_absolute() and ".." not in target.parts
    assert absolute_source.exists(), f"Missing runtime resource: {source}"
    if absolute_source.is_dir():
        return tuple(
            ((target / path.relative_to(absolute_source).as_posix()).as_posix(), path)
            for path in absolute_source.rglob("*")
            if path.is_file()
        )
    return ((target.as_posix(), absolute_source),)


@pytest.fixture
def freezer_files(freeze_build: SimpleNamespace) -> dict[str, Path]:
    files = tuple(
        pair
        for source, target in include_pairs(freeze_build)
        for pair in expand_resource(source, target)
    )
    assert len(files) == len(dict(files)), "Runtime asset destinations must be unique"
    return dict(files)


def container_resource_files() -> dict[str, Path]:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    final_stage = re.sub(r"\\\r?\n\s*", " ", text).rsplit("\nFROM ", 1)[1]
    copies = tuple(
        shlex.split(line.strip())[1:]
        for line in final_stage.splitlines()
        if line.strip().startswith("COPY ")
    )
    assert ["source", "/app/source"] in copies
    assert ["main.py", "/app/main.py"] in copies
    local_copies = tuple(copy for copy in copies if not copy[0].startswith("--from="))
    resources = tuple(
        (Path(source), PurePosixPath(copy[-1]).relative_to("/app"),
         len(copy) > 2 or copy[-1].endswith("/"))
        for copy in local_copies
        for source in copy[:-1]
        if source not in {"source", "main.py"}
    )
    return dict(
        pair
        for source, target, directory_target in resources
        for pair in expand_resource(
            source,
            target / source.name if directory_target and not (ROOT / source).is_dir()
            else target,
        )
    )


class AssetReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: tuple[str, ...] = ()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.references = (*self.references, *(
            value for key, value in attrs
            if key in {"src", "href", "poster"} and value
        ))


def local_references(path: Path) -> tuple[str, ...]:
    text = path.read_text(encoding="utf-8")
    parser = AssetReferences()
    if path.suffix == ".html":
        parser.feed(text)
        parser.close()
    css = tuple(re.findall(r"\burl\(\s*['\"]?([^'\"\s)]+)", text))
    imports = tuple(re.findall(
        r"(?:@import\s+|\bfrom\s+|\bimport\s*\(?\s*|\bfetch\s*\(\s*|"
        r"\bnew\s+URL\s*\(\s*)['\"]([^'\"]+)", text,
    ))
    urls = tuple(urlsplit(value) for value in (*parser.references, *css, *imports))
    local_paths = tuple(
        (path.parent / unquote(url.path)).resolve()
        for url in urls if not (url.scheme or url.netloc) and url.path
    )
    assert all(target.is_relative_to(ROOT) for target in local_paths)
    return tuple(target.relative_to(ROOT).as_posix() for target in local_paths)


@pytest.mark.unit
def test_native_api_url_calls_are_not_mistaken_for_css_resource_references(
    tmp_path: Path,
) -> None:
    script = tmp_path / "app.js"
    script.write_text(
        "nativeApi.open_url(aboutLinks[button.dataset.aboutKey]);", encoding="utf-8"
    )
    assert local_references(script) == ()


@pytest.mark.integration
def test_freezer_includes_only_runtime_assets_and_required_metadata(
    freeze_build: SimpleNamespace, freezer_files: dict[str, Path]
) -> None:
    sources = {source.as_posix() for source, _ in include_pairs(freeze_build)}
    assert not sources.intersection({"static", "locale"}), "Do not copy broad asset trees"
    metadata = Path(distribution("opentelemetry-api")._path).resolve()
    project_files = {
        target for target, source in freezer_files.items()
        if source.is_relative_to(ROOT) and not source.is_relative_to(metadata)
    }
    assert project_files == RUNTIME_ASSETS
    assert all(freezer_files[target] == ROOT / target for target in RUNTIME_ASSETS)
    assert (metadata, PurePosixPath("lib") / metadata.name) in include_pairs(freeze_build)
    assert f"lib/{metadata.name}/METADATA" in freezer_files


@pytest.mark.unit
@pytest.mark.parametrize("metadata_in_checkout", (True, False))
def test_runtime_allowlist_is_independent_of_metadata_install_location(
    metadata_in_checkout: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    metadata_parent = (
        ROOT / ".venv" / "Lib" / "site-packages"
        if metadata_in_checkout else tmp_path / "site-packages"
    )
    metadata = metadata_parent / "opentelemetry_api-local.dist-info"
    metadata_target = f"lib/{metadata.name}"
    pairs = tuple((path, path) for path in sorted(RUNTIME_ASSETS)) + (
        (str(metadata), metadata_target),
    )
    build = SimpleNamespace(
        arguments={"options": {"build_exe": {"include_files": pairs}}}
    )
    files = {path: ROOT / path for path in RUNTIME_ASSETS} | {
        f"{metadata_target}/METADATA": metadata / "METADATA",
    }
    monkeypatch.setattr(
        sys.modules[__name__], "distribution",
        Mock(return_value=SimpleNamespace(_path=metadata)),
    )
    test_freezer_includes_only_runtime_assets_and_required_metadata(build, files)


@pytest.mark.integration
def test_container_and_freezer_have_the_same_runtime_asset_allowlist(
    freezer_files: dict[str, Path],
) -> None:
    container_files = container_resource_files()
    assert set(container_files) == RUNTIME_ASSETS
    assert all(container_files[target] == freezer_files[target] for target in RUNTIME_ASSETS)


@pytest.mark.integration
def test_all_gui_local_references_are_in_the_captured_runtime_manifest(
    freezer_files: dict[str, Path],
) -> None:
    references = frozenset(
        reference
        for source in GUI_ROOT.rglob("*")
        if source.is_file() and source.suffix in {".html", ".css", ".js"}
        for reference in local_references(source)
    )
    assert {
        "static/GUI/app.js", "static/GUI/navigation.js", "static/GUI/styles.css",
        "static/XHS-Downloader.png",
    } <= references
    assert references <= set(freezer_files)
    assert all((ROOT / reference).is_file() for reference in references)


@pytest.mark.integration
@pytest.mark.parametrize("catalog", CATALOGS)
def test_packaged_translation_catalogs_are_valid_and_keep_their_paths(
    catalog: str, freezer_files: dict[str, Path]
) -> None:
    assert catalog in freezer_files
    with freezer_files[catalog].open("rb") as stream:
        assert isinstance(GNUTranslations(stream), GNUTranslations)


@pytest.mark.integration
def test_unrelated_samples_helpers_ads_and_userscripts_stay_only_in_source_tree(
    freezer_files: dict[str, Path],
) -> None:
    assert all((ROOT / path).is_file() for path in UNSHIPPED_FILES)
    assert not set(UNSHIPPED_FILES).intersection(freezer_files)
    assert not any(
        target.startswith(("static/other/", "static/screenshot/"))
        or target.endswith((".po", ".pot"))
        or target.startswith("locale/") and not target.endswith(".mo")
        for target in freezer_files
    )


@pytest.mark.unit
def test_existing_executables_packages_and_runtime_metadata_options_are_preserved(
    freeze_build: SimpleNamespace,
) -> None:
    executable = {
        "script": "main.py", "icon": "./static/XHS-Downloader",
        "target_name": "XHS-Downloader",
    }
    gui_executable = {
        "script": "main.py", "base": "gui", "icon": "./static/XHS-Downloader",
        "target_name": "XHS-Downloader-GUI",
    }
    expected = (executable,) + ((gui_executable,) if freeze_build.platform == "win32" else ())
    assert tuple(freeze_build.arguments["executables"]) == expected
    options: dict[str, Any] = freeze_build.arguments["options"]["build_exe"]
    assert options["packages"] == ["rich", "opentelemetry", "uvicorn"]
    assert options["include_msvcr"] is True
    assert freeze_build.arguments["name"] == "XHS-Downloader"


@pytest.mark.unit
@pytest.mark.parametrize("directory", ("example.dist-info", "example.egg-info"))
def test_metadata_helper_keeps_supported_distribution_directories(
    directory: str, freeze_build: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    helper = freeze_build.namespace["include_distribution_metadata"]
    metadata = tmp_path / directory
    lookup = Mock(return_value=SimpleNamespace(_path=metadata))
    monkeypatch.setitem(helper.__globals__, "distribution", lookup)
    assert helper("opentelemetry-api") == (str(metadata), f"lib/{directory}")
    lookup.assert_called_once_with("opentelemetry-api")


@pytest.mark.unit
def test_metadata_helper_rejects_non_metadata_directories(
    freeze_build: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    helper = freeze_build.namespace["include_distribution_metadata"]
    lookup = Mock(return_value=SimpleNamespace(_path=tmp_path / "not-metadata"))
    monkeypatch.setitem(helper.__globals__, "distribution", lookup)
    with pytest.raises(RuntimeError, match="Cannot locate metadata"):
        helper("opentelemetry-api")

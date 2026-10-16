import ast
import importlib.util
import os
import re
import runpy
import shlex
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn
from unittest.mock import Mock
from zipfile import ZipFile

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / ".github" / "scripts" / "release_tools.py"
SCRIPT_ARGUMENT = ".github/scripts/release_tools.py"
DOCKER_WORKFLOW = "Manually_docker_image.yml"
RELEASE_WORKFLOW = "Release_build_executable_program.yml"
ARTIFACTS = ("Windows_X64", "Windows_ARM64", "macOS_X64", "macOS_ARM64")
ARCHIVE_STEPS = (
    ("创建签名版本压缩包", "signed", "matrix.sign && env.SIGNPATH_AVAILABLE == 'true'"),
    (
        "创建未签名 Windows 压缩包",
        "dist/XHS-Downloader",
        "matrix.sign && env.SIGNPATH_AVAILABLE != 'true'",
    ),
    ("创建 macOS 压缩包", "dist/XHS-Downloader", "${{ !matrix.sign }}"),
)
PAYLOADS = (
    'v2.8"; marker(); #',
    "v2.8'; marker(); #",
    "v2.8\nTAGS=injected",
    "v2.8\rLATEST_TAG=injected",
    "v2.8${MARKER}",
    "v2.8$(marker)",
    "v2.8`marker`",
    "v2.8;marker",
    "../outside",
    "v2.8/../../outside",
    "v2.8\\..\\outside",
)


@pytest.fixture(autouse=True)
def source_modules(
    monkeypatch: pytest.MonkeyPatch, offline_network: None
) -> None:
    """Keep the root network guard without bootstrapping the application."""

    def deny_process(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("CI input tests must not execute external processes")

    monkeypatch.setattr(subprocess, "Popen", deny_process)
    monkeypatch.setattr(os, "system", deny_process)
    monkeypatch.setattr(os, "popen", deny_process)


@pytest.fixture
def release_tools() -> ModuleType:
    assert SCRIPT_PATH.is_file(), "The isolated release input helper is missing"
    spec = importlib.util.spec_from_file_location("ci_release_tools", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workflow_steps(filename: str) -> Iterator[dict[str, Any]]:
    path = REPOSITORY_ROOT / ".github" / "workflows" / filename
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        yield from job["steps"]


def named_step(filename: str, name: str) -> dict[str, Any]:
    return next(step for step in workflow_steps(filename) if step["name"] == name)


def archive_arguments(step: dict[str, Any], tag: str, artifact: str) -> list[str]:
    """Bind only the two declared env inputs; never run or evaluate the shell text."""
    values = {
        "${{ github.event.release.tag_name }}": tag,
        "${{ matrix.artifact }}": artifact,
    }
    environment = {key: values[value] for key, value in step["env"].items()}
    command = shlex.split(step["run"])
    assert command[:6] == ["uv", "run", "--no-sync", "python", "-I", SCRIPT_ARGUMENT]
    return [
        environment[token[1:]] if token.startswith("$") else token
        for token in command[6:]
    ]


@pytest.mark.unit
@pytest.mark.parametrize("filename", [DOCKER_WORKFLOW, RELEASE_WORKFLOW])
def test_event_data_is_never_interpolated_into_workflow_source(filename: str) -> None:
    for step in workflow_steps(filename):
        assert not re.search(
            r"\$\{\{\s*(?:github\.event\.(?:inputs|release)\b|inputs\.)",
            step.get("run", ""),
        ), step["name"]


@pytest.mark.unit
def test_latest_release_uses_env_and_validates_before_writing_github_env() -> None:
    step = named_step(DOCKER_WORKFLOW, "获取最新的发布标签")
    assert step["env"] == {
        "CUSTOM_VERSION": "${{ github.event.inputs.custom_version }}",
        "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
    }
    source = step["run"]
    assert 'if [ -z "$CUSTOM_VERSION" ]; then' in source
    assert 'LATEST_TAG="$CUSTOM_VERSION"' in source
    assert 'curl -fsS' in source
    assert '-H "Authorization: Bearer $GH_TOKEN"' in source
    assert (
        '"https://api.github.com/repos/${GITHUB_REPOSITORY}/releases/latest"' in source
    )
    validate = f'LATEST_TAG=$(python3 -I {SCRIPT_ARGUMENT} validate "$LATEST_TAG")'
    write = 'printf \'LATEST_TAG=%s\\n\' "$LATEST_TAG" >> "$GITHUB_ENV"'
    assert source.index(validate) < source.index(write)
    assert "${{" not in source


@pytest.mark.unit
def test_beta_tag_generation_uses_quoted_env_and_python_decimal_bump() -> None:
    step = named_step(DOCKER_WORKFLOW, "生成标签")
    assert step["env"] == {"IS_BETA": "${{ inputs.is_beta }}"}
    source = step["run"]
    assert 'if [ "$IS_BETA" = "true" ]; then' in source
    bump = f'LATEST_TAG=$(python3 -I {SCRIPT_ARGUMENT} bump "$LATEST_TAG")'
    validate = f'python3 -I {SCRIPT_ARGUMENT} validate "${{LATEST_TAG}}-dev"'
    write = 'printf \'LATEST_TAG=%s\\n\' "$LATEST_TAG" >> "$GITHUB_ENV"'
    assert source.index(bump) < source.index(validate) < source.index(write)
    assert not re.search(r"\$\(\(", source)
    assert "${{" not in source
    assert (
        'TAGS="${DOCKER_REPO}:${LATEST_TAG}-dev,${GHCR_REPO}:${LATEST_TAG}-dev"'
        in source
    )
    assert (
        'TAGS="${DOCKER_REPO}:${LATEST_TAG},${DOCKER_REPO}:latest,'
        '${GHCR_REPO}:${LATEST_TAG},${GHCR_REPO}:latest"'
    ) in source
    assert 'printf \'TAGS=%s\\n\' "$TAGS" >> "$GITHUB_ENV"' in source
    assert 'printf \'LATEST_TAG=%s\\n\' "$LATEST_TAG" >> "$GITHUB_ENV"' in source


@pytest.mark.unit
@pytest.mark.parametrize("name,source,condition", ARCHIVE_STEPS)
def test_archive_steps_use_env_and_fixed_isolated_python_argv(
    name: str, source: str, condition: str
) -> None:
    step = named_step(RELEASE_WORKFLOW, name)
    assert step["env"] == {
        "RELEASE_TAG": "${{ github.event.release.tag_name }}",
        "ARTIFACT_NAME": "${{ matrix.artifact }}",
    }
    assert step["if"] == condition
    assert step["shell"] == "bash"
    assert '"$RELEASE_TAG" "$ARTIFACT_NAME"' in step["run"]
    assert archive_arguments(step, "v2.8", "Windows_X64") == [
        "archive", "v2.8", "Windows_X64", source
    ]


@pytest.mark.unit
@pytest.mark.parametrize("payload", PAYLOADS)
def test_dispatch_and_release_payloads_remain_env_data(payload: str) -> None:
    steps = (
        (DOCKER_WORKFLOW, "获取最新的发布标签", "CUSTOM_VERSION", "inputs.custom_version"),
        *( (RELEASE_WORKFLOW, name, "RELEASE_TAG", "release.tag_name")
           for name, _, _ in ARCHIVE_STEPS ),
    )
    for filename, name, variable, context in steps:
        step = named_step(filename, name)
        expression = "${{ github.event." + context + " }}"
        assert step["env"][variable] == expression
        assert step["env"][variable].replace(expression, payload) == payload
        assert step["run"].replace(expression, payload) == step["run"]


@pytest.mark.unit
@pytest.mark.parametrize("payload", PAYLOADS)
def test_archive_payloads_reach_helper_only_as_single_data_arguments(
    release_tools: ModuleType, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, payload: str
) -> None:
    monkeypatch.chdir(tmp_path)
    make_archive = Mock()
    monkeypatch.setattr(release_tools.shutil, "make_archive", make_archive)
    for name, _, _ in ARCHIVE_STEPS:
        step = named_step(RELEASE_WORKFLOW, name)
        for tag, artifact in ((payload, "Windows_X64"), ("v2.8", payload)):
            arguments = archive_arguments(step, tag, artifact)
            assert arguments[:3] == ["archive", tag, artifact]
            assert len(arguments) == 4
            with pytest.raises(SystemExit) as error:
                release_tools.main(arguments)
            assert error.value.code == 2
    make_archive.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_existing_release_artifacts_and_upload_paths_are_preserved() -> None:
    path = REPOSITORY_ROOT / ".github" / "workflows" / RELEASE_WORKFLOW
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    matrix = workflow["jobs"]["build"]["strategy"]["matrix"]["include"]
    assert {entry["artifact"] for entry in matrix} == set(ARTIFACTS)
    step = named_step(RELEASE_WORKFLOW, "上传构建产物")
    assert step["uses"].partition("@")[0] == "actions/upload-artifact"
    assert step["with"]["path"] == (
        "./XHS-Downloader_V${{ github.event.release.tag_name }}_"
        "${{ matrix.artifact }}.zip"
    )


@pytest.mark.unit
def test_release_build_keeps_frozen_group_and_no_sync_execution() -> None:
    commands = [
        shlex.split(line)
        for step in workflow_steps(RELEASE_WORKFLOW)
        for line in step.get("run", "").splitlines()
        if line.strip()
    ]
    syncs = [command for command in commands if command[:2] == ["uv", "sync"]]
    assert len(syncs) == 1
    assert "--frozen" in syncs[0]
    assert syncs[0][syncs[0].index("--group") + 1] == "build"
    runs = [command for command in commands if command[:2] == ["uv", "run"]]
    assert len(runs) == 5
    assert all("--no-sync" in command[2:command.index("python")] for command in runs)


@pytest.mark.unit
@pytest.mark.parametrize(
    "tag", ["v2.8", "2.8.1", "v2.8-beta", "v2.8_dev", "_dev", "A_9.x-Y", "a" * 128]
)
def test_safe_ascii_tags_are_preserved(release_tools: ModuleType, tag: str) -> None:
    assert release_tools.validate_tag(tag) == tag


@pytest.mark.unit
@pytest.mark.parametrize(
    "tag",
    ["", ".", "..", "-beta", " v2.8", "v2.8 ", "v2.8:other", "版本", "v2.8\x00",
     "v2.8\t", "v2.8\n", "v2.8\r\n", "a" * 129, *PAYLOADS],
)
def test_unsafe_tags_are_rejected(release_tools: ModuleType, tag: str) -> None:
    with pytest.raises(ValueError, match="tag"):
        release_tools.validate_tag(tag)


@pytest.mark.unit
@pytest.mark.parametrize(
    "tag,expected",
    [("v2.8", "v2.9"), ("2.8.1", "2.8.2"), ("v2.009", "v2.10"),
     ("2.000", "2.1"), ("009", "10"), ("v2." + "9" * 90, "v2.1" + "0" * 90)],
)
def test_beta_bump_uses_decimal_python_integers(
    release_tools: ModuleType, tag: str, expected: str
) -> None:
    assert release_tools.bump_tag(tag) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "tag", ["v2.8-beta", "v2.8_dev", "v2.", "v2", "v2.-1", "v2.0x10",
            "v2.1e3", "v2.1_0", "v2.１２", "a." + "9" * 126, *PAYLOADS]
)
def test_unbumpable_or_overlong_beta_tags_are_rejected(
    release_tools: ModuleType, tag: str
) -> None:
    with pytest.raises(ValueError):
        release_tools.bump_tag(tag)


@pytest.mark.unit
def test_appending_dev_cannot_produce_an_overlong_docker_tag(
    release_tools: ModuleType,
) -> None:
    tag = "a." + "8" * 126
    bumped = release_tools.bump_tag(tag)
    assert len(bumped) == 128
    with pytest.raises(ValueError, match="tag"):
        release_tools.validate_tag(bumped + "-dev")


@pytest.mark.unit
@pytest.mark.parametrize("artifact", ARTIFACTS)
@pytest.mark.parametrize("source", ["signed", "dist/XHS-Downloader"])
def test_archive_preserves_all_four_basenames_and_fixed_roots(
    release_tools: ModuleType, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, artifact: str, source: str
) -> None:
    monkeypatch.chdir(tmp_path)
    basename = f"XHS-Downloader_Vv2.8-beta_dev_{artifact}"
    make_archive = Mock(return_value=str(tmp_path / f"{basename}.zip"))
    monkeypatch.setattr(release_tools.shutil, "make_archive", make_archive)

    result = release_tools.create_archive("v2.8-beta_dev", artifact, source)

    make_archive.assert_called_once_with(basename, "zip", source)
    assert result == str(tmp_path / f"{basename}.zip")
    assert Path(basename).parent == Path(".")
    assert Path.cwd() == tmp_path


@pytest.mark.unit
@pytest.mark.parametrize(
    "tag,artifact,source",
    [
        *((payload, "Windows_X64", "signed") for payload in PAYLOADS),
        *(("v2.8", payload, "signed") for payload in PAYLOADS),
        ("v2.8", "unexpected", "signed"),
        ("v2.8", "windows_x64", "signed"),
        *(
            ("v2.8", "Windows_X64", source)
            for source in (
                "../outside", "/tmp/outside", "signed/../outside", "other", ""
            )
        ),
    ],
)
def test_invalid_archive_inputs_do_not_create_files_or_call_archiver(
    release_tools: ModuleType, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, tag: str, artifact: str, source: str
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"unchanged")
    monkeypatch.chdir(work)
    make_archive = Mock()
    monkeypatch.setattr(release_tools.shutil, "make_archive", make_archive)

    with pytest.raises(ValueError):
        release_tools.create_archive(tag, artifact, source)

    make_archive.assert_not_called()
    assert list(work.iterdir()) == []
    assert set(tmp_path.iterdir()) == {work, outside}
    assert outside.read_bytes() == b"unchanged"


@pytest.mark.integration
@pytest.mark.parametrize("source", ["signed", "dist/XHS-Downloader"])
def test_archive_writes_a_real_local_zip_with_original_contents(
    release_tools: ModuleType, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, source: str
) -> None:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / source
    (root / "nested").mkdir(parents=True)
    (root / "README.txt").write_bytes(b"offline release fixture")
    (root / "nested" / "payload.bin").write_bytes(b"\x00\x01")

    assert release_tools.main(["archive", "2.8.1", "macOS_X64", source]) == 0

    archive = tmp_path / "XHS-Downloader_V2.8.1_macOS_X64.zip"
    with ZipFile(archive) as result:
        assert result.read("README.txt") == b"offline release fixture"
        assert result.read("nested/payload.bin") == b"\x00\x01"
        assert not any(
            name.startswith(("signed/", "dist/")) for name in result.namelist()
        )
    assert set(tmp_path.glob("*.zip")) == {archive}


@pytest.mark.e2e
@pytest.mark.parametrize("name,source,condition", ARCHIVE_STEPS)
def test_archive_workflow_argv_creates_only_a_local_release_zip(
    release_tools: ModuleType, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, name: str, source: str, condition: str
) -> None:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / source
    root.mkdir(parents=True)
    (root / "executable.txt").write_bytes(b"not an executable; local fixture only")
    step = named_step(RELEASE_WORKFLOW, name)
    arguments = archive_arguments(step, "v2.8", "Windows_ARM64")

    assert release_tools.main(arguments) == 0

    with ZipFile(tmp_path / "XHS-Downloader_Vv2.8_Windows_ARM64.zip") as result:
        assert (
            result.read("executable.txt") == b"not an executable; local fixture only"
        )
    assert not (tmp_path.parent / "outside.zip").exists()


@pytest.mark.unit
@pytest.mark.parametrize("arguments", [["validate", "v2.8"], ["bump", "v2.008"]])
def test_validation_cli_returns_only_the_safe_tag(
    release_tools: ModuleType, capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    assert release_tools.main(arguments) == 0
    expected = "v2.8\n" if arguments[0] == "validate" else "v2.9\n"
    assert capsys.readouterr().out == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "arguments", [["validate", "v2.8\nLATEST_TAG=bad"], ["bump", "v2.beta"],
                  ["validate", "--help"], ["validate", "-h"], ["bump", "--help"],
                  ["archive", "--help", "Windows_X64", "signed"],
                  ["archive", "v2.8", "--help", "signed"],
                  ["archive", "v2.8", "../outside", "signed"], [], ["unknown"]]
)
def test_invalid_cli_arguments_fail_without_output_or_archive(
    release_tools: ModuleType, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    make_archive = Mock()
    monkeypatch.setattr(release_tools.shutil, "make_archive", make_archive)
    with pytest.raises(SystemExit) as error:
        release_tools.main(arguments)
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "error:" in captured.err
    make_archive.assert_not_called()


@pytest.mark.unit
def test_archive_errors_are_reported_without_hiding_failure(
    release_tools: ModuleType, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        release_tools.shutil,
        "make_archive",
        Mock(side_effect=OSError("missing source")),
    )
    with pytest.raises(SystemExit) as error:
        release_tools.main(["archive", "v2.8", "Windows_X64", "signed"])
    assert error.value.code == 2
    assert "missing source" in capsys.readouterr().err


@pytest.mark.unit
def test_helper_has_no_dynamic_execution_or_process_imports() -> None:
    assert SCRIPT_PATH.is_file(), "The isolated release input helper is missing"
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    modules = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert modules <= {"argparse", "collections.abc", "re", "shutil", "sys"}
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in {"eval", "exec", "compile", "__import__"}
        for node in ast.walk(tree)
    )
    assert len(SCRIPT_PATH.read_text(encoding="utf-8").splitlines()) < 200
    assert all(
        node.end_lineno - node.lineno + 1 < 50
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


@pytest.mark.unit
def test_helper_entrypoint_is_tested_in_process_without_launching_project_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert SCRIPT_PATH.is_file(), "The isolated release input helper is missing"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT_PATH), "validate", "v2.8"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(SCRIPT_PATH), run_name="__main__")
    assert error.value.code == 0
    assert capsys.readouterr().out == "v2.8\n"

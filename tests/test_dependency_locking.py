import importlib.util
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import Mock

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    "Manually_build_executable_programs.yml",
    "Release_build_executable_program.yml",
)
HEADER = (
    "# Generated from uv.lock; do not edit.\n"
    "# Regenerate with: python tools/export_requirements.py\n"
)
EXPORT_COMMAND = (
    "uv",
    "export",
    "--frozen",
    "--no-dev",
    "--no-group",
    "build",
    "--no-emit-project",
    "--no-editable",
    "--no-header",
    "--no-annotate",
    "--offline",
)
SAMPLE_REQUIREMENT = (
    "example==1.2.3 ; sys_platform == 'win32' and python_full_version < '3.14' "
    "and platform_version == 'test  release'"
)
SAMPLE_EXPORT = (
    f"{SAMPLE_REQUIREMENT} \\\n"
    f"    --hash=sha256:{'a' * 64} \\\n"
    f"    --hash=sha256:{'b' * 64}\n"
    "other==4.5.6 \\\n"
    f"    --hash=sha256:{'c' * 64}\n"
)


class Requirement(NamedTuple):
    name: str
    version: str
    marker: str
    hashes: frozenset[str]


def load_toml(filename: str) -> dict[str, Any]:
    return tomllib.loads((REPOSITORY_ROOT / filename).read_text(encoding="utf-8"))


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def logical_lines(content: str) -> tuple[str, ...]:
    # Independently reproduce pip's logical continuation lines, not uv's resolver.
    joined = " ".join(
        part.strip(" \t") for part in content.replace("\r\n", "\n").split("\\\n")
    )
    return tuple(
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def read_requirements() -> tuple[Requirement, ...]:
    content = (REPOSITORY_ROOT / "requirements.txt").read_text(encoding="utf-8")
    requirements = []
    for line in logical_lines(content):
        requirement, separator, hashes = line.partition(" --hash=")
        assert separator, f"Missing artifact hashes: {line}"
        match = re.fullmatch(
            r"([\w.-]+)(?:\[[\w,.-]+\])?==([^\s;]+)(?:\s*;\s*(.+))?",
            requirement,
        )
        assert match, f"Expected an exactly pinned requirement: {requirement}"
        hash_options = shlex.split("--hash=" + hashes)
        assert all(
            re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", option)
            for option in hash_options
        ), f"Invalid artifact hashes: {line}"
        requirements.append(
            Requirement(
                normalize_name(match[1]),
                match[2],
                match[3] or "",
                frozenset(option.removeprefix("--hash=") for option in hash_options),
            )
        )
    return tuple(requirements)


def load_exporter() -> ModuleType:
    path = REPOSITORY_ROOT / "tools" / "export_requirements.py"
    assert path.is_file(), "The reproducible requirements exporter is missing"
    spec = importlib.util.spec_from_file_location("dependency_exporter", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workflow_data(filename: str) -> dict[str, Any]:
    path = REPOSITORY_ROOT / ".github" / "workflows" / filename
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def workflow_commands(workflow: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(shlex.split(line))
        for job in workflow["jobs"].values()
        for step in job["steps"]
        for line in step.get("run", "").splitlines()
        if line.strip()
    )


@pytest.mark.unit
def test_cx_freeze_is_pinned_only_in_a_python_compatible_build_group() -> None:
    project = load_toml("pyproject.toml")
    assert project["dependency-groups"].get("build") == ["cx-freeze==8.7.2"]
    assert not any(
        normalize_name(re.split(r"[<>=!~\[]", dependency)[0]) == "cx-freeze"
        for dependency in project["project"]["dependencies"]
    )
    assert project["project"]["requires-python"] == ">=3.12"
    assert project["tool"]["uv"]["dependency-groups"]["build"] == {
        "requires-python": ">=3.12,<3.16"
    }


@pytest.mark.unit
def test_lock_includes_the_pinned_build_group_with_artifact_hashes() -> None:
    packages = load_toml("uv.lock")["package"]
    cx_freeze = next(
        (package for package in packages if package["name"] == "cx-freeze"), None
    )
    assert cx_freeze is not None, "cx-Freeze must be resolved in uv.lock"
    assert cx_freeze["version"] == "8.7.2"
    assert cx_freeze["wheels"]
    assert all(wheel["hash"].startswith("sha256:") for wheel in cx_freeze["wheels"])
    root = next(package for package in packages if package["name"] == "xhs-downloader")
    assert root["dev-dependencies"]["build"] == [
        {"name": "cx-freeze", "marker": "python_full_version < '3.16'"}
    ]
    assert root["group-requires-python"]["build"] == ">=3.12,<3.16"
    assert root["metadata"]["requires-dev"]["build"] == [
        {
            "name": "cx-freeze",
            "marker": "python_full_version >= '3.12' and python_full_version < '3.16'",
            "specifier": "==8.7.2",
        }
    ]


@pytest.mark.unit
def test_runtime_requirements_include_transitives_with_exact_lock_hashes() -> None:
    requirements = read_requirements()
    packages = load_toml("uv.lock")["package"]
    by_identity = {
        (package["name"], package["version"]): package for package in packages
    }
    names = {requirement.name for requirement in requirements}
    assert {"pydantic-core", "httpcore2", "cffi", "email-validator"} <= names
    assert len(names) > len(load_toml("pyproject.toml")["project"]["dependencies"])
    for requirement in requirements:
        package = by_identity[(requirement.name, requirement.version)]
        artifacts = package.get("wheels", []) + (
            [package["sdist"]] if "sdist" in package else []
        )
        assert requirement.hashes == {artifact["hash"] for artifact in artifacts}


@pytest.mark.unit
def test_runtime_requirements_preserve_platform_and_implementation_markers() -> None:
    requirements = read_requirements()
    by_name = {requirement.name: requirement for requirement in requirements}
    assert "win32" in by_name["pythonnet"].marker
    assert "darwin" in by_name["pyobjc-core"].marker
    assert "openbsd6" in by_name["qtpy"].marker
    assert "implementation_name" in by_name["pycparser"].marker


@pytest.mark.unit
def test_runtime_requirements_exclude_the_project_dev_and_build_dependencies() -> None:
    names = {requirement.name for requirement in read_requirements()}
    assert not names & {
        "xhs-downloader",
        "pytest",
        "pytest-cov",
        "coverage",
        "cx-freeze",
        "freeze-core",
        "patchelf",
        "pefile",
    }


@pytest.mark.unit
def test_requirements_are_compact_and_have_a_machine_independent_header() -> None:
    content = (REPOSITORY_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert content.startswith(HEADER)
    assert len(content.splitlines()) < 800
    assert "\\\n" not in content
    assert str(REPOSITORY_ROOT) not in content
    assert len(logical_lines(content)) == len(read_requirements())


@pytest.mark.unit
def test_docker_installs_requirements_in_hash_checking_mode() -> None:
    commands = [
        shlex.split(line.removeprefix("RUN "))
        for line in (REPOSITORY_ROOT / "Dockerfile").read_text().splitlines()
        if line.startswith("RUN pip install ")
    ]
    assert len(commands) == 1
    command = commands[0]
    assert "--require-hashes" in command
    assert command[command.index("-r") + 1] == "requirements.txt"
    assert '--prefix=/install' in command


@pytest.mark.unit
@pytest.mark.parametrize("filename", WORKFLOWS)
def test_executable_workflows_consume_the_frozen_build_group(filename: str) -> None:
    commands = workflow_commands(workflow_data(filename))
    syncs = [command for command in commands if command[:2] == ("uv", "sync")]
    assert len(syncs) == 1
    sync = syncs[0]
    assert {"--frozen", "--no-dev", "--no-install-project"} <= set(sync)
    assert "--group" in sync and sync[sync.index("--group") + 1] == "build"
    assert sync[sync.index("--python") + 1] == "3.12"
    assert not any(command[:3] == ("uv", "pip", "install") for command in commands)
    assert not any(command[:2] == ("uv", "lock") for command in commands)


@pytest.mark.unit
@pytest.mark.parametrize("filename", WORKFLOWS)
def test_every_build_workflow_uv_run_uses_the_already_synced_environment(
    filename: str,
) -> None:
    commands = workflow_commands(workflow_data(filename))
    runs = [command for command in commands if command[:2] == ("uv", "run")]
    assert len(runs) == (3 if filename.startswith("Manually") else 5)
    for command in runs:
        assert "--no-sync" in command[2 : command.index("python")]
        assert not {"--with", "--with-editable", "--isolated"} & set(command)
    assert any("setup.py" in command and "build_exe" in command for command in runs)
    assert any("--version" in " ".join(command) for command in runs)


@pytest.mark.unit
@pytest.mark.parametrize("filename", WORKFLOWS)
def test_build_workflows_keep_all_four_platforms(filename: str) -> None:
    workflow = workflow_data(filename)
    matrix = workflow["jobs"]["build"]["strategy"]["matrix"]["include"]
    assert len(matrix) == 4
    prefix = "XHS-Downloader_" if filename.startswith("Manually") else ""
    assert {
        item["os"]: (item["sign"], item["executable"], item["artifact"])
        for item in matrix
    } == {
        "windows-latest": (
            True,
            "dist/XHS-Downloader/XHS-Downloader.exe",
            f"{prefix}Windows_X64",
        ),
        "windows-11-arm": (
            True,
            "dist/XHS-Downloader/XHS-Downloader.exe",
            f"{prefix}Windows_ARM64",
        ),
        "macos-15-intel": (
            False,
            "dist/XHS-Downloader/XHS-Downloader",
            f"{prefix}macOS_X64",
        ),
        "macos-latest": (
            False,
            "dist/XHS-Downloader/XHS-Downloader",
            f"{prefix}macOS_ARM64",
        ),
    }


@pytest.mark.unit
def test_exporter_uses_frozen_runtime_only_offline_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = load_exporter()
    run = Mock(return_value=SimpleNamespace(stdout=SAMPLE_EXPORT))
    monkeypatch.setattr(exporter.subprocess, "run", run)

    content = exporter.export_requirements()

    run.assert_called_once_with(
        EXPORT_COMMAND,
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert content == HEADER + "\n".join(logical_lines(SAMPLE_EXPORT)) + "\n"
    assert SAMPLE_REQUIREMENT in content


@pytest.mark.unit
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_exporter_writes_deterministic_utf8_and_lf_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, newline: str
) -> None:
    exporter = load_exporter()
    monkeypatch.setattr(exporter, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(
        exporter.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(stdout=SAMPLE_EXPORT.replace("\n", newline))),
    )

    assert exporter.main([]) == 0

    expected = HEADER + "\n".join(logical_lines(SAMPLE_EXPORT)) + "\n"
    assert (tmp_path / "requirements.txt").read_bytes() == expected.encode("utf-8")


@pytest.mark.unit
@pytest.mark.parametrize("matches", [True, False])
def test_exporter_check_does_not_overwrite_requirements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, matches: bool, capsys: Any
) -> None:
    exporter = load_exporter()
    monkeypatch.setattr(exporter, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(
        exporter.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(stdout=SAMPLE_EXPORT)),
    )
    expected = HEADER + "\n".join(logical_lines(SAMPLE_EXPORT)) + "\n"
    original = expected if matches else "outdated==1.0\n"
    output = tmp_path / "requirements.txt"
    output.write_text(original, encoding="utf-8")
    write = Mock(side_effect=AssertionError("--check must not write any files"))
    monkeypatch.setattr(Path, "write_text", write)

    assert exporter.main(["--check"]) == (0 if matches else 1)

    write.assert_not_called()
    assert output.read_text(encoding="utf-8") == original
    if not matches:
        assert "out of date" in capsys.readouterr().err


@pytest.mark.unit
def test_exporter_check_reports_a_missing_file_without_creating_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    exporter = load_exporter()
    monkeypatch.setattr(exporter, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(
        exporter.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(stdout=SAMPLE_EXPORT)),
    )

    assert exporter.main(["--check"]) == 1

    assert not (tmp_path / "requirements.txt").exists()
    assert "Could not export requirements" in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("uv executable not found"),
        subprocess.CalledProcessError(2, EXPORT_COMMAND, stderr="uv export failed"),
    ],
)
def test_export_failures_are_visible_and_do_not_overwrite_requirements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any, error: Exception
) -> None:
    exporter = load_exporter()
    monkeypatch.setattr(exporter, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(exporter.subprocess, "run", Mock(side_effect=error))
    output = tmp_path / "requirements.txt"
    output.write_text("keep the previous export\n", encoding="utf-8")

    assert exporter.main([]) == 1

    assert output.read_text(encoding="utf-8") == "keep the previous export\n"
    assert "Could not export requirements" in capsys.readouterr().err


@pytest.mark.integration
def test_requirements_exactly_reproduce_an_offline_frozen_uv_export() -> None:
    lock = REPOSITORY_ROOT / "uv.lock"
    project = REPOSITORY_ROOT / "pyproject.toml"
    before = (lock.read_bytes(), project.read_bytes())

    result = subprocess.run(
        EXPORT_COMMAND,
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    expected = HEADER + "\n".join(logical_lines(result.stdout)) + "\n"
    assert (REPOSITORY_ROOT / "requirements.txt").read_bytes() == expected.encode("utf-8")
    assert (lock.read_bytes(), project.read_bytes()) == before


@pytest.mark.e2e
def test_exporter_check_works_from_an_unrelated_directory(tmp_path: Path) -> None:
    path = REPOSITORY_ROOT / "tools" / "export_requirements.py"
    assert path.is_file(), "The requirements exporter CLI is missing"
    result = subprocess.run(
        [sys.executable, "-I", str(path), "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "requirements.txt").exists()

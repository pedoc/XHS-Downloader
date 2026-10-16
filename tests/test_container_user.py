"""Static Dockerfile contracts; these checks do not build or run containers."""

import json
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_ID = "10001"
Instruction = tuple[str, str]


@pytest.fixture
def instructions() -> tuple[Instruction, ...]:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    logical_lines = re.sub(r"\\\r?\n\s*", " ", text).splitlines()
    return tuple(
        (line.split(maxsplit=1)[0].upper(), line.split(maxsplit=1)[1].strip())
        for line in logical_lines
        if line.strip() and not line.lstrip().startswith("#")
    )


def values(instructions: tuple[Instruction, ...], keyword: str) -> tuple[str, ...]:
    return tuple(value for name, value in instructions if name == keyword)


@pytest.fixture
def final_stage(instructions: tuple[Instruction, ...]) -> tuple[Instruction, ...]:
    start = max(index for index, (name, _) in enumerate(instructions) if name == "FROM")
    return instructions[start:]


def run_commands(instructions: tuple[Instruction, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(shlex.split(command.strip()))
        for value in values(instructions, "RUN")
        for command in value.split("&&")
    )


@pytest.mark.unit
def test_final_image_uses_an_explicit_ordinary_uid_and_gid(
    final_stage: tuple[Instruction, ...],
) -> None:
    assert values(final_stage, "USER") == (f"{APP_ID}:{APP_ID}",)
    commands = run_commands(final_stage)
    assert ("groupadd", "--gid", APP_ID, "app") in commands
    assert (
        "useradd", "--uid", APP_ID, "--gid", APP_ID, "--no-create-home",
        "--home-dir", "/app/Volume", "--shell", "/usr/sbin/nologin", "app",
    ) in commands


@pytest.mark.unit
def test_only_the_data_directory_is_made_app_owned_before_volume_declaration(
    final_stage: tuple[Instruction, ...],
) -> None:
    commands = run_commands(final_stage)
    assert ("mkdir", "-p", "/app/Volume") in commands
    ownership_commands = tuple(command for command in commands if command[0] == "chown")
    assert ownership_commands == (("chown", f"{APP_ID}:{APP_ID}", "/app/Volume"),)
    assert commands.index(("mkdir", "-p", "/app/Volume")) < commands.index(
        ownership_commands[0]
    )
    assert values(final_stage, "VOLUME") == ("/app/Volume",)
    volume_index = next(i for i, (name, _) in enumerate(final_stage) if name == "VOLUME")
    assert all(
        index < volume_index
        for index, (name, _) in enumerate(final_stage)
        if name == "RUN"
    )


@pytest.mark.unit
def test_code_and_dependencies_keep_default_root_copy_ownership(
    final_stage: tuple[Instruction, ...],
) -> None:
    copies = values(final_stage, "COPY")
    assert copies
    assert all(
        not token.startswith("--chown")
        for copy in copies
        for token in shlex.split(copy)
    )
    assert values(final_stage, "WORKDIR") == ("/app",)
    user_index = next(i for i, (name, _) in enumerate(final_stage) if name == "USER")
    assert all(
        index < user_index
        for index, (name, _) in enumerate(final_stage)
        if name in {"RUN", "COPY"}
    )
    assert not any(command[0] == "chmod" for command in run_commands(final_stage))


@pytest.mark.unit
def test_python_does_not_need_to_write_bytecode_into_root_owned_code(
    final_stage: tuple[Instruction, ...],
) -> None:
    environment = tuple(
        token for value in values(final_stage, "ENV") for token in shlex.split(value)
    )
    assert "PYTHONDONTWRITEBYTECODE=1" in environment


@pytest.mark.integration
def test_existing_platform_dependency_and_startup_contracts_are_preserved(
    instructions: tuple[Instruction, ...], final_stage: tuple[Instruction, ...]
) -> None:
    assert values(instructions, "FROM") == (
        "python:3.12-bookworm AS builder", "python:3.12-slim-bookworm",
    )
    assert "requirements.txt ." in values(instructions, "COPY")
    assert (
        "pip", "install", "--no-cache-dir", "--require-hashes", "--prefix=/install",
        "-r", "requirements.txt",
    ) in run_commands(instructions)
    assert "--from=builder /install /usr/local" in values(final_stage, "COPY")
    assert values(final_stage, "EXPOSE") == ("5556",)
    assert tuple(json.loads(value) for value in values(final_stage, "CMD")) == (
        ["python", "main.py", "TUI"],
    )
    assert not values(instructions, "ENTRYPOINT"), "Do not chown mounted user data at startup"
    user_index = next(i for i, (name, _) in enumerate(final_stage) if name == "USER")
    cmd_index = next(i for i, (name, _) in enumerate(final_stage) if name == "CMD")
    assert user_index < cmd_index


@pytest.mark.integration
@pytest.mark.parametrize(
    ("filename", "heading", "required_words"),
    [
        (
            "README.md", "容器数据目录权限：",
            ("10001:10001", "/app/Volume", "命名卷", "停止", "备份", "手动", "不会自动"),
        ),
        (
            "README_EN.md", "Container data permissions:",
            ("10001:10001", "/app/Volume", "named volumes", "stop", "back up", "manually", "does not automatically"),
        ),
    ],
)
def test_existing_volume_permission_migration_requires_user_backup_and_action(
    filename: str, heading: str, required_words: tuple[str, ...]
) -> None:
    text = (ROOT / filename).read_text(encoding="utf-8")
    paragraph = re.search(rf"<p><b>{re.escape(heading)}</b>(.*?)</p>", text, re.DOTALL)
    assert paragraph is not None, "Document the old root-owned volume upgrade boundary"
    assert all(word in paragraph.group(1) for word in required_words)

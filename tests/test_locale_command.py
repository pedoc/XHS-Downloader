from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
from types import ModuleType
from unittest.mock import Mock, call, patch

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "locale" / "po_to_mo.py"


@pytest.fixture
def locale_module() -> ModuleType:
    spec = spec_from_file_location("locale_command_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    with patch("subprocess.run") as run_mock:
        spec.loader.exec_module(module)
        run_mock.assert_not_called()
    return module


@pytest.mark.parametrize(
    "directory_name",
    [
        "directory with spaces",
        'directory with "double quotes"',
        "directory with 'single quotes'",
        "$(not_a_command)",
        "`not_a_command`",
        'spaces \'single\' "double" $(not_a_command); & literal',
    ],
)
def test_generate_mo_passes_paths_as_literal_arguments(
    directory_name: str,
    tmp_path: Path,
    locale_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    po_file = tmp_path / directory_name / "LC_MESSAGES" / "xhs.po"
    mo_file = po_file.with_suffix(".mo")
    command = ["msgfmt", "--check", "-o", str(mo_file), str(po_file)]
    completed = CompletedProcess(command, 0)
    run_mock = Mock(return_value=completed)
    monkeypatch.setattr(locale_module, "run", run_mock)

    locale_module.generate_mo([(po_file, mo_file)])

    run_mock.assert_called_once_with(command, shell=False, text=True, check=True)
    assert capsys.readouterr().out == f"{completed}\n"


def test_generate_mo_raises_for_nonzero_exit(
    tmp_path: Path,
    locale_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    po_file = tmp_path / "$(not_a_command)" / "xhs.po"
    mo_file = po_file.with_suffix(".mo")
    command = ["msgfmt", "--check", "-o", str(mo_file), str(po_file)]

    def nonzero_exit(
        args: list[str] | str, **kwargs: object
    ) -> CompletedProcess[str]:
        completed = CompletedProcess(args, 7)
        if kwargs.get("check"):
            completed.check_returncode()
        return completed

    run_mock = Mock(side_effect=nonzero_exit)
    monkeypatch.setattr(locale_module, "run", run_mock)

    with pytest.raises(CalledProcessError) as raised:
        locale_module.generate_mo([(po_file, mo_file)])

    assert raised.value.returncode == 7
    assert raised.value.cmd == command
    run_mock.assert_called_once_with(command, shell=False, text=True, check=True)
    assert capsys.readouterr().out == ""


def test_generate_mo_preserves_mapping_order_and_output(
    tmp_path: Path,
    locale_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    po_files = [tmp_path / "zh_CN.po", tmp_path / "en_US.po"]
    maps = locale_module.generate_map(po_files)
    commands = [
        ["msgfmt", "--check", "-o", str(mo_file), str(po_file)]
        for po_file, mo_file in maps
    ]
    completed = [CompletedProcess(command, 0) for command in commands]
    run_mock = Mock(side_effect=completed)
    monkeypatch.setattr(locale_module, "run", run_mock)

    locale_module.generate_mo(maps)

    assert run_mock.call_args_list == [
        call(command, shell=False, text=True, check=True) for command in commands
    ]
    assert capsys.readouterr().out == "".join(f"{result}\n" for result in completed)


def test_scan_directory_keeps_all_direct_directories(
    tmp_path: Path,
    locale_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locale_root = tmp_path / "locales"
    locale_root.mkdir()
    locale_names = ["en US", "zh_CN"]
    for locale_name in locale_names:
        (locale_root / locale_name).mkdir()
    (locale_root / "zh_CN" / "nested").mkdir()
    (locale_root / "README.txt").write_text("not a locale directory", encoding="utf-8")
    monkeypatch.setattr(locale_module, "ROOT", locale_root)

    files = locale_module.scan_directory()

    assert set(files) == {
        locale_root / locale_name / "LC_MESSAGES" / "xhs.po"
        for locale_name in locale_names
    }
    assert locale_module.generate_map(files) == [
        (po_file, po_file.with_suffix(".mo")) for po_file in files
    ]


def test_generate_mo_does_not_run_commands_for_empty_maps(
    locale_module: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    locale_module.generate_mo([])

    locale_module.run.assert_not_called()
    assert capsys.readouterr().out == ""

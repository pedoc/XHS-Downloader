import asyncio
from collections.abc import AsyncIterator, Callable
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

Factory = Callable[..., Any]
pytestmark = pytest.mark.integration
BAD_IDS = [
    "", "../escape", r"..\escape", "/escape", r"C:\escape", "C:escape",
    r"\\host\share", "id:stream", "中文", "CON", None, 42,
]
SAFE_COMPONENTS = ["笔记 中文", "note-1_A", "COM10", "a.b", "a..b"]
BAD_COMPONENTS = BAD_IDS[:8] + [
    None, 1, ".", "..", "CON", "con.txt", "NUL", "LPT9.log", "COM¹",
    "trailing.", "trailing ", "bad?", "a\x00b", "a\x7fb",
]
STORE_CASES = [("settings", "")] + [
    (kind, suffix) for kind in ("IDRecorder", "DataRecorder", "MapRecorder")
    for suffix in ("", "-journal", "-wal", "-shm")
] + [
    (kind, mode) for kind in ("settings", "IDRecorder", "MapRecorder")
    for mode in ("legacy-safe", "legacy-link")
]


def link_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows does not grant symbolic-link creation permission")
        raise


def mock_stream(manager: Any) -> None:
    async def chunks(size: int) -> AsyncIterator[bytes]:
        yield b"offline media"

    context = AsyncMock()
    context.__aenter__.return_value = SimpleNamespace(
        status_code=200, headers={}, raise_for_status=Mock(), aiter_content=chunks,
    )
    manager.download_client.stream = Mock(return_value=context)


class TestPathSecurity:
    @pytest.fixture
    def core_factory(
        self, source_modules: SimpleNamespace, manager_factory: Factory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Factory:
        def checked(manager: Any, url: str, purpose: str, **options: Any) -> Any:
            assert purpose == "media"
            return manager.download_client.stream("GET", url, headers=options.get("headers"))

        monkeypatch.setattr(import_module("source.application.download"), "checked_stream", checked)

        def create(**options: Any) -> Any:
            module = source_modules.app
            manager = manager_factory(**options)
            core = object.__new__(module.XHS)
            core.manager, core.mapping_data = manager, {}
            core.downloader = module.Download(manager)
            database = SimpleNamespace(select=AsyncMock(return_value=None), add=AsyncMock())
            core.mapping = module.Mapping(manager, database)
            core.id_recorder = SimpleNamespace(add=AsyncMock())
            core.note_generator = module.NoteGenerator(manager.note_format, manager.folder)
            core.save_data, core.logging = AsyncMock(), Mock()
            core._XHS__extract_video = Mock()
            mock_stream(manager)
            return core

        return create

    @pytest.fixture
    def metadata(self) -> dict[str, Any]:
        return {
            "作品ID": "note-1_A", "作者ID": "author-2_B", "作者昵称": "中文昵称",
            "作品标题": "中文 标题", "作品描述": "正文", "作品标签": "标签",
            "作品类型": "视频", "发布时间": "2026-10-16 10:20:30",
            "最后更新时间": "2026-10-16 10:20:30", "收藏数量": "1", "评论数量": "2",
            "分享数量": "3", "点赞数量": "4", "作者链接": "https://example.invalid/author",
            "下载地址": ["https://sns-video-bd.xhscdn.com/offline-media"], "动图地址": [None],
            "封面地址": None, "时间戳": 1700000000,
        }

    @pytest.fixture
    def outside(self, tmp_path: Path) -> Path:
        directory = tmp_path / "outside"
        directory.mkdir()
        sentinel = directory / "old_sentinel.txt"
        sentinel.write_bytes(b"must remain unchanged")
        return sentinel

    @pytest.mark.unit
    @pytest.mark.parametrize("value", SAFE_COMPONENTS + BAD_COMPONENTS)
    def test_component_preserves_or_rejects_without_cleaning(self, value: Any) -> None:
        validate_component = import_module("source.expansion.paths").validate_component
        if value in SAFE_COMPONENTS:
            assert validate_component(value) == value
        else:
            with pytest.raises(ValueError):
                validate_component(value)

    @pytest.mark.parametrize("names", [
        ("chosen 中文 directory", ""), ("K", "K"), ("İ", "i̇"), ("CaseRoot", "caseroot"),
    ])
    def test_trusted_roots_and_strict_containment(self,
        manager_factory: Factory, tmp_path: Path, tmp_volume: Path, names: tuple[str, str],
    ) -> None:
        contained_path = import_module("source.expansion.paths").contained_path
        chosen = tmp_path / names[0]
        chosen.mkdir()
        manager = manager_factory(path=str(chosen))
        assert manager.path == chosen.resolve()
        assert manager.folder == chosen / "Download"
        assert manager.temp == tmp_volume / "Temp"
        candidate = tmp_volume / "new" / "file"
        assert contained_path(tmp_volume, candidate) == candidate
        for candidate in (tmp_volume, tmp_volume / ".." / "escape"):
            with pytest.raises(ValueError):
                contained_path(tmp_volume, candidate)
        if names[1]:
            pytest.importorskip("_winapi")
            sibling = tmp_path / names[1]
            if names[0] == "CaseRoot":
                assert contained_path(chosen, sibling / "new.txt") == chosen / "new.txt"
            else:
                sibling.mkdir()
                sentinel = sibling / "sentinel"
                sentinel.write_bytes(b"external")
                assert not chosen.samefile(sibling)
                with pytest.raises(ValueError):
                    contained_path(chosen, sentinel)
                assert sentinel.read_bytes() == b"external"

    @pytest.mark.e2e
    @pytest.mark.parametrize("author_archive", [False, True])
    @pytest.mark.parametrize("folder_mode", [False, True])
    @pytest.mark.parametrize("empty_names", [False, True])
    def test_unicode_fallback_layout_and_mtime(self,
        core_factory: Factory, metadata: dict, author_archive: bool,
        folder_mode: bool, empty_names: bool,
    ) -> None:
        core = core_factory(
            author_archive=author_archive, folder_mode=folder_mode, note_format="all",
            name_format="" if empty_names else "作品标题", write_mtime=True,
        )
        if empty_names:
            metadata = metadata | {"作者昵称": "", "作品标题": ""}
        name = "author-2_B_note-1_A" if empty_names else "中文_标题"
        (core.manager.temp / f"{name}.mp4").write_bytes(b"partial ")
        count = SimpleNamespace(success=0, fail=0, skip=0)
        result = asyncio.run(core._deal_download_tasks(
            metadata, None, metadata["作品ID"], True, None, count,
        ))
        nickname = "author-2_B" if empty_names else "中文昵称"
        path = core.manager.folder
        if author_archive:
            path = path / f"author-2_B_{nickname}"
        path = path / name if folder_mode else path
        assert result["作品ID"] == "note-1_A"
        assert (path / f"{name}.mp4").read_bytes() == b"partial offline media"
        headers = core.manager.download_client.stream.call_args.kwargs["headers"]
        assert headers["Range"] == "bytes=8-"
        assert int((path / f"{name}.mp4").stat().st_mtime) == metadata["时间戳"]
        assert all((path / f"{name}.{fmt}").is_file() for fmt in ("md", "txt"))
        assert (count.success, count.fail) == (1, 0)
        core.id_recorder.add.assert_awaited_once_with("note-1_A")

    @pytest.mark.e2e
    @pytest.mark.parametrize("key", ["作品ID", "作者ID"])
    @pytest.mark.parametrize("bad_id", BAD_IDS)
    @pytest.mark.parametrize("layout", [(False, False), (False, True), (True, False), (True, True)])
    def test_bad_ids_fail_before_io_or_records(self,
        core_factory: Factory, metadata: dict, key: str, bad_id: Any, layout: tuple,
    ) -> None:
        core = core_factory(name_format="", author_archive=layout[0], folder_mode=layout[1])
        count = SimpleNamespace(success=0, fail=0, skip=0)
        result = asyncio.run(core._deal_download_tasks(
            metadata | {key: bad_id}, None, "note", True, None, count,
        ))
        assert count.fail == 1 and count.success == 0 and result.get("message")
        assert not list(core.manager.folder.iterdir())
        core.manager.download_client.stream.assert_not_called()
        core.id_recorder.add.assert_not_awaited()
        core.mapping.database.add.assert_not_awaited()

    @pytest.mark.parametrize("location", ["Temp", "Download"])
    @pytest.mark.parametrize("kind", ["symlink", "junction"])
    def test_derived_roots_cannot_become_external_trust(self,
        manager_factory: Factory, tmp_volume: Path, outside: Path, location: str, kind: str,
    ) -> None:
        link = tmp_volume / location
        if kind == "junction":
            pytest.importorskip("_winapi").CreateJunction(str(outside.parent), str(link))
        else:
            link_or_skip(link, outside.parent)
        with pytest.raises(ValueError):
            manager_factory(path="")
        assert outside.read_bytes() == b"must remain unchanged"
        assert sorted(p.name for p in outside.parent.iterdir()) == [outside.name]

    @pytest.mark.parametrize("location", ["author", "media", "sidecar", "temp", "loop"])
    def test_linked_files_fail_task_without_download_record(self,
        core_factory: Factory, metadata: dict, outside: Path, location: str,
    ) -> None:
        core = core_factory(
            name_format="作品标题", author_archive=location == "author", note_format="txt",
        )
        if location == "author":
            link, target = core.manager.folder / "author-2_B_中文昵称", outside.parent
        else:
            suffix = "txt" if location == "sidecar" else "mp4"
            root = core.manager.temp if location == "temp" else core.manager.folder
            link = root / f"中文_标题.{suffix}"
            target = link if location == "loop" else outside
        link_or_skip(link, target)
        count = SimpleNamespace(success=0, fail=0, skip=0)
        result = asyncio.run(core._deal_download_tasks(metadata, None, "note", True, None, count))
        assert count.fail == 1 and count.success == 0 and result.get("message")
        assert outside.read_bytes() == b"must remain unchanged"
        assert sorted(p.name for p in outside.parent.iterdir()) == [outside.name]
        core.id_recorder.add.assert_not_awaited()
        assert location != "sidecar" or any(core.manager.folder.glob("*.mp4"))

    @pytest.mark.parametrize("kind", ["suffix", "temp", "move", "sidecar", "cleanup"])
    def test_independent_io_entry_points_check_final_links(self,
        manager_factory: Factory, outside: Path, kind: str,
    ) -> None:
        module = import_module("source.application.app")
        manager = manager_factory()
        downloader = module.Download(manager)
        temp = manager.temp / "safe.jpeg"
        temp.write_bytes(b"\x89PNG\r\n\x1a\n")
        link = temp if kind == "temp" else manager.folder / (
            "safe.png" if kind == "suffix" else "safe.txt"
        )
        if kind == "temp":
            temp.unlink()
        link_or_skip(link, outside.parent if kind == "cleanup" else outside)
        suffix = downloader._Download__suffix_with_file
        generator = module.NoteGenerator("txt", manager.folder)
        actions = {
            "temp": lambda: downloader._Download__update_headers_range({}, temp),
            "suffix": lambda: asyncio.run(suffix(temp, manager.folder, "safe", "jpeg")),
            "move": lambda: manager.move(temp, link),
            "sidecar": lambda: asyncio.run(generator.write_note_info("unsafe", link)),
            "cleanup": lambda: asyncio.run(manager.close()),
        }
        with pytest.raises(ValueError):
            actions[kind]()
        if kind == "temp":
            with pytest.raises(ValueError):
                manager.delete(temp)
        assert outside.read_bytes() == b"must remain unchanged"
        assert manager.root.is_dir()

    @pytest.mark.parametrize("folder_mode", [False, True])
    @pytest.mark.parametrize("location", ["root", "child", "indirect", "collision", "convergent"])
    def test_mapping_preflights_before_first_rename(self,
        manager_factory: Factory, outside: Path, folder_mode: bool, location: str,
    ) -> None:
        Mapping = import_module("source.module.mapping").Mapping
        manager = manager_factory(author_archive=True, folder_mode=folder_mode)
        database = SimpleNamespace(select=AsyncMock(return_value=("old",)), add=AsyncMock())
        old = manager.folder / "author_old"
        if location == "root":
            link_or_skip(old, outside.parent)
        else:
            old.mkdir()
            if location in {"collision", "convergent"}:
                work = old / "old_work" if folder_mode else old
                work.mkdir(exist_ok=True)
                names = ("old_file", "new_file") if location == "collision" else ("old_new", "new_old")
                for name in names:
                    (work / f"{name}.txt").write_bytes(name.encode())
            else:
                (old / "old_safe.txt").write_bytes(b"inside")
                target = outside.parent
                if location == "indirect":
                    target = manager.folder / "existing_work"
                    target.mkdir()
                    link_or_skip(target / "old_external.txt", outside)
                link_or_skip(old / "old_work", target)
        with pytest.raises((ValueError, FileExistsError)):
            asyncio.run(Mapping(manager, database).update_cache("author", "new"))
        assert old.exists() and not (manager.folder / "author_new").exists()
        assert outside.read_bytes() == b"must remain unchanged"
        assert sorted(p.name for p in outside.parent.iterdir()) == [outside.name]
        database.add.assert_not_awaited()

    @pytest.mark.parametrize("old_alias,alias", [
        ("../bad", "new"), ("old", "../bad"), ("C:bad", "new"), ("old", "CON"),
        ("", "new"), ("old", ""), ("old", "new"), ("old", "OLD"),
    ])
    def test_mapping_validates_aliases_and_preserves_case(self,
        manager_factory: Factory, monkeypatch: pytest.MonkeyPatch, old_alias: str, alias: str,
    ) -> None:
        Mapping = import_module("source.module.mapping").Mapping
        manager = manager_factory(author_archive=True, folder_mode=True)
        database = SimpleNamespace(select=AsyncMock(return_value=(old_alias,)), add=AsyncMock())
        if old_alias != "old" or alias not in {"new", "OLD"}:
            with pytest.raises(ValueError):
                asyncio.run(Mapping(manager, database).update_cache("author", alias))
            database.add.assert_not_awaited()
            return
        old = manager.folder / "author_old"
        (old / "old_work").mkdir(parents=True)
        (old / "old_work" / "old_file.txt").write_bytes(b"inside")
        asyncio.run(Mapping(manager, database).update_cache("author", alias))
        folder = next(manager.folder.iterdir())
        work = next(folder.iterdir())
        file = next(work.iterdir())
        assert (folder.name, work.name, file.name) == (
            f"author_{alias}", f"{alias}_work", f"{alias}_file.txt",
        )
        assert file.read_bytes() == b"inside"
        database.add.assert_awaited_once_with("author", alias)
        database.add.reset_mock()
        database.select.return_value = (alias,)
        monkeypatch.setattr(Path, "rename", Mock(side_effect=PermissionError("offline denied")))
        with pytest.raises(PermissionError):
            asyncio.run(Mapping(manager, database).update_cache("author", "next"))
        database.add.assert_not_awaited()

    @pytest.mark.parametrize("unsafe", [False, True, "internal", "custom"])
    def test_default_legacy_download_migration_is_not_redirected(self,
        manager_factory: Factory, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path, outside: Path, unsafe: bool | str,
    ) -> None:
        legacy = tmp_path / "application"
        volume = (tmp_path / "custom" if unsafe == "custom" else legacy) / "Volume"
        volume.mkdir(parents=True)
        monkeypatch.setattr(import_module("source.module.manager"), "ROOT", legacy)
        old = volume.parent / "Download"
        if unsafe == "internal":
            unrelated = legacy / "unrelated"
            unrelated.mkdir()
            (unrelated / "keep").write_bytes(b"must not migrate")
            link_or_skip(old, unrelated)
        else:
            old.mkdir()
        if unsafe in {True, "internal"}:
            if unsafe is True:
                link_or_skip(old / "linked", outside.parent)
            with pytest.raises(ValueError):
                manager_factory(root=volume, path="")
            assert old.is_dir() and not (volume / "Download").exists()
            if unsafe == "internal":
                assert (unrelated / "keep").read_bytes() == b"must not migrate"
        else:
            (old / "old.txt").write_bytes(b"legacy")
            manager = manager_factory(root=volume, path="")
            if unsafe == "custom":
                assert (old / "old.txt").read_bytes() == b"legacy"
                assert not list(manager.folder.iterdir())
            else:
                assert (manager.folder / "old.txt").read_bytes() == b"legacy"
                assert not old.exists()
        assert outside.read_bytes() == b"must remain unchanged"

    @pytest.mark.parametrize("kind,suffix", STORE_CASES)
    def test_store_files_and_fixed_legacy_sources(self,
        source_modules: SimpleNamespace, manager_factory: Factory, outside: Path,
        tmp_volume: Path, monkeypatch: pytest.MonkeyPatch, kind: str, suffix: str,
    ) -> None:
        module = source_modules.settings if kind == "settings" else import_module("source.module.recorder")
        store = module.Settings(tmp_volume) if kind == "settings" else getattr(module, kind)(
            manager_factory(author_archive=True, record_data=True)
        )
        if suffix.startswith("legacy"):
            monkeypatch.setattr(module, "ROOT", tmp_volume.parent)
            source = tmp_volume.parent / store.name
            action = store.migration_file if kind == "settings" else store.compatible
            if suffix == "legacy-safe":
                source.write_bytes(b"legacy")
                action()
                destination = store.path if kind == "settings" else store.file
                assert destination.read_bytes() == b"legacy" and not source.exists()
            else:
                unrelated = tmp_volume.parent / "unrelated"
                unrelated.write_bytes(b"must not migrate")
                link_or_skip(source, unrelated)
                with pytest.raises(ValueError):
                    action()
                assert unrelated.read_bytes() == b"must not migrate"
        elif kind == "settings":
            outside.write_text('{"sentinel": "unchanged"}', encoding=store.encode)
            link_or_skip(store.path, outside)
            for operation in (store.read, store.create, lambda: store.update({})):
                with pytest.raises(ValueError, match="authorized"):
                    operation()
            assert "unchanged" in outside.read_text(encoding=store.encode)
        else:
            connect = AsyncMock()
            monkeypatch.setattr(module, "connect", connect)
            link_or_skip(store.file.with_name(store.file.name + suffix), outside)
            with pytest.raises(ValueError, match="authorized"):
                asyncio.run(store._connect_database())
            connect.assert_not_awaited()
            assert outside.read_bytes() == b"must remain unchanged"

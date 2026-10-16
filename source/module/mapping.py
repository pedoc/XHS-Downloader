from collections.abc import Iterable
from os.path import normcase
from pathlib import Path
from typing import TYPE_CHECKING

from ..expansion.paths import ID_TOKEN, contained_path, validate_component
from ..translation import _
from .static import ERROR
from .tools import logging

if TYPE_CHECKING:
    from manager import Manager
    from recorder import MapRecorder


__all__ = ["Mapping"]


class Mapping:
    def __init__(
        self,
        manager: "Manager",
        mapping: "MapRecorder",
    ):
        self.work_root = manager.path
        self.root = manager.folder
        self.folder_mode = manager.folder_mode
        self.database = mapping
        self.switch = manager.author_archive
        self.print = manager.print

    async def update_cache(
        self,
        id_: str,
        alias: str,
    ):
        if not self.switch:
            return
        validate_component(id_)
        if not ID_TOKEN.fullmatch(id_):
            raise ValueError(f"Invalid metadata ID: {id_!r}")
        validate_component(alias)
        old_alias = await self.has_mapping(id_)
        if old_alias is not None:
            validate_component(old_alias)
            if old_alias != alias:
                self.__check_file(id_, alias, old_alias)
        await self.database.add(id_, alias)

    def __path(self, path: Path) -> Path:
        root = contained_path(self.work_root, self.root)
        return contained_path(root, path)

    def __check_rename_paths(self, old: Path, new: Path) -> None:
        validate_component(old.name)
        validate_component(new.name)
        self.__path(old)
        self.__path(new)
        if new.exists() and not old.samefile(new):
            raise FileExistsError(f"Mapping destination already exists: {new}")

    def __preflight(self, old: Path, new: Path, alias: str, old_alias: str) -> None:
        self.__check_rename_paths(old, new)
        pending = [old]
        visited = set()
        while pending:
            folder = self.__path(pending.pop())
            if folder.parts in visited:
                continue
            visited.add(folder.parts)
            for child in folder.iterdir():
                child = self.__path(child)
                if child.is_dir():
                    pending.append(child)
        self.__preflight_names(old, alias, old_alias)

    def __preflight_names(self, root: Path, alias: str, old_alias: str) -> None:
        items = tuple(root.iterdir())
        if not self.folder_mode:
            self.__preflight_batch(items, alias, old_alias)
            return
        folders = tuple(item for item in items if item.is_dir())
        self.__preflight_batch(folders, alias, old_alias)
        for folder in folders:
            self.__preflight_batch(folder.iterdir(), alias, old_alias)

    def __preflight_batch(
        self, files: Iterable[Path], alias: str, old_alias: str
    ) -> None:
        destinations = set()
        for file in files:
            new = file.with_name(file.name.replace(old_alias, alias, 1))
            self.__check_rename_paths(file, new)
            key = normcase(new.name)
            if key in destinations:
                raise FileExistsError(f"Duplicate mapping destination: {new}")
            destinations.add(key)

    async def has_mapping(self, id_: str) -> str | None:
        return d[0] if (d := await self.database.select(id_)) else None

    def __check_file(
        self,
        id_: str,
        alias: str,
        old_alias: str,
    ):
        old_folder = self.root.joinpath(f"{id_}_{old_alias}")
        new_folder = self.root.joinpath(f"{id_}_{alias}")
        self.__path(old_folder)
        self.__path(new_folder)
        if not old_folder.is_dir():
            logging(
                self.print,
                _("{old_folder} 文件夹不存在，跳过处理").format(
                    old_folder=old_folder.name
                ),
            )
            return
        self.__preflight(old_folder, new_folder, alias, old_alias)
        self.__rename_folder(
            old_folder,
            id_,
            alias,
        )
        self.__scan_file(
            id_,
            alias,
            old_alias,
        )

    def __rename_folder(
        self,
        old_folder: Path,
        id_: str,
        alias: str,
    ):
        new_folder = self.root.joinpath(f"{id_}_{alias}")
        self.__rename(
            old_folder,
            new_folder,
            _("文件夹"),
        )
        logging(
            self.print,
            _("文件夹 {old_folder} 已重命名为 {new_folder}").format(
                old_folder=old_folder.name, new_folder=new_folder.name
            ),
        )

    def __rename_works_folder(
        self,
        old_: Path,
        alias: str,
        old_alias: str,
    ) -> Path:
        if old_alias in old_.name:
            new_ = old_.parent / old_.name.replace(old_alias, alias, 1)
            self.__rename(
                old_,
                new_,
                _("文件夹"),
            )
            logging(
                self.print,
                _("文件夹 {old_} 重命名为 {new_}").format(
                    old_=old_.name, new_=new_.name
                ),
            )
            return new_
        return old_

    def __scan_file(
        self,
        id_: str,
        alias: str,
        old_alias: str,
    ):
        root = self.root.joinpath(f"{id_}_{alias}")
        item_list = root.iterdir()
        if self.folder_mode:
            for f in item_list:
                if f.is_dir():
                    f = self.__rename_works_folder(
                        f,
                        alias,
                        old_alias,
                    )
                    files = f.iterdir()
                    self.__batch_rename(
                        f,
                        files,
                        alias,
                        old_alias,
                    )
        else:
            self.__batch_rename(
                root,
                item_list,
                alias,
                old_alias,
            )

    def __batch_rename(
        self,
        root: Path,
        files,
        alias: str,
        old_alias: str,
    ):
        for old_file in files:
            if old_alias not in old_file.name:
                break
            self.__rename_file(
                root,
                old_file,
                alias,
                old_alias,
            )

    def __rename_file(
        self,
        root: Path,
        old_file: Path,
        alias: str,
        old_alias: str,
    ):
        new_file = root.joinpath(old_file.name.replace(old_alias, alias, 1))
        self.__rename(
            old_file,
            new_file,
            _("文件"),
        )
        logging(
            self.print,
            _("文件 {old_file} 重命名为 {new_file}").format(
                old_file=old_file.name, new_file=new_file.name
            ),
        )
        return True

    def __rename(
        self,
        old_: Path,
        new_: Path,
        type_=_("文件"),
    ) -> bool:
        try:
            self.__check_rename_paths(old_, new_)
            old_.rename(new_)
            return True
        except PermissionError as e:
            logging(
                self.print,
                _("{type} {old}被占用，重命名失败: {error}").format(
                    type=type_, old=old_.name, error=e
                ),
                ERROR,
            )
            raise
        except FileExistsError as e:
            logging(
                self.print,
                _("{type} {new}名称重复，重命名失败: {error}").format(
                    type=type_, new=new_.name, error=e
                ),
                ERROR,
            )
            raise
        except OSError as e:
            logging(
                self.print,
                _("处理{type} {old}时发生预期之外的错误: {error}").format(
                    type=type_, old=old_.name, error=e
                ),
                ERROR,
            )
            raise

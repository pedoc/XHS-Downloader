from pathlib import Path
from re import IGNORECASE, compile

ID_TOKEN = compile(r"[A-Za-z0-9_-]+\Z")
_INVALID_COMPONENT = compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_DEVICE_NAME = compile(
    r"(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])",
    flags=IGNORECASE,
)


def validate_component(value: str) -> str:
    """验证单个跨平台文件名组件；拒绝危险值，不修改原始名称。"""
    if not isinstance(value, str) or not value:
        raise ValueError(f"Unsafe path component: {value!r}")
    if (
        value in {".", ".."}
        or value.endswith((".", " "))
        or _INVALID_COMPONENT.search(value)
        or _DEVICE_NAME.fullmatch(value.split(".", 1)[0].rstrip(" "))
    ):
        raise ValueError(f"Unsafe path component: {value!r}")
    return value


def contained_path(trusted_root: Path, candidate: Path) -> Path:
    """解析已有链接，确保目标严格位于调用者授权的目录之内。"""
    try:
        root = trusted_root.resolve(strict=False)
        path = candidate.resolve(strict=False)
    except RuntimeError as error:
        raise ValueError(f"Cannot resolve path safely: {candidate}") from error
    # WindowsPath 的 Unicode lower 比较可能混淆 NTFS 上不同的目录（如 K / K）。
    if (
        path == root
        or not path.is_relative_to(root)
        or path.parts[: len(root.parts)] != root.parts
    ):
        raise ValueError(f"Path is outside the authorized directory: {candidate}")
    return path

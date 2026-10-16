"""Validate release inputs and archive fixed build directories without a shell."""

import argparse
import re
import shutil
import sys
from collections.abc import Sequence

TAG_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
ARTIFACT_NAMES = frozenset(
    ("Windows_X64", "Windows_ARM64", "macOS_X64", "macOS_ARM64")
)
SOURCE_DIRECTORIES = frozenset(("signed", "dist/XHS-Downloader"))


def validate_tag(tag: str) -> str:
    if TAG_PATTERN.fullmatch(tag) is None:
        raise ValueError(
            "tag must be 1-128 ASCII letters, digits, underscores, dots or hyphens, "
            "starting with a letter, digit or underscore"
        )
    return tag


def bump_tag(tag: str) -> str:
    prefix, separator, number = validate_tag(tag).rpartition(".")
    if re.fullmatch(r"[0-9]+", number) is None:
        raise ValueError("tag must end in a decimal numeric segment to bump")
    return validate_tag(f"{prefix}{separator}{int(number, 10) + 1}")


def create_archive(tag: str, artifact: str, source: str) -> str:
    validate_tag(tag)
    if artifact not in ARTIFACT_NAMES:
        raise ValueError("artifact must be one of the four release targets")
    if source not in SOURCE_DIRECTORIES:
        raise ValueError("source must be signed or dist/XHS-Downloader")
    return shutil.make_archive(f"XHS-Downloader_V{tag}_{artifact}", "zip", source)


def build_parser() -> argparse.ArgumentParser:
    # No help option: a tag such as --help must not emit multiline stdout as a tag.
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "bump", "archive"):
        command = commands.add_parser(name, add_help=False)
        command.add_argument("tag")
        if name == "archive":
            command.add_argument("artifact")
            command.add_argument("source")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "archive":
            create_archive(args.tag, args.artifact, args.source)
        else:
            tag = (
                validate_tag(args.tag)
                if args.command == "validate"
                else bump_tag(args.tag)
            )
            sys.stdout.write(tag + "\n")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

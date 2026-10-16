"""Export compact, hashed runtime requirements from the existing uv lockfile."""

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
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


def export_requirements() -> str:
    result = subprocess.run(
        EXPORT_COMMAND,
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    compact = re.sub(r"[ \t]*\\\r?\n[ \t]*", " ", result.stdout)
    lines = (line.strip() for line in compact.splitlines() if line.strip())
    return HEADER + "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Check the export without writing files"
    )
    args = parser.parse_args(argv)
    output = REPOSITORY_ROOT / "requirements.txt"
    try:
        content = export_requirements()
        if args.check:
            if output.read_text(encoding="utf-8") == content:
                return 0
            sys.stderr.write(
                "requirements.txt is out of date; "
                "run python tools/export_requirements.py\n"
            )
            return 1
        output.write_text(content, encoding="utf-8", newline="\n")
    except (OSError, subprocess.CalledProcessError) as error:
        sys.stderr.write(f"Could not export requirements: {error}\n")
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            sys.stderr.write(error.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

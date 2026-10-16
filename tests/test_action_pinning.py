import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_ROOT = Path(__file__).resolve().parents[1] / ".github" / "workflows"
WORKFLOWS = tuple(sorted(WORKFLOW_ROOT.glob("*.y*ml")))
PINNED_ACTION = re.compile(r"[^/\s@]+/[^\s@]+@[0-9a-f]{40}")
EXPECTED_ACTIONS = frozenset({
    "actions/stale", "actions/checkout", "actions/upload-artifact",
    "actions/download-artifact", "astral-sh/setup-uv", "softprops/action-gh-release",
    "Chizkiyahu/delete-untagged-ghcr-action",
    "signpath/github-action-submit-signing-request", "docker/setup-qemu-action",
    "docker/setup-buildx-action", "docker/login-action", "docker/build-push-action",
})


def action_references(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "uses":
                assert isinstance(item, str)
                yield item
            else:
                yield from action_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from action_references(item)


@pytest.mark.unit
@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_workflow_actions_are_pinned_to_immutable_commits(workflow: Path) -> None:
    references = tuple(action_references(yaml.safe_load(workflow.read_text("utf-8"))))

    assert references
    for reference in references:
        assert PINNED_ACTION.fullmatch(reference), reference


@pytest.mark.unit
def test_pinning_does_not_change_the_existing_action_repositories() -> None:
    repositories = {
        reference.rsplit("@", 1)[0]
        for path in WORKFLOWS
        for reference in action_references(yaml.safe_load(path.read_text("utf-8")))
    }

    assert repositories == EXPECTED_ACTIONS

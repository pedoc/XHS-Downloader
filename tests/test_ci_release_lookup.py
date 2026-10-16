import re
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/Manually_docker_image.yml"


@pytest.mark.unit
def test_latest_release_lookup_rejects_http_and_missing_tag_errors() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text("utf-8"))
    step = next(
        item for item in workflow["jobs"]["publish-docker"]["steps"]
        if item.get("id") == "get-latest-release"
    )
    script = step["run"]

    assert re.search(r"\bcurl\s+-[A-Za-z]*f[A-Za-z]*\b", script)
    assert re.search(r"\bjq\s+-[A-Za-z]*e[A-Za-z]*\b", script)
    assert 'type == "string"' in script
    assert "length > 0" in script

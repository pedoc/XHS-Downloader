import shutil
import subprocess
from pathlib import Path

import pytest

GUARD_PATH = Path(__file__).resolve().parents[1] / "static/GUI/navigation.js"
HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const listeners = {};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
    document: {addEventListener: (name, fn) => { listeners[name] = fn; }}
});
function click(href) {
    const event = {prevented: false,
        target: {closest: () => ({getAttribute: () => href})},
        preventDefault() { this.prevented = true; }};
    listeners.click(event);
    return event.prevented;
}
function drop(types, value, input = false) {
    const event = {prevented: false,
        target: {closest: () => input ? {} : null},
        dataTransfer: {types, getData: () => value},
        preventDefault() { this.prevented = true; }};
    listeners.drop(event);
    return event.prevented;
}
assert.equal(click('https://example.invalid/'), true);
assert.equal(click('file:///offline/other.html'), true);
assert.equal(click('javascript:offline'), true);
assert.equal(click('#settings'), false);
assert.equal(drop(['Files'], ''), true);
assert.equal(drop(['text/uri-list'], 'https://example.invalid/'), true);
assert.equal(drop(['text/plain'], 'https://example.invalid/'), true);
assert.equal(drop(['text/plain'], '作品标题'), false);
assert.equal(drop(['text/plain'], 'https://example.invalid/', true), false);
"""


@pytest.mark.unit
def test_gui_navigation_guards_reject_external_navigation_and_keep_local_interactions() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for the local navigation-guard test")

    result = subprocess.run(
        [node, "-e", HARNESS, str(GUARD_PATH)],
        check=False, capture_output=True, text=True, encoding="utf-8", timeout=15,
    )

    assert result.returncode == 0, result.stderr

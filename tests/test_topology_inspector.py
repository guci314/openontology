"""Tests for interactive element selection and detail inspector on /topology."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from urllib.request import urlopen

import pytest

from examples.command_center_demo import build_default_command_center
from open_ontology import start_command_center_server


@pytest.fixture()
def demo_server():
    registry = build_default_command_center()
    handle = start_command_center_server(registry, host="127.0.0.1", port=0)
    try:
        yield handle.base_url
    finally:
        handle.stop()


def test_topology_page_includes_inspector_and_selection_styles(demo_server: str) -> None:
    html = urlopen(f"{demo_server}/topology?ontology=task_board").read().decode("utf-8")
    assert 'id="inspector"' in html
    assert "selectTopoItem" in html
    assert "renderObjectTypeInspector" in html
    assert "renderActionInspector" in html
    assert ".topo-item.is-selected" in html
    assert "#scene.has-selection" in html


def test_topology_scene_tags_all_elements_for_task_board(demo_server: str) -> None:
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not installed")

    html = urlopen(f"{demo_server}/topology?ontology=task_board").read().decode("utf-8")
    meta = json.loads(urlopen(f"{demo_server}/api/ontology/task_board").read().decode("utf-8"))

    # Extract DISPLAY_JS + TOPOLOGY_JS (everything from `const DISPLAY` up to `const params =`)
    m = re.search(r"<script>\n(.*?)\nconst params =", html, re.S)
    assert m, "Could not extract TOPOLOGY_JS bundle from /topology"
    bundle = m.group(1)

    driver = (
        bundle
        + f"\nconst data = {json.dumps(meta, ensure_ascii=False)};\n"
        + """
const scene = topologyScene(data, 1200, 700, 1.5);
console.log(JSON.stringify({
  hasBoard: scene.inner.includes('data-topo-kind="object_type" data-topo-id="Board"'),
  hasAction: scene.inner.includes('data-topo-kind="action"'),
  hasProp: scene.inner.includes('data-topo-kind="object_property"'),
  hasFn: scene.inner.includes('data-topo-kind="function"'),
  hasAuto: scene.inner.includes('data-topo-kind="automation"'),
  hasNodeRect: scene.inner.includes('class="node"'),
  hasActionRect: scene.inner.includes('class="action-node"'),
  hasFnRect: scene.inner.includes('class="fn-node"'),
  hasAutoRect: scene.inner.includes('class="auto-node"'),
}));
"""
    )
    proc = subprocess.run(
        [node_bin, "-e", driver],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(proc.stdout.strip())
    assert result["hasBoard"] is True
    assert result["hasAction"] is True
    assert result["hasProp"] is True
    assert result["hasFn"] is True
    assert result["hasAuto"] is bool(meta.get("automations"))
    assert result["hasNodeRect"] is True
    assert result["hasActionRect"] is True
    assert result["hasFnRect"] is True
    assert result["hasAutoRect"] is bool(meta.get("automations"))

"""Read-only inspection of a prepared task workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.workspaces.models import WorkspaceState


def _create(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/project-workspaces",
        json={
            "mission_id": "mission-1",
            "task_id": "task-1",
            "source_url": "https://github.com/example/demo",
        },
    )
    assert response.status_code == 201
    body = response.json()
    workspace = body["workspace"]
    assert isinstance(workspace, dict)
    assert workspace["state"] == WorkspaceState.READY.value
    assert workspace["sandbox_runtime"] == "UNCONFIGURED"
    root = Path(str(workspace["root_path"]))
    assert root.is_dir()
    assert not (root / ".git").exists()
    assert list(root.iterdir()) == []
    return {str(key): str(value) for key, value in workspace.items()}


def test_inspection_reads_only_inside_the_workspace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("project code was executed")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)

    workspace = _create(client)
    root = Path(workspace["root_path"])
    (root / "README.md").write_text(
        "# Demo\n\n```bash\npip install -e .\npytest\n```\napi_key: supersecretvalue\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        '{"name":"demo","scripts":{"test":"pytest"}}\n',
        encoding="utf-8",
    )
    (root / "index.js").write_text("console.log('ready')\n", encoding="utf-8")
    (root / "main.py").write_text("TOKEN = 'abc'\nprint('launch-code')\n", encoding="utf-8")
    (root / "Makefile").write_text("test:\n\tpytest -q\n", encoding="utf-8")
    (root / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (root / ".env").write_text("SEARCH_API_KEY=secret\n", encoding="utf-8")
    outside = root.parent / "outside.txt"
    outside.write_text("outside-secret\n", encoding="utf-8")
    (root / "escape").symlink_to(outside)

    workspace_id = workspace["workspace_id"]
    inspected = client.get(f"/api/v1/project-workspaces/{workspace_id}/inspect")
    assert inspected.status_code == 200
    summary = inspected.json()["inspection"]
    assert summary["languages"]["Python"] >= 1
    assert summary["languages"]["JavaScript"] >= 1
    assert summary["manifests"]["package.json"]["package_manager"] == "npm-compatible"
    assert summary["suggested_scripts_untrusted"]["package.json::test"] == "pytest"
    assert "Makefile::test" in summary["suggested_scripts_untrusted"]
    assert "pip install -e ." in summary["readme_setup_steps_untrusted"]
    assert "supersecretvalue" not in summary["readme_excerpt_untrusted"]
    assert "[REDACTED]" in summary["readme_excerpt_untrusted"]
    assert "main.py" in summary["likely_entry_points"]
    assert summary["license_summaries_untrusted"][0]["title_untrusted"] == "MIT License"
    assert not (root / ".git").exists()

    read = client.get(
        f"/api/v1/project-workspaces/{workspace_id}/read",
        params={"path": "main.py", "start_line": 2, "line_count": 1},
    )
    assert read.status_code == 200
    assert read.json()["file"]["content_untrusted"] == "print('launch-code')"

    blocked = client.get(
        f"/api/v1/project-workspaces/{workspace_id}/read",
        params={"path": "../outside.txt"},
    )
    assert blocked.status_code == 400
    assert "outside-secret" not in blocked.text

    linked = client.get(
        f"/api/v1/project-workspaces/{workspace_id}/read",
        params={"path": "escape"},
    )
    assert linked.status_code == 400
    assert "outside-secret" not in linked.text

    secret = client.get(
        f"/api/v1/project-workspaces/{workspace_id}/read",
        params={"path": ".env"},
    )
    assert secret.status_code == 400

    found = client.get(
        f"/api/v1/project-workspaces/{workspace_id}/search",
        params={"query": "launch-code"},
    )
    assert found.status_code == 200
    hits = found.json()["search"]["results_untrusted"]
    assert hits[0]["path"] == "main.py"
    assert hits[0]["line"] == 2

    hidden = client.get(
        f"/api/v1/project-workspaces/{workspace_id}/search",
        params={"query": "SEARCH_API_KEY"},
    )
    assert hidden.json()["search"]["count"] == 0


def test_credential_source_url_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/v1/project-workspaces",
        json={
            "mission_id": "mission-1",
            "task_id": "task-1",
            "source_url": "https://user:token@github.com/example/demo",
        },
    )
    assert response.status_code == 400


def test_repo_tools_stay_read_only(client: TestClient) -> None:
    tools = {tool["name"]: tool for tool in client.get("/api/v1/tools").json()["tools"]}
    for name in ("repo.inspect", "repo.read", "repo.search"):
        assert tools[name]["risk"] == "SAFE"
        assert tools[name]["capability"] == "READ"
        assert tools[name]["requires_node"] is False
    agents = client.get("/api/v1/agents").json()["agents"]
    workshop = next(agent for agent in agents if agent["id"] == "project_workshop")
    assert set(workshop["tools"]) == {"repo.inspect", "repo.read", "repo.search"}


def test_unauthenticated_workspace_routes_are_rejected(client: TestClient) -> None:
    listed = client.get(
        "/api/v1/project-workspaces",
        params={"task_id": "task-1"},
        headers={"X-Jarvis-Passcode": ""},
    )
    assert listed.status_code == 401

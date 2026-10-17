from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import app as backend


@pytest.fixture()
def material_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, object]]:
    project_root = tmp_path / "project"
    workspace = project_root / "workspaces" / "project-a"
    input_root = tmp_path / "ComfyUI-Shared" / "input"
    output_root = tmp_path / "ComfyUI-Shared" / "output"
    downloads = tmp_path / "Downloads"
    for directory in (workspace, input_root, output_root, downloads):
        directory.mkdir(parents=True)

    manifest: dict[str, object] = {
        "id": "project-a",
        "series": "测试系列",
        "title": "素材桥测试",
        "workspace_root": str(workspace),
        "media": {},
        "assets": [],
        "pipeline": [],
    }
    monkeypatch.setattr(backend, "ROOT", tmp_path)
    monkeypatch.setattr(backend, "PROJECT", project_root)
    monkeypatch.setattr(backend, "DIRECTOR_WORKSPACES_ROOT", project_root / "workspaces")
    monkeypatch.setattr(backend, "INPUT_ROOT", input_root)
    monkeypatch.setattr(backend, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(backend, "CURRENT_PROJECT_ID", "project-a")
    monkeypatch.setattr(backend, "load_project_manifest", lambda project_id: manifest)
    monkeypatch.setattr(backend, "save_project_manifest", lambda value: None)
    monkeypatch.setattr(backend, "apply_project_manifest", lambda value: None)
    monkeypatch.setattr(backend, "windows_downloads_root", lambda: downloads)
    return TestClient(backend.app), {
        "manifest": manifest,
        "workspace": workspace,
        "input": input_root,
        "output": output_root,
        "downloads": downloads,
    }


def test_library_lists_windows_downloads_without_absolute_paths(
    material_client: tuple[TestClient, dict[str, object]],
) -> None:
    client, paths = material_client
    downloads = paths["downloads"]
    assert isinstance(downloads, Path)
    (downloads / "参考画面.png").write_bytes(b"image")

    response = client.get(
        "/api/material-library",
        params={"project_id": "project-a", "source_id": "downloads"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert {source["id"] for source in payload["sources"]} >= {"input", "output", "project", "downloads"}
    assert payload["entries"][0]["relative_path"] == "参考画面.png"
    assert str(downloads) not in response.text
    assert ":\\" not in response.text


def test_library_rejects_parent_directory_escape(
    material_client: tuple[TestClient, dict[str, object]],
) -> None:
    client, _ = material_client

    response = client.get(
        "/api/material-library",
        params={"project_id": "project-a", "source_id": "downloads", "path": "../"},
    )

    assert response.status_code == 422


def test_downloads_registration_copies_and_deduplicates_media(
    material_client: tuple[TestClient, dict[str, object]],
) -> None:
    client, paths = material_client
    downloads = paths["downloads"]
    workspace = paths["workspace"]
    manifest = paths["manifest"]
    assert isinstance(downloads, Path) and isinstance(workspace, Path) and isinstance(manifest, dict)
    source = downloads / "voice.wav"
    source.write_bytes(b"voice-bytes")
    request = {"items": [{"source_id": "downloads", "relative_path": "voice.wav"}]}

    first = client.post("/api/projects/project-a/materials/register", json=request)
    second = client.post("/api/projects/project-a/materials/register", json=request)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    copied = workspace / "assets" / "imported-windows" / "downloads" / "voice.wav"
    assert copied.read_bytes() == b"voice-bytes"
    assert len(manifest["assets"]) == 1
    assert len(manifest["media"]) == 1


def test_shared_input_registration_references_original_file(
    material_client: tuple[TestClient, dict[str, object]],
) -> None:
    client, paths = material_client
    input_root = paths["input"]
    workspace = paths["workspace"]
    manifest = paths["manifest"]
    assert isinstance(input_root, Path) and isinstance(workspace, Path) and isinstance(manifest, dict)
    (input_root / "frame.png").write_bytes(b"frame")

    response = client.post(
        "/api/projects/project-a/materials/register",
        json={"items": [{"source_id": "input", "relative_path": "frame.png"}]},
    )

    assert response.status_code == 200, response.text
    assert not (workspace / "assets" / "imported-windows").exists()
    assert list(manifest["media"].values()) == ["ComfyUI-Shared/input/frame.png"]


def test_non_media_file_can_download_but_cannot_register(
    material_client: tuple[TestClient, dict[str, object]],
) -> None:
    client, paths = material_client
    downloads = paths["downloads"]
    assert isinstance(downloads, Path)
    (downloads / "notes.txt").write_text("reference notes", encoding="utf-8")

    downloaded = client.get(
        "/material-file",
        params={"project_id": "project-a", "source_id": "downloads", "path": "notes.txt"},
    )
    registered = client.post(
        "/api/projects/project-a/materials/register",
        json={"items": [{"source_id": "downloads", "relative_path": "notes.txt"}]},
    )

    assert downloaded.status_code == 200
    assert downloaded.content == b"reference notes"
    assert registered.status_code == 422


def test_download_endpoint_is_limited_to_registered_roots(
    material_client: tuple[TestClient, dict[str, object]],
) -> None:
    client, _ = material_client

    response = client.get(
        "/material-file",
        params={"project_id": "project-a", "source_id": "downloads", "path": "../outside.mp4"},
    )

    assert response.status_code == 422

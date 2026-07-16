from __future__ import annotations

import errno
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    config = main.AppConfig(save_root=tmp_path, max_text_chars=20)
    monkeypatch.setattr(main, "load_config", lambda: config)
    return TestClient(main.app)


def test_save_creates_file_and_returns_path(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/save", json={"text": "hello", "path": "notes/today.txt"})

    expected = (tmp_path / "notes/today.txt").resolve()
    assert response.status_code == 200
    assert response.json() == {"ok": True, "path": str(expected)}
    assert expected.read_text(encoding="utf-8") == "hello"


def test_save_accepts_absolute_paths_inside_save_root(
    client: TestClient, tmp_path: Path
) -> None:
    target = tmp_path / "absolute.txt"

    response = client.post("/save", json={"text": "absolute", "path": str(target)})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "path": str(target.resolve())}
    assert target.read_text(encoding="utf-8") == "absolute"


def test_save_rejects_paths_outside_save_root(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/save", json={"text": "no", "path": "../outside.txt"})

    assert response.status_code == 400
    assert not (tmp_path.parent / "outside.txt").exists()


def test_save_rejects_oversized_text(client: TestClient) -> None:
    response = client.post("/save", json={"text": "x" * 21, "path": "large.txt"})

    assert response.status_code == 413


def test_save_atomic_failure_does_not_corrupt_existing_file(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("original", encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError(errno.EIO, "simulated rename failure")

    monkeypatch.setattr(main.os, "replace", fail_replace)
    response = client.post("/save", json={"text": "replacement", "path": "existing.txt"})

    assert response.status_code == 500
    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".existing.txt.*.tmp")) == []


def test_paths_lists_files_and_directories(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "alpha.txt").write_text("a", encoding="utf-8")
    (tmp_path / "notes").mkdir()

    response = client.get("/paths")

    assert response.status_code == 200
    assert response.json() == {"paths": ["alpha.txt", "notes/"]}


def test_paths_filters_by_prefix(client: TestClient, tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "today.md").write_text("today", encoding="utf-8")
    (notes / "tomorrow.md").write_text("tomorrow", encoding="utf-8")
    (notes / "archive.md").write_text("archive", encoding="utf-8")

    response = client.get("/paths", params={"prefix": "notes/to"})

    assert response.status_code == 200
    assert response.json() == {"paths": ["notes/today.md", "notes/tomorrow.md"]}


def test_paths_returns_at_most_30_results(client: TestClient, tmp_path: Path) -> None:
    for index in range(35):
        (tmp_path / f"file-{index:02}.txt").write_text("x", encoding="utf-8")

    response = client.get("/paths")

    assert response.status_code == 200
    assert len(response.json()["paths"]) == 30


@pytest.mark.parametrize("prefix", ["../", "../../secret", "/tmp/"])
def test_paths_rejects_traversal_attempts(client: TestClient, prefix: str) -> None:
    response = client.get("/paths", params={"prefix": prefix})

    assert response.status_code == 200
    assert response.json() == {"paths": []}


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}

from __future__ import annotations

import errno
import inspect
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    config = main.AppConfig(save_root=tmp_path)
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


def test_save_rejects_paths_outside_save_root(
    client: TestClient, tmp_path: Path
) -> None:
    response = client.post("/save", json={"text": "no", "path": "../outside.txt"})

    assert response.status_code == 400
    assert not (tmp_path.parent / "outside.txt").exists()


def test_save_allows_text_within_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main.AppConfig(save_root=tmp_path, max_text_bytes=2)
    monkeypatch.setattr(main, "load_config", lambda: config)
    client = TestClient(main.app)

    response = client.post("/save", json={"text": "é", "path": "within.txt"})

    assert response.status_code == 200
    assert (tmp_path / "within.txt").read_text(encoding="utf-8") == "é"


def test_save_rejects_text_exceeding_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main.AppConfig(save_root=tmp_path, max_text_bytes=3)
    monkeypatch.setattr(main, "load_config", lambda: config)
    client = TestClient(main.app)

    response = client.post("/save", json={"text": "éé", "path": "too-large.txt"})

    assert response.status_code == 413
    assert response.json() == {
        "detail": "Text is 4 bytes when UTF-8 encoded; maximum allowed is 3 bytes"
    }
    assert not (tmp_path / "too-large.txt").exists()


def test_save_with_zero_limit_allows_large_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main.AppConfig(save_root=tmp_path, max_text_bytes=0)
    monkeypatch.setattr(main, "load_config", lambda: config)
    client = TestClient(main.app)
    text = "x" * 100_000

    response = client.post("/save", json={"text": text, "path": "large.txt"})

    assert response.status_code == 200
    assert (tmp_path / "large.txt").read_text(encoding="utf-8") == text


def test_save_atomic_failure_does_not_corrupt_existing_file(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("original", encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError(errno.EIO, "simulated rename failure")

    monkeypatch.setattr(main.os, "replace", fail_replace)
    response = client.post(
        "/save", json={"text": "replacement", "path": "existing.txt"}
    )

    assert response.status_code == 500
    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".existing.txt.*.tmp")) == []


def test_save_encoding_failure_removes_temp_file(
    client: TestClient, tmp_path: Path
) -> None:
    # Use raw content to bypass httpx JSON serialization which rejects
    # unpaired surrogates before the request reaches the server.
    response = client.post(
        "/save",
        content='{"text": "\\ud800", "path": "invalid-encoding.txt"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 500
    assert not (tmp_path / "invalid-encoding.txt").exists()
    assert list(tmp_path.glob(".invalid-encoding.txt.*.tmp")) == []


def test_save_overwrite_preserves_existing_permissions(
    client: TestClient, tmp_path: Path
) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("original", encoding="utf-8")
    target.chmod(0o644)

    response = client.post(
        "/save", json={"text": "replacement", "path": "existing.txt"}
    )

    assert response.status_code == 200
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


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


def test_paths_lists_directory_contents_when_prefix_ends_with_slash(
    client: TestClient, tmp_path: Path
) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "today.md").write_text("today", encoding="utf-8")
    (notes / "archive").mkdir()

    response = client.get("/paths", params={"prefix": "notes/"})

    assert response.status_code == 200
    assert response.json() == {"paths": ["notes/archive/", "notes/today.md"]}


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


def test_health_returns_not_ready_for_missing_save_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing"
    monkeypatch.setattr(main, "load_config", lambda: main.AppConfig(save_root=missing))

    response = TestClient(main.app).get("/health")

    assert response.status_code == 503
    assert "not an accessible directory" in response.json()["detail"]


def test_health_returns_not_ready_for_unwritable_save_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main.AppConfig(save_root=tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: config)
    monkeypatch.setattr(main.os, "access", lambda _path, _mode: False)

    response = TestClient(main.app).get("/health")

    assert response.status_code == 503
    assert "not an accessible directory" in response.json()["detail"]


def test_health_returns_not_ready_for_invalid_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_config() -> main.AppConfig:
        raise RuntimeError("Unknown config key(s): typo")

    monkeypatch.setattr(main, "load_config", invalid_config)

    response = TestClient(main.app).get("/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "Not ready: Unknown config key(s): typo"}


def test_save_and_paths_handlers_are_synchronous() -> None:
    assert not inspect.iscoroutinefunction(main.save_text)
    assert not inspect.iscoroutinefunction(main.list_paths)


def test_config_reads_port(tmp_path: Path) -> None:
    import yaml

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({"port": 9999, "max_text_bytes": 4096}))
    config = main.load_config(path=config_path)

    assert config.port == 9999
    assert config.max_text_bytes == 4096


def test_config_defaults_port(tmp_path: Path) -> None:
    config = main.load_config(path=tmp_path / "nonexistent.yaml")

    assert config.port == 8766
    assert config.max_text_bytes == 0
    assert config.save_root == Path("~").expanduser().resolve()


@pytest.mark.parametrize("yaml_value", ["null", "123", "false"])
def test_config_rejects_null_or_non_string_save_root(
    tmp_path: Path, yaml_value: str
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"save_root: {yaml_value}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="save_root must be a non-null string"):
        main.load_config(path=config_path)


@pytest.mark.parametrize("port", [0, 65536, 70000])
def test_config_rejects_port_outside_valid_range(tmp_path: Path, port: int) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"port: {port}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="port must be from 1 to 65535"):
        main.load_config(path=config_path)


def test_config_rejects_unknown_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("port: 8766\ntyop: 9000\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"Unknown config key\(s\): tyop"):
        main.load_config(path=config_path)


def test_config_normalizes_relative_save_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("save_root: data/../saves\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    config = main.load_config(path=config_path)

    assert config.save_root == (tmp_path / "saves").resolve()


def test_save_accepts_absolute_path_with_normalized_relative_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_root = tmp_path / "data" / ".." / "saves"
    config = main.AppConfig(save_root=save_root)
    monkeypatch.setattr(main, "load_config", lambda: config)
    client = TestClient(main.app)
    target = tmp_path / "saves" / "absolute.txt"

    response = client.post("/save", json={"text": "ok", "path": str(target)})

    assert response.status_code == 200
    assert target.read_text(encoding="utf-8") == "ok"

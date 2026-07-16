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
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    with TestClient(main.app) as test_client:
        yield test_client


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
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    with TestClient(main.app) as client:
        response = client.post("/save", json={"text": "é", "path": "within.txt"})

    assert response.status_code == 200
    assert (tmp_path / "within.txt").read_text(encoding="utf-8") == "é"


def test_save_rejects_text_exceeding_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main.AppConfig(save_root=tmp_path, max_text_bytes=3)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    with TestClient(main.app) as client:
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
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    text = "x" * 100_000

    with TestClient(main.app) as client:
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

    assert response.status_code == 400
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


def test_save_returns_success_when_directory_fsync_fails(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = main.os.fsync
    calls = 0

    def fail_second_fsync(file_descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("sync failed")
        real_fsync(file_descriptor)

    monkeypatch.setattr(main.os, "fsync", fail_second_fsync)

    response = client.post("/save", json={"text": "saved", "path": "durable.txt"})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "durability sync failed" in response.json()["warning"]
    assert (tmp_path / "durable.txt").read_text(encoding="utf-8") == "saved"


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
    for index in reversed(range(35)):
        (tmp_path / f"file-{index:02}.txt").write_text("x", encoding="utf-8")

    response = client.get("/paths")

    assert response.status_code == 200
    assert response.json()["paths"] == [f"file-{index:02}.txt" for index in range(30)]


def test_paths_treats_backslash_as_a_posix_filename_character(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "notes\\file.txt").write_text("x", encoding="utf-8")

    response = client.get("/paths", params={"prefix": "notes\\"})

    assert response.status_code == 200
    assert response.json() == {"paths": ["notes\\file.txt"]}


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
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=missing))

    with TestClient(main.app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert "not an accessible directory" in response.json()["detail"]


def test_health_returns_not_ready_for_unwritable_save_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main.AppConfig(save_root=tmp_path)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    monkeypatch.setattr(
        main,
        "_probe_save_root",
        lambda _path: (_ for _ in ()).throw(PermissionError("denied")),
    )

    with TestClient(main.app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert "not an accessible directory" in response.json()["detail"]


def test_overwriting_config_file_does_not_change_active_save_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active_root = tmp_path / "active"
    changed_root = tmp_path / "changed"
    active_root.mkdir()
    changed_root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f'save_root: "{active_root}"\n', encoding="utf-8")
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.load_config(config_path))

    config_path.write_text(f'save_root: "{changed_root}"\n', encoding="utf-8")
    with TestClient(main.app) as client:
        response = client.post(
            "/save", json={"text": "still active", "path": "runtime.txt"}
        )

    assert response.status_code == 200
    assert (active_root / "runtime.txt").read_text(encoding="utf-8") == "still active"
    assert not (changed_root / "runtime.txt").exists()


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


@pytest.mark.parametrize("yaml_value", ["false", "123"])
def test_config_rejects_non_mapping_top_level(tmp_path: Path, yaml_value: str) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_value + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="must contain a YAML mapping"):
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
    monkeypatch.chdir(tmp_path.parent)

    config = main.load_config(path=config_path)

    assert config.save_root == (tmp_path / "saves").resolve()


@pytest.mark.parametrize("key", ["port", "max_text_bytes"])
@pytest.mark.parametrize("value", ["-1", "1.9", "false", '"8766"'])
def test_config_rejects_non_strict_or_invalid_integers(
    tmp_path: Path, key: str, value: str
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"{key}: {value}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=f"config {key}"):
        main.load_config(path=config_path)


def test_config_rejects_negative_max_text_bytes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("max_text_bytes: -1\n", encoding="utf-8")

    with pytest.raises(
        RuntimeError, match="max_text_bytes must be a non-negative integer"
    ):
        main.load_config(path=config_path)


def test_config_rejects_malformed_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("save_root: [unterminated\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Invalid YAML"):
        main.load_config(path=config_path)


def test_config_wraps_file_read_errors(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.mkdir()

    with pytest.raises(RuntimeError, match="Cannot read config file"):
        main.load_config(path=config_path)


def test_save_accepts_absolute_path_with_normalized_relative_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_root = tmp_path / "data" / ".." / "saves"
    config = main.AppConfig(save_root=save_root)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    target = tmp_path / "saves" / "absolute.txt"

    with TestClient(main.app) as client:
        response = client.post("/save", json={"text": "ok", "path": str(target)})

    assert response.status_code == 200
    assert target.read_text(encoding="utf-8") == "ok"


@pytest.mark.parametrize("filename", ["config.yaml", "main.py"])
def test_resolve_save_path_rejects_application_files(filename: str) -> None:
    config = main.AppConfig(save_root=main.APPLICATION_ROOT.parent)

    with pytest.raises(
        main.HTTPException,
        match="Cannot save inside the file-bridge application directory",
    ) as exc_info:
        main._resolve_save_path(str(main.APPLICATION_ROOT / filename), config)

    assert exc_info.value.status_code == 400


def test_resolve_save_path_rejects_symlink_into_application_directory(
    tmp_path: Path,
) -> None:
    application_link = tmp_path / "application"
    application_link.symlink_to(main.APPLICATION_ROOT, target_is_directory=True)
    config = main.AppConfig(save_root=tmp_path)

    with pytest.raises(
        main.HTTPException,
        match="Cannot save inside the file-bridge application directory",
    ) as exc_info:
        main._resolve_save_path(str(application_link / "main.py"), config)

    assert exc_info.value.status_code == 400


@pytest.mark.parametrize(
    "path", ["bad\x00name.txt", "~definitely-no-such-user/file.txt"]
)
def test_save_rejects_invalid_user_paths(client: TestClient, path: str) -> None:
    response = client.post("/save", json={"text": "no", "path": path})

    assert response.status_code == 400


def test_save_rejects_path_that_is_too_long(client: TestClient) -> None:
    response = client.post("/save", json={"text": "no", "path": "x" * 5000})

    assert response.status_code == 400


def test_paths_rejects_invalid_user_paths(client: TestClient) -> None:
    for prefix in ["bad\x00name", "~definitely-no-such-user", "x" * 5000]:
        response = client.get("/paths", params={"prefix": prefix})
        assert response.status_code == 400


def test_save_rejects_extra_request_fields(client: TestClient) -> None:
    response = client.post(
        "/save", json={"text": "hello", "path": "ok.txt", "unexpected": True}
    )

    assert response.status_code == 422

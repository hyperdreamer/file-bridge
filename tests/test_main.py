from __future__ import annotations

import errno
import inspect
import json
import logging
import os
import stat
import time
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


def test_save_rejects_non_utf8_path_without_side_effects(
    client: TestClient, tmp_path: Path
) -> None:
    response = client.post(
        "/save",
        content='{"text": "no", "path": "invalid-\\udcff.txt"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid path: must be valid UTF-8"}
    assert list(tmp_path.iterdir()) == []


def test_save_rejects_trailing_slash_without_side_effects(
    client: TestClient, tmp_path: Path
) -> None:
    response = client.post("/save", json={"text": "no", "path": "directory/"})

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Save path must name a file, not a directory"
    }
    assert not (tmp_path / "directory").exists()


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
    assert list(tmp_path.glob(".file-bridge-*.tmp")) == []


def test_save_maps_storage_einval_to_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_write(_path: Path, _text: str) -> None:
        raise OSError(errno.EINVAL, "simulated storage failure")

    monkeypatch.setattr(main, "_atomic_write_text", fail_write)

    response = client.post("/save", json={"text": "no", "path": "target.txt"})

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to save text"}


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
    assert list(tmp_path.glob(".file-bridge-*.tmp")) == []


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
    assert "durability sync failed" in response.json()["warning"].lower()
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


def test_paths_lists_directory_contents_for_exact_directory_prefix(
    client: TestClient, tmp_path: Path
) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "today.md").write_text("today", encoding="utf-8")
    (notes / "archive").mkdir()

    response = client.get("/paths", params={"prefix": "notes"})

    assert response.status_code == 200
    assert response.json() == {"paths": ["notes/archive/", "notes/today.md"]}


def test_paths_returns_at_most_30_results(client: TestClient, tmp_path: Path) -> None:
    for index in reversed(range(35)):
        (tmp_path / f"file-{index:02}.txt").write_text("x", encoding="utf-8")

    response = client.get("/paths")

    assert response.status_code == 200
    assert response.json()["paths"] == [f"file-{index:02}.txt" for index in range(30)]


def test_paths_stops_at_scanned_entry_budget(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "MAX_PATH_SCAN_ENTRIES", 2)
    real_resolve = main.Path.resolve
    resolved_entries = 0

    def recording_resolve(path: Path, strict: bool = False) -> Path:
        nonlocal resolved_entries
        if path.parent == tmp_path:
            resolved_entries += 1
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(main.Path, "resolve", recording_resolve)
    for filename in ["third.txt", "first.txt", "second.txt"]:
        (tmp_path / filename).write_text("x", encoding="utf-8")

    response = client.get("/paths")

    assert response.status_code == 200
    assert len(response.json()["paths"]) == 2
    assert resolved_entries == 2


def test_paths_skips_directory_entries_that_are_not_valid_utf8(
    client: TestClient, tmp_path: Path
) -> None:
    invalid_path = bytes(tmp_path) + b"/invalid-\xff.txt"
    file_descriptor = main.os.open(
        invalid_path, main.os.O_WRONLY | main.os.O_CREAT, 0o600
    )
    main.os.close(file_descriptor)
    (tmp_path / "valid.txt").write_text("ok", encoding="utf-8")

    response = client.get("/paths")

    assert response.status_code == 200
    assert response.json() == {"paths": ["valid.txt"]}


def test_paths_excludes_internal_temporary_files(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / ".file-bridge-abcdefgh.tmp").write_text("partial", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("ok", encoding="utf-8")

    response = client.get("/paths")

    assert response.status_code == 200
    assert response.json() == {"paths": ["visible.txt"]}


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


def test_startup_fails_for_missing_save_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing"
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=missing))

    with pytest.raises(RuntimeError, match="save_root does not exist"):
        with TestClient(main.app):
            pass


def test_liveness_and_readiness_are_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main.AppConfig(save_root=tmp_path)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)

    with TestClient(main.app) as client:
        monkeypatch.setattr(
            main,
            "_probe_save_root",
            lambda _path: (_ for _ in ()).throw(RuntimeError("denied")),
        )
        health_response = client.get("/health")
        ready_response = client.get("/ready")

    assert health_response.status_code == 200
    assert health_response.json() == {"ok": True}
    assert ready_response.status_code == 503
    assert ready_response.json() == {"detail": "Service is not ready"}
    assert str(tmp_path) not in ready_response.text


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
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = main.load_config(path=config_path)

    assert config.port == 8766
    assert config.max_text_bytes == main.DEFAULT_MAX_TEXT_BYTES
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
    target.parent.mkdir()

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


def test_paths_excludes_symlinks_that_escape_save_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_root = tmp_path / "root"
    outside = tmp_path / "outside"
    save_root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (save_root / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=save_root))

    with TestClient(main.app) as client:
        root_response = client.get("/paths")
        escaped_response = client.get("/paths", params={"prefix": "escape/"})

    assert root_response.status_code == 200
    assert root_response.json() == {"paths": []}
    assert escaped_response.status_code == 200
    assert escaped_response.json() == {"paths": []}


def test_paths_preserves_safe_internal_symlink_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_root = tmp_path / "root"
    target = save_root / "target"
    target.mkdir(parents=True)
    (target / "note.txt").write_text("note", encoding="utf-8")
    (save_root / "alias").symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=save_root))

    with TestClient(main.app) as client:
        response = client.get("/paths", params={"prefix": "alias/"})

    assert response.status_code == 200
    assert response.json() == {"paths": ["alias/note.txt"]}


def test_paths_excludes_application_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    save_root = main.APPLICATION_ROOT.parent
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=save_root))

    with TestClient(main.app) as client:
        response = client.get("/paths")

    assert response.status_code == 200
    assert f"{main.APPLICATION_ROOT.name}/" not in response.json()["paths"]


def test_request_body_limit_rejects_before_json_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main.AppConfig(save_root=tmp_path, max_text_bytes=1)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    oversized_malformed_json = b"{" + b"x" * main._request_body_limit(config)

    with TestClient(main.app) as client:
        response = client.post(
            "/save",
            content=oversized_malformed_json,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body is too large"}


def test_missing_config_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Configuration file not found"):
        main.load_config(tmp_path / "missing.yaml")


@pytest.mark.parametrize("save_root", ["''", '""', "'   '", '"\t"'])
def test_config_rejects_empty_or_whitespace_save_root(
    tmp_path: Path, save_root: str
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"save_root: {save_root}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="empty or whitespace-only"):
        main.load_config(config_path)


def test_config_rejects_duplicate_top_level_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f'save_root: "{tmp_path}"\nsave_root: "/tmp"\n', encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="duplicate key 'save_root'"):
        main.load_config(config_path)


def test_config_rejects_duplicate_nested_mapping_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f'save_root: "{tmp_path}"\nunknown:\n  key: 1\n  key: 2\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="duplicate key 'key'"):
        main.load_config(config_path)


def test_startup_rejects_save_root_that_is_not_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_root = tmp_path / "file.txt"
    save_root.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=save_root))

    with pytest.raises(RuntimeError, match="save_root is not a directory"):
        with TestClient(main.app):
            pass


def test_startup_rejects_save_root_inside_application_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main, "RUNTIME_CONFIG", main.AppConfig(save_root=main.APPLICATION_ROOT)
    )

    with pytest.raises(RuntimeError, match="inside the application directory"):
        with TestClient(main.app):
            pass


def test_startup_removes_only_old_owned_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = tmp_path / ".file-bridge-abcdefgh.tmp"
    fresh = tmp_path / ".file-bridge-ijklmnop.tmp"
    stale.write_text("stale", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")
    old_timestamp = time.time() - main.STALE_TEMP_FILE_AGE_SECONDS - 1
    os.utime(stale, (old_timestamp, old_timestamp))
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=tmp_path))

    with TestClient(main.app):
        pass

    assert not stale.exists()
    assert fresh.exists()


def test_stale_temp_cleanup_preserves_files_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = tmp_path / ".file-bridge-abcdefgh.tmp"
    stale.write_text("stale", encoding="utf-8")
    old_timestamp = time.time() - main.STALE_TEMP_FILE_AGE_SECONDS - 1
    os.utime(stale, (old_timestamp, old_timestamp))
    monkeypatch.setattr(main.os, "getuid", lambda: stale.stat().st_uid + 1)

    main._cleanup_stale_temp_files(tmp_path)

    assert stale.exists()


def test_startup_rejects_inaccessible_save_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=tmp_path))
    monkeypatch.setattr(
        main,
        "_probe_save_root",
        lambda _path: (_ for _ in ()).throw(
            RuntimeError("save_root must be readable and writable")
        ),
    )

    with pytest.raises(RuntimeError, match="readable and writable"):
        with TestClient(main.app):
            pass


def test_save_supports_maximum_length_filename(
    client: TestClient, tmp_path: Path
) -> None:
    name_max = main.os.pathconf(tmp_path, "PC_NAME_MAX")
    if name_max == -1:
        pytest.skip("filesystem does not report NAME_MAX")
    filename = "n" * name_max

    response = client.post("/save", json={"text": "long", "path": filename})

    assert response.status_code == 200
    assert (tmp_path / filename).read_text(encoding="utf-8") == "long"
    assert list(tmp_path.glob(".file-bridge-*.tmp")) == []


def test_overwrite_fsyncs_after_permission_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("old", encoding="utf-8")
    events: list[str] = []
    real_fsync = main.os.fsync
    real_fchmod = main.os.fchmod

    def recording_fsync(file_descriptor: int) -> None:
        events.append("fsync")
        real_fsync(file_descriptor)

    def recording_fchmod(file_descriptor: int, mode: int) -> None:
        events.append("fchmod")
        real_fchmod(file_descriptor, mode)

    monkeypatch.setattr(main.os, "fsync", recording_fsync)
    monkeypatch.setattr(main.os, "fchmod", recording_fchmod)

    main._atomic_write_text(target, "new")

    chmod_index = events.index("fchmod")
    assert events[chmod_index - 1] == "fsync"
    assert events[chmod_index + 1] == "fsync"


def test_new_parent_directories_are_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[Path] = []

    def record_directory_sync(path: Path) -> None:
        synced.append(path)
        return None

    monkeypatch.setattr(main, "_fsync_directory", record_directory_sync)
    target = tmp_path / "one" / "two" / "file.txt"

    main._atomic_write_text(target, "durable")

    assert synced == [tmp_path, tmp_path / "one", tmp_path / "one" / "two"]


def test_save_maps_existing_directory_conflict_to_400(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "destination").mkdir()

    response = client.post(
        "/save", json={"text": "no", "path": "destination"}
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Destination conflicts with an existing filesystem entry"
    }
    assert str(tmp_path) not in response.text


def test_save_maps_non_directory_parent_conflict_to_400(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "parent").write_text("file", encoding="utf-8")

    response = client.post(
        "/save", json={"text": "no", "path": "parent/child.txt"}
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Destination conflicts with an existing filesystem entry"
    }
    assert str(tmp_path) not in response.text


def test_save_rejects_whitespace_only_path(client: TestClient) -> None:
    response = client.post("/save", json={"text": "no", "path": " \t "})

    assert response.status_code == 400
    assert response.json() == {"detail": "Missing save path"}


def test_save_preserves_nonempty_path_whitespace(
    client: TestClient, tmp_path: Path
) -> None:
    filename = " spaced name.txt "

    response = client.post("/save", json={"text": "exact", "path": filename})

    assert response.status_code == 200
    assert (tmp_path / filename).read_text(encoding="utf-8") == "exact"
    assert not (tmp_path / filename.strip()).exists()


def test_request_id_is_returned_and_invalid_values_are_replaced(
    client: TestClient,
) -> None:
    supplied = client.get("/health", headers={"X-Request-ID": "client-123"})
    invalid = client.get("/health", headers={"X-Request-ID": "contains spaces"})

    assert supplied.headers["x-request-id"] == "client-123"
    assert main.REQUEST_ID_PATTERN.fullmatch(invalid.headers["x-request-id"])
    assert invalid.headers["x-request-id"] != "contains spaces"


def test_json_log_formatter_includes_structured_request_context() -> None:
    record = logging.LogRecord(
        name="file_bridge",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request complete",
        args=(),
        exc_info=None,
    )
    record.event = "request_complete"
    record.status_code = 200
    token = main.REQUEST_ID.set("request-42")
    try:
        payload = json.loads(main.JsonLogFormatter().format(record))
    finally:
        main.REQUEST_ID.reset(token)

    assert payload["message"] == "request complete"
    assert payload["event"] == "request_complete"
    assert payload["request_id"] == "request-42"
    assert payload["status_code"] == 200

from __future__ import annotations

import errno
import inspect
import json
import logging
import os
import shutil
import socket
import stat
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message

import main


@pytest.fixture(autouse=True)
def _reset_module_state() -> Generator[None, None, None]:
    """Reset module-level state before each test to prevent cross-test leakage."""
    main._SAVE_ROOT_DEVICE = None
    main._SAVE_ROOT_INODE = None
    main.READY = False
    main.RUNTIME_CONFIG = None
    yield
    main._SAVE_ROOT_DEVICE = None
    main._SAVE_ROOT_INODE = None
    main.READY = False
    main.RUNTIME_CONFIG = None


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    config = main.AppConfig(save_root=tmp_path)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    with TestClient(main.app, base_url="http://127.0.0.1:8964") as test_client:
        yield test_client


def test_save_creates_file_and_returns_path(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/save", json={"text": "hello", "path": "notes/today.txt"})

    expected = (tmp_path / "notes/today.txt").resolve()
    assert response.status_code == 200
    assert response.json() == {"ok": True, "path": str(expected)}
    assert expected.read_text(encoding="utf-8") == "hello"


def test_save_expands_tilde_slash_relative_to_save_root(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/save", json={"text": "hello", "path": "~/test.txt"})

    expected = (tmp_path / "test.txt").resolve()
    assert response.status_code == 200
    assert response.json() == {"ok": True, "path": str(expected)}
    assert expected.read_text(encoding="utf-8") == "hello"


def test_save_accepts_absolute_paths_inside_save_root(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "absolute.txt"

    response = client.post("/save", json={"text": "absolute", "path": str(target)})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "path": str(target.resolve())}
    assert target.read_text(encoding="utf-8") == "absolute"


def test_save_rejects_paths_outside_save_root(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/save", json={"text": "no", "path": "../outside.txt"})

    assert response.status_code == 400
    assert not (tmp_path.parent / "outside.txt").exists()


def test_save_rejects_absolute_path_outside_save_root(client: TestClient, tmp_path: Path) -> None:
    outside = Path("/tmp/outside.txt")
    assert not outside.is_relative_to(tmp_path)

    response = client.post("/save", json={"text": "no", "path": str(outside)})

    assert response.status_code == 400
    assert response.json() == {"detail": "Save path must stay under configured save_root"}


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
    assert response.json() == {"detail": "Save path must name a file, not a directory"}
    assert not (tmp_path / "directory").exists()


def test_save_allows_text_within_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main.AppConfig(save_root=tmp_path, max_text_bytes=2)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client:
        response = client.post("/save", json={"text": "é", "path": "within.txt"})

    assert response.status_code == 200
    assert (tmp_path / "within.txt").read_text(encoding="utf-8") == "é"


def test_save_rejects_text_exceeding_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main.AppConfig(save_root=tmp_path, max_text_bytes=3)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client:
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
    legacy_body_limit = main.DEFAULT_MAX_TEXT_BYTES * 6 + main.REQUEST_BODY_OVERHEAD_BYTES
    text = "x" * (legacy_body_limit + 1)

    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client:
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

    monkeypatch.setattr(os, "replace", fail_replace)
    response = client.post("/save", json={"text": "replacement", "path": "existing.txt"})

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


def test_save_encoding_failure_removes_temp_file(client: TestClient, tmp_path: Path) -> None:
    # Use raw content to bypass httpx2 JSON serialization, which rejects
    # unpaired surrogates before the request reaches the server.
    response = client.post(
        "/save",
        content='{"text": "\\ud800", "path": "invalid-encoding.txt"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert not (tmp_path / "invalid-encoding.txt").exists()
    assert list(tmp_path.glob(".file-bridge-*.tmp")) == []


def test_save_overwrite_preserves_existing_permissions(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("original", encoding="utf-8")
    target.chmod(0o644)

    response = client.post("/save", json={"text": "replacement", "path": "existing.txt"})

    assert response.status_code == 200
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_save_returns_success_when_directory_fsync_fails(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_directory_sync(path: Path) -> str | None:
        return main._durability_warning()

    monkeypatch.setattr(main, "_fsync_directory", fail_directory_sync)

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


def test_paths_omits_exact_file_match(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "tts.txt").write_text("complete", encoding="utf-8")
    (tmp_path / "tts.txt.backup").write_text("backup", encoding="utf-8")

    response = client.get("/paths", params={"prefix": "tts.txt"})

    assert response.status_code == 200
    assert response.json() == {"paths": ["tts.txt.backup"]}


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


def test_paths_filters_exact_directory_name_as_prefix(client: TestClient, tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "today.md").write_text("today", encoding="utf-8")
    (notes / "archive").mkdir()
    (tmp_path / "notes-backup").mkdir()

    response = client.get("/paths", params={"prefix": "notes"})

    assert response.status_code == 200
    assert response.json() == {"paths": ["notes/", "notes-backup/"]}


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
    real_resolve = Path.resolve
    resolved_entries = 0

    def recording_resolve(path: Path, strict: bool = False) -> Path:
        nonlocal resolved_entries
        if path.parent == tmp_path:
            resolved_entries += 1
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", recording_resolve)
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
    file_descriptor = os.open(invalid_path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(file_descriptor)
    (tmp_path / "valid.txt").write_text("ok", encoding="utf-8")

    response = client.get("/paths")

    assert response.status_code == 200
    assert response.json() == {"paths": ["valid.txt"]}


def test_paths_excludes_internal_temporary_files(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / ".file-bridge-abcdefgh.tmp").write_text("partial", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("ok", encoding="utf-8")

    response = client.get("/paths")

    assert response.status_code == 200
    assert response.json() == {"paths": ["visible.txt"]}


def test_save_custom_temp_filename_appears_in_paths(client: TestClient) -> None:
    filename = ".file-bridge-custom.tmp"

    save_response = client.post("/save", json={"text": "visible", "path": filename})
    paths_response = client.get("/paths")

    assert save_response.status_code == 200
    assert paths_response.status_code == 200
    assert paths_response.json() == {"paths": [filename]}


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
        with TestClient(main.app, base_url="http://127.0.0.1:8964"):
            pass


def test_liveness_and_readiness_are_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main.AppConfig(save_root=tmp_path)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)

    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client:
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
    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client:
        response = client.post("/save", json={"text": "still active", "path": "runtime.txt"})

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

    assert config.port == main.DEFAULT_PORT
    assert config.max_text_bytes == main.DEFAULT_MAX_TEXT_BYTES
    assert config.save_root == Path("~").expanduser().resolve()


@pytest.mark.parametrize("yaml_value", ["null", "123", "false"])
def test_config_rejects_null_or_non_string_save_root(tmp_path: Path, yaml_value: str) -> None:
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

    with pytest.raises(RuntimeError, match="max_text_bytes must be a non-negative integer"):
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

    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client:
        response = client.post("/save", json={"text": "ok", "path": str(target)})

    assert response.status_code == 200
    assert target.read_text(encoding="utf-8") == "ok"


@pytest.mark.parametrize("filename", ["config.yaml", "main.py"])
def test_resolve_save_path_accepts_application_files_within_save_root(filename: str) -> None:
    config = main.AppConfig(save_root=main.APPLICATION_ROOT.parent)

    resolved = main._resolve_save_path(str(main.APPLICATION_ROOT / filename), config)

    assert resolved == main.APPLICATION_ROOT / filename


@pytest.mark.parametrize("path", ["bad\x00name.txt", "~definitely-no-such-user/file.txt"])
def test_save_rejects_invalid_user_paths(
    client: TestClient, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_expanduser = Path.expanduser

    def fail_expanduser(self: Path) -> Path:
        if "no-such-user" in str(self):
            raise RuntimeError("no such user")
        return real_expanduser(self)

    if "no-such-user" in path:
        monkeypatch.setattr(Path, "expanduser", fail_expanduser)

    response = client.post("/save", json={"text": "no", "path": path})

    assert response.status_code == 400


def test_save_rejects_path_that_is_too_long(client: TestClient) -> None:
    response = client.post("/save", json={"text": "no", "path": "x" * 5000})

    assert response.status_code == 400


def test_paths_rejects_invalid_user_paths(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_expanduser = Path.expanduser

    def fail_expanduser(self: Path) -> Path:
        if "no-such-user" in str(self):
            raise RuntimeError("no such user")
        return real_expanduser(self)

    monkeypatch.setattr(Path, "expanduser", fail_expanduser)

    for prefix in ["bad\x00name", "~definitely-no-such-user", "x" * 5000]:
        response = client.get("/paths", params={"prefix": prefix})
        assert response.status_code == 400


def test_save_rejects_extra_request_fields(client: TestClient) -> None:
    response = client.post("/save", json={"text": "hello", "path": "ok.txt", "unexpected": True})

    assert response.status_code == 422


def test_save_rejects_extra_field_with_unpaired_surrogate(
    client: TestClient, tmp_path: Path
) -> None:
    response = client.post(
        "/save",
        content=b'{"text":"hello","path":"ok.txt","unexpected":"\\ud800"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_save_rejects_wrong_type_field_with_unpaired_surrogate(
    client: TestClient, tmp_path: Path
) -> None:
    response = client.post(
        "/save",
        content=b'{"text":["\\ud800"],"path":"ok.txt"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_save_rejects_root_body_with_unpaired_surrogate(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        "/save",
        content=b'"\\ud800"',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_paths_includes_symlinks_that_escape_save_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_root = tmp_path / "root"
    outside = tmp_path / "outside"
    save_root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (save_root / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=save_root))

    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client:
        root_response = client.get("/paths")
        escaped_response = client.get("/paths", params={"prefix": "escape/"})

    assert root_response.status_code == 200
    assert root_response.json() == {"paths": ["escape/"]}
    assert escaped_response.status_code == 200
    assert escaped_response.json() == {"paths": ["escape/secret.txt"]}


def test_paths_preserves_safe_internal_symlink_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_root = tmp_path / "root"
    target = save_root / "target"
    target.mkdir(parents=True)
    (target / "note.txt").write_text("note", encoding="utf-8")
    (save_root / "alias").symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=save_root))

    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client:
        response = client.get("/paths", params={"prefix": "alias/"})

    assert response.status_code == 200
    assert response.json() == {"paths": ["alias/note.txt"]}


def test_paths_includes_application_directory_via_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_root = tmp_path / "root"
    application_root = save_root / "application"
    application_root.mkdir(parents=True)
    (application_root / "main.py").write_text("", encoding="utf-8")
    (save_root / "application-link").symlink_to(application_root, target_is_directory=True)
    monkeypatch.setattr(main, "APPLICATION_ROOT", application_root)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=save_root))

    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client:
        response = client.get("/paths", params={"prefix": "application-link/"})

    assert response.status_code == 200
    assert response.json() == {"paths": ["application-link/main.py"]}


def test_request_body_limit_rejects_before_json_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main.AppConfig(save_root=tmp_path, max_text_bytes=1)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    oversized_malformed_json = b"{" + b"x" * main._request_body_limit(config)

    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client:
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
def test_config_rejects_empty_or_whitespace_save_root(tmp_path: Path, save_root: str) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"save_root: {save_root}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="empty or whitespace-only"):
        main.load_config(config_path)


def test_config_rejects_duplicate_top_level_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f'save_root: "{tmp_path}"\nsave_root: "/tmp"\n', encoding="utf-8")

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
        with TestClient(main.app, base_url="http://127.0.0.1:8964"):
            pass


def test_startup_rejects_save_root_inside_application_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=main.APPLICATION_ROOT))

    with pytest.raises(RuntimeError, match="inside the application directory"):
        with TestClient(main.app, base_url="http://127.0.0.1:8964"):
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

    with TestClient(main.app, base_url="http://127.0.0.1:8964"):
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
    monkeypatch.setattr(os, "getuid", lambda: stale.stat().st_uid + 1)

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
        with TestClient(main.app, base_url="http://127.0.0.1:8964"):
            pass


def test_save_supports_maximum_length_filename(client: TestClient, tmp_path: Path) -> None:
    name_max = os.pathconf(tmp_path, "PC_NAME_MAX")
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
    real_fsync = os.fsync
    real_fchmod = os.fchmod

    def recording_fsync(file_descriptor: int) -> None:
        events.append("fsync")
        real_fsync(file_descriptor)

    def recording_fchmod(file_descriptor: int, mode: int) -> None:
        events.append("fchmod")
        real_fchmod(file_descriptor, mode)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "fchmod", recording_fchmod)

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


def test_save_maps_existing_directory_conflict_to_400(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "destination").mkdir()

    response = client.post("/save", json={"text": "no", "path": "destination"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Destination conflicts with an existing filesystem entry"}
    assert str(tmp_path) not in response.text


def test_save_maps_existing_fifo_conflict_to_400(client: TestClient, tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    os.mkfifo(destination)

    response = client.post("/save", json={"text": "no", "path": "destination"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Destination conflicts with an existing filesystem entry"}
    assert stat.S_ISFIFO(destination.stat().st_mode)


def test_save_maps_existing_socket_conflict_to_400(client: TestClient, tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    with socket.socket(socket.AF_UNIX) as unix_socket:
        unix_socket.bind(str(destination))

        response = client.post("/save", json={"text": "no", "path": "destination"})

        assert response.status_code == 400
        assert response.json() == {
            "detail": "Destination conflicts with an existing filesystem entry"
        }
        assert stat.S_ISSOCK(destination.stat().st_mode)


def test_save_maps_non_directory_parent_conflict_to_400(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "parent").write_text("file", encoding="utf-8")

    response = client.post("/save", json={"text": "no", "path": "parent/child.txt"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Destination conflicts with an existing filesystem entry"}
    assert str(tmp_path) not in response.text


def test_save_rejects_whitespace_only_path(client: TestClient) -> None:
    response = client.post("/save", json={"text": "no", "path": " \t "})

    assert response.status_code == 400
    assert response.json() == {"detail": "Missing save path"}


def test_save_preserves_nonempty_path_whitespace(client: TestClient, tmp_path: Path) -> None:
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


def test_save_rejects_reserved_temp_filename(client: TestClient) -> None:
    response = client.post("/save", json={"text": "no", "path": ".file-bridge-abcdefgh.tmp"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Save path uses a reserved filename pattern"}


# ---------------------------------------------------------------------------
# MEDIUM 1 — Host / Origin validation
# ---------------------------------------------------------------------------


def test_host_header_accepts_loopback(client: TestClient) -> None:
    response = client.get("/health", headers={"Host": "127.0.0.1:8964"})
    assert response.status_code == 200


def test_host_header_accepts_localhost(client: TestClient) -> None:
    response = client.get("/health", headers={"Host": "localhost:8964"})
    assert response.status_code == 200


def test_host_header_rejects_external(client: TestClient) -> None:
    response = client.get("/health", headers={"Host": "example.com"})
    assert response.status_code == 421
    assert response.json() == {"detail": "Misdirected Request"}


def test_origin_header_rejects_null(client: TestClient) -> None:
    """Opaque origins (null) are untrusted and must be rejected."""
    response = client.get("/health", headers={"Origin": "null"})
    assert response.status_code == 403


def test_origin_header_accepts_loopback(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": "http://127.0.0.1:8964"})
    assert response.status_code == 200


def test_origin_header_rejects_external(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": "http://evil.example.com"})
    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


def test_no_origin_is_accepted(client: TestClient) -> None:
    """Non-browser localhost clients typically send no Origin header."""
    response = client.get("/health")
    assert response.status_code == 200


# -- Host / Origin: IPv6, port range, and malformed inputs ------------------


@pytest.mark.parametrize(
    "host_value",
    [
        "[::1]",
        "[::1]:8964",
        "127.0.0.1",
        "127.0.0.1:8964",
        "localhost",
        "localhost:8964",
    ],
)
def test_host_header_accepts_valid_loopback(client: TestClient, host_value: str) -> None:
    response = client.get("/health", headers={"Host": host_value})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "host_value",
    [
        # Bare IPv6 without brackets — ambiguous with port syntax
        "::1",
        "[::1]:0",
        "[::1]:99999",
        "127.0.0.1:0",
        "127.0.0.1:99999",
        "localhost:0",
        # Malformed — userinfo, path, whitespace tricks
        "user@127.0.0.1",
        "127.0.0.1/path",
        "[::1] ",
        " [::1]",
        "[::1",
        "::1]:8964",
        "127.0.0.1:not-a-port",
    ],
)
def test_host_header_rejects_invalid_loopback(client: TestClient, host_value: str) -> None:
    response = client.get("/health", headers={"Host": host_value})
    assert response.status_code == 421


@pytest.mark.parametrize(
    "origin_value",
    [
        "http://127.0.0.1",
        "http://127.0.0.1:8964",
        "http://localhost",
        "http://localhost:8964",
        "http://[::1]",
        "http://[::1]:8964",
        "https://127.0.0.1",
        "https://localhost:8964",
    ],
)
def test_origin_header_accepts_valid_loopback(client: TestClient, origin_value: str) -> None:
    response = client.get("/health", headers={"Origin": origin_value})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "origin_value",
    [
        "http://127.0.0.1:0",
        "http://127.0.0.1:99999",
        "http://localhost:65536",
        "http://[::1]:0",
        # Non-loopback origins
        "http://evil.example.com",
        "https://example.com:443",
        "http://127.0.0.2",
        "http://[::2]",
        # Malformed
        "file://127.0.0.1",
        "http://user@127.0.0.1:8964",
        "http://127.0.0.1/path",
        "",
    ],
)
def test_origin_header_rejects_invalid(client: TestClient, origin_value: str) -> None:
    response = client.get("/health", headers={"Origin": origin_value})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# MEDIUM 2 — transport body cap
# ---------------------------------------------------------------------------


def test_transport_cap_enforced_when_max_text_bytes_is_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main.AppConfig(save_root=tmp_path, max_text_bytes=0)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    body = b"{" + b"x" * main.MAX_TRANSPORT_BODY_BYTES
    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client:
        response = client.post(
            "/save",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json() == {"detail": "Request body is too large"}


def test_streaming_receive_passes_chunks_without_buffering() -> None:
    """Downstream sees chunks as they arrive; no replay list exists."""
    chunks: list[bytes] = [b"chunk-1-", b"chunk-2-", b"chunk-3"]
    position = 0

    async def fake_receive() -> Message:
        nonlocal position
        if position < len(chunks):
            body = chunks[position]
            more = position < len(chunks) - 1
            position += 1
            return {"type": "http.request", "body": body, "more_body": more}
        return {"type": "http.request", "body": b"", "more_body": False}

    capped = main._LimitedReceive(fake_receive, 1_000_000)

    import asyncio

    async def _run() -> None:
        # First chunk passes through
        msg1 = await capped()
        assert msg1.get("body") == b"chunk-1-"
        assert msg1.get("more_body") is True
        assert capped.received_bytes == 8
        assert capped.exceeded is False

        # Second chunk passes through
        msg2 = await capped()
        assert msg2.get("body") == b"chunk-2-"
        assert capped.received_bytes == 16
        assert capped.exceeded is False

        # Third chunk passes through
        msg3 = await capped()
        assert msg3.get("body") == b"chunk-3"
        assert capped.received_bytes == 23
        assert capped.exceeded is False

    asyncio.run(_run())


def test_streaming_receive_detects_limit_exceeded() -> None:
    """When the cap is exceeded mid-stream, remaining chunks are drained."""

    async def fake_receive() -> Message:
        # First call returns an oversize chunk with more_body still True.
        # The wrapper should drain, then return an empty final frame.
        return {"type": "http.request", "body": b"x" * 100, "more_body": True}

    capped = main._LimitedReceive(fake_receive, 99)

    import asyncio

    async def _run() -> None:
        msg = await capped()
        # The fake_receive always returns more_body=True for every call,
        # so the drain loop will exhaust its safety bound and return the
        # empty final frame.
        assert capped.exceeded is True
        assert capped.received_bytes == 100
        assert msg.get("body") == b""
        assert msg.get("more_body") is False

    asyncio.run(_run())


def test_streaming_receive_handles_empty_chunks() -> None:
    """Zero-length body chunks are counted but do not trigger the cap alone."""
    chunked: list[Message] = [
        {"type": "http.request", "body": b"", "more_body": True},
        {"type": "http.request", "body": b"x", "more_body": True},
        {"type": "http.request", "body": b"", "more_body": True},
        {"type": "http.request", "body": b"y", "more_body": False},
    ]
    position = 0

    async def fake_receive() -> Message:
        nonlocal position
        msg = chunked[position]
        position += 1
        return msg

    capped = main._LimitedReceive(fake_receive, 10)

    import asyncio

    async def _run() -> None:
        msg1 = await capped()
        assert msg1.get("body") == b"" and msg1.get("more_body") is True
        assert capped.received_bytes == 0

        msg2 = await capped()
        assert msg2.get("body") == b"x"
        assert capped.received_bytes == 1

        msg3 = await capped()
        assert msg3.get("body") == b""
        assert capped.received_bytes == 1

        msg4 = await capped()
        assert msg4.get("body") == b"y" and msg4.get("more_body") is False
        assert capped.received_bytes == 2
        assert capped.exceeded is False

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# MEDIUM 3 — save_root identity
# ---------------------------------------------------------------------------


def test_save_root_identity_change_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """save_root identity change is detected after startup."""
    config = main.AppConfig(save_root=tmp_path)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    monkeypatch.setattr(main, "_SAVE_ROOT_DEVICE", 99999)
    monkeypatch.setattr(main, "_SAVE_ROOT_INODE", 99999)

    with pytest.raises(RuntimeError, match="save_root identity changed"):
        with TestClient(main.app, base_url="http://127.0.0.1:8964"):
            pass


def test_readiness_fails_when_save_root_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness probe fails when save_root device/inode changes."""
    config = main.AppConfig(save_root=tmp_path)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", config)
    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client:
        main._SAVE_ROOT_DEVICE = 99999  # simulate identity mismatch
        response = client.get("/ready")
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# LOW 4 — reserved temp name via symlink / resolved path
# ---------------------------------------------------------------------------


def test_save_rejects_reserved_temp_name_through_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink whose resolved name matches the temp pattern is rejected."""
    real_file = tmp_path / "real-file.txt"
    real_file.write_text("real", encoding="utf-8")
    symlink = tmp_path / ".file-bridge-abcdefgh.tmp"
    symlink.symlink_to(real_file)
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=tmp_path))

    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client:
        response = client.post("/save", json={"text": "x", "path": "real-file.txt"})
        assert response.status_code == 200  # real-file.txt is fine

        response = client.post("/save", json={"text": "x", "path": ".file-bridge-abcdefgh.tmp"})
        assert response.status_code == 400
        assert "reserved filename" in response.json()["detail"]


# ---------------------------------------------------------------------------
# LOW 5 — /paths?prefix=. regression
# ---------------------------------------------------------------------------


def test_paths_dot_prefix_does_not_scan_parent(client: TestClient, tmp_path: Path) -> None:
    """prefix=. should not scan the parent of save_root."""
    (tmp_path / "somefile.txt").write_text("x", encoding="utf-8")
    response = client.get("/paths", params={"prefix": "."})
    assert response.status_code == 200
    # Should return empty — '.' resolves to save_root itself, and none of its
    # children start with '.'
    assert response.json() == {"paths": []}


# ---------------------------------------------------------------------------
# LOW 6 — stale cleanup entry budget
# ---------------------------------------------------------------------------


def test_stale_cleanup_charges_every_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each DirEntry counts against the cleanup budget; entries beyond the cap are not scanned."""
    budget = 5
    monkeypatch.setattr(main, "MAX_CLEANUP_SCAN_ENTRIES", budget)

    # Create many stale temp files so we exceed the budget.
    for idx in range(15):
        stale = tmp_path / f".file-bridge-{idx:08d}.tmp"
        stale.write_text("stale", encoding="utf-8")
        old_timestamp = time.time() - main.STALE_TEMP_FILE_AGE_SECONDS - 1
        os.utime(stale, (old_timestamp, old_timestamp))

    # Wrap scandir to count how many DirEntry items are actually produced.
    scanned_count = 0
    real_scandir = os.scandir

    def counting_scandir(path: str) -> Any:
        nonlocal scanned_count
        iterator = real_scandir(path)
        # os.scandir() returns a context-manager iterator; wrap the iteration
        # so we can inspect entries without affecting the caller.
        class _CountingIterator:
            def __init__(self, it: Any) -> None:
                self._it = it
            def __iter__(self) -> "_CountingIterator":
                return self
            def __next__(self) -> "os.DirEntry[str]":
                nonlocal scanned_count
                entry: "os.DirEntry[str]" = next(self._it)
                scanned_count += 1
                return entry
            def __enter__(self) -> "_CountingIterator":
                return self
            def __exit__(self, *args: object) -> None:
                self._it.close()
        return _CountingIterator(iterator)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    main._cleanup_stale_temp_files(tmp_path)

    # The scan stops when the (budget+1)-th entry triggers exhaustion —
    # that entry is still produced by scandir but rejected before processing.
    # So the number of entries examined is at most budget + 1.
    assert scanned_count <= budget + 1, (
        f"scanned {scanned_count} entries, expected at most {budget + 1}"
    )


def test_stale_cleanup_does_not_follow_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup should not follow directory symlinks to targets outside save_root."""
    outside_dir = tmp_path.with_name(tmp_path.name + "_external")
    outside_dir.mkdir()
    try:
        stale = outside_dir / ".file-bridge-abcdefgh.tmp"
        stale.write_text("stale", encoding="utf-8")
        old_timestamp = time.time() - main.STALE_TEMP_FILE_AGE_SECONDS - 1
        os.utime(stale, (old_timestamp, old_timestamp))
        link = tmp_path / "link_out"
        link.symlink_to(outside_dir, target_is_directory=True)

        main._cleanup_stale_temp_files(tmp_path)

        # Symlink not followed, so stale file outside save_root survives.
        assert stale.exists()
    finally:
        shutil.rmtree(outside_dir, ignore_errors=True)


def test_stale_cleanup_handles_nesting_beyond_recursion_limit(tmp_path: Path) -> None:
    recursion_limit = 200
    depth = recursion_limit + 50
    current = tmp_path
    for _ in range(depth):
        current = current / "d"
        current.mkdir()

    stale = current / ".file-bridge-abcdefgh.tmp"
    stale.write_text("stale", encoding="utf-8")
    old_timestamp = time.time() - main.STALE_TEMP_FILE_AGE_SECONDS - 1
    os.utime(stale, (old_timestamp, old_timestamp))

    original_recursion_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(recursion_limit)
        main._cleanup_stale_temp_files(tmp_path)
    finally:
        sys.setrecursionlimit(original_recursion_limit)

    assert not stale.exists()


# ---------------------------------------------------------------------------
# LOW 7 — explicit null config
# ---------------------------------------------------------------------------


def test_config_rejects_explicit_null_top_level(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("null\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not null"):
        main.load_config(path=config_path)


def test_config_rejects_explicit_tilde_top_level(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("~\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not null"):
        main.load_config(path=config_path)


def test_config_accepts_comment_only_yaml_as_empty(tmp_path: Path) -> None:
    """A config containing only YAML comments is treated as empty."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("# Application settings\n# port: 9999\n", encoding="utf-8")
    config = main.load_config(path=config_path)
    assert config.port == main.DEFAULT_PORT
    assert config.max_text_bytes == main.DEFAULT_MAX_TEXT_BYTES


def test_config_accepts_comment_with_blank_lines_as_empty(tmp_path: Path) -> None:
    """Comments interspersed with blank lines are still comment-only."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("\n  # comment\n\n", encoding="utf-8")
    config = main.load_config(path=config_path)
    assert config.port == main.DEFAULT_PORT


def test_config_rejects_comment_followed_by_null(tmp_path: Path) -> None:
    """A comment followed by explicit null is NOT blank — null is a value."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("# some comment\nnull\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not null"):
        main.load_config(path=config_path)


def test_config_rejects_comment_followed_by_tilde(tmp_path: Path) -> None:
    """A comment followed by ~ is NOT blank — ~ evaluates to null."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("# header comment\n~\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not null"):
        main.load_config(path=config_path)


def test_config_accepts_empty_file_as_empty(tmp_path: Path) -> None:
    """A truly empty config file is treated as empty dict."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = main.load_config(path=config_path)
    assert config.port == main.DEFAULT_PORT


def test_config_accepts_whitespace_only_as_empty(tmp_path: Path) -> None:
    """Whitespace-only config files are treated as empty dict."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("     \n  \n", encoding="utf-8")
    config = main.load_config(path=config_path)
    assert config.port == main.DEFAULT_PORT


# ---------------------------------------------------------------------------
# LOW 8 — exact file suppression + cap = 30
# ---------------------------------------------------------------------------


def test_paths_returns_30_results_with_exact_match(client: TestClient, tmp_path: Path) -> None:
    """When 33 entries share a prefix and one is the exact file match, still get 30."""
    # "item-aa" is the exact match; all others start with "item-aa-" so they
    # match the prefix filter and fill the result set beyond MAX_PATH_RESULTS.
    (tmp_path / "item-aa").write_text("x", encoding="utf-8")
    for index in range(32):
        (tmp_path / f"item-aa-{index:02}").write_text("x", encoding="utf-8")
    response = client.get("/paths", params={"prefix": "item-aa"})
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert len(paths) == 30
    # The exact match "item-aa" must not appear
    assert "item-aa" not in paths


# ---------------------------------------------------------------------------
# LOW 11 — deterministic imports
# ---------------------------------------------------------------------------


def test_main_module_is_the_local_file() -> None:
    """Guard against importing an unrelated ambient main module."""
    assert str(main.APPLICATION_ROOT) in str(main.__file__)


# ---------------------------------------------------------------------------
# LOW 1 — lifespan globals for save_root identity
# ---------------------------------------------------------------------------


def test_lifespan_restart_with_different_save_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After shutdown, a new lifespan with a different save_root must work.

    _SAVE_ROOT_DEVICE and _SAVE_ROOT_INODE must be cleared by the lifespan
    shutdown path so that the next startup (with a potentially different
    save_root) does not see a stale identity and reject it as "changed."
    """
    root1 = tmp_path / "root1"
    root2 = tmp_path / "root2"
    root1.mkdir()
    root2.mkdir()

    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=root1))
    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client1:
        assert client1.get("/health").status_code == 200

    # After the lifespan exits the shutdown path must have cleared the globals.
    assert main._SAVE_ROOT_DEVICE is None, (
        "_SAVE_ROOT_DEVICE was not cleared on shutdown"
    )
    assert main._SAVE_ROOT_INODE is None, (
        "_SAVE_ROOT_INODE was not cleared on shutdown"
    )

    # A second startup with a different save_root must succeed.
    monkeypatch.setattr(main, "RUNTIME_CONFIG", main.AppConfig(save_root=root2))
    with TestClient(main.app, base_url="http://127.0.0.1:8964") as client2:
        assert client2.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# LOW 2 — missing Host header rejection
# ---------------------------------------------------------------------------


def test_missing_host_header_rejected() -> None:
    """Requests without a Host header must be rejected (HTTP 421).

    TestClient always synthesizes a Host header, so we drive the ASGI app
    directly with an empty header list.
    """
    import asyncio

    async def _send_request() -> int:
        scope: dict[str, Any] = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "headers": [],  # deliberately no Host header
        }
        messages: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await main.app(scope, receive, send)  # type: ignore[arg-type]
        assert messages, "no ASGI response messages produced"
        start = messages[0]
        assert start["type"] == "http.response.start"
        return cast(int, start["status"])

    status = asyncio.run(_send_request())
    assert status == 421, f"expected 421, got {status}"


# ---------------------------------------------------------------------------
# LOW 3 — body enforcement on bodyless routes
# ---------------------------------------------------------------------------


def test_get_with_content_length_rejected(client: TestClient) -> None:
    """A GET request that declares a Content-Length > 0 is rejected (413).

    GET /health must not accept a request body.  The middleware must drain
    the body before sending the rejection response to preserve transport
    integrity.
    """
    response = client.request(
        "GET",
        "/health",
        headers={"Content-Length": "5"},
        content=b"hello",
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "Request body is too large"}


def test_get_with_chunked_body_rejected() -> None:
    """A GET request with Transfer-Encoding: chunked is rejected (413).

    Streamed/chunked bodies on bodyless routes must be caught before the
    downstream consumer ever sees the body.
    """
    import asyncio

    async def _send_request() -> int:
        scope: dict[str, Any] = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1:8964"),
                (b"transfer-encoding", b"chunked"),
            ],
        }
        chunk_sent = False
        messages: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            nonlocal chunk_sent
            if not chunk_sent:
                chunk_sent = True
                return {
                    "type": "http.request",
                    "body": b"chunked-data-here",
                    "more_body": True,
                }
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await main.app(scope, receive, send)  # type: ignore[arg-type]
        assert messages, "no ASGI response messages produced"
        start = messages[0]
        assert start["type"] == "http.response.start"
        return cast(int, start["status"])

    status = asyncio.run(_send_request())
    assert status == 413, f"expected 413, got {status}"


def test_get_without_body_still_works(client: TestClient) -> None:
    """Ordinary bodyless GET requests must continue to work."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


# ---------------------------------------------------------------------------
# BODY-LESS ROUTE DRAIN — disconnect and oversized-stream regression tests
# ---------------------------------------------------------------------------


def test_bodyless_drain_handles_disconnect_without_infinite_loop() -> None:
    """Disconnect during bodyless-route drain must not spin forever.

    The ASGI spec allows ``receive()`` to return ``http.disconnect``
    repeatedly after the client disconnects.  The middleware must detect
    this and stop, not loop indefinitely.
    """
    import asyncio

    async def _drive() -> None:
        scope: dict[str, Any] = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1:8964"),
                (b"content-length", b"100"),
            ],
        }

        call_count = 0
        messages: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count > 50:
                raise RuntimeError("infinite loop — exceeded 50 receive calls")
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await main.app(scope, receive, send)  # type: ignore[arg-type]

        # On disconnect no response should be attempted because the
        # client is gone.
        response_starts = [
            m for m in messages if m["type"] == "http.response.start"
        ]
        assert len(response_starts) == 0, (
            f"expected no response on disconnect, got {response_starts}"
        )

    asyncio.run(_drive())


def test_bodyless_drain_rejects_oversized_multi_frame_stream() -> None:
    """A multi-frame body exceeding MAX_TRANSPORT_BODY_BYTES is rejected.

    After the cap is exceeded the middleware drains remaining body
    frames (bounded) so the transport stays clean, then sends 413.
    """
    import asyncio

    async def _drive() -> None:
        scope: dict[str, Any] = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1:8964"),
                (b"transfer-encoding", b"chunked"),
            ],
        }

        # Produce multiple frames so that collectively they exceed the
        # transport cap but no single frame does.
        chunk_size = main.MAX_TRANSPORT_BODY_BYTES // 4
        frame_count = 6  # 6 × (chunk_size+1) > MAX_TRANSPORT_BODY_BYTES
        chunks: list[dict[str, Any]] = []
        for i in range(frame_count):
            chunks.append(
                {
                    "type": "http.request",
                    "body": b"x" * (chunk_size + 1),
                    "more_body": i < frame_count - 1,
                }
            )

        position = 0
        messages: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            nonlocal position
            if position < len(chunks):
                msg = chunks[position]
                position += 1
                return msg
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await main.app(scope, receive, send)  # type: ignore[arg-type]

        response_starts = [
            m for m in messages if m["type"] == "http.response.start"
        ]
        assert len(response_starts) == 1
        assert response_starts[0]["status"] == 413

        body_chunks = [
            m.get("body", b"")
            for m in messages
            if m["type"] == "http.response.body"
        ]
        body = b"".join(body_chunks)
        payload = json.loads(body)
        assert payload == {"detail": "Request body is too large"}

    asyncio.run(_drive())


def test_bodyless_drain_disconnect_during_oversized_drain() -> None:
    """Disconnect while draining an oversized body must not spin.

    The first frame exceeds the transport cap and declares more body,
    then the client disconnects.  The middleware must stop cleanly.
    """
    import asyncio

    async def _drive() -> None:
        scope: dict[str, Any] = {
            "type": "http",
            "http_version": "1.1",
            "method": "DELETE",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1:8964"),
                (
                    b"content-length",
                    str(main.MAX_TRANSPORT_BODY_BYTES * 2).encode(),
                ),
            ],
        }

        received_count = 0
        messages: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            nonlocal received_count
            received_count += 1
            if received_count > 50:
                raise RuntimeError("infinite loop — exceeded 50 receive calls")
            if received_count == 1:
                return {
                    "type": "http.request",
                    "body": b"x" * (main.MAX_TRANSPORT_BODY_BYTES + 1),
                    "more_body": True,
                }
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await main.app(scope, receive, send)  # type: ignore[arg-type]

        # No response — client disconnected before we could send one.
        response_starts = [
            m for m in messages if m["type"] == "http.response.start"
        ]
        assert len(response_starts) == 0

    asyncio.run(_drive())

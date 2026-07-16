"""Local-only FastAPI bridge for saving text and completing file paths."""

from __future__ import annotations

import bisect
import errno
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict


APPLICATION_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(__file__).with_name("config.yaml")
DEFAULT_SAVE_ROOT = "~"
LOGGER = logging.getLogger(__name__)
INVALID_PATH_ERRNOS = {errno.EINVAL, errno.ENAMETOOLONG}


class APIModel(BaseModel):
    """Base model with a deliberately closed API schema."""

    model_config = ConfigDict(extra="forbid")


class SaveRequest(APIModel):
    """Request body accepted by the save endpoint."""

    text: str
    path: str


class HealthResponse(APIModel):
    ok: bool


class SaveResponse(APIModel):
    ok: bool
    path: str
    warning: str | None = None


class PathsResponse(APIModel):
    paths: list[str]


@dataclass(frozen=True)
class AppConfig:
    """Application settings loaded from config.yaml."""

    save_root: Path = Path(DEFAULT_SAVE_ROOT)
    port: int = 8766
    max_text_bytes: int = 0

    def __post_init__(self) -> None:
        try:
            save_root = self.save_root.expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(f"Invalid save_root: {exc}") from exc
        object.__setattr__(self, "save_root", save_root)


def _load_yaml_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as config_file:
            loaded = yaml.safe_load(config_file)
    except FileNotFoundError:
        return {}
    except (yaml.YAMLError, UnicodeError) as exc:
        raise RuntimeError(f"Invalid YAML in {path}: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot read config file {path}: {exc}") from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping at the top level")
    return loaded


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    try:
        path = path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"Invalid config path {path}: {exc}") from exc

    raw_config = _load_yaml_config(path)

    unknown_keys = set(raw_config) - {"save_root", "port", "max_text_bytes"}
    if unknown_keys:
        unknown = ", ".join(sorted(map(str, unknown_keys)))
        raise RuntimeError(f"Unknown config key(s): {unknown}")

    save_root = raw_config.get("save_root", DEFAULT_SAVE_ROOT)
    if not isinstance(save_root, str):
        raise RuntimeError("config save_root must be a non-null string")

    try:
        save_root_path = Path(save_root).expanduser()
    except (RuntimeError, ValueError) as exc:
        raise RuntimeError(f"config save_root is invalid: {exc}") from exc
    if not save_root_path.is_absolute():
        save_root_path = path.parent / save_root_path

    port = raw_config.get("port", 8766)
    if type(port) is not int:
        raise RuntimeError("config port must be an integer from 1 to 65535")
    if not 1 <= port <= 65535:
        raise RuntimeError("config port must be from 1 to 65535")

    max_text_bytes = raw_config.get("max_text_bytes", 0)
    if type(max_text_bytes) is not int or max_text_bytes < 0:
        raise RuntimeError("config max_text_bytes must be a non-negative integer")

    return AppConfig(
        save_root=save_root_path,
        port=port,
        max_text_bytes=max_text_bytes,
    )


RUNTIME_CONFIG = load_config()


def _invalid_path(exc: BaseException) -> HTTPException:
    message = str(exc) or exc.__class__.__name__
    return HTTPException(status_code=400, detail=f"Invalid path: {message}")


def _validate_path_length(path: Path, save_root: Path) -> None:
    """Reject paths the target filesystem cannot represent before I/O."""

    try:
        # The configured root may not exist yet (save creates parents), so
        # query the nearest existing ancestor on the same filesystem.
        probe = save_root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        path_max = os.pathconf(probe, "PC_PATH_MAX")
        name_max = os.pathconf(probe, "PC_NAME_MAX")
        encoded_path = os.fsencode(path)
        encoded_parts = (os.fsencode(part) for part in path.parts)
    except (UnicodeError, ValueError) as exc:
        raise _invalid_path(exc) from exc
    except OSError:
        # Filesystems without pathconf support are left to the actual I/O
        # operation, whose ENAMETOOLONG/EINVAL errors are mapped to HTTP 400.
        return

    if path_max != -1 and len(encoded_path) >= path_max:
        raise HTTPException(status_code=400, detail="Invalid path: path is too long")
    if name_max != -1 and any(len(part) > name_max for part in encoded_parts):
        raise HTTPException(
            status_code=400, detail="Invalid path: a path component is too long"
        )


def _expand_user_path(raw_path: str, save_root: Path) -> Path:
    if "\x00" in raw_path:
        raise HTTPException(status_code=400, detail="Invalid path: contains a NUL byte")

    try:
        candidate = Path(raw_path).expanduser()
        path = candidate if candidate.is_absolute() else save_root / candidate
        clean = Path(os.path.normpath(str(path)))
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid_path(exc) from exc

    _validate_path_length(clean, save_root)
    return clean


def _resolve_save_path(raw_path: str, config: AppConfig) -> Path:
    raw_path = raw_path.strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="Missing save path")

    save_root = config.save_root
    # Collapse ``..`` without resolving symlinks for the containment check, so
    # paths such as ~/Ramdisk remain usable by design.
    clean = _expand_user_path(raw_path, save_root)
    try:
        clean.relative_to(save_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Save path must stay under configured save_root: {save_root}",
        ) from exc

    try:
        resolved = clean.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid_path(exc) from exc
    try:
        resolved.relative_to(APPLICATION_ROOT)
    except ValueError:
        pass
    else:
        raise HTTPException(
            status_code=400,
            detail="Cannot save inside the file-bridge application directory",
        )

    return resolved


def _fsync_directory(path: Path) -> str | None:
    """Best-effort directory sync after a file replacement has committed."""

    directory_fd: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path, flags)
        os.fsync(directory_fd)
    except OSError as exc:
        warning = f"File saved, but directory durability sync failed: {exc}"
        LOGGER.warning("%s", warning, exc_info=True)
        return warning
    finally:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                LOGGER.warning("Failed to close directory after fsync: %s", path)
    return None


def _atomic_write_text(path: Path, text: str) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        try:
            destination_mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            pass
        else:
            os.chmod(temp_path, destination_mode)

        os.replace(temp_path, path)
        temp_path = None
        return _fsync_directory(path.parent)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


app = FastAPI(title="File Bridge")


def _probe_save_root(save_root: Path) -> None:
    root_stat = save_root.stat()
    if not stat.S_ISDIR(root_stat.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, "Not a directory", str(save_root))

    with os.scandir(save_root) as entries:
        next(entries, None)
    with tempfile.TemporaryFile(dir=save_root):
        pass


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    config = RUNTIME_CONFIG

    try:
        _probe_save_root(config.save_root)
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Not ready: save_root is not an accessible directory: "
                f"{config.save_root}: {exc}"
            ),
        ) from exc
    return HealthResponse(ok=True)


@app.post("/save", response_model=SaveResponse, response_model_exclude_none=True)
def save_text(request: SaveRequest) -> SaveResponse:
    config = RUNTIME_CONFIG

    if config.max_text_bytes > 0:
        try:
            text_bytes = len(request.text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise HTTPException(
                status_code=400, detail=f"Text cannot be encoded as UTF-8: {exc}"
            ) from exc
        if text_bytes > config.max_text_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Text is {text_bytes} bytes when UTF-8 encoded; "
                    f"maximum allowed is {config.max_text_bytes} bytes"
                ),
            )

    path = _resolve_save_path(request.path, config)

    try:
        warning = _atomic_write_text(path, request.text)
    except UnicodeEncodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"Text cannot be encoded as UTF-8: {exc}"
        ) from exc
    except OSError as exc:
        if exc.errno in INVALID_PATH_ERRNOS:
            raise _invalid_path(exc) from exc
        LOGGER.exception("Failed to save text to %s", path)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save text: {exc.strerror or exc}",
        ) from exc
    return SaveResponse(ok=True, path=str(path), warning=warning)


@app.get("/paths", response_model=PathsResponse)
def list_paths(prefix: str = "") -> PathsResponse:
    config = RUNTIME_CONFIG

    save_root = config.save_root
    prefix = prefix.strip()
    prefix_ends_with_separator = prefix.endswith("/")

    if prefix:
        search_dir = _expand_user_path(prefix, save_root)
        try:
            exact_directory = search_dir.is_dir()
        except OSError as exc:
            if exc.errno in INVALID_PATH_ERRNOS:
                raise _invalid_path(exc) from exc
            exact_directory = False
        if not prefix_ends_with_separator and not exact_directory:
            search_dir = search_dir.parent
    else:
        search_dir = save_root

    try:
        search_dir.relative_to(save_root)
    except ValueError:
        return PathsResponse(paths=[])

    if not search_dir.is_dir():
        return PathsResponse(paths=[])

    prefix_lower = ""
    if prefix and not prefix_ends_with_separator:
        raw_name = Path(prefix).name.lower()
        if raw_name and raw_name != "~":
            prefix_lower = raw_name

    paths: list[tuple[str, bool]] = []
    try:
        with os.scandir(search_dir) as entries:
            for entry in entries:
                if prefix_lower and not entry.name.lower().startswith(prefix_lower):
                    continue
                try:
                    is_directory = entry.is_dir()
                except OSError as exc:
                    LOGGER.warning(
                        "Cannot inspect path suggestion %s: %s", entry.path, exc
                    )
                    continue
                bisect.insort(paths, (entry.name, is_directory))
                if len(paths) > 30:
                    paths.pop()
    except OSError as exc:
        if exc.errno in INVALID_PATH_ERRNOS:
            raise _invalid_path(exc) from exc
        LOGGER.warning("Cannot list path suggestions in %s: %s", search_dir, exc)
        return PathsResponse(paths=[])

    relative_directory = search_dir.relative_to(save_root)
    results = []
    for name, is_directory in paths:
        relative_path = str(relative_directory / name)
        if is_directory:
            relative_path += "/"
        results.append(relative_path)
    return PathsResponse(paths=results)


BIND_HOST = "127.0.0.1"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=BIND_HOST, port=RUNTIME_CONFIG.port)

"""Local-only FastAPI bridge for saving text and completing file paths."""

from __future__ import annotations

import argparse
import bisect
import contextvars
import errno
import json
import logging
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any, cast

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


APPLICATION_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(__file__).with_name("config.yaml")
DEFAULT_SAVE_ROOT = "~"
DEFAULT_MAX_TEXT_BYTES = 1_048_576
MAX_MAX_TEXT_BYTES = 100 * 1_048_576
REQUEST_BODY_OVERHEAD_BYTES = 65_536
MAX_PATH_RESULTS = 30
MAX_PATH_SCAN_ENTRIES = 300
STALE_TEMP_FILE_AGE_SECONDS = 24 * 60 * 60
MAX_CLEANUP_SCAN_ENTRIES = 10_000
BIND_HOST = "127.0.0.1"
LOGGER = logging.getLogger("file_bridge")
REQUEST_ID = contextvars.ContextVar("request_id", default="-")
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
INVALID_PATH_ERRNOS = {errno.EINVAL, errno.ENAMETOOLONG}
DESTINATION_CONFLICT_ERRNOS = {
    errno.EEXIST,
    errno.EISDIR,
    errno.ELOOP,
    errno.ENOTDIR,
    errno.ENOTEMPTY,
}
TEMP_FILE_PATTERN = re.compile(r"\.file-bridge-[a-z0-9_]{8}\.tmp\Z")


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
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES

    def __post_init__(self) -> None:
        try:
            save_root = self.save_root.expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(f"Invalid save_root: {exc}") from exc
        object.__setattr__(self, "save_root", save_root)


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses ambiguous duplicate mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable mapping key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _load_yaml_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as config_file:
            loaded = yaml.load(config_file, Loader=UniqueKeyLoader)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Configuration file not found: {path}") from exc
    except (yaml.YAMLError, UnicodeError, ValueError) as exc:
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
    if not save_root or save_root.isspace():
        raise RuntimeError("config save_root must not be empty or whitespace-only")

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

    max_text_bytes = raw_config.get("max_text_bytes", DEFAULT_MAX_TEXT_BYTES)
    if type(max_text_bytes) is not int or max_text_bytes < 0:
        raise RuntimeError("config max_text_bytes must be a non-negative integer")
    if max_text_bytes > MAX_MAX_TEXT_BYTES:
        raise RuntimeError(f"config max_text_bytes must not exceed {MAX_MAX_TEXT_BYTES}")

    return AppConfig(
        save_root=save_root_path,
        port=port,
        max_text_bytes=max_text_bytes,
    )


RUNTIME_CONFIG: AppConfig | None = None
READY = False


def _get_runtime_config() -> AppConfig:
    if RUNTIME_CONFIG is None:
        raise HTTPException(status_code=503, detail="Service is not ready")
    return RUNTIME_CONFIG


def _invalid_path(_exc: BaseException | None = None) -> HTTPException:
    return HTTPException(status_code=400, detail="Invalid path")


def _validate_path_length(path: Path, save_root: Path) -> None:
    """Reject paths the target filesystem cannot represent before I/O."""

    try:
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
        return

    if path_max != -1 and len(encoded_path) >= path_max:
        raise HTTPException(status_code=400, detail="Invalid path: path is too long")
    if name_max != -1 and any(len(part) > name_max for part in encoded_parts):
        raise HTTPException(status_code=400, detail="Invalid path: a path component is too long")


def _expand_user_path(raw_path: str, save_root: Path) -> Path:
    try:
        raw_path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid path: must be valid UTF-8") from exc
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


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_save_path(raw_path: str, config: AppConfig) -> Path:
    if not raw_path or raw_path.isspace():
        raise HTTPException(status_code=400, detail="Missing save path")
    if raw_path.endswith("/"):
        raise HTTPException(status_code=400, detail="Save path must name a file, not a directory")
    filename = Path(raw_path.rstrip("/")).name
    if TEMP_FILE_PATTERN.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Save path uses a reserved filename pattern")

    save_root = config.save_root
    clean = _expand_user_path(raw_path, save_root)
    if not _is_within(clean, save_root):
        raise HTTPException(
            status_code=400,
            detail="Save path must stay under configured save_root",
        )

    try:
        resolved = clean.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid_path(exc) from exc
    if _is_within(resolved, APPLICATION_ROOT):
        raise HTTPException(
            status_code=400,
            detail="Cannot save inside the file-bridge application directory",
        )

    return resolved


def _durability_warning() -> str:
    return "Durability sync failed; saved data may not survive a crash"


def _fsync_directory(path: Path) -> str | None:
    """Best-effort directory sync after a filesystem entry changes."""

    directory_fd: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path, flags)
        os.fsync(directory_fd)
    except OSError:
        LOGGER.warning(
            "directory durability sync failed",
            exc_info=True,
            extra={"event": "directory_fsync_failed", "filesystem_path": str(path)},
        )
        return _durability_warning()
    finally:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                LOGGER.warning(
                    "failed to close directory after fsync",
                    exc_info=True,
                    extra={"event": "directory_close_failed"},
                )
    return None


def _ensure_parent_directories(path: Path) -> list[str]:
    """Create missing parents and sync each directory entry creation."""

    missing: list[Path] = []
    current = path
    while True:
        try:
            current.stat()
        except FileNotFoundError:
            missing.append(current)
            if current == current.parent:
                break
            current = current.parent
        else:
            if not current.is_dir():
                raise NotADirectoryError(errno.ENOTDIR, "A parent component is not a directory")
            break

    warnings: list[str] = []
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if not directory.is_dir():
                raise
            continue
        warning = _fsync_directory(directory.parent)
        if warning:
            warnings.append(warning)
    return warnings


def _atomic_write_text(path: Path, text: str) -> str | None:
    warnings = _ensure_parent_directories(path.parent)
    temp_path: Path | None = None
    try:
        try:
            destination_stat = path.stat()
        except FileNotFoundError:
            destination_mode = None
        else:
            if not stat.S_ISREG(destination_stat.st_mode):
                raise FileExistsError(
                    errno.EEXIST,
                    "Destination is not a regular file",
                    path,
                )
            destination_mode = stat.S_IMODE(destination_stat.st_mode)

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".file-bridge-",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            if destination_mode is not None:
                os.fchmod(temp_file.fileno(), destination_mode)
                os.fsync(temp_file.fileno())

        os.replace(temp_path, path)
        temp_path = None
        warning = _fsync_directory(path.parent)
        if warning:
            warnings.append(warning)
        return warnings[0] if warnings else None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning(
                    "failed to remove temporary save file",
                    exc_info=True,
                    extra={"event": "temp_cleanup_failed"},
                )


def _is_internal_temp_name(name: str) -> bool:
    return name.startswith(".file-bridge-") and name.endswith(".tmp")


def _cleanup_stale_temp_files(save_root: Path) -> None:
    """Remove old bridge temp files owned by the current user without following links."""

    cutoff = time.time() - STALE_TEMP_FILE_AGE_SECONDS
    owner_id = os.getuid()

    def log_walk_error(exc: OSError) -> None:
        LOGGER.warning(
            "cannot inspect directory for stale temporary files",
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"event": "temp_cleanup_scan_failed"},
        )

    scanned = 0
    for directory, directory_names, filenames in os.walk(
        save_root, followlinks=False, onerror=log_walk_error
    ):
        if scanned >= MAX_CLEANUP_SCAN_ENTRIES:
            LOGGER.warning(
                "stale temp cleanup reached scan limit",
                extra={"event": "temp_cleanup_scan_limit"},
            )
            break
        scanned += 1
        directory_path = Path(directory)
        if _is_within(directory_path, APPLICATION_ROOT):
            directory_names.clear()
            continue

        for filename in filenames:
            if TEMP_FILE_PATTERN.fullmatch(filename) is None:
                continue
            temp_path = directory_path / filename
            try:
                temp_stat = temp_path.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(temp_stat.st_mode)
                    or temp_stat.st_uid != owner_id
                    or temp_stat.st_mtime > cutoff
                ):
                    continue
                temp_path.unlink()
            except OSError:
                LOGGER.warning(
                    "failed to remove stale temporary save file",
                    exc_info=True,
                    extra={"event": "temp_cleanup_failed"},
                )


def _probe_save_root(save_root: Path) -> None:
    if _is_within(save_root, APPLICATION_ROOT):
        raise RuntimeError("save_root must not be inside the application directory")

    try:
        root_stat = save_root.stat()
    except FileNotFoundError as exc:
        raise RuntimeError("save_root does not exist") from exc
    except OSError as exc:
        raise RuntimeError("save_root cannot be accessed") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError("save_root is not a directory")

    try:
        with os.scandir(save_root) as entries:
            next(entries, None)
        with tempfile.TemporaryFile(dir=save_root):
            pass
    except OSError as exc:
        raise RuntimeError("save_root must be readable and writable") from exc


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global READY, RUNTIME_CONFIG

    config = RUNTIME_CONFIG if RUNTIME_CONFIG is not None else load_config()
    try:
        _probe_save_root(config.save_root)
    except RuntimeError as exc:
        LOGGER.critical(
            "startup validation failed: %s",
            exc,
            exc_info=True,
            extra={
                "event": "startup_failed",
                "filesystem_path": str(config.save_root),
            },
        )
        raise RuntimeError(f"Startup validation failed: {exc}") from exc

    RUNTIME_CONFIG = config
    READY = True
    _cleanup_stale_temp_files(config.save_root)
    LOGGER.info(
        "file-bridge is ready",
        extra={
            "event": "startup_complete",
            "filesystem_path": str(config.save_root),
            "port": config.port,
        },
    )
    try:
        yield
    finally:
        READY = False
        LOGGER.info("file-bridge stopped", extra={"event": "shutdown_complete"})


def _request_body_limit(config: AppConfig) -> int | None:
    if config.max_text_bytes == 0:
        return None
    return config.max_text_bytes * 6 + REQUEST_BODY_OVERHEAD_BYTES


def _header_value(scope: Scope, name: bytes) -> str | None:
    headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
    for header_name, value in headers:
        if header_name.lower() == name:
            return value.decode("latin-1")
    return None


class RequestMiddleware:
    """Attach request IDs, log requests, and bound bodies before parsing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _header_value(scope, b"x-request-id")
        if request_id is None or REQUEST_ID_PATTERN.fullmatch(request_id) is None:
            request_id = uuid.uuid4().hex
        token = REQUEST_ID.set(request_id)
        started = time.monotonic()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            config = _get_runtime_config()
            max_body_bytes = _request_body_limit(config)
            content_length = _header_value(scope, b"content-length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    declared_length = 0
                if max_body_bytes is not None and declared_length > max_body_bytes:
                    status_code = 413
                    response = JSONResponse(
                        status_code=413,
                        content={"detail": "Request body is too large"},
                    )
                    await response(scope, receive, send_with_request_id)
                    return

            messages: list[Message] = []
            received_bytes = 0
            while True:
                message = await receive()
                messages.append(message)
                if message["type"] != "http.request":
                    break
                received_bytes += len(message.get("body", b""))
                if max_body_bytes is not None and received_bytes > max_body_bytes:
                    status_code = 413
                    response = JSONResponse(
                        status_code=413,
                        content={"detail": "Request body is too large"},
                    )
                    await response(scope, receive, send_with_request_id)
                    return
                if not message.get("more_body", False):
                    break

            async def replay_receive() -> Message:
                if messages:
                    return messages.pop(0)
                return {"type": "http.request", "body": b"", "more_body": False}

            await self.app(scope, replay_receive, send_with_request_id)
        except Exception:
            LOGGER.exception(
                "unhandled error in request handler",
                extra={"event": "request_error"},
            )
            status_code = 500
            response = JSONResponse(
                status_code=500,
                content={"detail": "Internal Server Error"},
            )
            await response(scope, receive, send_with_request_id)
        finally:
            duration_ms = round((time.monotonic() - started) * 1000, 3)
            LOGGER.info(
                "request complete",
                extra={
                    "event": "request_complete",
                    "method": scope.get("method"),
                    "request_path": scope.get("path"),
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            REQUEST_ID.reset(token)


app = FastAPI(title="File Bridge", version="0.0.2", lifespan=lifespan)
app.add_middleware(RequestMiddleware)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(ok=True)


@app.get("/ready", response_model=HealthResponse)
def readiness() -> HealthResponse:
    if not READY:
        raise HTTPException(status_code=503, detail="Service is not ready")
    config = _get_runtime_config()
    try:
        _probe_save_root(config.save_root)
    except RuntimeError as exc:
        LOGGER.warning(
            "readiness probe failed: %s",
            exc,
            exc_info=True,
            extra={"event": "readiness_failed"},
        )
        raise HTTPException(status_code=503, detail="Service is not ready") from exc
    return HealthResponse(ok=True)


@app.post("/save", response_model=SaveResponse, response_model_exclude_none=True)
def save_text(request: SaveRequest) -> SaveResponse:
    config = _get_runtime_config()

    try:
        text_bytes = len(request.text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=400, detail="Text cannot be encoded as UTF-8") from exc
    if config.max_text_bytes > 0:
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
    except OSError as exc:
        if exc.errno in DESTINATION_CONFLICT_ERRNOS:
            raise HTTPException(
                status_code=400,
                detail="Destination conflicts with an existing filesystem entry",
            ) from exc
        LOGGER.exception(
            "failed to save text",
            extra={"event": "save_failed", "filesystem_path": str(path)},
        )
        raise HTTPException(status_code=500, detail="Failed to save text") from exc
    return SaveResponse(ok=True, path=str(path), warning=warning)


@app.get("/paths", response_model=PathsResponse)
def list_paths(prefix: str = "") -> PathsResponse:
    """Return sorted suggestions from at most MAX_PATH_SCAN_ENTRIES entries."""

    config = _get_runtime_config()
    save_root = config.save_root
    prefix_ends_with_separator = prefix.endswith("/")
    exact_directory = False

    if prefix:
        search_dir = _expand_user_path(prefix, save_root)
        if not _is_within(search_dir, save_root):
            return PathsResponse(paths=[])
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
        resolved_search_dir = search_dir.resolve()
    except (OSError, RuntimeError, ValueError):
        return PathsResponse(paths=[])
    if not _is_within(resolved_search_dir, save_root):
        return PathsResponse(paths=[])
    if _is_within(resolved_search_dir, APPLICATION_ROOT):
        return PathsResponse(paths=[])
    if not resolved_search_dir.is_dir():
        return PathsResponse(paths=[])

    prefix_lower = ""
    if prefix and not prefix_ends_with_separator and not exact_directory:
        raw_name = Path(prefix).name.lower()
        if raw_name and raw_name != "~":
            prefix_lower = raw_name

    paths: list[tuple[str, bool]] = []
    try:
        with os.scandir(resolved_search_dir) as entries:
            for entry in islice(entries, MAX_PATH_SCAN_ENTRIES):
                if _is_internal_temp_name(entry.name):
                    continue
                try:
                    entry.name.encode("utf-8", errors="strict")
                except UnicodeEncodeError:
                    LOGGER.warning(
                        "skipping path suggestion that is not valid UTF-8",
                        extra={"event": "path_encoding_invalid"},
                    )
                    continue
                if prefix_lower and not entry.name.lower().startswith(prefix_lower):
                    continue
                try:
                    resolved_entry = Path(entry.path).resolve()
                    if not _is_within(resolved_entry, save_root):
                        continue
                    if _is_within(resolved_entry, APPLICATION_ROOT):
                        continue
                    is_directory = resolved_entry.is_dir()
                except (OSError, RuntimeError):
                    LOGGER.warning(
                        "cannot inspect path suggestion",
                        exc_info=True,
                        extra={"event": "path_inspection_failed"},
                    )
                    continue
                bisect.insort(paths, (entry.name, is_directory))
                if len(paths) > MAX_PATH_RESULTS:
                    paths.pop()
    except OSError as exc:
        if exc.errno in INVALID_PATH_ERRNOS:
            raise _invalid_path(exc) from exc
        LOGGER.warning(
            "cannot list path suggestions",
            exc_info=True,
            extra={"event": "path_listing_failed"},
        )
        return PathsResponse(paths=[])

    relative_directory = search_dir.relative_to(save_root)
    results = []
    for name, is_directory in paths:
        relative_path = str(relative_directory / name)
        if is_directory:
            relative_path += "/"
        results.append(relative_path)
    return PathsResponse(paths=results)


class JsonLogFormatter(logging.Formatter):
    """Render application logs as one JSON object per line."""

    EXTRA_FIELDS = (
        "duration_ms",
        "event",
        "filesystem_path",
        "method",
        "port",
        "request_path",
        "status_code",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": REQUEST_ID.get(),
        }
        for field in self.EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(*, force: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root_logger = logging.getLogger()
    if force or not root_logger.handlers:
        root_logger.handlers.clear()
        root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


def _validated_config(path: Path = CONFIG_PATH) -> AppConfig:
    config = load_config(path)
    _probe_save_root(config.save_root)
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local file-bridge service")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate config.yaml and save_root, then exit",
    )
    args = parser.parse_args()
    configure_logging(force=True)

    global RUNTIME_CONFIG
    try:
        RUNTIME_CONFIG = _validated_config()
    except RuntimeError as exc:
        LOGGER.critical(
            "startup configuration error: %s",
            exc,
            extra={"event": "startup_failed"},
        )
        return 2

    if args.check_config:
        LOGGER.info("configuration is valid", extra={"event": "config_valid"})
        return 0

    import uvicorn

    uvicorn.run(
        app,
        host=BIND_HOST,
        port=RUNTIME_CONFIG.port,
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

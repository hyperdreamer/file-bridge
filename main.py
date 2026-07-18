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
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


APPLICATION_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(__file__).with_name("config.yaml")
DEFAULT_SAVE_ROOT = "~"
DEFAULT_PORT = 8964
DEFAULT_MAX_TEXT_BYTES = 1_048_576
MAX_MAX_TEXT_BYTES = 100 * 1_048_576
REQUEST_BODY_OVERHEAD_BYTES = 65_536
MAX_PATH_RESULTS = 30
MAX_PATH_SCAN_ENTRIES = 300
STALE_TEMP_FILE_AGE_SECONDS = 24 * 60 * 60
MAX_CLEANUP_SCAN_ENTRIES = 10_000
MAX_TRANSPORT_BODY_BYTES = 128 * 1_048_576
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
    port: int = DEFAULT_PORT
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
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"Configuration file not found: {path}") from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot read config file {path}: {exc}") from exc

    # A document that contains only whitespace and YAML comments (lines
    # whose first non-space character is '#') is considered blank and
    # maps to an empty config.  Lines that follow comments and evaluate
    # to null / ~ are NOT blank — they are explicit null documents.
    _non_comment = re.sub(r"^\s*#.*$", "", raw_text, flags=re.MULTILINE)
    is_blank = not _non_comment.strip()

    try:
        loaded = yaml.load(raw_text, Loader=UniqueKeyLoader)
    except (yaml.YAMLError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid YAML in {path}: {exc}") from exc

    if loaded is None:
        if is_blank:
            return {}
        raise RuntimeError(f"{path} must contain a YAML mapping at the top level, not null")
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

    port = raw_config.get("port", DEFAULT_PORT)
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
_SAVE_ROOT_DEVICE: int | None = None
_SAVE_ROOT_INODE: int | None = None


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
        candidate = (
            save_root / raw_path[2:] if raw_path.startswith("~/") else Path(raw_path).expanduser()
        )
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

    if TEMP_FILE_PATTERN.fullmatch(resolved.name):
        raise HTTPException(status_code=400, detail="Save path uses a reserved filename pattern")

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
    return TEMP_FILE_PATTERN.fullmatch(name) is not None


def _cleanup_stale_temp_files(save_root: Path) -> None:
    """Remove old bridge temp files owned by the current user without following symlinks."""

    cutoff = time.time() - STALE_TEMP_FILE_AGE_SECONDS
    owner_id = os.getuid()
    scanned = 0

    def _record_scan() -> bool:
        nonlocal scanned
        if scanned >= MAX_CLEANUP_SCAN_ENTRIES:
            LOGGER.warning(
                "stale temp cleanup reached scan limit",
                extra={"event": "temp_cleanup_scan_limit"},
            )
            return False
        scanned += 1
        return True

    stack = [save_root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as dir_entries:
                for entry in dir_entries:
                    if not _record_scan():
                        return
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        LOGGER.warning(
                            "cannot inspect directory entry for stale temporary files",
                            exc_info=True,
                            extra={"event": "temp_cleanup_scan_failed"},
                        )
                        continue
                    if is_dir:
                        dir_path = Path(entry.path)
                        if _is_within(dir_path, APPLICATION_ROOT):
                            continue
                        stack.append(dir_path)
                    elif TEMP_FILE_PATTERN.fullmatch(entry.name):
                        try:
                            entry_stat = entry.stat(follow_symlinks=False)
                            if (
                                not stat.S_ISREG(entry_stat.st_mode)
                                or entry_stat.st_uid != owner_id
                                or entry_stat.st_mtime > cutoff
                            ):
                                continue
                            Path(entry.path).unlink()
                        except OSError:
                            LOGGER.warning(
                                "failed to remove stale temporary save file",
                                exc_info=True,
                                extra={"event": "temp_cleanup_failed"},
                            )
        except OSError:
            LOGGER.warning(
                "cannot inspect directory for stale temporary files",
                exc_info=True,
                extra={"event": "temp_cleanup_scan_failed"},
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

    global _SAVE_ROOT_DEVICE, _SAVE_ROOT_INODE
    if _SAVE_ROOT_DEVICE is not None:
        if (root_stat.st_dev, root_stat.st_ino) != (_SAVE_ROOT_DEVICE, _SAVE_ROOT_INODE):
            raise RuntimeError(
                "save_root identity changed (device or inode differs from startup); "
                "the directory may have been recreated or replaced"
            )

    try:
        with os.scandir(save_root) as entries:
            next(entries, None)
        with tempfile.TemporaryFile(dir=save_root):
            pass
    except OSError as exc:
        raise RuntimeError("save_root must be readable and writable") from exc

    if _SAVE_ROOT_DEVICE is None:
        _SAVE_ROOT_DEVICE = root_stat.st_dev
        _SAVE_ROOT_INODE = root_stat.st_ino


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global READY, RUNTIME_CONFIG, _SAVE_ROOT_DEVICE, _SAVE_ROOT_INODE

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
        _SAVE_ROOT_DEVICE = None
        _SAVE_ROOT_INODE = None
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
        _SAVE_ROOT_DEVICE = None
        _SAVE_ROOT_INODE = None
        LOGGER.info("file-bridge stopped", extra={"event": "shutdown_complete"})


def _request_body_limit(config: AppConfig) -> int:
    if config.max_text_bytes == 0:
        return MAX_TRANSPORT_BODY_BYTES
    return min(
        config.max_text_bytes * 6 + REQUEST_BODY_OVERHEAD_BYTES,
        MAX_TRANSPORT_BODY_BYTES,
    )


def _header_value(scope: Scope, name: bytes) -> str | None:
    headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
    for header_name, value in headers:
        if header_name.lower() == name:
            return value.decode("latin-1")
    return None


_LOOPBACK_IDENTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Parse a Host header value: mandatory host, optional :port.  IPv6
# addresses must be bracket-wrapped; raw IPv6 would be ambiguous
# because addresses contain multiple colons.
_HOST_RE = re.compile(
    r"^\[(?P<v6>[^\]]+)\]:(?P<v6port>\d+)$"  # [::1]:port
    r"|^\[(?P<v6noport>[^\]]+)\]$"  # [::1]
    r"|^(?P<host>[^:]+):(?P<hostport>\d+)$"  # host:port
    r"|^(?P<hostnoport>[^:]+)$"  # host
)


def _validate_loopback_host(host_value: str | None) -> bool:
    """Return True when the Host header names a loopback address."""
    if host_value is None:
        return False
    m = _HOST_RE.fullmatch(host_value)
    if m is None:
        return False
    host = m.group("v6") or m.group("v6noport") or m.group("host") or m.group("hostnoport")
    port_str = m.group("v6port") or m.group("hostport")
    if port_str is not None:
        port = int(port_str)
        if not 1 <= port <= 65535:
            return False
    return host.lower() in _LOOPBACK_IDENTS


# Match loopback HTTP/HTTPS origins with an optional numeric port.
_ORIGIN_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::(\d+))?")


def _validate_browser_origin(origin_value: str | None) -> bool:
    """Return True when the Origin is absent (non-browser) or a loopback origin."""
    if origin_value is None:
        return True
    # Opaque origins (null) are untrusted — reject them.
    if origin_value == "null":
        return False
    m = _ORIGIN_RE.fullmatch(origin_value)
    if m is None:
        return False
    port_str = m.group(1)
    if port_str is not None:
        port = int(port_str)
        if not 1 <= port <= 65535:
            return False
    return True


class _LimitedReceive:
    """Wraps an ASGI *receive* callable, counting cumulative bytes.

    Messages are passed through without buffering.  When the optional
    byte cap is exceeded the wrapper sets the ``exceeded`` flag, drains
    the remaining body chunks, and returns a final empty
    ``http.request`` message so the downstream app sees a truncated
    body that will fail its own parsing.
    """

    def __init__(self, receive: Receive, max_bytes: int | None) -> None:
        self._receive = receive
        self._max_bytes = max_bytes
        self.received_bytes = 0
        self.exceeded = False

    async def __call__(self) -> Message:
        message = await self._receive()
        if message["type"] != "http.request":
            return message

        body_len = len(message.get("body", b""))
        self.received_bytes += body_len

        if self._max_bytes is not None and self.received_bytes > self._max_bytes:
            self.exceeded = True
            # Drain remaining body chunks (bounded to prevent runaway
            # loops on a misbehaving transport).
            _drained = 0
            while message.get("more_body", False):
                _drained += 1
                if _drained > 65_536:
                    break
                message = await self._receive()
            # Return an empty final frame — downstream will see a
            # truncated body and fail to parse it.
            return {"type": "http.request", "body": b"", "more_body": False}

        return message


class RequestMiddleware:
    """Attach request IDs, validate Host/Origin, log requests, and bound bodies."""

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
        status_code: int | None = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        async def _error_response(code: int, detail: str) -> None:
            nonlocal status_code
            status_code = code
            response = JSONResponse(status_code=code, content={"detail": detail})
            await response(scope, receive, send_with_request_id)

        # ------------------------------------------------------------------
        # Capped receive — counts bytes as downstream consumes the body
        # without buffering the entire request in the middleware.
        response_sent = False
        try:
            # Validate Host header is loopback
            host_value = _header_value(scope, b"host")
            if not _validate_loopback_host(host_value):
                await _error_response(421, "Misdirected Request")
                return

            # Validate Origin for browser-originated requests
            origin_value = _header_value(scope, b"origin")
            if not _validate_browser_origin(origin_value):
                await _error_response(403, "Forbidden")
                return

            # Reject declared request bodies on methods that do not accept one.
            # Drain before sending the response to preserve HTTP/1.1 transport
            # integrity — the downstream consumer must not be left mid-body.
            method = scope.get("method", "GET").upper()
            _METHODS_WITHOUT_BODY = frozenset({"GET", "HEAD", "DELETE", "OPTIONS", "CONNECT", "TRACE"})
            if method in _METHODS_WITHOUT_BODY:
                cl_value = _header_value(scope, b"content-length")
                te_value = _header_value(scope, b"transfer-encoding")
                declared_body = False
                if cl_value is not None:
                    try:
                        if int(cl_value) > 0:
                            declared_body = True
                    except ValueError:
                        pass
                if te_value is not None and "chunked" in te_value.lower():
                    declared_body = True
                if declared_body:
                    drained = 0
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            # Client disconnected while draining the
                            # declared body — no client remains to
                            # receive a response.
                            status_code = None
                            return
                        if message["type"] != "http.request":
                            continue
                        drained += len(message.get("body", b""))
                        if drained > MAX_TRANSPORT_BODY_BYTES:
                            # Drain remaining body frames (bounded) so
                            # the transport stays in a clean state for
                            # any subsequent request on this connection.
                            _extra = 0
                            while message.get("more_body", False):
                                _extra += 1
                                if _extra > 65_536:
                                    break
                                message = await receive()
                                if message["type"] == "http.disconnect":
                                    status_code = None
                                    return
                            break
                        if not message.get("more_body", False):
                            break
                    await _error_response(413, "Request body is too large")
                    return

            config = _get_runtime_config()
            max_body_bytes = _request_body_limit(config)

            # Early reject when Content-Length is declared and too large.
            content_length = _header_value(scope, b"content-length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    declared_length = 0
                if max_body_bytes is not None and declared_length > max_body_bytes:
                    await _error_response(413, "Request body is too large")
                    return

            capped_receive = _LimitedReceive(receive, max_body_bytes)
            response_sent = False

            async def send_checked(message: Message) -> None:
                nonlocal status_code, response_sent
                if message["type"] == "http.response.start":
                    response_sent = True
                    if capped_receive.exceeded:
                        # Downstream got a truncated body; replace its error
                        # response with a clean 413.
                        status_code = 413
                        await send_with_request_id(
                            {
                                "type": "http.response.start",
                                "status": 413,
                                "headers": [(b"content-type", b"application/json")],
                            }
                        )
                        await send_with_request_id(
                            {
                                "type": "http.response.body",
                                "body": b'{"detail":"Request body is too large"}',
                                "more_body": False,
                            }
                        )
                        return
                    status_code = message["status"]
                    await send_with_request_id(message)
                elif not capped_receive.exceeded:
                    await send_with_request_id(message)
                # When exceeded, drop all non-start messages (the 413 body
                # was already sent above).

            await self.app(scope, capped_receive, send_checked)
        except Exception:
            LOGGER.exception(
                "unhandled error in request handler",
                extra={"event": "request_error"},
            )
            if not response_sent:
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


app = FastAPI(title="File Bridge", lifespan=lifespan)
app.add_middleware(RequestMiddleware)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = [
        {key: value for key, value in error.items() if key != "input"} for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


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
        _probe_save_root(config.save_root)
    except RuntimeError as exc:
        LOGGER.warning(
            "save_root identity check failed during save: %s",
            exc,
            exc_info=True,
            extra={"event": "save_root_identity_failed"},
        )
        raise HTTPException(status_code=503, detail="Service is not ready") from exc

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

    if prefix:
        search_dir = _expand_user_path(prefix, save_root)
        if not _is_within(search_dir, save_root):
            return PathsResponse(paths=[])
        if not prefix_ends_with_separator:
            search_dir = search_dir.parent
    else:
        search_dir = save_root

    if not _is_within(search_dir, save_root):
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
                    entry_path = search_dir / entry.name
                    if not _is_within(entry_path, save_root):
                        continue
                    is_directory = Path(entry.path).resolve().is_dir()
                except (OSError, RuntimeError):
                    LOGGER.warning(
                        "cannot inspect path suggestion",
                        exc_info=True,
                        extra={"event": "path_inspection_failed"},
                    )
                    continue
                # Suppress exact file match before capping so the cap never
                # reduces the result count below MAX_PATH_RESULTS because of
                # an entry that will later be removed.
                if not is_directory:
                    relative_entry = str(search_dir.relative_to(save_root) / entry.name)
                    if relative_entry == prefix:
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

    import socket
    import uvicorn

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_socket.bind((BIND_HOST, RUNTIME_CONFIG.port))
        server_socket.listen(2048)
    except OSError as exc:
        LOGGER.critical(
            "cannot bind to %s:%d: %s",
            BIND_HOST,
            RUNTIME_CONFIG.port,
            exc,
            extra={"event": "bind_failed"},
        )
        return 1

    sock_fd = server_socket.fileno()
    try:
        uvicorn.run(
            app,
            fd=sock_fd,
            log_config=None,
        )
    finally:
        server_socket.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

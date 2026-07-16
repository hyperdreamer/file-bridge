"""Local-only FastAPI bridge for saving text and completing file paths."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


APPLICATION_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(__file__).with_name("config.yaml")
DEFAULT_SAVE_ROOT = "~"


class SaveRequest(BaseModel):
    """Request body accepted by the save endpoint."""

    text: str
    path: str


@dataclass(frozen=True)
class AppConfig:
    """Application settings loaded from config.yaml."""

    save_root: Path = Path(DEFAULT_SAVE_ROOT)
    port: int = 8766
    max_text_bytes: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "save_root", self.save_root.expanduser().resolve())


def _load_yaml_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as config_file:
        loaded = yaml.safe_load(config_file) or {}

    if not isinstance(loaded, dict):
        raise RuntimeError("config.yaml must contain a YAML mapping at the top level")
    return loaded


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    raw_config = _load_yaml_config(path)

    unknown_keys = set(raw_config) - {"save_root", "port", "max_text_bytes"}
    if unknown_keys:
        unknown = ", ".join(sorted(map(str, unknown_keys)))
        raise RuntimeError(f"Unknown config key(s): {unknown}")

    save_root = raw_config.get("save_root", DEFAULT_SAVE_ROOT)
    if not isinstance(save_root, str):
        raise RuntimeError("config save_root must be a non-null string")

    try:
        port = int(raw_config.get("port", 8766))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("config port must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("config port must be from 1 to 65535")

    return AppConfig(
        save_root=Path(save_root),
        port=port,
        max_text_bytes=int(raw_config.get("max_text_bytes", 0)),
    )


RUNTIME_CONFIG = load_config()


def _resolve_save_path(raw_path: str, config: AppConfig) -> Path:
    raw_path = raw_path.strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="Missing save path")

    save_root = config.save_root
    candidate = Path(raw_path).expanduser()
    path = candidate if candidate.is_absolute() else save_root / candidate

    # Collapse ``..`` without resolving symlinks, so paths such as
    # ~/Ramdisk remain usable.
    clean = Path(os.path.normpath(str(path)))
    try:
        clean.relative_to(save_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Save path must stay under configured save_root: {save_root}",
        ) from exc

    resolved = path.resolve()
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


def _atomic_write_text(path: Path, text: str) -> None:
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
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


app = FastAPI(title="File Bridge")


@app.get("/health")
def health() -> dict[str, bool]:
    config = RUNTIME_CONFIG

    if not config.save_root.is_dir() or not os.access(
        config.save_root, os.R_OK | os.W_OK | os.X_OK
    ):
        raise HTTPException(
            status_code=503,
            detail=f"Not ready: save_root is not an accessible directory: {config.save_root}",
        )
    return {"ok": True}


@app.post("/save")
def save_text(request: SaveRequest) -> dict[str, str | bool]:
    config = RUNTIME_CONFIG

    if config.max_text_bytes > 0:
        try:
            text_bytes = len(request.text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to save text: {exc}"
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
        _atomic_write_text(path, request.text)
    except Exception as exc:
        message = exc.strerror if isinstance(exc, OSError) else str(exc)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save text: {message or exc}",
        ) from exc
    return {"ok": True, "path": str(path)}


@app.get("/paths")
def list_paths(prefix: str = "") -> dict[str, list[str]]:
    config = RUNTIME_CONFIG

    save_root = config.save_root
    prefix = prefix.strip()
    prefix_ends_with_separator = prefix.endswith(("/", "\\"))

    if prefix:
        candidate = Path(prefix).expanduser()
        search_dir = candidate if candidate.is_absolute() else save_root / candidate
        if not prefix_ends_with_separator and not (
            search_dir.is_dir() and search_dir != save_root / candidate.parent
        ):
            search_dir = search_dir.parent
    else:
        search_dir = save_root

    clean = Path(os.path.normpath(str(search_dir)))
    try:
        clean.relative_to(save_root)
    except ValueError:
        return {"paths": []}
    search_dir = clean

    if not search_dir.is_dir():
        return {"paths": []}

    prefix_lower = ""
    if prefix and not prefix_ends_with_separator:
        raw_name = Path(prefix).name.lower()
        if raw_name and raw_name != "~":
            prefix_lower = raw_name

    paths: list[str] = []
    try:
        for entry in sorted(search_dir.iterdir()):
            if prefix_lower and not entry.name.lower().startswith(prefix_lower):
                continue
            relative_path = str(entry.relative_to(save_root))
            if entry.is_dir():
                relative_path += "/"
            paths.append(relative_path)
            if len(paths) >= 30:
                break
    except OSError:
        pass

    return {"paths": paths}


BIND_HOST = "127.0.0.1"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=BIND_HOST, port=RUNTIME_CONFIG.port)

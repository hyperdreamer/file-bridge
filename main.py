"""Local-only FastAPI bridge for saving text and completing file paths."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


CONFIG_PATH = Path(__file__).with_name("config.yaml")
DEFAULT_SAVE_ROOT = "~"
DEFAULT_MAX_TEXT_CHARS = 200_000


class SaveRequest(BaseModel):
    """Request body accepted by the save endpoint."""

    text: str
    path: str


@dataclass(frozen=True)
class AppConfig:
    """Application settings loaded from config.yaml."""

    save_root: Path = Path(DEFAULT_SAVE_ROOT)
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS


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
    return AppConfig(
        save_root=Path(str(raw_config.get("save_root", DEFAULT_SAVE_ROOT))).expanduser(),
        max_text_chars=int(raw_config.get("max_text_chars", DEFAULT_MAX_TEXT_CHARS)),
    )


def _validate_text_size(text: str, config: AppConfig) -> None:
    if len(text) > config.max_text_chars:
        raise HTTPException(
            status_code=413,
            detail=f"Text exceeds configured limit of {config.max_text_chars} characters",
        )


def _resolve_save_path(raw_path: str, config: AppConfig) -> Path:
    raw_path = raw_path.strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="Missing save path")

    save_root_expanded = config.save_root.expanduser()
    save_root = save_root_expanded.resolve()
    candidate = Path(raw_path).expanduser()
    path = candidate if candidate.is_absolute() else save_root_expanded / candidate

    # Match TextKit's traversal guard: collapse ``..`` without resolving
    # symlinks, so paths such as ~/Ramdisk remain usable.
    clean = Path(os.path.normpath(str(path)))
    try:
        clean.relative_to(save_root_expanded)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Save path must stay under configured save_root: {save_root}",
        ) from exc

    return path.resolve()


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
        os.replace(temp_path, path)
    except OSError:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


app = FastAPI(title="File Bridge")


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/save")
async def save_text(request: SaveRequest) -> dict[str, str | bool]:
    try:
        config = load_config()
    except (RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _validate_text_size(request.text, config)
    path = _resolve_save_path(request.path, config)

    try:
        _atomic_write_text(path, request.text)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save text: {exc.strerror or exc}",
        ) from exc
    return {"ok": True, "path": str(path)}


@app.get("/paths")
async def list_paths(prefix: str = "") -> dict[str, list[str]]:
    try:
        config = load_config()
    except (RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    save_root_expanded = config.save_root.expanduser()
    prefix = prefix.strip()

    if prefix:
        candidate = Path(prefix).expanduser()
        search_dir = candidate if candidate.is_absolute() else save_root_expanded / candidate
        if (
            prefix.endswith("/")
            or prefix.endswith("\\")
            or (
                search_dir.is_dir()
                and search_dir != save_root_expanded / candidate.parent
            )
        ):
            search_dir = search_dir if search_dir.is_dir() else search_dir.parent
        else:
            search_dir = search_dir.parent
    else:
        search_dir = save_root_expanded

    clean = Path(os.path.normpath(str(search_dir)))
    try:
        clean.relative_to(save_root_expanded)
    except ValueError:
        return {"paths": []}

    if not search_dir.is_dir():
        return {"paths": []}

    prefix_lower = ""
    if prefix:
        raw_name = Path(prefix).name.lower()
        if raw_name and raw_name != "~":
            prefix_lower = raw_name

    paths: list[str] = []
    try:
        for entry in sorted(search_dir.iterdir()):
            if prefix_lower and not entry.name.lower().startswith(prefix_lower):
                continue
            relative_path = str(entry.relative_to(save_root_expanded))
            if entry.is_dir():
                relative_path += "/"
            paths.append(relative_path)
            if len(paths) >= 30:
                break
    except OSError:
        pass

    return {"paths": paths}

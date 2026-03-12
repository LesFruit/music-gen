"""Centralized storage policy for downloaded media.

By default, all media is stored under /host/d/media/projects/suno-wrapper/audio.
Set SUNO_MEDIA_ROOT to customize the subdirectory under /host/d.
"""

from __future__ import annotations

import os
from pathlib import Path

HOST_D_ROOT = Path("/host/d")
DEFAULT_MEDIA_ROOT = HOST_D_ROOT / "media" / "projects" / "suno-wrapper" / "audio"
MEDIA_ROOT_ENV = "SUNO_MEDIA_ROOT"
ENFORCE_HOST_D_ENV = "SUNO_ENFORCE_HOST_D"


def _is_truthy(value: str | None, *, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def enforce_host_d() -> bool:
    """Whether non-/host/d paths should be redirected to /host/d."""
    return _is_truthy(os.environ.get(ENFORCE_HOST_D_ENV), default=True)


def media_root() -> Path:
    """Return the configured media root, guaranteed to live under /host/d."""
    configured = (os.environ.get(MEDIA_ROOT_ENV) or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = DEFAULT_MEDIA_ROOT / candidate
        try:
            candidate.relative_to(HOST_D_ROOT)
            return candidate
        except ValueError:
            return DEFAULT_MEDIA_ROOT
    return DEFAULT_MEDIA_ROOT


def resolve_media_dir(requested: str | Path, *, default_subdir: str = "downloads") -> Path:
    """Resolve a media directory with optional /host/d enforcement.

    Behavior:
    - relative paths are redirected under ``media_root()`` when enforcement is on
    - absolute paths are treated as explicit caller intent and preserved
    """
    target = Path(requested).expanduser()
    if not enforce_host_d():
        return target

    if target.is_absolute():
        return target

    return media_root() / target

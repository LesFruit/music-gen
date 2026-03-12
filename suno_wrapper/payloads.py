"""Shared payload builders for Suno API v2-web generation.

Scripts that make raw HTTP/fetch calls (e.g. via BrowserOS proxy) MUST use
these builders instead of hand-rolling payload dicts.  This ensures critical
fields like ``task`` and ``is_remix`` are never accidentally omitted.
"""

from __future__ import annotations

import uuid
from typing import Any


def _default_web_metadata() -> dict[str, Any]:
    return {
        "web_client_pathname": "/create",
        "is_max_mode": False,
        "is_mumble": False,
        "create_mode": "custom",
    }


def cover_payload(
    *,
    cover_clip_id: str,
    project_id: str,
    tags: str,
    title: str,
    token: str | None = None,
    make_instrumental: bool = True,
    model: str = "chirp-crow",
    negative_tags: str = "",
    transaction_uuid: str | None = None,
    metadata_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a correct cover generation payload for ``/api/generate/v2-web/``.

    Guarantees ``task="cover"`` and ``metadata.is_remix=True`` are always set.
    """
    tx = transaction_uuid or str(uuid.uuid4())
    meta = _default_web_metadata()
    meta["is_remix"] = True
    if metadata_extra:
        meta.update(metadata_extra)

    return {
        "project_id": project_id,
        "transaction_uuid": tx,
        "task": "cover",
        "generation_type": "TEXT",
        "make_instrumental": make_instrumental,
        "mv": model,
        "negative_tags": negative_tags,
        "override_fields": [],
        "metadata": meta,
        "token": token if token and token.strip() else None,
        "artist_clip_id": None,
        "artist_start_s": None,
        "artist_end_s": None,
        "continue_at": None,
        "continue_clip_id": None,
        "continued_aligned_prompt": None,
        "cover_clip_id": cover_clip_id,
        "cover_start_s": None,
        "cover_end_s": None,
        "persona_id": None,
        "user_uploaded_images_b64": None,
        "prompt": "",
        "tags": tags,
        "title": title,
    }


def generation_payload(
    *,
    project_id: str,
    prompt: str = "",
    tags: str = "",
    title: str = "",
    token: str | None = None,
    make_instrumental: bool = False,
    model: str = "chirp-crow",
    negative_tags: str = "",
    is_custom: bool = False,
    transaction_uuid: str | None = None,
    metadata_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a regular (non-cover) generation payload."""
    tx = transaction_uuid or str(uuid.uuid4())
    meta = _default_web_metadata()
    if metadata_extra:
        meta.update(metadata_extra)

    payload: dict[str, Any] = {
        "project_id": project_id,
        "transaction_uuid": tx,
        "generation_type": "TEXT",
        "make_instrumental": make_instrumental,
        "mv": model,
        "negative_tags": negative_tags,
        "override_fields": [],
        "metadata": meta,
        "token": token if token and token.strip() else None,
        "artist_clip_id": None,
        "artist_start_s": None,
        "artist_end_s": None,
        "continue_at": None,
        "continue_clip_id": None,
        "continued_aligned_prompt": None,
        "cover_clip_id": None,
        "persona_id": None,
        "user_uploaded_images_b64": None,
        "prompt": "",
        "tags": tags,
        "title": title,
    }

    if is_custom:
        payload["prompt"] = prompt
    else:
        payload["gpt_description_prompt"] = prompt

    return payload

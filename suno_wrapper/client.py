"""Main async client for Suno API."""

import asyncio
import base64
import json as _json
import pathlib
import time
import mimetypes
import shutil
import uuid
from typing import Callable

import aiofiles
import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .exceptions import (
    SunoAuthError,
    SunoDownloadError,
    SunoError,
    SunoGenerationError,
    SunoRateLimitError,
    SunoTimeoutError,
)
from .models import (
    Clip,
    CreditsInfo,
    DownloadProgress,
    GenerationParams,
    LyricsResult,
    ModelVersions,
    SunoSettings,
)
from .audio import AudioConverter
from .fingerprint import get_browser_headers, get_device_id
from .storage import resolve_media_dir


class SunoClient:
    """Async client for Suno AI music generation API.
    
    This client handles authentication, music generation, and audio downloads
    with automatic session management and retry logic.
    
    Example:
        ```python
        client = SunoClient(cookie="your_cookie")
        clips = await client.generate("A happy pop song", wait_for_completion=True)
        ```
    """
    
    DEFAULT_MODEL = ModelVersions.CHIRP_CROW
    # Match the Clerk JS version observed in recent Suno web traffic.
    CLERK_VERSION = "5.117.0"
    
    def __init__(
        self,
        cookie: str | None = None,
        auth_token: str | None = None,
        device_id: str | None = None,
        browser_token: str | None = None,
        api_session_id: str | None = None,
        model_version: str | None = None,
        base_url: str = "https://studio-api.prod.suno.com",
        clerk_url: str = "https://auth.suno.com",
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        """Initialize the Suno client.
        
        Args:
            cookie: Suno session cookie (can also use from_env())
            auth_token: Direct Bearer auth token (last_active_token.jwt)
            model_version: Model version for generation
            base_url: Suno API base URL
            clerk_url: Clerk authentication URL
            timeout: Request timeout in seconds
            max_retries: Maximum retry attempts for failed requests
        """
        self.cookie = cookie or ""
        self._direct_auth_token = auth_token or ""
        self.model_version = model_version or self.DEFAULT_MODEL
        self.base_url = base_url.rstrip("/")
        self.clerk_url = clerk_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        
        self._session_id: str | None = None
        self._auth_token: str | None = None
        self._device_id: str = get_device_id(explicit=device_id)
        self._browser_token: str = (browser_token or "").strip()
        self._api_session_id: str | None = (api_session_id or "").strip() or None
        self._client: httpx.AsyncClient | None = None
        self._audio_converter = AudioConverter()
        
    @classmethod
    def from_env(cls) -> "SunoClient":
        """Create a client from environment variables.
        
        Environment variables:
            SUNO_COOKIE: Authentication cookie (required)
            SUNO_MODEL_VERSION: Model version (default: chirp-crow / v5)
            SUNO_TIMEOUT: Request timeout (default: 30.0)
            SUNO_MAX_RETRIES: Max retries (default: 3)
        """
        settings = SunoSettings()
        return cls(
            cookie=settings.cookie,
            auth_token=settings.auth_token,
            device_id=settings.device_id,
            browser_token=settings.browser_token,
            api_session_id=settings.api_session_id,
            model_version=settings.model_version,
            base_url=settings.base_url,
            clerk_url=settings.clerk_url,
            timeout=settings.timeout,
            max_retries=settings.max_retries,
        )
    
    @classmethod
    def from_settings(cls, settings: SunoSettings) -> "SunoClient":
        """Create a client from a settings object."""
        return cls(
            cookie=settings.cookie,
            auth_token=settings.auth_token,
            device_id=settings.device_id,
            browser_token=settings.browser_token,
            api_session_id=settings.api_session_id,
            model_version=settings.model_version,
            base_url=settings.base_url,
            clerk_url=settings.clerk_url,
            timeout=settings.timeout,
            max_retries=settings.max_retries,
        )
    
    async def __aenter__(self) -> "SunoClient":
        """Async context manager entry."""
        await self._init_client()
        return self
    
    async def __aexit__(self, *args) -> None:
        """Async context manager exit."""
        await self.close()
    
    async def _init_client(self) -> None:
        """Initialize the HTTP client and authenticate."""
        if self._client is not None:
            return
            
        if not self.cookie and not self._direct_auth_token:
            raise SunoAuthError(
                "Cookie or auth token is required. Set SUNO_COOKIE/SUNO_AUTH_TOKEN or pass cookie/auth_token."
            )
        
        headers = get_browser_headers(device_id=self._device_id)
        if self.cookie:
            headers["Cookie"] = self.cookie
        
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(self.timeout, connect=10.0),
            follow_redirects=True,
        )
        
        if self._direct_auth_token:
            self._auth_token = self._direct_auth_token
            self._client.headers["Authorization"] = f"Bearer {self._auth_token}"
            # For direct token auth, still obtain the API session-id header (required by some endpoints).
            await self._ensure_api_session_id()
            return

        # Authenticate and get session
        await self._authenticate()
    
    async def _authenticate(self) -> None:
        """Authenticate with Suno and get session token."""
        if self._client is None:
            raise SunoError("HTTP client not initialized")
        
        # Get session ID from Clerk
        clerk_url = f"{self.clerk_url}/v1/client?_is_native=true&_clerk_js_version={self.CLERK_VERSION}"
        
        try:
            response = await self._client.get(clerk_url)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                raise SunoAuthError(f"Authentication failed. Check your cookie. Status: {e.response.status_code}")
            raise SunoError(f"Failed to authenticate: {e}", e.response.status_code)
        
        data = response.json()
        
        if not data.get("response"):
            raise SunoAuthError("Invalid response from authentication server")
        
        self._session_id = data["response"].get("last_active_session_id")
        if not self._session_id:
            raise SunoAuthError("No session ID found. Cookie may be expired or invalid.")
        
        # Renew token
        await self._renew_token()
    
    async def _renew_token(self) -> None:
        """Renew the authentication token."""
        if self._client is None or not self._session_id:
            raise SunoError("Client not initialized or no session ID")
        
        renew_url = f"{self.clerk_url}/v1/client/sessions/{self._session_id}/tokens?_is_native=true&_clerk_js_version={self.CLERK_VERSION}"
        
        try:
            response = await self._client.post(renew_url)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise SunoAuthError(f"Failed to renew token: {e}", e.response.status_code)
        
        data = response.json()
        self._auth_token = data.get("jwt")
        
        if not self._auth_token:
            raise SunoAuthError("No token received from authentication server")
        
        # Update authorization header
        self._client.headers["Authorization"] = f"Bearer {self._auth_token}"
    
    async def _keep_alive(self) -> None:
        """Keep the session alive by renewing the token if needed."""
        # For cookie/session auth, renew token every request to ensure freshness.
        # For direct token auth, skip renewal.
        if self._session_id:
            await self._renew_token()
        await self._ensure_browser_token()
        await self._ensure_api_session_id()

    async def _ensure_browser_token(self) -> None:
        """Ensure browser-token header exists (used by some Suno endpoints)."""
        if self._client is None:
            return
        if not self._browser_token:
            payload = {"timestamp": int(time.time() * 1000)}
            raw = _json.dumps(payload, separators=(",", ":")).encode("utf-8")
            # Browser requests send a base64url token without padding.
            token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
            self._browser_token = _json.dumps({"token": token}, separators=(",", ":"))
        self._client.headers["browser-token"] = self._browser_token

    async def _ensure_api_session_id(self) -> None:
        """Fetch and attach `session-id` header used by studio-api."""
        if self._client is None or not self._auth_token:
            return
        # Some studio-api endpoints require a browser-token header even when using a direct Bearer token.
        # Ensure it's present before attempting to fetch the session-id.
        await self._ensure_browser_token()
        if self._api_session_id:
            self._client.headers["session-id"] = self._api_session_id
            return
        await self.get_user_session_id()

    async def get_user_session_id(self) -> str:
        """Fetch the studio-api user session id (returned in response header `session-id`)."""
        if self._client is None:
            await self._init_client()
        if self._client is None:
            raise SunoError("HTTP client not initialized")
        await self._ensure_browser_token()

        url = f"{self.base_url}/api/user/get_user_session_id/"
        response = await self._client.get(url)
        self._check_response(response)
        response.raise_for_status()

        # Older behavior: session id returned as a response header.
        sid = response.headers.get("session-id", "").strip()
        if not sid:
            # Newer behavior observed: JSON body includes {"session_id": "..."}.
            try:
                data = response.json()
                if isinstance(data, dict):
                    sid = str(data.get("session_id", "") or "").strip()
            except Exception:
                sid = ""
        if not sid:
            raise SunoError("Missing session-id from get_user_session_id()", response.status_code)

        self._api_session_id = sid
        self._client.headers["session-id"] = sid
        return sid
    
    def _check_response(self, response: httpx.Response) -> None:
        """Check response for errors and raise appropriate exceptions."""
        if response.status_code == 429:
            retry_after = 0
            raw = (response.headers.get("Retry-After") or "").strip()
            if raw:
                try:
                    retry_after = int(raw)
                except ValueError:
                    retry_after = 0
            raise SunoRateLimitError(
                "Rate limit exceeded. Please wait before making more requests.",
                429,
                retry_after=retry_after,
            )
        elif response.status_code in (401, 403):
            # Include the endpoint to make auth debugging actionable (do not include headers).
            try:
                url = str(response.request.url)
                method = response.request.method
                where = f"{method} {url}"
            except Exception:
                where = "unknown"
            raise SunoAuthError(f"Authentication error: {response.status_code} ({where})", response.status_code)
        elif response.status_code >= 500:
            raise SunoError(f"Server error: {response.status_code}", response.status_code)
        
        try:
            data = response.json()
            if isinstance(data, dict) and data.get("detail"):
                raise SunoError(f"API error: {data['detail']}", response.status_code)
        except (ValueError, KeyError):
            pass
    
    @retry(
        retry=retry_if_exception_type(httpx.NetworkError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def _request(
        self,
        method: str,
        url: str,
        _auth_retried: bool = False,
        _rate_limit_retried: bool = False,
        **kwargs,
    ) -> httpx.Response:
        """Make an HTTP request with retry logic.

        Auth errors (401/403) and rate-limit errors (429) are handled
        inline rather than by the tenacity decorator so we can take
        targeted recovery actions (token renewal / backoff) instead of
        blindly retrying with the same expired credentials.
        """
        if self._client is None:
            await self._init_client()

        await self._keep_alive()

        try:
            response = await self._client.request(method, url, **kwargs)
            self._check_response(response)
            response.raise_for_status()
            return response
        except SunoAuthError:
            if _auth_retried or not self._session_id:
                # Already retried once, or using direct-token mode (no refresh
                # capability) — give up immediately.
                raise
            # Cookie mode: attempt one token renewal and retry.
            await self._renew_token()
            return await self._request(
                method, url, _auth_retried=True, _rate_limit_retried=_rate_limit_retried, **kwargs
            )
        except SunoRateLimitError as exc:
            if _rate_limit_retried:
                raise
            wait = min(exc.retry_after or 5, 60)
            await asyncio.sleep(wait)
            return await self._request(
                method, url, _auth_retried=_auth_retried, _rate_limit_retried=True, **kwargs
            )
        except httpx.TimeoutException as e:
            raise SunoTimeoutError(f"Request timed out: {e}")
        except httpx.HTTPStatusError as e:
            self._check_response(e.response)
            raise SunoError(f"HTTP error: {e}", e.response.status_code)
    
    async def generate(
        self,
        prompt: str,
        is_custom: bool = False,
        tags: str = "",
        title: str = "",
        make_instrumental: bool = False,
        model_version: str | None = None,
        wait_for_completion: bool = False,
        timeout: float = 120.0,
        poll_interval: float = 3.0,
        negative_tags: str | None = None,
        token: str = "",
    ) -> list[Clip]:
        """Generate music from a text prompt.

        Args:
            prompt: Text description (non-custom) or lyrics (custom mode)
            is_custom: If True, prompt is treated as lyrics
            tags: Music style/genre tags (custom mode only)
            title: Song title (custom mode only)
            make_instrumental: Generate instrumental track (no vocals)
            model_version: Model to use (defaults to client setting)
            wait_for_completion: Wait for audio generation to complete
            timeout: Maximum time to wait for completion
            poll_interval: Status polling interval in seconds while waiting
            negative_tags: Tags to avoid (custom mode only)
            token: hCaptcha token (P1_...). Required since ~Feb 2026.

        Returns:
            List of Clip objects (usually 2 clips per generation)

        Raises:
            SunoGenerationError: If generation fails
            SunoTimeoutError: If wait_for_completion times out
        """
        model = model_version or self.model_version

        if model not in ModelVersions.AVAILABLE_MODELS:
            raise ValueError(f"Invalid model version. Available: {ModelVersions.AVAILABLE_MODELS}")

        payload: dict = {
            "make_instrumental": make_instrumental,
            "mv": model,
            "prompt": "",
            "generation_type": "TEXT",
            "token": token,
        }

        if is_custom:
            payload["tags"] = tags
            payload["title"] = title
            payload["prompt"] = prompt
            if negative_tags:
                payload["negative_tags"] = negative_tags
        else:
            payload["gpt_description_prompt"] = prompt

        url = f"{self.base_url}/api/generate/v2/"
        
        try:
            response = await self._request("POST", url, json=payload)
            data = response.json()
        except SunoError:
            raise
        except Exception as e:
            raise SunoGenerationError(f"Failed to start generation: {e}")
        
        clips = [Clip.model_validate(clip) for clip in data.get("clips", [])]
        
        if wait_for_completion:
            clip_ids = [clip.id for clip in clips]
            clips = await self._wait_for_completion(clip_ids, timeout, poll_interval)
        
        return clips

    def _default_web_metadata(self, is_custom: bool) -> dict:
        # Minimal metadata observed in browser `v2-web` requests.
        return {
            "web_client_pathname": "/create",
            "is_max_mode": False,
            "is_mumble": False,
            "create_mode": "custom" if is_custom else "auto",
        }

    async def generate_v2_web(
        self,
        prompt: str,
        *,
        generate_token: str | None = None,
        project_id: str,
        transaction_uuid: str | None = None,
        is_custom: bool = False,
        tags: str = "",
        title: str = "",
        make_instrumental: bool = False,
        model_version: str | None = None,
        wait_for_completion: bool = False,
        timeout: float = 120.0,
        poll_interval: float = 3.0,
        negative_tags: str | None = None,
        metadata: dict | None = None,
        generation_type: str = "TEXT",
        task: str | None = None,
        cover_clip_id: str | None = None,
        mashup_clip_ids: list[str] | None = None,
        playlist_id: str | None = None,
        playlist_clip_ids: list[str] | None = None,
    ) -> list[Clip]:
        """Generate using the browser-style endpoint ``/api/generate/v2-web/``.

        Supports regular generation, covers, mashups, and inspiration.

        Args:
            task: ``"cover"``, ``"mashup_condition"``,
                  ``"playlist_condition"`` (inspiration), or *None* for
                  regular generation.  Auto-detected from clip ID args.
            generate_token: P1_ hCaptcha token, or *None* if captcha already
                            cleared for the session.
            cover_clip_id: Source clip ID for cover generation.
            mashup_clip_ids: List of exactly 2 clip IDs for mashup generation.
            playlist_id: Playlist UUID for inspiration, or the literal string
                         ``"inspiration"`` for ad-hoc clip-based inspiration.
            playlist_clip_ids: List of up to 4 clip IDs for inspiration.
            model_version: ``"chirp-crow"`` (v5), ``"chirp-v4"``, etc.
        """
        model = model_version or self.model_version
        if model not in ModelVersions.AVAILABLE_MODELS:
            raise ValueError(f"Invalid model version. Available: {ModelVersions.AVAILABLE_MODELS}")

        if not project_id.strip():
            raise SunoGenerationError("Missing project_id for v2-web (set SUNO_PROJECT_ID)")

        # Auto-detect task from clip IDs
        if task is None and cover_clip_id:
            task = "cover"
        if task is None and mashup_clip_ids:
            task = "mashup_condition"
        if task is None and playlist_clip_ids:
            task = "playlist_condition"

        if task == "mashup_condition":
            if not mashup_clip_ids or len(mashup_clip_ids) != 2:
                raise SunoGenerationError("Mashup requires exactly 2 clip IDs in mashup_clip_ids")

        if task == "playlist_condition":
            if not playlist_clip_ids or len(playlist_clip_ids) < 1:
                raise SunoGenerationError("Inspiration requires at least 1 clip ID in playlist_clip_ids")
            if len(playlist_clip_ids) > 4:
                raise SunoGenerationError("Inspiration supports at most 4 clip IDs")

        tx = (transaction_uuid or "").strip() or str(uuid.uuid4())

        merged_metadata = self._default_web_metadata(is_custom=is_custom)
        if task == "cover":
            merged_metadata["is_remix"] = True
        if metadata:
            merged_metadata.update(metadata)

        # token is null when captcha has been cleared for the session
        token_val = generate_token if generate_token and generate_token.strip() else None

        payload: dict = {
            "project_id": project_id,
            "token": token_val,
            "transaction_uuid": tx,
            "generation_type": generation_type,
            "make_instrumental": make_instrumental,
            "mv": model,
            "negative_tags": negative_tags or "",
            "override_fields": [],
            "metadata": merged_metadata,
            # Common web payload fields (sent as null when unused)
            "artist_clip_id": None,
            "artist_start_s": None,
            "artist_end_s": None,
            "continue_at": None,
            "continue_clip_id": None,
            "continued_aligned_prompt": None,
            "cover_clip_id": cover_clip_id,
            "persona_id": None,
            "user_uploaded_images_b64": None,
            # Text fields
            "prompt": "",
            "tags": tags,
            "title": title,
        }

        if task:
            payload["task"] = task

        if mashup_clip_ids:
            payload["mashup_clip_ids"] = mashup_clip_ids

        if playlist_clip_ids:
            payload["playlist_id"] = playlist_id or "inspiration"
            payload["playlist_clip_ids"] = playlist_clip_ids

        if is_custom or task in ("cover", "mashup_condition", "playlist_condition"):
            payload["prompt"] = prompt
        else:
            payload["gpt_description_prompt"] = prompt

        url = f"{self.base_url}/api/generate/v2-web/"
        try:
            response = await self._request("POST", url, json=payload)
            data = response.json()
        except SunoError:
            raise
        except Exception as e:
            raise SunoGenerationError(f"Failed to start v2-web generation: {e}")

        clips = [Clip.model_validate(clip) for clip in data.get("clips", [])]

        if wait_for_completion:
            clip_ids = [clip.id for clip in clips]
            clips = await self._wait_for_completion(clip_ids, timeout, poll_interval)

        return clips

    async def generate_mashup(
        self,
        clip_id_a: str,
        clip_id_b: str,
        *,
        prompt: str = "",
        tags: str = "",
        title: str = "",
        make_instrumental: bool = False,
        generate_token: str | None = None,
        project_id: str,
        model_version: str | None = None,
        wait_for_completion: bool = False,
        timeout: float = 120.0,
        poll_interval: float = 3.0,
    ) -> list[Clip]:
        """Generate a mashup of two clips.

        Args:
            clip_id_a: First source clip ID.
            clip_id_b: Second source clip ID.
            prompt: Lyrics or description for the mashup.
            tags: Style tags (e.g. "uilleann pipes, four-on-the-floor").
            title: Title for the mashup.  If empty, Suno auto-generates one.
        """
        return await self.generate_v2_web(
            prompt=prompt,
            generate_token=generate_token,
            project_id=project_id,
            task="mashup_condition",
            mashup_clip_ids=[clip_id_a, clip_id_b],
            tags=tags,
            title=title,
            make_instrumental=make_instrumental,
            is_custom=True,
            model_version=model_version or "chirp-crow",
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    async def generate_inspo(
        self,
        clip_ids: list[str],
        *,
        prompt: str = "",
        tags: str = "",
        title: str = "",
        make_instrumental: bool = False,
        generate_token: str | None = None,
        project_id: str,
        playlist_id: str | None = None,
        model_version: str | None = None,
        wait_for_completion: bool = False,
        timeout: float = 120.0,
        poll_interval: float = 3.0,
    ) -> list[Clip]:
        """Generate a song inspired by 1-4 source clips.

        Args:
            clip_ids: 1-4 source clip IDs for inspiration.
            prompt: Lyrics or description.
            tags: Style tags.
            title: Title for the generated song.
            playlist_id: Playlist UUID if clips come from a playlist,
                         or *None* to use ``"inspiration"`` (ad-hoc clips).
        """
        return await self.generate_v2_web(
            prompt=prompt,
            generate_token=generate_token,
            project_id=project_id,
            task="playlist_condition",
            playlist_id=playlist_id,
            playlist_clip_ids=clip_ids,
            tags=tags,
            title=title,
            make_instrumental=make_instrumental,
            is_custom=True,
            model_version=model_version or "chirp-crow",
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    async def list_uploads(self, max_pages: int = 20, page_size: int = 50) -> list[dict]:
        """List all uploaded audio clips from the library.

        Returns a list of dicts with keys: id, title, type, model_name,
        created_at, status, metadata.
        """
        uploads: list[dict] = []
        for page in range(max_pages):
            url = f"{self.base_url}/api/feed/v3"
            response = await self._request("POST", url, json={"page": page, "page_size": page_size})
            data = response.json()
            clips = data.get("clips", [])
            if not clips:
                break
            for clip in clips:
                meta = clip.get("metadata", {})
                if meta.get("type") == "upload":
                    uploads.append({
                        "id": clip["id"],
                        "title": clip.get("title", ""),
                        "type": "upload",
                        "model_name": clip.get("model_name", ""),
                        "created_at": clip.get("created_at", ""),
                        "status": clip.get("status", ""),
                        "audio_url": clip.get("audio_url", ""),
                        "metadata": meta,
                    })
        return uploads

    async def get_clip_detail(self, clip_id: str) -> dict:
        """Get full clip details from ``/api/clip/{id}``.

        Unlike :meth:`get_clip` (which uses the feed endpoint), this returns
        the raw server response including all metadata fields.
        """
        url = f"{self.base_url}/api/clip/{clip_id}"
        response = await self._request("GET", url)
        return response.json()

    def _guess_content_type(self, path: pathlib.Path) -> str:
        ctype, _ = mimetypes.guess_type(str(path))
        if ctype:
            return ctype
        # Conservative fallback; Suno upload accepts wav/mp3 commonly.
        if path.suffix.lower() == ".wav":
            return "audio/wav"
        if path.suffix.lower() == ".mp3":
            return "audio/mpeg"
        return "application/octet-stream"

    async def create_audio_upload(
        self,
        *,
        filename: str,
        content_type: str,
        size_bytes: int,
    ) -> dict:
        """Create an audio upload and receive a presigned POST destination."""
        url = f"{self.base_url}/api/uploads/audio/"
        payload = {
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
        }
        response = await self._request("POST", url, json=payload)
        return response.json()

    async def upload_audio_to_s3_presigned_post(
        self,
        *,
        post_url: str,
        fields: dict,
        file_path: str | pathlib.Path,
        content_type: str,
    ) -> str:
        """Upload a file to S3 using a presigned POST (no auth headers)."""
        p = pathlib.Path(file_path)
        if not p.exists():
            raise SunoError(f"Audio file not found: {p}")

        # Use a clean client: do not leak Authorization/session-id to S3.
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as s3:
            with p.open("rb") as f:
                files = {"file": (p.name, f, content_type)}
                resp = await s3.post(post_url, data=fields, files=files)
                # S3 presigned POST often returns 204 with Location header.
                if resp.status_code not in (200, 201, 204):
                    raise SunoError(
                        f"S3 upload failed: status={resp.status_code} body={resp.text[:300]}",
                        resp.status_code,
                    )
                return resp.headers.get("Location", "").strip()

    async def finish_audio_upload(
        self,
        upload_id: str,
        *,
        upload_type: str,
        upload_filename: str,
    ) -> dict:
        url = f"{self.base_url}/api/uploads/audio/{upload_id}/upload-finish/"
        body = {
            "upload_type": upload_type,
            "upload_filename": upload_filename,
        }
        response = await self._request("POST", url, json=body)
        try:
            return response.json()
        except Exception:
            return {}

    async def get_audio_upload(self, upload_id: str) -> dict:
        url = f"{self.base_url}/api/uploads/audio/{upload_id}/"
        response = await self._request("GET", url)
        return response.json()

    async def initialize_clip(self, upload_id: str) -> str:
        """Convert an audio upload into a Suno clip.

        This step is required before the upload can be used as a cover_clip_id
        in generation requests.  Returns the clip_id.
        """
        url = f"{self.base_url}/api/uploads/audio/{upload_id}/initialize-clip/"
        response = await self._request("POST", url, json={})
        try:
            data = response.json()
        except Exception:
            data = {}
        clip_id = data.get("clip_id") or ""
        if not clip_id:
            # Fallback: the upload's s3_id IS the clip_id after initialization.
            meta = await self.get_audio_upload(upload_id)
            clip_id = meta.get("s3_id") or ""
        if not clip_id:
            raise SunoError(f"initialize-clip returned no clip_id: {data}")
        return clip_id

    async def upload_audio_file(
        self,
        file_path: str | pathlib.Path,
        *,
        initialize: bool = False,
    ) -> dict:
        """End-to-end: create upload -> S3 POST -> upload-finish -> fetch upload info.

        If *initialize* is True, also calls initialize-clip to convert the
        upload into a proper Suno clip (required for cover generation).
        When initialized, the returned dict includes a ``clip_id`` key.

        Returns the final upload metadata (GET /api/uploads/audio/{id}/).
        """
        p = pathlib.Path(file_path)
        ctype = self._guess_content_type(p)
        size = p.stat().st_size

        created = await self.create_audio_upload(filename=p.name, content_type=ctype, size_bytes=size)

        # Expected shape (common presigned-post pattern):
        # {
        #   "id": "...",
        #   "post_url": "https://suno-uploads.s3.amazonaws.com/",
        #   "fields": {"key": "...", "policy": "...", ...}
        # }
        upload_id = (created.get("id") or created.get("upload_id") or "").strip()
        post_url = (created.get("post_url") or created.get("url") or "").strip()
        fields = created.get("fields") or created.get("form_fields") or {}

        if not upload_id or not post_url or not isinstance(fields, dict) or not fields:
            raise SunoError(f"Unexpected create_audio_upload() response shape: keys={list(created.keys())}")

        location = await self.upload_audio_to_s3_presigned_post(
            post_url=post_url,
            fields=fields,
            file_path=p,
            content_type=ctype,
        )

        await self.finish_audio_upload(
            upload_id,
            upload_type="file_upload",
            upload_filename=p.name,
        )
        meta = await self.get_audio_upload(upload_id)
        if location and isinstance(meta, dict) and "location" not in meta:
            meta["location"] = location
        if initialize:
            # Poll until the upload reaches "complete" status before
            # initializing the clip (Suno processes audio server-side).
            for _ in range(30):
                if meta.get("status") == "complete":
                    break
                await asyncio.sleep(2)
                meta = await self.get_audio_upload(upload_id)
            clip_id = await self.initialize_clip(upload_id)
            meta["clip_id"] = clip_id
        return meta
    
    async def _wait_for_completion(
        self,
        clip_ids: list[str],
        timeout: float = 120.0,
        poll_interval: float = 3.0,
    ) -> list[Clip]:
        """Wait for clips to complete generation.
        
        Args:
            clip_ids: IDs of clips to wait for
            timeout: Maximum time to wait
            poll_interval: Seconds between status checks
        
        Returns:
            List of completed Clip objects
        """
        start_time = time.time()
        last_clips: list[Clip] = []
        
        while time.time() - start_time < timeout:
            try:
                clips = await self.get_clips(clip_ids)
                last_clips = clips
                
                all_completed = all(
                    clip.status in ("streaming", "complete") for clip in clips
                )
                all_error = all(clip.status == "error" for clip in clips)
                
                if all_completed:
                    return clips
                if all_error:
                    raise SunoGenerationError("All clips failed to generate")
                
            except Exception:
                pass
            
            await asyncio.sleep(poll_interval)
        
        # Return last known state on timeout
        return last_clips
    
    async def get_clips(self, clip_ids: list[str] | str | None = None) -> list[Clip]:
        """Get clip information by IDs.
        
        Args:
            clip_ids: Single ID, list of IDs, or None for recent clips
        
        Returns:
            List of Clip objects
        """
        url = f"{self.base_url}/api/feed/v2"
        
        if clip_ids:
            if isinstance(clip_ids, str):
                clip_ids = [clip_ids]
            url = f"{url}?ids={','.join(clip_ids)}"
        
        response = await self._request("GET", url)
        data = response.json()
        
        clips_data = data.get("clips", data if isinstance(data, list) else [])
        return [Clip.model_validate(clip) for clip in clips_data]
    
    async def get_clip(self, clip_id: str) -> Clip:
        """Get a single clip by ID.
        
        Args:
            clip_id: The clip ID
        
        Returns:
            Clip object
        """
        clips = await self.get_clips([clip_id])
        if not clips:
            raise SunoError(f"Clip not found: {clip_id}")
        return clips[0]
    
    async def get_credits(self) -> CreditsInfo:
        """Get current credits and billing information.
        
        Returns:
            CreditsInfo with remaining credits and limits
        """
        url = f"{self.base_url}/api/billing/info/"
        response = await self._request("GET", url)
        data = response.json()
        
        return CreditsInfo(
            credits_left=data.get("total_credits_left", 0),
            period=data.get("period"),
            monthly_limit=data.get("monthly_limit", 0),
            monthly_usage=data.get("monthly_usage", 0),
        )

    async def get_session(self) -> dict:
        """Fetch session info from `/api/session/`.

        This endpoint is useful as a lightweight auth check; it also returns a `session-id`
        header we can reuse for other studio-api calls.
        """
        url = f"{self.base_url}/api/session/"
        response = await self._request("GET", url)
        try:
            return response.json()
        except Exception:
            return {"_raw": (response.text or "")[:2000]}
    
    async def extend_clip(
        self,
        clip_id: str,
        prompt: str = "",
        continue_at: float | None = None,
        tags: str = "",
        title: str = "",
        negative_tags: str = "",
        model_version: str | None = None,
        wait_for_completion: bool = False,
        token: str = "",
    ) -> list[Clip]:
        """Extend an existing clip.
        
        Args:
            clip_id: ID of clip to extend
            prompt: Lyrics or description for extension
            continue_at: Start extension at this timestamp (seconds)
            tags: Music style tags
            title: Song title
            negative_tags: Tags to avoid
            model_version: Model to use
            wait_for_completion: Wait for generation to complete
        
        Returns:
            List of extended Clip objects
        """
        model = model_version or self.model_version
        
        payload = {
            "make_instrumental": False,
            "mv": model,
            "prompt": prompt,
            "tags": tags,
            "title": title,
            "negative_tags": negative_tags,
            "task": "extend",
            "continue_clip_id": clip_id,
            "continue_at": continue_at,
            "generation_type": "TEXT",
            "token": token,
        }

        url = f"{self.base_url}/api/generate/v2/"
        response = await self._request("POST", url, json=payload)
        data = response.json()
        
        clips = [Clip.model_validate(clip) for clip in data.get("clips", [])]
        
        if wait_for_completion:
            clip_ids = [clip.id for clip in clips]
            clips = await self._wait_for_completion(clip_ids)
        
        return clips
    
    async def generate_lyrics(self, prompt: str) -> str:
        """Generate lyrics from a prompt.
        
        Args:
            prompt: Description of the desired lyrics
        
        Returns:
            Generated lyrics text
        """
        # Start generation
        url = f"{self.base_url}/api/generate/lyrics/"
        response = await self._request("POST", url, json={"prompt": prompt})
        data = response.json()
        
        lyrics_id = data.get("id")
        if not lyrics_id:
            raise SunoError("Failed to start lyrics generation")
        
        # Poll for completion
        poll_url = f"{self.base_url}/api/generate/lyrics/{lyrics_id}"
        
        for _ in range(30):  # Max 60 seconds
            response = await self._request("GET", poll_url)
            data = response.json()
            
            if data.get("status") == "complete":
                return data.get("text", "")
            
            await asyncio.sleep(2)
        
        raise SunoTimeoutError("Lyrics generation timed out")
    
    async def download_audio(
        self,
        clip: Clip | str,
        output_dir: str = "./downloads",
        filename: str | None = None,
        convert_to_wav: bool = False,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> str:
        """Download audio file with optional WAV conversion.
        
        Args:
            clip: Clip object or clip ID
            output_dir: Directory to save the file
            filename: Custom filename (without extension)
            convert_to_wav: Convert downloaded audio to WAV format
            progress_callback: Callback for download progress
        
        Returns:
            Path to downloaded file
        
        Raises:
            SunoDownloadError: If download fails
        """
        if isinstance(clip, str):
            clip = await self.get_clip(clip)
        
        if not clip.audio_url:
            raise SunoDownloadError(f"No audio URL available for clip {clip.id}")
        
        # Create output directory
        output_path = resolve_media_dir(output_dir, default_subdir="downloads")
        output_path.mkdir(parents=True, exist_ok=True)
        
        # Determine filename
        if filename is None:
            safe_title = clip.title.replace("/", "-").replace("\\", "-")[:50]
            filename = f"{safe_title}_{clip.id}"
        
        # Download with progress tracking
        ext = "wav" if convert_to_wav else "mp3"
        output_file = output_path / f"{filename}.{ext}"
        
        try:
            temp_mp3 = output_path / f"{filename}_temp.mp3"
            if temp_mp3.exists():
                temp_mp3.unlink()

            attempts = max(1, self.max_retries)
            backoff_seconds = 1.0
            for attempt in range(1, attempts + 1):
                try:
                    await self._download_audio_httpx(
                        url=clip.audio_url,
                        temp_mp3=temp_mp3,
                        clip_id=clip.id,
                        progress_callback=progress_callback,
                    )
                    break
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as err:
                    if attempt >= attempts:
                        break
                    await asyncio.sleep(backoff_seconds)
                    backoff_seconds = min(backoff_seconds * 2.0, 8.0)

            if not temp_mp3.exists() or temp_mp3.stat().st_size == 0:
                await self._download_audio_with_cli_fallback(clip.audio_url, temp_mp3)

            # Convert to WAV if requested
            if convert_to_wav:
                await self._audio_converter.convert_to_wav(temp_mp3, output_file)
                temp_mp3.unlink()  # Remove temp MP3
            else:
                temp_mp3.rename(output_file)
            
            return str(output_file)
            
        except Exception as e:
            raise SunoDownloadError(f"Failed to download audio: {e}") from e

    async def _download_audio_httpx(
        self,
        *,
        url: str,
        temp_mp3: pathlib.Path,
        clip_id: str,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> None:
        timeout = httpx.Timeout(connect=20.0, read=300.0, write=120.0, pool=120.0)
        async with httpx.AsyncClient(timeout=timeout) as download_client:
            async with download_client.stream("GET", url, follow_redirects=True) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length", 0))
                downloaded = 0
                start_time = time.time()

                async with aiofiles.open(temp_mp3, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        await f.write(chunk)
                        downloaded += len(chunk)

                        if progress_callback and total > 0:
                            elapsed = time.time() - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            eta = (total - downloaded) / speed if speed > 0 else None

                            progress = DownloadProgress(
                                clip_id=clip_id,
                                total=total,
                                downloaded=downloaded,
                                percentage=(downloaded / total) * 100,
                                speed_bps=speed,
                                eta_seconds=eta,
                            )
                            progress_callback(progress)

    async def _download_audio_with_cli_fallback(self, url: str, temp_mp3: pathlib.Path) -> None:
        candidates: list[list[str]] = []
        if shutil.which("wget"):
            candidates.append(["wget", "--no-check-certificate", "-O", str(temp_mp3), url])
        if shutil.which("curl"):
            candidates.append(["curl", "-L", "--fail", "--connect-timeout", "20", "--max-time", "600", "-o", str(temp_mp3), url])

        if not candidates:
            raise RuntimeError("No CLI downloader available (tried wget/curl)")

        last_error: str | None = None
        for cmd in candidates:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0 and temp_mp3.exists() and temp_mp3.stat().st_size > 0:
                return
            last_error = stderr.decode("utf-8", errors="replace").strip() or f"exit={proc.returncode}"

        raise RuntimeError(last_error or "CLI downloader failed")
    
    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

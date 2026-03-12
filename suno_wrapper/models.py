"""Pydantic models for Suno API data structures."""

from typing import Any, Callable
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings


class ModelVersions:
    """Available Suno AI model versions.

    Models:
    - CHIRP_CROW: v5, latest model (aka chirp-crow).
    - CHIRP_V4: v4 model.
    - CHIRP_V3_5: v3.5, better song structure, max 4 minutes.
    - CHIRP_V3_0: Broad, versatile, max 2 minutes.
    - CHIRP_V2_0: Vintage Suno model, max 1.3 minutes.
    """
    CHIRP_V2_0 = "chirp-v2-0"
    CHIRP_V3_0 = "chirp-v3-0"
    CHIRP_V3_5 = "chirp-v3-5"
    CHIRP_V4 = "chirp-v4"
    CHIRP_CROW = "chirp-crow"  # v5
    AVAILABLE_MODELS = [CHIRP_V2_0, CHIRP_V3_0, CHIRP_V3_5, CHIRP_V4, CHIRP_CROW]


class TaskTypes:
    """Known Suno generation task types for /api/generate/v2-web/.

    Confirmed via Suno web client JS source (Mar 2026):
    - COVER: ``"cover"`` — cover a source clip
    - MASHUP: ``"mashup_condition"`` — blend two clips
    - INSPO: ``"playlist_condition"`` — generate inspired by up to 4 clips
    - SAMPLE: ``"sample_condition"`` — sample a time range from a clip
    - EXTEND: ``"extend"`` — continue from a point in a clip
    """
    COVER = "cover"
    MASHUP = "mashup_condition"
    INSPO = "playlist_condition"
    SAMPLE = "sample_condition"
    EXTEND = "extend"


class ClipMetadata(BaseModel):
    """Metadata for a generated clip."""
    model_config = ConfigDict(protected_namespaces=())

    tags: str | None = None
    prompt: str | None = None
    gpt_description_prompt: str | None = None
    audio_prompt_id: str | None = None
    history: str | list | None = None
    concat_history: str | list | None = None
    type: str | None = None
    duration: float | None = None
    refund_credits: float | None = None
    stream: bool | None = None
    error_type: str | None = None
    error_message: str | None = None
    # Cover/mashup/remix fields
    task: str | None = None
    cover_clip_id: str | None = None
    edited_clip_id: str | None = None
    mashup_clip_ids: list[str] | None = None
    is_remix: bool | None = None
    make_instrumental: bool | None = None


class Clip(BaseModel):
    """Represents a generated audio clip."""
    model_config = ConfigDict(protected_namespaces=())
    
    id: str
    video_url: str | None = None
    audio_url: str | None = None
    image_url: str | None = None
    image_large_url: str | None = None
    is_video_pending: bool = False
    major_model_version: str = ""
    model_name: str = ""
    metadata: ClipMetadata = Field(default_factory=ClipMetadata)
    is_liked: bool = False
    user_id: str = ""
    display_name: str = ""
    handle: str = ""
    is_handle_updated: bool = False
    is_trashed: bool = False
    reaction: dict | None = None
    created_at: str = ""
    status: str = ""  # "streaming", "complete", "error", "pending"
    title: str = ""
    play_count: int = 0
    upvote_count: int = 0
    is_public: bool = False


class CreditsInfo(BaseModel):
    """User credits and billing information."""
    model_config = ConfigDict(protected_namespaces=())
    
    credits_left: int
    period: str | None = None
    monthly_limit: int
    monthly_usage: int


class GenerationParams(BaseModel):
    """Parameters for music generation."""
    model_config = ConfigDict(protected_namespaces=())
    
    prompt: str
    is_custom: bool = False
    tags: str = ""
    title: str = ""
    make_instrumental: bool = False
    model_version: str = ModelVersions.CHIRP_CROW
    wait_for_completion: bool = False
    timeout: float = 120.0
    negative_tags: str | None = None


class DownloadProgress(BaseModel):
    """Progress information for audio downloads."""
    model_config = ConfigDict(protected_namespaces=())
    
    clip_id: str
    total: int
    downloaded: int
    percentage: float
    speed_bps: float | None = None
    eta_seconds: float | None = None


class SunoSettings(BaseSettings):
    """Settings for Suno client configuration."""
    model_config = ConfigDict(
        env_prefix="SUNO_",
        protected_namespaces=(),
    )
    
    cookie: str = ""
    auth_token: str = ""
    device_id: str = ""
    browser_token: str = ""
    api_session_id: str = ""
    # Web (browser) generation flow fields used by /api/generate/v2-web/
    generate_token: str = ""
    project_id: str = ""
    transaction_uuid: str = ""
    web_metadata_json: str = ""
    model_version: str = ModelVersions.CHIRP_CROW
    base_url: str = "https://studio-api.prod.suno.com"
    clerk_url: str = "https://auth.suno.com"
    timeout: float = 30.0
    max_retries: int = 3
    clerk_version: str = "5.117.0"


class LyricsResult(BaseModel):
    """Result from lyrics generation."""
    model_config = ConfigDict(protected_namespaces=())
    
    id: str
    status: str
    text: str | None = None
    title: str | None = None


class GenerationResponse(BaseModel):
    """Raw response from generation API."""
    model_config = ConfigDict(protected_namespaces=())
    
    clips: list[Clip]
    metadata: dict[str, Any] | None = None

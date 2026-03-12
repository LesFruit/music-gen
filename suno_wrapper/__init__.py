"""Suno Wrapper - Async Python client for Suno AI music generation."""

from .client import SunoClient
from .models import (
    Clip,
    ClipMetadata,
    CreditsInfo,
    ModelVersions,
    TaskTypes,
    GenerationParams,
    DownloadProgress,
    SunoSettings,
)
from .exceptions import (
    SunoError,
    SunoAuthError,
    SunoCaptchaError,
    SunoDownloadError,
    SunoRateLimitError,
    SunoGenerationError,
    SunoTimeoutError,
)
from .captcha_solver import CaptchaSolver, SolveResult, SolveMethod
from .payloads import cover_payload, generation_payload
from .token_manager import TokenManager
from .log import get_logger
from .preflight import run_preflight, PreflightResult
from .env_util import save_token, load_token, save_jwt, load_jwt

__version__ = "0.1.0"
__all__ = [
    "SunoClient",
    "Clip",
    "ClipMetadata",
    "CreditsInfo",
    "ModelVersions",
    "TaskTypes",
    "GenerationParams",
    "DownloadProgress",
    "SunoSettings",
    "SunoError",
    "SunoAuthError",
    "SunoCaptchaError",
    "SunoDownloadError",
    "SunoRateLimitError",
    "SunoGenerationError",
    "SunoTimeoutError",
    "CaptchaSolver",
    "SolveResult",
    "SolveMethod",
    "cover_payload",
    "generation_payload",
    "TokenManager",
    "get_logger",
    "run_preflight",
    "PreflightResult",
    "save_token",
    "load_token",
    "save_jwt",
    "load_jwt",
]

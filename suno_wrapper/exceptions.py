"""Exception classes for Suno Wrapper."""


class SunoError(Exception):
    """Base exception for Suno Wrapper."""
    
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class SunoAuthError(SunoError):
    """Raised when authentication fails."""
    pass


class SunoRateLimitError(SunoError):
    """Raised when rate limit is exceeded."""

    def __init__(
        self, message: str, status_code: int | None = None, retry_after: int = 0
    ) -> None:
        super().__init__(message, status_code)
        self.retry_after = retry_after


class SunoGenerationError(SunoError):
    """Raised when music generation fails."""
    pass


class SunoTimeoutError(SunoError):
    """Raised when a timeout occurs."""
    pass


class SunoDownloadError(SunoError):
    """Raised when audio download fails."""
    pass


class SunoCaptchaError(SunoError):
    """Raised when captcha solving fails across all strategies."""
    pass

"""Audio conversion utilities for Suno Wrapper."""

import asyncio
import pathlib
import subprocess
from typing import BinaryIO

from .exceptions import SunoError


class AudioConverter:
    """Handles audio format conversions."""
    
    SUPPORTED_INPUT_FORMATS = (".mp3", ".mp4", ".m4a", ".flac", ".ogg", ".aac", ".wav")
    
    async def convert_to_wav(
        self,
        input_path: pathlib.Path | str,
        output_path: pathlib.Path | str,
        sample_rate: int = 44100,
        channels: int = 2,
    ) -> pathlib.Path:
        """Convert audio file to WAV format using ffmpeg.
        
        Args:
            input_path: Path to input audio file
            output_path: Path for output WAV file
            sample_rate: Output sample rate in Hz (default: 44100)
            channels: Number of audio channels (default: 2 for stereo)
        
        Returns:
            Path to the converted WAV file
        
        Raises:
            SunoError: If conversion fails or ffmpeg is not available
        """
        input_path = pathlib.Path(input_path)
        output_path = pathlib.Path(output_path)
        
        if not input_path.exists():
            raise SunoError(f"Input file not found: {input_path}")
        
        # Ensure output has .wav extension
        if output_path.suffix.lower() != ".wav":
            output_path = output_path.with_suffix(".wav")

        ffmpeg_exe = "ffmpeg"
        if not self.is_ffmpeg_available():
            # Prefer a bundled ffmpeg binary if installed via imageio-ffmpeg.
            try:
                import imageio_ffmpeg  # type: ignore[import-not-found]

                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception as e:
                raise SunoError(
                    "ffmpeg not found. Install a system ffmpeg OR install imageio-ffmpeg.\n"
                    "For this repo: pip install imageio-ffmpeg\n"
                    f"Underlying error: {e}"
                )

        # Build ffmpeg command
        cmd = [
            ffmpeg_exe,
            "-y",  # Overwrite output file if exists
            "-i", str(input_path),  # Input file
            "-ar", str(sample_rate),  # Sample rate
            "-ac", str(channels),  # Channels
            "-c:a", "pcm_s16le",  # PCM 16-bit little-endian codec
            str(output_path),
        ]
        
        try:
            # Run ffmpeg in subprocess
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                error_msg = stderr.decode("utf-8", errors="ignore")[:500]
                raise SunoError(f"FFmpeg conversion failed: {error_msg}")
            
            return output_path
            
        except FileNotFoundError:
            raise SunoError(
                "ffmpeg not found. Please install ffmpeg to use audio conversion features. "
                "Visit: https://ffmpeg.org/download.html"
            )
        except Exception as e:
            raise SunoError(f"Audio conversion failed: {e}")
    
    async def convert_from_bytes(
        self,
        audio_bytes: bytes,
        output_path: pathlib.Path | str,
        input_format: str = ".mp3",
        sample_rate: int = 44100,
        channels: int = 2,
    ) -> pathlib.Path:
        """Convert audio bytes to WAV format.
        
        Args:
            audio_bytes: Raw audio data
            output_path: Path for output WAV file
            input_format: Format of input audio (e.g., ".mp3")
            sample_rate: Output sample rate
            channels: Number of channels
        
        Returns:
            Path to converted file
        """
        output_path = pathlib.Path(output_path)
        
        # Write bytes to temp file
        temp_input = output_path.parent / f"temp_input_{id(audio_bytes)}{input_format}"
        try:
            temp_input.write_bytes(audio_bytes)
            return await self.convert_to_wav(temp_input, output_path, sample_rate, channels)
        finally:
            if temp_input.exists():
                temp_input.unlink()
    
    def get_audio_info(self, file_path: pathlib.Path | str) -> dict:
        """Get audio file metadata using mutagen.
        
        Args:
            file_path: Path to audio file
        
        Returns:
            Dictionary with audio metadata
        """
        try:
            from mutagen.mp3 import MP3
            from mutagen.wave import WAVE
            from mutagen.flac import FLAC
            
            file_path = pathlib.Path(file_path)
            
            if not file_path.exists():
                return {"error": "File not found"}
            
            suffix = file_path.suffix.lower()
            
            if suffix == ".mp3":
                audio = MP3(file_path)
            elif suffix == ".wav":
                audio = WAVE(file_path)
            elif suffix == ".flac":
                audio = FLAC(file_path)
            else:
                return {"format": suffix, "size_bytes": file_path.stat().st_size}
            
            return {
                "format": suffix,
                "duration_seconds": audio.info.length,
                "sample_rate": audio.info.sample_rate,
                "channels": audio.info.channels,
                "bitrate": getattr(audio.info, "bitrate", None),
                "size_bytes": file_path.stat().st_size,
            }
            
        except Exception as e:
            return {"error": str(e)}
    
    @staticmethod
    def is_ffmpeg_available() -> bool:
        """Check if ffmpeg is installed and available."""
        try:
            subprocess.run(
                ["ffmpeg", "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

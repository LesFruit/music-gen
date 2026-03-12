#!/usr/bin/env python3
"""
Audio similarity analyzer for comparing reference tracks with generated covers.

Computes multiple similarity metrics between two audio files using only
numpy, scipy, and the standard library (no librosa dependency).

Metrics:
  1. Spectral similarity  - Cosine similarity of mel-spectrogram features
  2. Tempo similarity     - BPM ratio (closer to 1.0 = more similar)
  3. Chroma similarity    - Pitch-class distribution cosine similarity
  4. Energy similarity    - RMS energy contour correlation
  5. Overall cover score  - Weighted combination (0-100)

Usage:
  python audio_similarity.py reference.wav cover.wav [--verbose]
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import wave
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy import signal
from scipy.spatial.distance import cosine as cosine_dist


# ---------------------------------------------------------------------------
# Audio I/O
# ---------------------------------------------------------------------------

def read_wav(path: str) -> tuple[NDArray[np.float64], int]:
    """Read a WAV file and return (mono float64 samples in [-1,1], sample_rate)."""
    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sample_width == 1:
        dtype = np.uint8
        max_val = 128.0
    elif sample_width == 2:
        dtype = np.int16
        max_val = 32768.0
    elif sample_width == 3:
        # 24-bit: unpack manually
        n_samples = len(raw) // 3
        samples = np.zeros(n_samples, dtype=np.int32)
        for i in range(n_samples):
            b = raw[i * 3 : i * 3 + 3]
            val = struct.unpack_from("<i", b + (b"\x00" if b[2] < 128 else b"\xff"))[0]
            samples[i] = val
        if n_channels > 1:
            samples = samples.reshape(-1, n_channels).mean(axis=1)
        return samples.astype(np.float64) / 8388608.0, sample_rate
    elif sample_width == 4:
        dtype = np.int32
        max_val = 2147483648.0
    else:
        raise ValueError(f"Unsupported sample width: {sample_width}")

    samples = np.frombuffer(raw, dtype=dtype).astype(np.float64)
    if sample_width == 1:
        samples = (samples - 128.0) / max_val
    else:
        samples = samples / max_val

    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)

    return samples, sample_rate


# ---------------------------------------------------------------------------
# DSP helpers
# ---------------------------------------------------------------------------

def mel_filterbank(sr: int, n_fft: int, n_mels: int = 128,
                   fmin: float = 0.0, fmax: float | None = None) -> NDArray:
    """Create a mel filterbank matrix (n_mels x (n_fft//2 + 1))."""
    fmax = fmax or sr / 2.0
    n_bins = n_fft // 2 + 1

    def hz_to_mel(f: float) -> float:
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel_to_hz(m: float) -> float:
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    mel_min = hz_to_mel(fmin)
    mel_max = hz_to_mel(fmax)
    mel_points = np.linspace(mel_min, mel_max, n_mels + 2)
    hz_points = np.array([mel_to_hz(m) for m in mel_points])
    bin_points = np.floor((n_fft + 1) * hz_points / sr).astype(int)

    fb = np.zeros((n_mels, n_bins))
    for i in range(n_mels):
        left, center, right = bin_points[i], bin_points[i + 1], bin_points[i + 2]
        for j in range(left, center):
            if center != left:
                fb[i, j] = (j - left) / (center - left)
        for j in range(center, right):
            if right != center:
                fb[i, j] = (right - j) / (right - center)
    return fb


def compute_stft(samples: NDArray, n_fft: int = 2048,
                 hop_length: int = 512) -> NDArray:
    """Compute the magnitude STFT. Returns shape (n_fft//2+1, n_frames)."""
    win = np.hanning(n_fft)
    n_frames = 1 + (len(samples) - n_fft) // hop_length
    if n_frames <= 0:
        raise ValueError("Audio too short for STFT analysis")
    stft = np.zeros((n_fft // 2 + 1, n_frames))
    for i in range(n_frames):
        start = i * hop_length
        frame = samples[start:start + n_fft] * win
        spectrum = np.fft.rfft(frame)
        stft[:, i] = np.abs(spectrum)
    return stft


def compute_mel_spectrogram(samples: NDArray, sr: int,
                            n_fft: int = 2048, hop_length: int = 512,
                            n_mels: int = 128) -> NDArray:
    """Compute a log-mel spectrogram. Returns shape (n_mels, n_frames)."""
    mag = compute_stft(samples, n_fft, hop_length)
    fb = mel_filterbank(sr, n_fft, n_mels, fmax=sr / 2.0)
    mel = fb @ mag
    # Log scale with floor to avoid log(0)
    return np.log1p(mel * 100.0)


def compute_chroma(samples: NDArray, sr: int,
                   n_fft: int = 4096, hop_length: int = 512) -> NDArray:
    """Compute chromagram (12 pitch classes x n_frames)."""
    mag = compute_stft(samples, n_fft, hop_length)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)

    chroma = np.zeros((12, mag.shape[1]))
    for i, f in enumerate(freqs):
        if f < 20 or f > sr / 2:
            continue
        # Map frequency to pitch class
        pitch = 12.0 * np.log2(f / 440.0) + 69.0
        pc = int(round(pitch)) % 12
        chroma[pc, :] += mag[i, :]
    return chroma


def compute_rms(samples: NDArray, frame_length: int = 2048,
                hop_length: int = 512) -> NDArray:
    """Compute RMS energy envelope."""
    n_frames = 1 + (len(samples) - frame_length) // hop_length
    if n_frames <= 0:
        return np.array([np.sqrt(np.mean(samples ** 2))])
    rms = np.zeros(n_frames)
    for i in range(n_frames):
        start = i * hop_length
        frame = samples[start:start + frame_length]
        rms[i] = np.sqrt(np.mean(frame ** 2))
    return rms


def estimate_bpm(samples: NDArray, sr: int) -> float:
    """Estimate tempo via onset-strength autocorrelation."""
    hop = 512
    # Compute spectral flux (onset strength)
    mag = compute_stft(samples, n_fft=2048, hop_length=hop)
    flux = np.sum(np.maximum(0, np.diff(mag, axis=1)), axis=0)
    if len(flux) < 4:
        return 120.0  # fallback

    # Autocorrelation of onset strength
    # Search BPM range 40-240
    min_lag = int(60.0 * sr / (240.0 * hop))
    max_lag = int(60.0 * sr / (40.0 * hop))
    max_lag = min(max_lag, len(flux) - 1)
    if min_lag >= max_lag:
        return 120.0

    ac = np.correlate(flux - flux.mean(), flux - flux.mean(), mode="full")
    ac = ac[len(ac) // 2:]  # positive lags only

    if max_lag >= len(ac):
        max_lag = len(ac) - 1

    search = ac[min_lag:max_lag + 1]
    if len(search) == 0:
        return 120.0
    best_lag = min_lag + np.argmax(search)
    bpm = 60.0 * sr / (best_lag * hop)
    return round(bpm, 1)


# ---------------------------------------------------------------------------
# Similarity metrics
# ---------------------------------------------------------------------------

def _cosine_similarity(a: NDArray, b: NDArray) -> float:
    """Cosine similarity (1 = identical, 0 = orthogonal)."""
    a_flat = a.flatten().astype(np.float64)
    b_flat = b.flatten().astype(np.float64)
    norm_a = np.linalg.norm(a_flat)
    norm_b = np.linalg.norm(b_flat)
    if norm_a < 1e-12 or norm_b < 1e-12:
        return 0.0
    return float(np.dot(a_flat, b_flat) / (norm_a * norm_b))


def _align_lengths(a: NDArray, b: NDArray) -> tuple[NDArray, NDArray]:
    """Resample the longer 2D array (axis=1) to match the shorter, or pad."""
    if a.shape[1] == b.shape[1]:
        return a, b
    # Use the shorter length as target
    target = min(a.shape[1], b.shape[1])
    def resample_2d(x: NDArray, n: int) -> NDArray:
        indices = np.linspace(0, x.shape[1] - 1, n).astype(int)
        return x[:, indices]
    return resample_2d(a, target), resample_2d(b, target)


def _align_1d(a: NDArray, b: NDArray) -> tuple[NDArray, NDArray]:
    """Resample 1D arrays to matching length."""
    target = min(len(a), len(b))
    def resample_1d(x: NDArray, n: int) -> NDArray:
        indices = np.linspace(0, len(x) - 1, n).astype(int)
        return x[indices]
    return resample_1d(a, target), resample_1d(b, target)


def spectral_similarity(mel_a: NDArray, mel_b: NDArray) -> float:
    """Compare mel spectrograms via cosine similarity."""
    a, b = _align_lengths(mel_a, mel_b)
    return _cosine_similarity(a, b)


def tempo_similarity(bpm_a: float, bpm_b: float) -> float:
    """BPM ratio similarity. Returns value in [0, 1].
    Also checks half/double tempo relationships."""
    if bpm_a <= 0 or bpm_b <= 0:
        return 0.0
    ratio = min(bpm_a, bpm_b) / max(bpm_a, bpm_b)
    # Also check half/double tempo (common in covers)
    ratio_half = min(bpm_a, bpm_b * 2) / max(bpm_a, bpm_b * 2)
    ratio_double = min(bpm_a * 2, bpm_b) / max(bpm_a * 2, bpm_b)
    return max(ratio, ratio_half, ratio_double)


def chroma_similarity(chroma_a: NDArray, chroma_b: NDArray) -> float:
    """Compare pitch-class distributions."""
    a, b = _align_lengths(chroma_a, chroma_b)
    # Frame-wise cosine similarity, then average
    n_frames = a.shape[1]
    sims = []
    for i in range(n_frames):
        na = np.linalg.norm(a[:, i])
        nb = np.linalg.norm(b[:, i])
        if na < 1e-12 or nb < 1e-12:
            continue
        sims.append(float(np.dot(a[:, i], b[:, i]) / (na * nb)))
    if not sims:
        return 0.0
    return float(np.mean(sims))


def energy_similarity(rms_a: NDArray, rms_b: NDArray) -> float:
    """Compare RMS energy contours via Pearson correlation."""
    a, b = _align_1d(rms_a, rms_b)
    if len(a) < 2:
        return 0.0
    # Normalize
    a_std = np.std(a)
    b_std = np.std(b)
    if a_std < 1e-12 or b_std < 1e-12:
        return 0.0
    corr = np.corrcoef(a, b)[0, 1]
    # Map from [-1, 1] to [0, 1]
    return float((corr + 1.0) / 2.0)


def compute_overall_score(spectral: float, tempo: float,
                          chroma: float, energy: float) -> float:
    """Weighted combination of metrics, scaled to 0-100.

    Weights reflect importance for cover detection:
      - Chroma (key/melody):  35%  - most important for covers
      - Spectral:             30%  - overall timbral similarity
      - Tempo:                20%  - rhythmic similarity
      - Energy:               15%  - dynamics similarity
    """
    score = (
        0.35 * chroma +
        0.30 * spectral +
        0.20 * tempo +
        0.15 * energy
    )
    return round(score * 100.0, 1)


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(ref_path: str, cover_path: str, verbose: bool = False) -> dict[str, Any]:
    """Run full similarity analysis between reference and cover audio."""
    if verbose:
        print(f"Loading reference: {ref_path}", file=sys.stderr)
    ref_samples, ref_sr = read_wav(ref_path)

    if verbose:
        print(f"Loading cover: {cover_path}", file=sys.stderr)
    cov_samples, cov_sr = read_wav(cover_path)

    # Resample cover to match reference sample rate if needed
    if cov_sr != ref_sr:
        if verbose:
            print(f"Resampling cover from {cov_sr} to {ref_sr} Hz", file=sys.stderr)
        n_target = int(len(cov_samples) * ref_sr / cov_sr)
        cov_samples = signal.resample(cov_samples, n_target)
        cov_sr = ref_sr

    sr = ref_sr
    ref_dur = len(ref_samples) / sr
    cov_dur = len(cov_samples) / sr

    if verbose:
        print(f"Reference: {ref_dur:.1f}s @ {sr}Hz, "
              f"Cover: {cov_dur:.1f}s @ {sr}Hz", file=sys.stderr)

    # --- Compute features ---
    if verbose:
        print("Computing mel spectrograms...", file=sys.stderr)
    mel_ref = compute_mel_spectrogram(ref_samples, sr)
    mel_cov = compute_mel_spectrogram(cov_samples, sr)

    if verbose:
        print("Estimating tempo...", file=sys.stderr)
    bpm_ref = estimate_bpm(ref_samples, sr)
    bpm_cov = estimate_bpm(cov_samples, sr)

    if verbose:
        print("Computing chroma features...", file=sys.stderr)
    chroma_ref = compute_chroma(ref_samples, sr)
    chroma_cov = compute_chroma(cov_samples, sr)

    if verbose:
        print("Computing RMS energy...", file=sys.stderr)
    rms_ref = compute_rms(ref_samples)
    rms_cov = compute_rms(cov_samples)

    # --- Compute similarities ---
    spec_sim = spectral_similarity(mel_ref, mel_cov)
    temp_sim = tempo_similarity(bpm_ref, bpm_cov)
    chrom_sim = chroma_similarity(chroma_ref, chroma_cov)
    nrg_sim = energy_similarity(rms_ref, rms_cov)
    overall = compute_overall_score(spec_sim, temp_sim, chrom_sim, nrg_sim)

    return {
        "reference": {
            "path": ref_path,
            "duration_s": round(ref_dur, 2),
            "sample_rate": sr,
            "estimated_bpm": bpm_ref,
        },
        "cover": {
            "path": cover_path,
            "duration_s": round(cov_dur, 2),
            "sample_rate": sr,
            "estimated_bpm": bpm_cov,
        },
        "metrics": {
            "spectral_similarity": round(spec_sim, 4),
            "tempo_similarity": round(temp_sim, 4),
            "chroma_similarity": round(chrom_sim, 4),
            "energy_similarity": round(nrg_sim, 4),
        },
        "overall_cover_score": overall,
        "interpretation": _interpret(overall),
    }


def _interpret(score: float) -> str:
    """Human-readable interpretation of the overall score."""
    if score >= 75:
        return "Strong cover match - high melodic/rhythmic similarity"
    elif score >= 55:
        return "Moderate cover match - recognizable similarities"
    elif score >= 40:
        return "Weak cover match - some shared characteristics"
    elif score >= 25:
        return "Low similarity - mostly different content, similar genre at best"
    else:
        return "Not a cover - unrelated audio"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare audio similarity between a reference track and a cover"
    )
    parser.add_argument("reference", help="Path to reference WAV file")
    parser.add_argument("cover", help="Path to cover WAV file")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print progress to stderr")
    args = parser.parse_args()

    for p in [args.reference, args.cover]:
        if not Path(p).exists():
            print(f"Error: File not found: {p}", file=sys.stderr)
            sys.exit(1)
        if not p.lower().endswith(".wav"):
            print(f"Warning: {p} may not be a WAV file", file=sys.stderr)

    try:
        result = analyze(args.reference, args.cover, verbose=args.verbose)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

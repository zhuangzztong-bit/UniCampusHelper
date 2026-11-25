"""User voice profile capture, local OpenVoice V2 neural timbre cloning, and audio finishing.

Architecture:
  1. Capture user's reference voice via live microphone (sounddevice) or audio file import.
  2. Clean & analyze sample via FFmpeg + numpy (silence trim, mild denoise, F0 pitch estimation,
     SNR quality score) and cache the speaker embedding as raw float32 on disk under D drive.
  3. Generate fluent native English base speech (Edge-TTS with pitch/gender-matched neural voice,
     or textbook reference audio for follow-along/role-play tasks), then transfer speaker timbre
     using a self-contained pure-PyTorch OpenVoice V2 ToneColorConverter (MIT license, zero
     librosa/torchaudio/C++ build dependencies).
  4. Apply broadcast-grade FFmpeg finishing (warmth EQ, gentle dynamics, fade-in/out, EBU R128
     loudness normalization) so cloned speech sounds smooth, natural, non-dry, and glitch-free.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import io
import json
import math
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import warnings
import wave
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Tuple

import numpy as np

from .paths import PROJECT_ROOT

# ---------------------------------------------------------------------------
# Paths (strictly kept inside PROJECT_ROOT on D drive)
# ---------------------------------------------------------------------------

VOICE_PROFILES_DIR = PROJECT_ROOT / "voice_profiles"
VOICE_MODELS_DIR = PROJECT_ROOT / "logs" / "voice_models" / "openvoice_v2"
VOICE_TMP_DIR = PROJECT_ROOT / "logs" / "voice_tmp"

_OPENVOICE_V2_REVISION = "f36e7edfe1684461a8343844af60babc2efbb727"
_OPENVOICE_V2_RESOLVER = f"myshell-ai/OpenVoiceV2/resolve/{_OPENVOICE_V2_REVISION}/converter"
# File sizes are from the official repository's pinned tree. The checkpoint digest is its LFS OID.
OPENVOICE_V2_ASSETS = {
    "config.json": (
        838,
        "9dfff60350b8c63f2c664efd92a61b2516efb22671466960f0e5dfebd881fa47",
        (f"https://hf-mirror.com/{_OPENVOICE_V2_RESOLVER}/config.json",
         f"https://huggingface.co/{_OPENVOICE_V2_RESOLVER}/config.json"),
    ),
    "checkpoint.pth": (
        131320490,
        "9652c27e92b6b2a91632590ac9962ef7ae2b712e5c5b7f4c34ec55ee2b37ab9e",
        (f"https://hf-mirror.com/{_OPENVOICE_V2_RESOLVER}/checkpoint.pth",
         f"https://huggingface.co/{_OPENVOICE_V2_RESOLVER}/checkpoint.pth"),
    ),
}

# Fluent neural Edge-TTS base voices grouped by vocal register
BASE_VOICES_FEMALE = (
    "en-US-AvaMultilingualNeural",
    "en-US-EmmaMultilingualNeural",
    "en-US-JennyNeural",
    "en-US-AriaNeural",
)
BASE_VOICES_MALE = (
    "en-US-AndrewMultilingualNeural",
    "en-US-BrianMultilingualNeural",
    "en-US-GuyNeural",
)

GUIDED_SCRIPTS = [
    {
        "id": "bilingual_warm",
        "title": "中英自然朗读（推荐 · 约 12 秒）",
        "text": (
            "Hello everyone, my voice sounds clear, calm, and natural when I speak English. "
            "Learning a language takes steady practice every day, and I really enjoy sharing ideas with people around the world."
        ),
        "hint": "用平常放松聊天的音量与语速朗读，距离麦克风约 15–20 厘米，避免贴麦喷气。",
    },
    {
        "id": "campus_daily",
        "title": "校园口语场景（约 15 秒）",
        "text": (
            "In modern university life, effective communication helps us collaborate better and think more deeply. "
            "Whenever I prepare for a presentation, I focus on clear pronunciation, smooth pacing, and genuine expression."
        ),
        "hint": "保持自然连读与轻重音起伏，录满 8–18 秒即可提取高保真声纹。",
    },
    {
        "id": "chinese_free",
        "title": "中文自由朗读（跨语种也支持 · 约 12 秒）",
        "text": (
            "你好，这是我的真实声音样本。平时说话时语气自然平稳、咬字清晰，没有刻意压低或提高音调。"
            "希望这套音色模型能够流畅自然地模拟我的声音，完成接下来的英语口语练习。"
        ),
        "hint": "OpenVoice V2 支持跨语种音色迁移：即使录中文，也能用你的音色说出地道流利的英文。",
    },
]


# ---------------------------------------------------------------------------
# FFmpeg discovery & PCM helpers
# ---------------------------------------------------------------------------

_FFMPEG_CACHE: Optional[str] = None
_FFMPEG_PROBED: bool = False


def _probe_ffmpeg_binary(path: str) -> bool:
    """Verify an ffmpeg binary actually starts cleanly (avoids DLL init failures like 0xC0000142)."""
    if not path or not os.path.isfile(path):
        return False
    try:
        proc = subprocess.run(
            [path, "-version"],
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=_creationflags(),
        )
        return proc.returncode == 0 and b"ffmpeg version" in proc.stdout.lower()
    except Exception:
        return False


def find_ffmpeg() -> str:
    """Locate a working ffmpeg executable across PATH, imageio_ffmpeg, and D-drive folders."""
    global _FFMPEG_CACHE, _FFMPEG_PROBED
    if _FFMPEG_PROBED:
        if _FFMPEG_CACHE and os.path.isfile(_FFMPEG_CACHE):
            return _FFMPEG_CACHE
        return ""

    candidates: List[str] = []
    hit = shutil.which("ffmpeg")
    if hit:
        candidates.append(hit)
    try:
        import imageio_ffmpeg
        img_ff = imageio_ffmpeg.get_ffmpeg_exe()
        if img_ff:
            candidates.append(img_ff)
    except Exception:
        pass
    for path in (
        PROJECT_ROOT / "ffmpeg.exe",
        PROJECT_ROOT / "bin" / "ffmpeg.exe",
        PROJECT_ROOT.parent / "ffmpeg" / "ffmpeg-master-latest-win64-gpl" / "bin" / "ffmpeg.exe",
        Path(r"D:\creator\ffmpeg\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe"),
        Path(r"D:\ffmpeg\bin\ffmpeg.exe"),
    ):
        candidates.append(str(path))
    ff_root = Path(r"D:\creator\ffmpeg")
    if ff_root.is_dir():
        for exe in ff_root.rglob("ffmpeg.exe"):
            candidates.append(str(exe))
            break

    seen = set()
    for cand in candidates:
        norm = os.path.normcase(os.path.abspath(cand)) if cand else ""
        if not norm or norm in seen:
            continue
        seen.add(norm)
        if _probe_ffmpeg_binary(cand):
            _FFMPEG_CACHE = cand
            _FFMPEG_PROBED = True
            return cand

    _FFMPEG_CACHE = ""
    _FFMPEG_PROBED = True
    return ""


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _ensure_dirs():
    VOICE_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    VOICE_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    VOICE_TMP_DIR.mkdir(parents=True, exist_ok=True)


def _decode_with_pyav(source: str | Path | bytes, sample_rate: int = 22050) -> Optional[np.ndarray]:
    """Fast in-process audio decoding via PyAV (supports mp3/m4a/flac/ogg/wav without spawning ffmpeg)."""
    try:
        import av
        from av.audio.resampler import AudioResampler
    except Exception:
        return None
    try:
        container = av.open(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else str(source))
        try:
            resampler = AudioResampler(format="fltp", layout="mono", rate=int(sample_rate))
            chunks: List[np.ndarray] = []
            for frame in container.decode(audio=0):
                for rframe in resampler.resample(frame):
                    arr = rframe.to_ndarray()
                    if arr.ndim > 1:
                        arr = arr[0]
                    chunks.append(arr.astype(np.float32, copy=False))
            for rframe in resampler.resample(None):
                arr = rframe.to_ndarray()
                if arr.ndim > 1:
                    arr = arr[0]
                chunks.append(arr.astype(np.float32, copy=False))
            if not chunks:
                return None
            return np.concatenate(chunks, axis=0)
        finally:
            container.close()
    except Exception:
        return None


def decode_audio_to_float32(
    source: str | Path | bytes,
    sample_rate: int = 22050,
    extra_af: str = "",
) -> np.ndarray:
    """Decode any audio file or encoded bytes to mono float32 numpy array [-1, 1]."""
    # Fast native WAV or in-process PyAV path when no FFmpeg filter chain is requested
    if not extra_af:
        if isinstance(source, (str, Path)) and str(source).lower().endswith(".wav"):
            wav_arr, wav_sr = _read_wav_pcm(str(source))
            if wav_arr is not None and wav_sr == sample_rate:
                return wav_arr
        elif isinstance(source, (bytes, bytearray)) and source[:4] == b"RIFF":
            wav_arr, wav_sr = _read_wav_pcm_bytes(bytes(source))
            if wav_arr is not None and wav_sr == sample_rate:
                return wav_arr
        av_arr = _decode_with_pyav(source, sample_rate=sample_rate)
        if av_arr is not None and av_arr.size > 0:
            return av_arr

    ffmpeg = find_ffmpeg()
    if isinstance(source, (str, Path)):
        src_path = str(source)
        if not ffmpeg:
            raise RuntimeError("未找到可用的 FFmpeg，无法解码该音频文件")
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", src_path]
        if extra_af:
            cmd += ["-af", extra_af]
        cmd += ["-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "pipe:1"]
        proc = subprocess.run(
            cmd, capture_output=True, check=False, creationflags=_creationflags()
        )
        if proc.returncode != 0 or not proc.stdout:
            err = proc.stderr.decode("utf-8", "ignore").strip()
            raise RuntimeError(f"FFmpeg 解码失败 (rc={proc.returncode}): {err[:160]}")
        return np.frombuffer(proc.stdout, dtype=np.float32).copy()

    # Encoded bytes (mp3/wav/m4a/ogg...)
    if not ffmpeg:
        raise RuntimeError("未找到可用的 FFmpeg，无法解码音频流")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0"]
    if extra_af:
        cmd += ["-af", extra_af]
    cmd += ["-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "pipe:1"]
    proc = subprocess.run(
        cmd, input=source, capture_output=True, check=False, creationflags=_creationflags()
    )
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode("utf-8", "ignore").strip()
        raise RuntimeError(f"FFmpeg 音频流解码失败 (rc={proc.returncode}): {err[:160]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def _read_wav_pcm(path: str) -> Tuple[Optional[np.ndarray], int]:
    try:
        with wave.open(path, "rb") as wf:
            return _extract_wave_reader(wf)
    except Exception:
        return None, 0


def _read_wav_pcm_bytes(data: bytes) -> Tuple[Optional[np.ndarray], int]:
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            return _extract_wave_reader(wf)
    except Exception:
        return None, 0


def _extract_wave_reader(wf: wave.Wave_read) -> Tuple[Optional[np.ndarray], int]:
    ch = wf.getnchannels()
    sw = wf.getsampwidth()
    sr = wf.getframerate()
    frames = wf.readframes(wf.getnframes())
    if sw == 2:
        arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        arr = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        return None, 0
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    return arr, sr


def write_wav_pcm16(path: str | Path, audio: np.ndarray, sample_rate: int = 22050) -> None:
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype(np.int16)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm.tobytes())


def encode_wav_bytes(audio: np.ndarray, sample_rate: int = 22050) -> bytes:
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def finish_cloned_audio(
    audio: np.ndarray,
    sample_rate: int = 22050,
    out_format: str = "mp3",
    out_sr: int = 24000,
) -> Tuple[str, bytes]:
    """Apply broadcast-grade audio finishing so cloned speech is warm, smooth, and glitch-free.

    Chain:
      1. Sanitize NaN/DC offset and apply gentle 15ms fade-in / 25ms fade-out + 100ms tail pad.
      2. FFmpeg filter graph:
         - highpass=f=65 (clean sub-bass rumble)
         - subtle warmth (+1.3dB @ 210Hz), clarity (+0.9dB @ 3.1kHz), de-harsh (-1.3dB @ 7.4kHz)
         - gentle glue compressor (ratio 2.0, soft knee) for steady vocal flow
         - EBU R128 loudness normalization (I=-16 LUFS, TP=-1.5 dBTP)
      3. Encode to clean 128kbps MP3 (for Unipus mic injection) or 16-bit WAV (for preview).
    """
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    if arr.size == 0:
        return "", b""
    # Remove DC offset
    arr = arr - float(np.mean(arr))
    # Prevent clipping before FFmpeg while preserving headroom
    peak = float(np.max(np.abs(arr)))
    if peak > 0.92:
        arr = arr * (0.92 / peak)
    # Smooth 15ms fade-in and 25ms fade-out to eliminate boundary clicks
    fade_in = min(int(sample_rate * 0.015), arr.size // 4)
    fade_out = min(int(sample_rate * 0.025), arr.size // 4)
    if fade_in > 1:
        arr[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
    if fade_out > 1:
        arr[-fade_out:] *= np.linspace(1.0, 0.0, fade_out, dtype=np.float32)
    # Add short natural silence cushion at end so speech never feels abruptly cut off
    pad = np.zeros(int(sample_rate * 0.10), dtype=np.float32)
    arr = np.concatenate([np.zeros(int(sample_rate * 0.03), dtype=np.float32), arr, pad])

    raw_wav = encode_wav_bytes(arr, sample_rate)
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return "wav", raw_wav

    af = (
        "highpass=f=65,"
        "equalizer=f=210:t=q:w=1.2:g=1.3,"
        "equalizer=f=3100:t=q:w=1.5:g=0.9,"
        "equalizer=f=7400:t=q:w=1.8:g=-1.3,"
        "acompressor=threshold=-20dB:ratio=2.0:attack=12:release=140:makeup=1.4,"
        "loudnorm=I=-16:TP=-1.5:LRA=11"
    )
    fmt = (out_format or "mp3").lower()
    if fmt == "mp3":
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-af", af,
            "-ac", "1", "-ar", str(out_sr),
            "-c:a", "libmp3lame", "-b:a", "128k",
            "-f", "mp3", "pipe:1",
        ]
    else:
        fmt = "wav"
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-af", af,
            "-ac", "1", "-ar", str(out_sr),
            "-c:a", "pcm_s16le",
            "-f", "wav", "pipe:1",
        ]
    proc = subprocess.run(
        cmd, input=raw_wav, capture_output=True, check=False, creationflags=_creationflags()
    )
    if proc.returncode == 0 and proc.stdout:
        return fmt, proc.stdout
    return "wav", raw_wav


# ---------------------------------------------------------------------------
# Voice sample analysis (F0 pitch, gender estimate, SNR, active speech segments)
# ---------------------------------------------------------------------------

def estimate_f0_and_quality(audio: np.ndarray, sample_rate: int = 22050) -> Dict[str, Any]:
    """Estimate median fundamental frequency F0 (Hz), active duration, and clarity metrics."""
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    total_sec = float(arr.size) / max(1, sample_rate)
    if arr.size < int(sample_rate * 0.5):
        return {
            "duration": round(total_sec, 2),
            "active_duration": 0.0,
            "f0_hz": 0.0,
            "gender_guess": "female",
            "snr_db": 0.0,
            "quality_label": "录音过短",
            "quality_score": 20,
            "warning": "音频太短，请至少录制 6 秒清晰人声",
        }

    frame_len = int(sample_rate * 0.04)  # 40ms
    hop = int(sample_rate * 0.02)        # 20ms
    n_frames = max(0, (arr.size - frame_len) // hop)
    if n_frames < 5:
        return {
            "duration": round(total_sec, 2),
            "active_duration": round(total_sec, 2),
            "f0_hz": 0.0,
            "gender_guess": "female",
            "snr_db": 10.0,
            "quality_label": "样本偏短",
            "quality_score": 40,
            "warning": "建议录制 8–18 秒连续清晰朗读",
        }

    rms_vals = np.empty(n_frames, dtype=np.float32)
    for i in range(n_frames):
        seg = arr[i * hop : i * hop + frame_len]
        rms_vals[i] = float(np.sqrt(np.mean(seg * seg) + 1e-12))

    noise_floor = float(np.percentile(rms_vals, 15))
    speech_level = float(np.percentile(rms_vals, 85))
    snr_db = float(20.0 * math.log10((speech_level + 1e-6) / (noise_floor + 1e-6)))
    snr_db = max(0.0, min(50.0, snr_db))

    energy_thr = max(0.012, speech_level * 0.22)
    active_mask = rms_vals >= energy_thr
    active_sec = float(np.sum(active_mask)) * (hop / sample_rate)

    # Autocorrelation F0 estimation on voiced frames (70 Hz - 340 Hz)
    min_lag = max(1, int(sample_rate / 340.0))
    max_lag = min(frame_len - 1, int(sample_rate / 70.0))
    window = np.hanning(frame_len).astype(np.float32)
    f0_list: List[float] = []

    step = max(1, n_frames // 250)  # sample up to ~250 frames for speed
    for i in range(0, n_frames, step):
        if not active_mask[i]:
            continue
        seg = arr[i * hop : i * hop + frame_len]
        seg = (seg - float(np.mean(seg))) * window
        energy = float(np.dot(seg, seg))
        if energy < 1e-5:
            continue
        # FFT-based autocorrelation
        nfft = 1 << (frame_len * 2 - 1).bit_length()
        spec = np.fft.rfft(seg, n=nfft)
        acf = np.fft.irfft(spec * np.conj(spec), n=nfft)[:frame_len]
        if acf[0] <= 1e-9:
            continue
        acf_norm = acf[min_lag:max_lag] / acf[0]
        if acf_norm.size == 0:
            continue
        best_idx = int(np.argmax(acf_norm))
        best_corr = float(acf_norm[best_idx])
        if best_corr >= 0.45:
            lag = min_lag + best_idx
            # Parabolic interpolation around peak
            if 0 < best_idx < acf_norm.size - 1:
                y0, y1, y2 = acf_norm[best_idx - 1], acf_norm[best_idx], acf_norm[best_idx + 1]
                denom = (y0 - 2.0 * y1 + y2)
                if abs(denom) > 1e-6:
                    lag = lag + 0.5 * (y0 - y2) / denom
            f0 = sample_rate / max(1.0, float(lag))
            if 75.0 <= f0 <= 330.0:
                f0_list.append(f0)

    median_f0 = float(np.median(f0_list)) if f0_list else 185.0
    gender_guess = "male" if median_f0 < 168.0 else "female"

    # Compute quality score and readable hint
    score = 100
    warning = ""
    if speech_level < 0.018:
        score -= 40
        warning = "音量偏小，建议靠近麦克风重录"
    elif active_sec < 4.5:
        score -= 35
        warning = "有效人声偏短（不足 5 秒），建议录满 8–15 秒"
    elif active_sec < 7.5:
        score -= 12
    if snr_db < 10.0:
        score -= 22
        if not warning:
            warning = "环境底噪偏高，建议在安静环境重录以提升音色纯净度"
    elif snr_db < 16.0:
        score -= 8
    score = max(15, min(100, score))

    if score >= 85:
        label = "音质极佳"
    elif score >= 68:
        label = "音质良好"
    elif score >= 48:
        label = "可用（建议更清晰）"
    else:
        label = "建议重录"

    return {
        "duration": round(total_sec, 2),
        "active_duration": round(active_sec, 2),
        "f0_hz": round(median_f0, 1),
        "gender_guess": gender_guess,
        "snr_db": round(snr_db, 1),
        "quality_label": label,
        "quality_score": int(score),
        "warning": warning,
    }


def extract_speech_chunks(
    audio: np.ndarray,
    sample_rate: int = 22050,
    min_chunk_sec: float = 1.6,
    max_chunk_sec: float = 6.0,
) -> List[np.ndarray]:
    """Split reference audio into active speech chunks (skipping dead silence) for cleaner SE."""
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    if arr.size < int(sample_rate * 1.2):
        return [arr] if arr.size > 0 else []

    hop = int(sample_rate * 0.05)  # 50ms
    n_frames = max(1, arr.size // hop)
    rms = np.array(
        [float(np.sqrt(np.mean(arr[i * hop : (i + 1) * hop] ** 2) + 1e-12)) for i in range(n_frames)],
        dtype=np.float32,
    )
    peak_rms = float(np.percentile(rms, 85))
    thr = max(0.010, peak_rms * 0.18)
    active = rms >= thr

    # Merge tiny silence gaps (< 0.35s = 7 frames)
    gap_limit = 7
    i = 0
    while i < n_frames:
        if not active[i]:
            j = i
            while j < n_frames and not active[j]:
                j += 1
            if i > 0 and j < n_frames and (j - i) <= gap_limit:
                active[i:j] = True
            i = j
        else:
            i += 1

    chunks: List[np.ndarray] = []
    max_len = int(max_chunk_sec * sample_rate)
    min_len = int(min_chunk_sec * sample_rate)

    i = 0
    while i < n_frames:
        if active[i]:
            j = i
            while j < n_frames and active[j]:
                j += 1
            seg = arr[i * hop : min(arr.size, j * hop)]
            if seg.size >= min_len:
                if seg.size <= max_len:
                    chunks.append(seg)
                else:
                    for start in range(0, seg.size, max_len):
                        sub = seg[start : start + max_len]
                        if sub.size >= min_len:
                            chunks.append(sub)
            i = j
        else:
            i += 1

    if not chunks:
        # Fallback: slice whole audio into <= 6s windows
        for start in range(0, arr.size, max_len):
            sub = arr[start : start + max_len]
            if sub.size >= int(sample_rate * 0.8):
                chunks.append(sub)
    return chunks or [arr]


# ---------------------------------------------------------------------------
# Self-contained Pure-PyTorch OpenVoice V2 ToneColorConverter (MIT License)
# Zero librosa / torchaudio / wavmark dependency
# ---------------------------------------------------------------------------

def _build_openvoice_v2_classes():
    """Lazy-construct OpenVoice V2 PyTorch nn.Modules only when torch is actually needed."""
    import torch
    from torch import nn
    from torch.nn import Conv1d, ConvTranspose1d, functional as F
    from torch.nn.utils import remove_weight_norm, weight_norm

    LRELU_SLOPE = 0.1

    def get_padding(kernel_size: int, dilation: int = 1) -> int:
        return int((kernel_size * dilation - dilation) / 2)

    def init_weights(m, mean: float = 0.0, std: float = 0.01):
        if m.__class__.__name__.find("Conv") != -1:
            m.weight.data.normal_(mean, std)

    def sequence_mask(length, max_length=None):
        if max_length is None:
            max_length = length.max()
        x = torch.arange(max_length, dtype=length.dtype, device=length.device)
        return x.unsqueeze(0) < length.unsqueeze(1)

    def fused_add_tanh_sigmoid_multiply(input_a, input_b, n_channels: int):
        in_act = input_a + input_b
        t_act = torch.tanh(in_act[:, :n_channels, :])
        s_act = torch.sigmoid(in_act[:, n_channels:, :])
        return t_act * s_act

    class WN(nn.Module):
        def __init__(self, hidden_channels, kernel_size, dilation_rate, n_layers, gin_channels=0, p_dropout=0):
            super().__init__()
            assert kernel_size % 2 == 1
            self.hidden_channels = hidden_channels
            self.kernel_size = (kernel_size,)
            self.dilation_rate = dilation_rate
            self.n_layers = n_layers
            self.gin_channels = gin_channels
            self.p_dropout = p_dropout
            self.in_layers = nn.ModuleList()
            self.res_skip_layers = nn.ModuleList()
            self.drop = nn.Dropout(p_dropout)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if gin_channels != 0:
                    cond_layer = nn.Conv1d(gin_channels, 2 * hidden_channels * n_layers, 1)
                    self.cond_layer = weight_norm(cond_layer, name="weight")
                for i in range(n_layers):
                    dilation = dilation_rate ** i
                    padding = int((kernel_size * dilation - dilation) / 2)
                    in_layer = nn.Conv1d(
                        hidden_channels, 2 * hidden_channels, kernel_size,
                        dilation=dilation, padding=padding,
                    )
                    self.in_layers.append(weight_norm(in_layer, name="weight"))
                    res_skip_ch = 2 * hidden_channels if i < n_layers - 1 else hidden_channels
                    res_skip_layer = nn.Conv1d(hidden_channels, res_skip_ch, 1)
                    self.res_skip_layers.append(weight_norm(res_skip_layer, name="weight"))

        def forward(self, x, x_mask, g=None):
            output = torch.zeros_like(x)
            if g is not None:
                g = self.cond_layer(g)
            for i in range(self.n_layers):
                x_in = self.in_layers[i](x)
                if g is not None:
                    off = i * 2 * self.hidden_channels
                    g_l = g[:, off : off + 2 * self.hidden_channels, :]
                else:
                    g_l = torch.zeros_like(x_in)
                acts = fused_add_tanh_sigmoid_multiply(x_in, g_l, self.hidden_channels)
                acts = self.drop(acts)
                res_skip = self.res_skip_layers[i](acts)
                if i < self.n_layers - 1:
                    x = (x + res_skip[:, : self.hidden_channels, :]) * x_mask
                    output = output + res_skip[:, self.hidden_channels :, :]
                else:
                    output = output + res_skip
            return output * x_mask

        def remove_weight_norm(self):
            if self.gin_channels != 0:
                remove_weight_norm(self.cond_layer)
            for l in self.in_layers:
                remove_weight_norm(l)
            for l in self.res_skip_layers:
                remove_weight_norm(l)

    class ResBlock1(nn.Module):
        def __init__(self, channels, kernel_size=3, dilation=(1, 3, 5)):
            super().__init__()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.convs1 = nn.ModuleList([
                    weight_norm(Conv1d(channels, channels, kernel_size, 1, dilation=d, padding=get_padding(kernel_size, d)))
                    for d in dilation
                ])
                self.convs2 = nn.ModuleList([
                    weight_norm(Conv1d(channels, channels, kernel_size, 1, dilation=1, padding=get_padding(kernel_size, 1)))
                    for _ in dilation
                ])
            self.convs1.apply(init_weights)
            self.convs2.apply(init_weights)

        def forward(self, x, x_mask=None):
            for c1, c2 in zip(self.convs1, self.convs2):
                xt = F.leaky_relu(x, LRELU_SLOPE)
                if x_mask is not None:
                    xt = xt * x_mask
                xt = c1(xt)
                xt = F.leaky_relu(xt, LRELU_SLOPE)
                if x_mask is not None:
                    xt = xt * x_mask
                xt = c2(xt)
                x = xt + x
            return x * x_mask if x_mask is not None else x

        def remove_weight_norm(self):
            for l in self.convs1:
                remove_weight_norm(l)
            for l in self.convs2:
                remove_weight_norm(l)

    class ResBlock2(nn.Module):
        def __init__(self, channels, kernel_size=3, dilation=(1, 3)):
            super().__init__()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.convs = nn.ModuleList([
                    weight_norm(Conv1d(channels, channels, kernel_size, 1, dilation=d, padding=get_padding(kernel_size, d)))
                    for d in dilation
                ])
            self.convs.apply(init_weights)

        def forward(self, x, x_mask=None):
            for c in self.convs:
                xt = F.leaky_relu(x, LRELU_SLOPE)
                if x_mask is not None:
                    xt = xt * x_mask
                x = c(xt) + x
            return x * x_mask if x_mask is not None else x

        def remove_weight_norm(self):
            for l in self.convs:
                remove_weight_norm(l)

    class Flip(nn.Module):
        def forward(self, x, *args, reverse=False, **kwargs):
            x = torch.flip(x, [1])
            if not reverse:
                return x, torch.zeros(x.size(0), dtype=x.dtype, device=x.device)
            return x

    class ResidualCouplingLayer(nn.Module):
        def __init__(self, channels, hidden_channels, kernel_size, dilation_rate, n_layers, gin_channels=0, mean_only=True):
            super().__init__()
            self.half_channels = channels // 2
            self.mean_only = mean_only
            self.pre = nn.Conv1d(self.half_channels, hidden_channels, 1)
            self.enc = WN(hidden_channels, kernel_size, dilation_rate, n_layers, gin_channels=gin_channels)
            self.post = nn.Conv1d(hidden_channels, self.half_channels * (2 - mean_only), 1)
            self.post.weight.data.zero_()
            self.post.bias.data.zero_()

        def forward(self, x, x_mask, g=None, reverse=False):
            x0, x1 = torch.split(x, [self.half_channels, self.half_channels], 1)
            h = self.pre(x0) * x_mask
            h = self.enc(h, x_mask, g=g)
            stats = self.post(h) * x_mask
            if not self.mean_only:
                m, logs = torch.split(stats, [self.half_channels, self.half_channels], 1)
            else:
                m = stats
                logs = torch.zeros_like(m)
            if not reverse:
                x1 = m + x1 * torch.exp(logs) * x_mask
                return torch.cat([x0, x1], 1), torch.sum(logs, [1, 2])
            x1 = (x1 - m) * torch.exp(-logs) * x_mask
            return torch.cat([x0, x1], 1)

        def remove_weight_norm(self):
            self.enc.remove_weight_norm()

    class ResidualCouplingBlock(nn.Module):
        def __init__(self, channels, hidden_channels, kernel_size, dilation_rate, n_layers, n_flows=4, gin_channels=0):
            super().__init__()
            self.flows = nn.ModuleList()
            for _ in range(n_flows):
                self.flows.append(ResidualCouplingLayer(channels, hidden_channels, kernel_size, dilation_rate, n_layers, gin_channels=gin_channels, mean_only=True))
                self.flows.append(Flip())

        def forward(self, x, x_mask, g=None, reverse=False):
            if not reverse:
                for flow in self.flows:
                    x, _ = flow(x, x_mask, g=g, reverse=False)
            else:
                for flow in reversed(self.flows):
                    x = flow(x, x_mask, g=g, reverse=True)
            return x

        def remove_weight_norm(self):
            for flow in self.flows:
                if hasattr(flow, "remove_weight_norm"):
                    flow.remove_weight_norm()

    class PosteriorEncoder(nn.Module):
        def __init__(self, in_channels, out_channels, hidden_channels, kernel_size, dilation_rate, n_layers, gin_channels=0):
            super().__init__()
            self.out_channels = out_channels
            self.pre = nn.Conv1d(in_channels, hidden_channels, 1)
            self.enc = WN(hidden_channels, kernel_size, dilation_rate, n_layers, gin_channels=gin_channels)
            self.proj = nn.Conv1d(hidden_channels, out_channels * 2, 1)

        def forward(self, x, x_lengths, g=None, tau=1.0):
            x_mask = torch.unsqueeze(sequence_mask(x_lengths, x.size(2)), 1).to(x.dtype)
            x = self.pre(x) * x_mask
            x = self.enc(x, x_mask, g=g)
            stats = self.proj(x) * x_mask
            m, logs = torch.split(stats, self.out_channels, dim=1)
            z = (m + torch.randn_like(m) * tau * torch.exp(logs)) * x_mask
            return z, m, logs, x_mask

        def remove_weight_norm(self):
            self.enc.remove_weight_norm()

    class Generator(nn.Module):
        def __init__(
            self, initial_channel, resblock, resblock_kernel_sizes, resblock_dilation_sizes,
            upsample_rates, upsample_initial_channel, upsample_kernel_sizes, gin_channels=0,
        ):
            super().__init__()
            self.num_kernels = len(resblock_kernel_sizes)
            self.num_upsamples = len(upsample_rates)
            self.conv_pre = Conv1d(initial_channel, upsample_initial_channel, 7, 1, padding=3)
            rb_cls = ResBlock1 if str(resblock) == "1" else ResBlock2
            self.ups = nn.ModuleList()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                for i, (u, k) in enumerate(zip(upsample_rates, upsample_kernel_sizes)):
                    self.ups.append(weight_norm(ConvTranspose1d(
                        upsample_initial_channel // (2 ** i),
                        upsample_initial_channel // (2 ** (i + 1)),
                        k, u, padding=(k - u) // 2,
                    )))
            self.resblocks = nn.ModuleList()
            ch = upsample_initial_channel
            for i in range(len(self.ups)):
                ch = upsample_initial_channel // (2 ** (i + 1))
                for k, d in zip(resblock_kernel_sizes, resblock_dilation_sizes):
                    self.resblocks.append(rb_cls(ch, k, d))
            self.conv_post = Conv1d(ch, 1, 7, 1, padding=3, bias=False)
            self.ups.apply(init_weights)
            if gin_channels != 0:
                self.cond = nn.Conv1d(gin_channels, upsample_initial_channel, 1)

        def forward(self, x, g=None):
            x = self.conv_pre(x)
            if g is not None:
                x = x + self.cond(g)
            for i in range(self.num_upsamples):
                x = F.leaky_relu(x, LRELU_SLOPE)
                x = self.ups[i](x)
                xs = None
                for j in range(self.num_kernels):
                    out = self.resblocks[i * self.num_kernels + j](x)
                    xs = out if xs is None else xs + out
                x = xs / self.num_kernels
            x = F.leaky_relu(x)
            x = self.conv_post(x)
            return torch.tanh(x)

        def remove_weight_norm(self):
            for l in self.ups:
                remove_weight_norm(l)
            for l in self.resblocks:
                l.remove_weight_norm()

    class ReferenceEncoder(nn.Module):
        def __init__(self, spec_channels, gin_channels=0, layernorm=True):
            super().__init__()
            self.spec_channels = spec_channels
            ref_enc_filters = [32, 32, 64, 64, 128, 128]
            filters = [1] + ref_enc_filters
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.convs = nn.ModuleList([
                    weight_norm(nn.Conv2d(filters[i], filters[i + 1], kernel_size=(3, 3), stride=(2, 2), padding=(1, 1)))
                    for i in range(len(ref_enc_filters))
                ])
            out_ch = spec_channels
            for _ in range(len(ref_enc_filters)):
                out_ch = (out_ch - 3 + 2) // 2 + 1
            self.gru = nn.GRU(input_size=ref_enc_filters[-1] * out_ch, hidden_size=128, batch_first=True)
            self.proj = nn.Linear(128, gin_channels)
            self.layernorm = nn.LayerNorm(spec_channels) if layernorm else None

        def forward(self, inputs):
            n = inputs.size(0)
            out = inputs.view(n, 1, -1, self.spec_channels)
            if self.layernorm is not None:
                out = self.layernorm(out)
            for conv in self.convs:
                out = F.relu(conv(out))
            out = out.transpose(1, 2)
            t = out.size(1)
            out = out.contiguous().view(n, t, -1)
            self.gru.flatten_parameters()
            _, hidden = self.gru(out)
            return self.proj(hidden.squeeze(0))

        def remove_weight_norm(self):
            for conv in self.convs:
                remove_weight_norm(conv)

    class ConverterSynthesizerTrn(nn.Module):
        """Inference-only SynthesizerTrn matching OpenVoice V2 converter checkpoint."""

        def __init__(self, spec_channels: int, model_cfg: Dict[str, Any]):
            super().__init__()
            inter_ch = int(model_cfg["inter_channels"])
            hidden_ch = int(model_cfg["hidden_channels"])
            gin_ch = int(model_cfg["gin_channels"])
            self.zero_g = bool(model_cfg.get("zero_g", True))

            self.dec = Generator(
                inter_ch,
                model_cfg["resblock"],
                model_cfg["resblock_kernel_sizes"],
                model_cfg["resblock_dilation_sizes"],
                model_cfg["upsample_rates"],
                int(model_cfg["upsample_initial_channel"]),
                model_cfg["upsample_kernel_sizes"],
                gin_channels=gin_ch,
            )
            self.enc_q = PosteriorEncoder(
                spec_channels, inter_ch, hidden_ch, 5, 1, 16, gin_channels=gin_ch
            )
            self.flow = ResidualCouplingBlock(
                inter_ch, hidden_ch, 5, 1, 4, gin_channels=gin_ch
            )
            self.ref_enc = ReferenceEncoder(spec_channels, gin_ch)

        def remove_weight_norm(self):
            self.dec.remove_weight_norm()
            self.enc_q.remove_weight_norm()
            self.flow.remove_weight_norm()
            self.ref_enc.remove_weight_norm()

        def voice_conversion(self, y, y_lengths, sid_src, sid_tgt, tau=0.35):
            g_src = sid_src
            g_tgt = sid_tgt
            zero_src = torch.zeros_like(g_src) if self.zero_g else g_src
            zero_tgt = torch.zeros_like(g_tgt) if self.zero_g else g_tgt
            z, _, _, y_mask = self.enc_q(y, y_lengths, g=zero_src, tau=tau)
            z_p = self.flow(z, y_mask, g=g_src, reverse=False)
            z_hat = self.flow(z_p, y_mask, g=g_tgt, reverse=True)
            o_hat = self.dec(z_hat * y_mask, g=zero_tgt)
            return o_hat

    return ConverterSynthesizerTrn


def _spectrogram_torch(y, n_fft: int, hop_size: int, win_size: int, window_cache: dict):
    import torch
    key = (win_size, str(y.dtype), str(y.device))
    if key not in window_cache:
        window_cache[key] = torch.hann_window(win_size, dtype=y.dtype, device=y.device)
    pad = int((n_fft - hop_size) / 2)
    y_pad = torch.nn.functional.pad(y.unsqueeze(1), (pad, pad), mode="reflect").squeeze(1)
    spec = torch.stft(
        y_pad,
        n_fft=n_fft,
        hop_length=hop_size,
        win_length=win_size,
        window=window_cache[key],
        center=False,
        pad_mode="reflect",
        normalized=False,
        onesided=True,
        return_complex=True,
    )
    return torch.sqrt(spec.real.pow(2) + spec.imag.pow(2) + 1e-6)


class OpenVoiceV2Converter:
    """Thread-safe lazy loader and inference wrapper for OpenVoice V2 tone color conversion."""

    def __init__(self, model_dir: Path = VOICE_MODELS_DIR):
        self.model_dir = Path(model_dir)
        self._lock = threading.RLock()
        self._asset_lock = threading.Lock()
        self._download_lock = threading.Lock()
        self._asset_status: Dict[str, Tuple[Tuple[int, ...], bool]] = {}
        self._model = None
        self._cfg: Optional[Dict[str, Any]] = None
        self._device = "cpu"
        self._window_cache: dict = {}

    @property
    def config_path(self) -> Path:
        return self.model_dir / "config.json"

    @property
    def ckpt_path(self) -> Path:
        return self.model_dir / "checkpoint.pth"

    @staticmethod
    def _fingerprint(info: os.stat_result) -> Tuple[int, ...]:
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    @staticmethod
    def _matches_asset(handle: BinaryIO, info: os.stat_result, expected_size: int, expected_digest: str) -> bool:
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_size == expected_size
            and hashlib.file_digest(handle, "sha256").hexdigest() == expected_digest
        )

    def _asset_is_ready(self, name: str) -> bool:
        path = self.model_dir / name
        expected_size, expected_digest, _ = OPENVOICE_V2_ASSETS[name]
        with self._asset_lock:
            try:
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
                    return False
                signature = self._fingerprint(info)
                cached = self._asset_status.get(name)
                if cached and cached[0] == signature:
                    return cached[1]
                with path.open("rb") as handle:
                    info = os.fstat(handle.fileno())
                    valid = self._matches_asset(handle, info, expected_size, expected_digest)
                    self._asset_status[name] = (self._fingerprint(info), valid)
                return valid
            except OSError:
                return False

    def is_downloaded(self) -> bool:
        return all(self._asset_is_ready(name) for name in OPENVOICE_V2_ASSETS)

    @contextmanager
    def _open_verified_asset(self, name: str):
        path = self.model_dir / name
        expected_size, expected_digest, _ = OPENVOICE_V2_ASSETS[name]
        if not path.is_file():
            raise RuntimeError(f"缺少 OpenVoice V2 文件 {name}，请先下载本地模型")
        with path.open("rb") as handle:
            info = os.fstat(handle.fileno())
            if not self._matches_asset(handle, info, expected_size, expected_digest):
                with self._asset_lock:
                    self._asset_status.pop(name, None)
                raise RuntimeError(f"OpenVoice V2 文件 {name} 校验失败，请重新下载本地模型")
            handle.seek(0)
            yield handle

    def model_size_mb(self) -> float:
        if not self.ckpt_path.is_file():
            return 0.0
        return round(self.ckpt_path.stat().st_size / (1024 * 1024), 1)

    def ensure_downloaded(
        self,
        progress_cb: Optional[Callable[[float, str], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> bool:
        """Download OpenVoice V2 converter weights (~125 MB) strictly into D drive."""
        with self._download_lock:
            self._check_download_cancelled(stop_event)
            if self.is_downloaded():
                if progress_cb:
                    progress_cb(1.0, "本地音色引擎已就绪")
                return True

            self.model_dir.mkdir(parents=True, exist_ok=True)
            for name, (size, digest, urls) in OPENVOICE_V2_ASSETS.items():
                self._check_download_cancelled(stop_event)
                if self._asset_is_ready(name):
                    continue
                last_error = None
                for url in urls:
                    try:
                        self._download_asset(name, size, digest, url, progress_cb, stop_event)
                        break
                    except InterruptedError:
                        raise
                    except (OSError, ValueError, http.client.HTTPException) as exc:
                        last_error = exc
                else:
                    reason = str(last_error) if last_error else "无可用下载地址"
                    raise RuntimeError(f"下载 {name} 失败: {reason}") from last_error

            if progress_cb:
                progress_cb(1.0, "本地音色引擎下载完成")
            return True

    @staticmethod
    def _check_download_cancelled(stop_event: Optional[threading.Event]) -> None:
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("下载已取消")

    def _download_asset(
        self,
        name: str,
        size: int,
        digest: str,
        url: str,
        progress_cb: Optional[Callable[[float, str], None]],
        stop_event: Optional[threading.Event],
    ) -> None:
        if progress_cb:
            progress_cb(0.05 if name == "checkpoint.pth" else 0.02, f"正在下载 {name}…")
        self._check_download_cancelled(stop_event)
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        sha256 = hashlib.sha256()
        done = 0
        last_emit = 0.0
        tmp = None
        try:
            with (
                urllib.request.urlopen(request, timeout=60) as response,
                tempfile.NamedTemporaryFile(dir=self.model_dir, suffix=".part", delete=False) as out,
            ):
                tmp = Path(out.name)
                while True:
                    self._check_download_cancelled(stop_event)
                    chunk = response.read(256 * 1024)
                    self._check_download_cancelled(stop_event)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > size:
                        raise ValueError("下载大小超过预期")
                    out.write(chunk)
                    sha256.update(chunk)
                    now = time.monotonic()
                    if progress_cb and (now - last_emit >= 0.15 or done == size):
                        pct = 0.05 + 0.94 * done / size if name == "checkpoint.pth" else 0.04
                        mb_done = done / (1024 * 1024)
                        mb_total = size / (1024 * 1024)
                        progress_cb(pct, f"下载音色模型 {mb_done:.1f} / {mb_total:.1f} MB")
                        last_emit = now
            if done != size or sha256.hexdigest() != digest:
                raise ValueError("文件大小或 SHA-256 校验失败")
            self._check_download_cancelled(stop_event)
            os.replace(tmp, self.model_dir / name)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def _load(self):
        with self._lock:
            if self._model is not None:
                return
            with self._open_verified_asset("config.json") as config:
                cfg = json.load(config)

            with self._open_verified_asset("checkpoint.pth") as checkpoint:
                import torch

                self._device = "cuda" if torch.cuda.is_available() else "cpu"
                ckpt = torch.load(checkpoint, map_location=self._device, weights_only=True)

            # Keep CPU threads polite so UI and browser stay silky responsive
            try:
                cpu_cnt = os.cpu_count() or 4
                torch.set_num_threads(max(2, min(6, cpu_cnt // 2)))
            except Exception:
                pass

            data_cfg = cfg["data"]
            model_cfg = cfg["model"]
            spec_channels = int(data_cfg["filter_length"]) // 2 + 1

            model_cls = _build_openvoice_v2_classes()
            model = model_cls(spec_channels, model_cfg).to(self._device)
            state_dict = ckpt.get("model", ckpt)
            missing, unexpected = model.load_state_dict(state_dict, strict=False)
            if missing:
                raise RuntimeError(f"OpenVoice V2 权重缺失关键参数: {missing[:5]}")
            model.eval()
            try:
                model.remove_weight_norm()
            except Exception:
                pass

            self._cfg = cfg
            self._model = model

    def release(self):
        """Free PyTorch weights between batches (similar to Whisper release)."""
        with self._lock:
            if self._model is not None:
                self._model = None
                self._window_cache.clear()
                try:
                    import gc
                    gc.collect()
                except Exception:
                    pass

    @property
    def sample_rate(self) -> int:
        if self._cfg:
            return int(self._cfg["data"]["sampling_rate"])
        return 22050

    def extract_speaker_embedding(self, audio_f32: np.ndarray):
        """Extract averaged normalized speaker embedding tensor [1, 256, 1] from active speech chunks."""
        self._load()
        import torch

        data_cfg = self._cfg["data"]
        sr = int(data_cfg["sampling_rate"])
        n_fft = int(data_cfg["filter_length"])
        hop = int(data_cfg["hop_length"])
        win = int(data_cfg["win_length"])

        chunks = extract_speech_chunks(audio_f32, sample_rate=sr)
        if not chunks:
            raise ValueError("音频中未检测到有效人声")

        gs = []
        with self._lock, torch.no_grad():
            for chunk in chunks:
                if chunk.size < n_fft + hop:
                    continue
                peak = float(np.max(np.abs(chunk)))
                if peak > 0.99:
                    chunk = chunk * (0.95 / peak)
                y = torch.from_numpy(chunk.astype(np.float32)).unsqueeze(0).to(self._device)
                spec = _spectrogram_torch(y, n_fft, hop, win, self._window_cache)
                g = self._model.ref_enc(spec.transpose(1, 2)).unsqueeze(-1)
                gs.append(g.detach().cpu())
        if not gs:
            raise ValueError("音频过短，无法提取有效声纹特征")
        return torch.stack(gs, dim=0).mean(dim=0)  # [1, 256, 1]

    def convert_waveform(
        self,
        src_audio: np.ndarray,
        tgt_se,
        tau: float = 0.35,
        src_se=None,
    ) -> Tuple[np.ndarray, int]:
        """Convert source speech waveform into target speaker's timbre.

        For long recordings (> 18s), splits at low-energy silence valleys and cross-fades
        to guarantee smooth fluency with bounded CPU memory.
        """
        self._load()
        import torch

        data_cfg = self._cfg["data"]
        sr = int(data_cfg["sampling_rate"])
        n_fft = int(data_cfg["filter_length"])
        hop = int(data_cfg["hop_length"])
        win = int(data_cfg["win_length"])
        tau = float(max(0.10, min(0.85, tau)))

        arr = np.asarray(src_audio, dtype=np.float32).reshape(-1)
        if arr.size < n_fft + hop:
            return arr, sr

        # Extract source speaker embedding once across the full clip for consistent timbre
        if src_se is None:
            src_se = self.extract_speaker_embedding(arr)

        src_se_dev = src_se.to(self._device)
        tgt_se_dev = tgt_se.to(self._device)

        # Split long clips (>18s) at quietest pause points to keep memory low and seamless
        max_seg = int(sr * 18.0)
        if arr.size <= max_seg:
            segments = [arr]
        else:
            segments = _split_at_silence_valleys(arr, sr, target_sec=14.0, overlap_sec=0.04)

        out_segments: List[np.ndarray] = []
        with self._lock, torch.no_grad():
            # Deterministic latent noise for consistent, stutter-free flow
            gen_state = torch.random.get_rng_state()
            torch.manual_seed(42)
            try:
                for seg in segments:
                    if seg.size < n_fft + hop:
                        out_segments.append(seg)
                        continue
                    y = torch.from_numpy(seg.astype(np.float32)).unsqueeze(0).to(self._device)
                    spec = _spectrogram_torch(y, n_fft, hop, win, self._window_cache)
                    spec_len = torch.LongTensor([spec.size(-1)]).to(self._device)
                    out = self._model.voice_conversion(
                        spec, spec_len, sid_src=src_se_dev, sid_tgt=tgt_se_dev, tau=tau
                    )
                    wav = out[0, 0].detach().cpu().float().numpy()
                    out_segments.append(wav)
            finally:
                torch.random.set_rng_state(gen_state)

        if len(out_segments) == 1:
            return out_segments[0], sr
        return _crossfade_concat(out_segments, int(sr * 0.03)), sr


def _split_at_silence_valleys(
    audio: np.ndarray,
    sr: int,
    target_sec: float = 14.0,
    overlap_sec: float = 0.04,
) -> List[np.ndarray]:
    """Split long audio near natural pause valleys so converted chunks join seamlessly."""
    n = audio.size
    step = int(target_sec * sr)
    win = int(0.08 * sr)
    overlap = int(overlap_sec * sr)
    segments = []
    start = 0
    while start < n:
        ideal_end = start + step
        if ideal_end >= n - int(2.5 * sr):
            segments.append(audio[start:n])
            break
        lo = max(start + int(6.0 * sr), ideal_end - int(3.0 * sr))
        hi = min(n - int(1.5 * sr), ideal_end + int(3.0 * sr))
        best_pos = ideal_end
        best_energy = float("inf")
        for pos in range(lo, hi, win // 2):
            e = float(np.mean(audio[pos : pos + win] ** 2))
            if e < best_energy:
                best_energy = e
                best_pos = pos + win // 2
        seg_end = min(n, best_pos + overlap)
        segments.append(audio[start:seg_end])
        start = max(start + 1, best_pos - overlap)
    return segments


def _crossfade_concat(segments: List[np.ndarray], fade_samples: int) -> np.ndarray:
    if not segments:
        return np.zeros(0, dtype=np.float32)
    out = segments[0].astype(np.float32)
    for nxt in segments[1:]:
        nxt = nxt.astype(np.float32)
        f = min(fade_samples, out.size // 2, nxt.size // 2)
        if f > 4:
            ramp_out = np.linspace(1.0, 0.0, f, dtype=np.float32)
            ramp_in = np.linspace(0.0, 1.0, f, dtype=np.float32)
            blended = out[-f:] * ramp_out + nxt[:f] * ramp_in
            out = np.concatenate([out[:-f], blended, nxt[f:]])
        else:
            out = np.concatenate([out, nxt])
    return out


# ---------------------------------------------------------------------------
# Microphone Recorder & Audio Playback (sounddevice + winsound fallback)
# ---------------------------------------------------------------------------

def list_input_microphones() -> List[Dict[str, Any]]:
    """Enumerate available microphone input devices via sounddevice."""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        default_in = sd.default.device[0] if isinstance(sd.default.device, (tuple, list)) else -1
        hostapis = sd.query_hostapis()
        # Prefer WASAPI / DirectSound / MME without duplicate spam
        seen_names = set()
        results = []
        # Sort so default device and WASAPI appear first
        indexed = list(enumerate(devices))
        def sort_key(pair):
            idx, d = pair
            api_name = hostapis[d["hostapi"]]["name"] if d["hostapi"] < len(hostapis) else ""
            is_def = 0 if idx == default_in else 1
            api_rank = 0 if "WASAPI" in api_name else 1 if "DirectSound" in api_name else 2 if "MME" in api_name else 3
            return (is_def, api_rank, idx)

        for idx, d in sorted(indexed, key=sort_key):
            if d.get("max_input_channels", 0) <= 0:
                continue
            name = str(d.get("name", "")).strip()
            if not name or any(skip in name for skip in ("Sound Mapper", "声音映射器", "Primary Sound Capture", "主声音捕获")):
                continue
            norm = name.lower()
            if norm in seen_names:
                continue
            seen_names.add(norm)
            results.append({
                "index": int(idx),
                "name": name,
                "isDefault": idx == default_in,
                "sampleRate": int(d.get("default_samplerate") or 22050),
            })
        return results
    except Exception:
        return []


class MicRecorder:
    """Non-blocking microphone recorder emitting real-time waveform bars."""

    BAR_COUNT = 28

    def __init__(self, sample_rate: int = 22050):
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._stream = None
        self._frames: List[np.ndarray] = []
        self._recording = False
        self._started_at = 0.0
        self._levels: List[float] = [0.06] * self.BAR_COUNT
        self._peak_level = 0.0
        self._actual_sr = sample_rate

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    @property
    def elapsed_seconds(self) -> float:
        with self._lock:
            if not self._recording:
                return 0.0
            return max(0.0, time.monotonic() - self._started_at)

    def get_levels(self) -> List[float]:
        with self._lock:
            return list(self._levels)

    def start(self, device_index: Optional[int] = None) -> None:
        import sounddevice as sd

        with self._lock:
            if self._recording:
                return
            self._frames = []
            self._levels = [0.06] * self.BAR_COUNT
            self._peak_level = 0.0

        # Query native sample rate for reliability on Windows WASAPI devices
        sr = self.sample_rate
        if device_index is not None and device_index >= 0:
            try:
                info = sd.query_devices(device_index)
                native_sr = int(info.get("default_samplerate") or sr)
                sr = native_sr if native_sr >= 16000 else sr
            except Exception:
                pass
        else:
            device_index = None
            try:
                info = sd.query_devices(kind="input")
                native_sr = int(info.get("default_samplerate") or sr)
                sr = native_sr if native_sr >= 16000 else sr
            except Exception:
                pass

        self._actual_sr = sr

        def callback(indata, frames, time_info, status):
            mono = indata[:, 0].copy() if indata.ndim > 1 else indata.reshape(-1).copy()
            rms = float(np.sqrt(np.mean(mono * mono) + 1e-12))
            # Smooth log-like level for natural visual bar height
            norm = min(1.0, max(0.06, math.pow(min(1.0, rms * 5.2), 0.62)))
            with self._lock:
                if self._recording:
                    self._frames.append(mono)
                    self._peak_level = max(self._peak_level, rms)
                    self._levels = self._levels[1:] + [round(norm, 3)]

        stream = sd.InputStream(
            device=device_index,
            samplerate=sr,
            channels=1,
            dtype="float32",
            blocksize=max(512, int(sr * 0.05)),
            callback=callback,
        )
        stream.start()
        with self._lock:
            self._stream = stream
            self._recording = True
            self._started_at = time.monotonic()

    def stop(self) -> Tuple[np.ndarray, int]:
        with self._lock:
            stream = self._stream
            self._stream = None
            self._recording = False
            frames = self._frames
            self._frames = []
            self._levels = [0.06] * self.BAR_COUNT
            sr = self._actual_sr

        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

        if not frames:
            return np.zeros(0, dtype=np.float32), sr
        return np.concatenate(frames, axis=0), sr


class AudioPlayer:
    """Non-blocking audio preview player (sounddevice primary, winsound WAV fallback)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._playing = False
        self._stop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._playing

    def stop(self):
        self._stop_flag.set()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        with self._lock:
            self._playing = False

    def play_audio(
        self,
        source: str | Path | bytes,
        on_finished: Optional[Callable[[], None]] = None,
    ) -> None:
        self.stop()
        self._stop_flag.clear()
        with self._lock:
            self._playing = True

        def worker():
            try:
                arr = decode_audio_to_float32(source, sample_rate=24000)
                if self._stop_flag.is_set() or arr.size == 0:
                    return
                try:
                    import sounddevice as sd
                    sd.play(arr, samplerate=24000, blocking=False)
                    dur = arr.size / 24000.0
                    deadline = time.monotonic() + dur + 0.15
                    while time.monotonic() < deadline and not self._stop_flag.is_set():
                        time.sleep(0.05)
                    if self._stop_flag.is_set():
                        sd.stop()
                except Exception:
                    if sys.platform == "win32" and not self._stop_flag.is_set():
                        import winsound
                        wav_bytes = encode_wav_bytes(arr, 24000)
                        winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)
            finally:
                with self._lock:
                    self._playing = False
                if on_finished:
                    try:
                        on_finished()
                    except Exception:
                        pass

        self._thread = threading.Thread(target=worker, daemon=True, name="voice-preview")
        self._thread.start()


def pick_audio_file_native(initial_dir: Optional[str] = None) -> str:
    """Open native Windows file dialog without requiring QtWidgets."""
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", wintypes.LPVOID),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", wintypes.LPVOID),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    buf = ctypes.create_unicode_buffer(4096)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.lpstrFilter = (
        "音频文件 (*.wav;*.mp3;*.m4a;*.flac;*.ogg;*.aac)\0"
        "*.wav;*.mp3;*.m4a;*.flac;*.ogg;*.aac\0"
        "所有文件 (*.*)\0*.*\0\0"
    )
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = len(buf)
    ofn.lpstrTitle = "选择你的声音样本（建议 6–30 秒清晰人声）"
    if initial_dir and os.path.isdir(initial_dir):
        ofn.lpstrInitialDir = initial_dir
    # OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR | OFN_EXPLORER
    ofn.Flags = 0x00001000 | 0x00000800 | 0x00000008 | 0x00080000
    if ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return buf.value
    return ""


# ---------------------------------------------------------------------------
# Voice Profile Manager (stored on D drive under voice_profiles/)
# ---------------------------------------------------------------------------

class VoiceProfileManager:
    """Manage user voice profiles: cleaning, F0 analysis, speaker embedding extraction & persistence."""

    DEFAULT_PROFILE_ID = "my_voice"
    _EMBEDDING_NAME = "target_se.f32"
    _EMBEDDING_SHAPE = (1, 256, 1)
    _EMBEDDING_DTYPE = np.dtype("<f4")

    def __init__(self, profiles_dir: Path = VOICE_PROFILES_DIR):
        self.profiles_dir = Path(profiles_dir)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

    def _profile_dir(self, profile_id: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", (profile_id or self.DEFAULT_PROFILE_ID).strip()) or self.DEFAULT_PROFILE_ID
        return self.profiles_dir / safe

    @classmethod
    def _read_cached_embedding(cls, path: Path) -> Optional[np.ndarray]:
        expected_size = math.prod(cls._EMBEDDING_SHAPE) * cls._EMBEDDING_DTYPE.itemsize
        try:
            with path.open("rb") as handle:
                data = handle.read(expected_size + 1)
        except OSError:
            return None
        if len(data) != expected_size:
            return None
        values = np.frombuffer(data, dtype=cls._EMBEDDING_DTYPE).reshape(cls._EMBEDDING_SHAPE)
        return values if np.isfinite(values).all() else None

    @classmethod
    def _save_cached_embedding(cls, path: Path, embedding) -> None:
        values = np.asarray(embedding.detach().cpu().numpy(), dtype=cls._EMBEDDING_DTYPE)
        if values.shape != cls._EMBEDDING_SHAPE or not np.isfinite(values).all():
            raise ValueError("提取的音色特征尺寸不符或包含无效数值")
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as handle:
                tmp = Path(handle.name)
                handle.write(values.tobytes())
            os.replace(tmp, path)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def get_profile(self, profile_id: str = DEFAULT_PROFILE_ID) -> Optional[Dict[str, Any]]:
        pdir = self._profile_dir(profile_id)
        meta_path = pdir / "profile.json"
        if not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        wav_path = pdir / "reference_clean.wav"
        se_path = pdir / self._EMBEDDING_NAME
        has_se = self._read_cached_embedding(se_path) is not None
        meta["id"] = pdir.name
        meta["wav_path"] = str(wav_path) if wav_path.is_file() else ""
        meta["se_path"] = str(se_path) if has_se else ""
        meta["has_wav"] = wav_path.is_file()
        meta["has_se"] = has_se
        meta["se_ready"] = has_se
        return meta

    def list_profiles(self) -> List[Dict[str, Any]]:
        items = []
        if not self.profiles_dir.is_dir():
            return items
        for sub in sorted(self.profiles_dir.iterdir()):
            if sub.is_dir() and (sub / "profile.json").is_file():
                info = self.get_profile(sub.name)
                if info:
                    items.append(info)
        return items

    def delete_profile(self, profile_id: str = DEFAULT_PROFILE_ID) -> bool:
        pdir = self._profile_dir(profile_id)
        if pdir.is_dir():
            shutil.rmtree(pdir, ignore_errors=True)
            return True
        return False

    def save_from_audio_file(
        self,
        source_path: str | Path,
        profile_id: str = DEFAULT_PROFILE_ID,
        name: str = "我的默认音色",
        source_type: str = "import",
        converter: Optional[OpenVoiceV2Converter] = None,
    ) -> Dict[str, Any]:
        """Clean, analyze, save reference WAV, and extract speaker embedding if model ready."""
        pdir = self._profile_dir(profile_id)
        pdir.mkdir(parents=True, exist_ok=True)
        raw_wav = pdir / "reference_raw.wav"
        clean_wav = pdir / "reference_clean.wav"
        se_path = pdir / self._EMBEDDING_NAME

        # 1. Decode original at 22050Hz mono
        raw_audio = decode_audio_to_float32(source_path, sample_rate=22050)
        if raw_audio.size < 22050:
            raise ValueError("音频长度不足 1 秒，请录制或导入 6–30 秒清晰人声")
        # Cap ultra-long imported files to 60 seconds (enough for optimal speaker embedding)
        if raw_audio.size > 22050 * 60:
            raw_audio = raw_audio[: 22050 * 60]
        write_wav_pcm16(raw_wav, raw_audio, 22050)

        # 2. Clean via FFmpeg: highpass + mild denoise + trim dead edge silence + normalize
        ffmpeg = find_ffmpeg()
        clean_audio = raw_audio
        if ffmpeg:
            af = (
                "highpass=f=70,"
                "afftdn=nf=-25:tn=1,"
                "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-42dB"
                ":stop_periods=-1:stop_duration=0.45:stop_threshold=-42dB,"
                "loudnorm=I=-18:TP=-1.5:LRA=11"
            )
            try:
                candidate = decode_audio_to_float32(raw_wav, sample_rate=22050, extra_af=af)
                if candidate.size >= int(22050 * 1.5):
                    clean_audio = candidate
            except Exception:
                # Fallback without silenceremove if silence threshold trimmed everything
                try:
                    af_safe = "highpass=f=70,afftdn=nf=-25:tn=1,loudnorm=I=-18:TP=-1.5:LRA=11"
                    candidate = decode_audio_to_float32(raw_wav, sample_rate=22050, extra_af=af_safe)
                    if candidate.size >= int(22050 * 1.0):
                        clean_audio = candidate
                except Exception:
                    pass
        se_path.unlink(missing_ok=True)
        write_wav_pcm16(clean_wav, clean_audio, 22050)

        # 3. Analyze F0, active duration, SNR, quality score
        metrics = estimate_f0_and_quality(clean_audio, sample_rate=22050)
        # Keep raw total duration for user clarity alongside active speech duration
        metrics["raw_duration"] = round(raw_audio.size / 22050.0, 2)

        # 4. Extract speaker embedding immediately if OpenVoice V2 checkpoint is ready
        se_ready = False
        if converter is not None and converter.is_downloaded():
            se = converter.extract_speaker_embedding(clean_audio)
            self._save_cached_embedding(se_path, se)
            se_ready = True

        meta = {
            "id": pdir.name,
            "name": name,
            "source_type": source_type,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": metrics["duration"],
            "raw_duration": metrics["raw_duration"],
            "active_duration": metrics["active_duration"],
            "f0_hz": metrics["f0_hz"],
            "gender_guess": metrics["gender_guess"],
            "snr_db": metrics["snr_db"],
            "quality_label": metrics["quality_label"],
            "quality_score": metrics["quality_score"],
            "warning": metrics["warning"],
            "se_ready": se_ready,
        }
        (pdir / "profile.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.get_profile(pdir.name)

    def save_from_waveform(
        self,
        waveform: np.ndarray,
        sample_rate: int,
        profile_id: str = DEFAULT_PROFILE_ID,
        name: str = "我的默认音色",
        converter: Optional[OpenVoiceV2Converter] = None,
    ) -> Dict[str, Any]:
        _ensure_dirs()
        tmp_wav = VOICE_TMP_DIR / f"mic_capture_{int(time.time() * 1000)}.wav"
        try:
            write_wav_pcm16(tmp_wav, waveform, sample_rate)
            return self.save_from_audio_file(
                tmp_wav,
                profile_id=profile_id,
                name=name,
                source_type="mic",
                converter=converter,
            )
        finally:
            try:
                if tmp_wav.exists():
                    tmp_wav.unlink()
            except OSError:
                pass

    def load_or_extract_se(self, profile_id: str, converter: OpenVoiceV2Converter):
        """Return cached target speaker embedding tensor, extracting on-demand if missing."""
        info = self.get_profile(profile_id)
        if not info or not info.get("has_wav"):
            raise RuntimeError("尚未录入使用者音色样本，请先在「我的音色」中录制或导入声音")
        pdir = self._profile_dir(profile_id)
        se_path = pdir / self._EMBEDDING_NAME
        cached = self._read_cached_embedding(se_path)
        if cached is not None:
            import torch

            return torch.from_numpy(cached.astype(np.float32, copy=True))
        # Legacy .pt caches stay on disk. Rebuild exclusively from the saved reference WAV.
        clean_audio = decode_audio_to_float32(info["wav_path"], sample_rate=22050)
        se = converter.extract_speaker_embedding(clean_audio)
        self._save_cached_embedding(se_path, se)
        # Update metadata flag
        meta_path = pdir / "profile.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["se_ready"] = True
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return se


# ---------------------------------------------------------------------------
# Unified Voice Clone Engine (Local OpenVoice V2 + Optional Cloud API)
# ---------------------------------------------------------------------------

class VoiceCloneEngine:
    """High-level facade integrating user timbre cloning into TTS and reference-audio playback."""

    CACHE_LIMIT = 24

    def __init__(
        self,
        enabled: bool = False,
        mode: str = "local",
        profile_id: str = VoiceProfileManager.DEFAULT_PROFILE_ID,
        tau: float = 0.35,
        base_gender: str = "auto",
        rate: str = "+0%",
        convert_reference: bool = True,
        proxy: str = "",
        api_url: str = "",
        api_key: str = "",
        api_model: str = "",
        profiles_dir: Path = VOICE_PROFILES_DIR,
        model_dir: Path = VOICE_MODELS_DIR,
    ):
        self.enabled = bool(enabled)
        self.mode = (mode or "local").strip().lower()
        self.profile_id = (profile_id or VoiceProfileManager.DEFAULT_PROFILE_ID).strip()
        self.tau = float(max(0.10, min(0.85, tau if tau is not None else 0.35)))
        self.base_gender = (base_gender or "auto").strip().lower()
        self.rate = (rate or "+0%").strip()
        self.convert_reference = bool(convert_reference)
        self.proxy = (proxy or "").strip()
        self.api_url = (api_url or "").strip()
        self.api_key = (api_key or "").strip()
        self.api_model = (api_model or "").strip()

        self.profiles = VoiceProfileManager(profiles_dir)
        self.converter = OpenVoiceV2Converter(model_dir)
        self.last_error = ""
        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[str, bytes]] = {}
        self._se_cache: Dict[str, Any] = {}

    def configure(self, **kwargs) -> None:
        """Hot-update runtime parameters from UI without restarting the app."""
        with self._lock:
            changed = False
            for k, v in kwargs.items():
                if hasattr(self, k) and getattr(self, k) != v:
                    setattr(self, k, v)
                    changed = True
            self.tau = float(max(0.10, min(0.85, self.tau)))
            if changed:
                self._cache.clear()
                self._se_cache.clear()

    def invalidate_profile_cache(self):
        with self._lock:
            self._cache.clear()
            self._se_cache.clear()

    def is_ready(self) -> bool:
        if not self.enabled:
            return False
        info = self.profiles.get_profile(self.profile_id)
        if not info or not info.get("has_wav"):
            return False
        if self.mode == "cloud":
            return bool(self.api_url and self.api_key)
        return self.converter.is_downloaded()

    def _effective_gender(self) -> str:
        if self.base_gender in ("male", "female"):
            return self.base_gender
        info = self.profiles.get_profile(self.profile_id)
        if info and info.get("gender_guess") in ("male", "female"):
            return info["gender_guess"]
        return "female"

    def _select_base_voices(self) -> Tuple[str, ...]:
        return BASE_VOICES_MALE if self._effective_gender() == "male" else BASE_VOICES_FEMALE

    def _get_target_se(self):
        with self._lock:
            cached = self._se_cache.get(self.profile_id)
        if cached is not None:
            return cached
        se = self.profiles.load_or_extract_se(self.profile_id, self.converter)
        with self._lock:
            self._se_cache[self.profile_id] = se
        return se

    def _synth_edge_base(self, text: str) -> bytes:
        """Generate crisp, fluent native English base speech via Edge-TTS."""
        import edge_tts

        _ensure_dirs()
        voices = self._select_base_voices()
        last_exc: Optional[Exception] = None
        for voice in voices:
            tmp_mp3 = VOICE_TMP_DIR / f"base_{int(time.time() * 1000)}_{os.getpid()}.mp3"
            kwargs: Dict[str, Any] = {}
            if self.rate and self.rate != "+0%":
                kwargs["rate"] = self.rate
            if self.proxy:
                kwargs["proxy"] = self.proxy
            try:
                async def _run():
                    comm = edge_tts.Communicate(text, voice, **kwargs)
                    await comm.save(str(tmp_mp3))

                try:
                    asyncio.run(_run())
                except TypeError:
                    async def _run_plain():
                        comm = edge_tts.Communicate(text, voice)
                        await comm.save(str(tmp_mp3))
                    asyncio.run(_run_plain())

                if tmp_mp3.is_file() and tmp_mp3.stat().st_size > 200:
                    return tmp_mp3.read_bytes()
            except Exception as exc:
                last_exc = exc
            finally:
                try:
                    if tmp_mp3.exists():
                        tmp_mp3.unlink()
                except OSError:
                    pass
        raise RuntimeError(f"基音 Edge-TTS 合成失败: {last_exc}")

    def _synth_cloud(self, text: str, out_format: str = "mp3") -> Tuple[str, bytes]:
        """Optional cloud zero-shot voice cloning (SiliconFlow CosyVoice2 / compatible endpoint)."""
        import base64

        info = self.profiles.get_profile(self.profile_id)
        if not info or not info.get("has_wav"):
            raise RuntimeError("未找到用户声音样本")
        if not self.api_url or not self.api_key:
            raise RuntimeError("云端克隆模式需要填写 API 地址和 API Key")

        ref_b64 = base64.b64encode(Path(info["wav_path"]).read_bytes()).decode("ascii")
        payload = {
            "model": self.api_model or "FunAudioLLM/CosyVoice2-0.5B",
            "input": text,
            "response_format": "mp3" if out_format == "mp3" else "wav",
            "reference_audio": f"data:audio/wav;base64,{ref_b64}",
        }
        req = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read()
        if not raw or len(raw) < 200:
            raise RuntimeError("云端语音克隆返回空数据")
        # Pass through broadcast finishing chain for uniform warmth and loudness
        arr = decode_audio_to_float32(raw, sample_rate=24000)
        return finish_cloned_audio(arr, sample_rate=24000, out_format=out_format)

    def synthesize(
        self,
        text: str,
        out_format: str = "mp3",
        force: bool = False,
    ) -> Tuple[str, bytes]:
        """Synthesize fluent English speech in the user's own timbre.

        Args:
            text: Clean English speech text.
            out_format: 'mp3' (for Unipus injection) or 'wav' (for low-latency preview).
            force: If True, run even when self.enabled is False (used by Voice Studio audition).
        """
        text = re.sub(r"\s+", " ", (text or "").strip())
        if not text:
            self.last_error = "朗读文本为空"
            return "", b""
        if not force and not self.enabled:
            return "", b""

        cache_key = hashlib.md5(
            f"tts|{self.mode}|{self.profile_id}|{self.tau:.2f}|{self.base_gender}|{self.rate}|{out_format}|{text}".encode()
        ).hexdigest()
        with self._lock:
            hit = self._cache.get(cache_key)
        if hit:
            return hit

        try:
            if self.mode == "cloud":
                fmt, data = self._synth_cloud(text, out_format=out_format)
            else:
                if not self.converter.is_downloaded():
                    self.last_error = "本地音色模型尚未下载"
                    return "", b""
                tgt_se = self._get_target_se()
                base_mp3 = self._synth_edge_base(text)
                src_wav = decode_audio_to_float32(base_mp3, sample_rate=self.converter.sample_rate)
                converted, sr = self.converter.convert_waveform(src_wav, tgt_se, tau=self.tau)
                fmt, data = finish_cloned_audio(converted, sample_rate=sr, out_format=out_format)

            if data:
                with self._lock:
                    if len(self._cache) >= self.CACHE_LIMIT:
                        self._cache.clear()
                    self._cache[cache_key] = (fmt, data)
                self.last_error = ""
                return fmt, data
            self.last_error = "音色克隆返回空音频"
            return "", b""
        except Exception as exc:
            self.last_error = f"音色克隆失败: {str(exc)[:140]}"
            return "", b""

    def convert_reference_audio(
        self,
        raw_audio_bytes: bytes,
        out_format: str = "mp3",
    ) -> Optional[bytes]:
        """Transfer user's timbre onto textbook native reference audio (follow-along / role-play).

        Preserves exact textbook pronunciation, timing, and cadence while sounding like the user.
        Returns original raw bytes if disabled/unavailable/failed so answering never breaks.
        """
        if not raw_audio_bytes or not self.enabled or not self.convert_reference:
            return raw_audio_bytes
        if self.mode != "local" or not self.converter.is_downloaded():
            return raw_audio_bytes

        h = hashlib.md5(raw_audio_bytes).hexdigest()
        cache_key = f"ref|{self.profile_id}|{self.tau:.2f}|{out_format}|{h}"
        with self._lock:
            hit = self._cache.get(cache_key)
        if hit:
            return hit[1]

        try:
            tgt_se = self._get_target_se()
            src_wav = decode_audio_to_float32(raw_audio_bytes, sample_rate=self.converter.sample_rate)
            # Ultra-short single-word clips (<0.45s) are padded gently so STFT/GRU stays clean
            if src_wav.size < int(self.converter.sample_rate * 0.45):
                pad = np.zeros(int(self.converter.sample_rate * 0.15), dtype=np.float32)
                src_wav = np.concatenate([pad, src_wav, pad])
            converted, sr = self.converter.convert_waveform(src_wav, tgt_se, tau=self.tau)
            fmt, data = finish_cloned_audio(converted, sample_rate=sr, out_format=out_format)
            if data:
                with self._lock:
                    if len(self._cache) >= self.CACHE_LIMIT:
                        self._cache.clear()
                    self._cache[cache_key] = (fmt, data)
                self.last_error = ""
                return data
        except Exception as exc:
            self.last_error = f"原声音色迁移失败，已回退原声: {str(exc)[:120]}"
        return raw_audio_bytes

    def release(self):
        self.converter.release()

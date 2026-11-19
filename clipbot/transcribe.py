"""Speech-to-text with faster-whisper (GPU if available) and word timestamps."""

import os
import re
import site
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np

from .config import FFMPEG, MODELS_DIR, settings

_model = None
_model_info: dict = {}
_lock = threading.Lock()

SAMPLE_RATE = 16000

# Typical Whisper hallucinations on silence/music.
_HALLUCINATIONS = re.compile(
    r"(thanks for watching|thank you for watching|subscribe|subtitles by|amara\.org|"
    r"mulțumesc pentru vizionare|multumesc pentru vizionare|abonează-te|aboneaza-te|"
    r"subtitrare|transcribed by)",
    re.I,
)


def _add_cuda_dlls() -> None:
    roots = [Path(p) for p in site.getsitepackages()] + [Path(sys.prefix) / "Lib" / "site-packages"]
    for root in roots:
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            p = root / "nvidia" / sub / "bin"
            if p.is_dir():
                try:
                    os.add_dll_directory(str(p))
                except (OSError, AttributeError):
                    pass
                if str(p) not in os.environ["PATH"]:
                    os.environ["PATH"] = str(p) + os.pathsep + os.environ["PATH"]


def load_audio(path: str, start: float | None = None, duration: float | None = None) -> np.ndarray:
    cmd = [FFMPEG, "-nostdin", "-v", "error"]
    if start:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", str(path)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=False).stdout
    return np.frombuffer(raw, np.float32).copy()


def get_model():
    global _model
    with _lock:
        if _model is not None:
            return _model
        _add_cuda_dlls()
        import ctranslate2
        from faster_whisper import WhisperModel

        cfg = settings()
        name = cfg.get("whisper_model", "large-v3-turbo")
        device = cfg.get("whisper_device", "auto")
        root = str(MODELS_DIR / "whisper")
        if device in ("auto", "cuda") and ctranslate2.get_cuda_device_count() > 0:
            for compute in ("int8_float16", "float16", "int8"):
                try:
                    m = WhisperModel(name, device="cuda", compute_type=compute, download_root=root)
                    list(m.transcribe(np.zeros(SAMPLE_RATE, np.float32), language="en")[0])
                    _model = m
                    _model_info.update(device="cuda", compute=compute, model=name)
                    return _model
                except Exception:
                    continue
        _model = WhisperModel(name, device="cpu", compute_type="int8", download_root=root)
        _model_info.update(device="cpu", compute="int8", model=name)
        return _model


def model_info() -> dict:
    return dict(_model_info)


def transcribe(audio: np.ndarray, language: str | None = None, offset: float = 0.0) -> dict:
    """Return {'language', 'segments': [...], 'words': [{'start','end','word'}]} with times shifted by offset."""
    model = get_model()
    if len(audio) < SAMPLE_RATE // 2:
        return {"language": language, "segments": [], "words": []}
    with _lock:
        segs, info = model.transcribe(
            audio,
            language=language or None,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
            beam_size=5,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        segs = list(segs)
    segments, words = [], []
    for s in segs:
        text = s.text.strip()
        if not text or _HALLUCINATIONS.search(text):
            continue
        if s.no_speech_prob > 0.7 and s.avg_logprob < -0.8:
            continue
        seg_words = []
        for w in s.words or []:
            token = w.word.strip()
            if not token:
                continue
            seg_words.append({
                "start": round(w.start + offset, 3),
                "end": round(max(w.end, w.start + 0.05) + offset, 3),
                "word": token,
            })
        if not seg_words:
            continue
        segments.append({"start": seg_words[0]["start"], "end": seg_words[-1]["end"], "text": text})
        words.extend(seg_words)
    return {"language": info.language, "segments": segments, "words": words}

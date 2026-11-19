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

_models: dict = {}
_model_info: dict = {}
_lock = threading.Lock()

SAMPLE_RATE = 16000
# eek/whisper-turbo-ro-en (MIT): Whisper turbo fine-tuned on spoken Romanian + Romglish, used for "ro" streamers.
RO_MODEL = MODELS_DIR / "whisper-ro" / "ctranslate2-int8"
RO_MODEL_NAME = "eek/whisper-turbo-ro-en"
_SPEAKER_TAG = re.compile(r"\[S\d+\]\s*")

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


def ro_model_available() -> bool:
    return (RO_MODEL / "model.bin").exists()


def get_model(language: str | None = None):
    key = "ro" if language == "ro" and ro_model_available() else "main"
    with _lock:
        if key in _models:
            return _models[key]
        _add_cuda_dlls()
        import ctranslate2
        from faster_whisper import WhisperModel

        cfg = settings()
        name = str(RO_MODEL) if key == "ro" else cfg.get("whisper_model", "large-v3-turbo")
        label = RO_MODEL_NAME if key == "ro" else name
        device = cfg.get("whisper_device", "auto")
        root = str(MODELS_DIR / "whisper")
        model, info = None, {}
        if device in ("auto", "cuda") and ctranslate2.get_cuda_device_count() > 0:
            for compute in ("int8_float16", "float16", "int8"):
                try:
                    m = WhisperModel(name, device="cuda", compute_type=compute, download_root=root)
                    list(m.transcribe(np.zeros(SAMPLE_RATE, np.float32), language="en")[0])
                    model, info = m, {"device": "cuda", "compute": compute, "model": label}
                    break
                except Exception:
                    continue
        if model is None:
            model = WhisperModel(name, device="cpu", compute_type="int8", download_root=root)
            info = {"device": "cpu", "compute": "int8", "model": label}
        _models[key] = model
        if key == "main":
            _model_info.update(info)
        else:
            _model_info["model_ro"] = label
        return model


def model_info() -> dict:
    info = dict(_model_info)
    if ro_model_available():
        info.setdefault("model_ro", RO_MODEL_NAME)
    return info


def _merge_attached(words: list[dict]) -> list[dict]:
    """Whisper splits Romanian clitics into their own words ("live" "-ul", "n" "-a"); glue them back."""
    out: list[dict] = []
    for w in words:
        tok = w["word"]
        attach = out and ((len(tok) > 1 and tok[0] in "-‑" and tok[1].isalpha() and out[-1]["word"][-1].isalnum())
                          or (tok in ("+", "%") and out[-1]["word"][-1].isdigit()))
        if attach:
            out[-1] = dict(out[-1], word=out[-1]["word"] + tok, end=max(out[-1]["end"], w["end"]))
        else:
            out.append(w)
    return out


def transcribe(audio: np.ndarray, language: str | None = None, offset: float = 0.0) -> dict:
    """Return {'language', 'segments': [...], 'words': [{'start','end','word'}]} with times shifted by offset."""
    model = get_model(language)
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
        text = _SPEAKER_TAG.sub("", s.text).strip()
        if not text or _HALLUCINATIONS.search(text):
            continue
        if s.no_speech_prob > 0.7 and s.avg_logprob < -0.8:
            continue
        seg_words = []
        for w in s.words or []:
            token = _SPEAKER_TAG.sub("", w.word).strip()
            if not token:
                continue
            seg_words.append({
                "start": round(w.start + offset, 3),
                "end": round(max(w.end, w.start + 0.05) + offset, 3),
                "word": token,
            })
        seg_words = _merge_attached(seg_words)
        if not seg_words:
            continue
        segments.append({"start": seg_words[0]["start"], "end": seg_words[-1]["end"], "text": text})
        words.extend(seg_words)
    return {"language": info.language, "segments": segments, "words": words}

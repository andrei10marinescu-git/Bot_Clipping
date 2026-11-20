import json
import os
import shutil
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CLIPS_DIR = DATA_DIR / "clips"
WORK_DIR = DATA_DIR / "work"
LIVE_DIR = DATA_DIR / "live"
SOURCES_DIR = DATA_DIR / "sources"  # raw source of each clip, kept so its title can be edited later
LOG_DIR = DATA_DIR / "logs"
MODELS_DIR = ROOT / "models"
TOOLS_DIR = ROOT / "tools"
FONTS_DIR = ROOT / "assets" / "fonts"
WEB_DIR = ROOT / "web"
CONFIG_PATH = ROOT / "config.json"
DEFAULT_CONFIG_PATH = ROOT / "config.default.json"
DB_PATH = DATA_DIR / "clips.db"

for _d in (DATA_DIR, CLIPS_DIR, WORK_DIR, LIVE_DIR, SOURCES_DIR, LOG_DIR, MODELS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# config.json is the local, editable copy (not in git); a fresh checkout starts from the safe defaults.
if not CONFIG_PATH.exists() and DEFAULT_CONFIG_PATH.exists():
    shutil.copyfile(DEFAULT_CONFIG_PATH, CONFIG_PATH)

# Keep every download/cache inside the project folder.
os.environ.setdefault("HF_HOME", str(MODELS_DIR / "huggingface"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
os.environ.setdefault("ORT_LOGGING_LEVEL", "3")
os.environ["PATH"] = str(TOOLS_DIR) + os.pathsep + os.environ.get("PATH", "")

FFMPEG = shutil.which("ffmpeg") or str(TOOLS_DIR / "ffmpeg.exe")
FFPROBE = shutil.which("ffprobe") or str(TOOLS_DIR / "ffprobe.exe")
DENO = TOOLS_DIR / "deno.exe"

DEFAULT_SETTINGS = {
    "whisper_model": "large-v3-turbo",
    "whisper_device": "auto",
    "max_clip_seconds": 50,
    "min_clip_seconds": 20,
    "clips_per_video": 4,
    "clips_per_stream": 8,
    "videos_per_scan": 1,
    "skip_videos_shorter_than": 90,
    "max_video_age_days": 7,
    "auto_scan": False,
    "scan_interval_minutes": 60,
    "auto_live": False,
    "live_check_interval_seconds": 120,
    "live_cooldown_seconds": 90,
    "live_sensitivity": 1.0,
    "live_max_clips_per_hour": 0,
    "live_best_of_minutes": 12,
    "youtube_chat_replay_max_hours": 3,
    "output_fps": 30,
    "encoder": "auto",
    "share_lan": False,
    "ai_titles": True,
    "cold_open": True,
    "keep_sources_days": 14,
    "share_internet": False,
    "public_port": 8766,
}

_lock = threading.RLock()


def load_config() -> dict:
    with _lock:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    merged = dict(DEFAULT_SETTINGS)
    merged.update(cfg.get("settings") or {})
    cfg["settings"] = merged
    cfg.setdefault("streamers", [])
    return cfg


def save_config(cfg: dict) -> None:
    with _lock:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)


def settings() -> dict:
    return load_config()["settings"]


def main_clip_max(cfg: dict) -> float:
    """Longest clip body: the cold open (up to ~3.5 s) goes in front and the whole video stays <= max_clip_seconds."""
    return float(cfg["max_clip_seconds"]) - (3.5 if cfg.get("cold_open", True) else 0.0)


def get_streamer(streamer_id: str) -> dict | None:
    for s in load_config()["streamers"]:
        if s["id"] == streamer_id:
            return s
    return None

"""FastAPI server: REST API for the UI + background schedulers."""

import re
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, pipeline, sources, transcribe
from .config import CLIPS_DIR, WEB_DIR, get_streamer, load_config, save_config
from .jobs import logger, manager
from .live import get_live_manager
from .share import share_manager

SETTING_LIMITS = {
    "max_clip_seconds": (10, 50),
    "min_clip_seconds": (5, 30),
    "clips_per_video": (1, 15),
    "clips_per_stream": (1, 30),
    "videos_per_scan": (1, 5),
    "skip_videos_shorter_than": (0, 3600),
    "max_video_age_days": (1, 365),
    "scan_interval_minutes": (5, 1440),
    "live_check_interval_seconds": (30, 1800),
    "live_cooldown_seconds": (20, 1800),
    "live_sensitivity": (0.3, 5.0),
    "live_max_clips_per_hour": (0, 60),
    "youtube_chat_replay_max_hours": (0, 24),
    "output_fps": (24, 60),
}
BOOL_SETTINGS = ("auto_scan", "auto_live")


def _scheduler() -> None:
    last_scan = time.time()
    last_cleanup = 0.0
    while True:
        try:
            if time.time() - last_cleanup > 3600:
                last_cleanup = time.time()
                removed = pipeline.cleanup_sources()
                if removed:
                    logger.info("removed %d old clip sources", removed)
                for st in load_config()["streamers"]:
                    if st.get("youtube"):
                        sources.yt_subscribers(st["youtube"])
            cfg = load_config()
            s = cfg["settings"]
            if s.get("auto_scan") and time.time() - last_scan >= float(s["scan_interval_minutes"]) * 60:
                last_scan = time.time()
                for st in cfg["streamers"]:
                    try:
                        n = pipeline.scan_streamer(st["id"])
                        logger.info("auto scan %s: %d queued", st["id"], n)
                    except Exception:  # noqa: BLE001
                        logger.exception("auto scan %s", st["id"])
            elif not s.get("auto_scan"):
                last_scan = 0
        except Exception:  # noqa: BLE001
            logger.exception("scheduler")
        time.sleep(20)


def _preload_model() -> None:
    try:
        transcribe.get_model()
        if any(s.get("language") == "ro" for s in load_config()["streamers"]):
            transcribe.get_model("ro")
    except Exception:  # noqa: BLE001
        logger.exception("whisper preload failed")


def _startup_tasks() -> None:
    try:
        share_manager.apply()
    except Exception:  # noqa: BLE001
        logger.exception("share start failed")
    try:
        pipeline.submit_title_upgrade()
    except Exception:  # noqa: BLE001
        logger.exception("title upgrade not started")


@asynccontextmanager
async def lifespan(_app):
    get_live_manager()
    threading.Thread(target=_scheduler, daemon=True, name="scheduler").start()
    threading.Thread(target=_preload_model, daemon=True, name="whisper-preload").start()
    threading.Thread(target=_startup_tasks, daemon=True, name="startup").start()
    yield


app = FastAPI(title="ClipBot", lifespan=lifespan)


@app.middleware("http")
async def _revalidate_ui(request, call_next):
    # Browsers must re-check app.js / index.html after an update, otherwise new buttons stay invisible.
    resp = await call_next(request)
    if not request.url.path.startswith("/media"):
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


class ProcessReq(BaseModel):
    streamer: str
    url: str
    max_clips: int | None = None


class WatchReq(BaseModel):
    on: bool


class ShareReq(BaseModel):
    lan: bool | None = None
    internet: bool | None = None


class TitleReq(BaseModel):
    title: str


class StreamerReq(BaseModel):
    id: str | None = None
    name: str
    youtube: str = ""
    kick: str = ""
    language: str = "en"
    color: str = "#53fc18"
    crop_sides: float = 0.0
    layout: str = "auto"
    show_subs: bool = False


@app.get("/api/state")
def api_state():
    cfg = load_config()
    counts = db.clip_counts()
    live = get_live_manager().status()
    return {
        "settings": cfg["settings"],
        "streamers": [{**s, "clips": counts.get(s["id"], 0), "live": live.get(s["id"]),
                       "subs": sources.cached_subscribers(s.get("youtube"))} for s in cfg["streamers"]],
        "jobs": manager.list(),
        "whisper": transcribe.model_info(),
    }


@app.get("/api/clips")
def api_clips(streamer: str | None = None, limit: int = 600):
    out = db.list_clips(streamer, limit)
    for c in out:
        f = CLIPS_DIR / (c.get("file") or "_")
        c["v"] = int(f.stat().st_mtime) if f.exists() else 0
        c["title_editable"] = pipeline.title_editable(c)
    return out


@app.post("/api/clips/{clip_id}/title")
def api_clip_title(clip_id: str, req: TitleReq):
    clip = db.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clipul nu există")
    title = " ".join(req.title.split())
    if not 2 <= len(title) <= 90:
        raise HTTPException(400, "Titlul trebuie să aibă între 2 și 90 de caractere.")
    if not pipeline.title_editable(clip):
        raise HTTPException(400, "Clip live mai vechi: sursa lui nu a fost păstrată, titlul din video nu se mai poate schimba.")
    job = pipeline.submit_retitle(clip_id, title)
    return {"job": job.id}


@app.get("/api/share")
def api_share():
    return share_manager.status()


@app.post("/api/share")
def api_share_set(req: ShareReq):
    threading.Thread(target=share_manager.set, kwargs={"lan": req.lan, "internet": req.internet},
                     daemon=True, name="share-apply").start()
    time.sleep(1.2)
    return share_manager.status()


@app.delete("/api/clips/{clip_id}")
def api_delete_clip(clip_id: str):
    clip = db.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clipul nu există")
    for key in ("file", "thumb", "srt"):
        if clip.get(key):
            (CLIPS_DIR / clip[key]).unlink(missing_ok=True)
    pipeline.delete_source(clip)
    db.delete_clip(clip_id)
    return {"ok": True}


@app.get("/api/streamers/{streamer_id}/videos")
def api_videos(streamer_id: str):
    if not get_streamer(streamer_id):
        raise HTTPException(404, "Streamer necunoscut")
    return pipeline.list_recent(streamer_id, limit=6)


@app.get("/api/sources")
def api_sources(streamer: str | None = None):
    return db.list_sources(streamer)


@app.post("/api/process")
def api_process(req: ProcessReq):
    if not get_streamer(req.streamer):
        raise HTTPException(404, "Streamer necunoscut")
    if not re.search(r"(youtube\.com|youtu\.be|kick\.com/[^/]+/videos/)", req.url):
        raise HTTPException(400, "Link invalid: folosește un link YouTube sau un VOD Kick (kick.com/<canal>/videos/<id>).")
    job = pipeline.submit_vod(req.streamer, req.url, max_clips=req.max_clips)
    return {"job": job.id}


@app.post("/api/streamers/{streamer_id}/scan")
def api_scan(streamer_id: str):
    if not get_streamer(streamer_id):
        raise HTTPException(404, "Streamer necunoscut")
    return {"queued": pipeline.scan_streamer(streamer_id)}


@app.post("/api/streamers/{streamer_id}/watch")
def api_watch(streamer_id: str, req: WatchReq):
    if not get_streamer(streamer_id):
        raise HTTPException(404, "Streamer necunoscut")
    get_live_manager().set_watch(streamer_id, req.on)
    return {"ok": True}


@app.post("/api/live/check")
def api_live_check():
    get_live_manager().check_now()
    return {"ok": True}


@app.post("/api/settings")
def api_settings(values: dict):
    cfg = load_config()
    s = cfg["settings"]
    for k, v in values.items():
        if k in BOOL_SETTINGS:
            s[k] = bool(v)
        elif k in SETTING_LIMITS:
            lo, hi = SETTING_LIMITS[k]
            try:
                num = float(v)
            except (TypeError, ValueError):
                raise HTTPException(400, f"Valoare invalidă pentru {k}")
            num = max(lo, min(hi, num))
            s[k] = num if isinstance(lo, float) or isinstance(hi, float) else int(num)
    if s["min_clip_seconds"] >= s["max_clip_seconds"]:
        s["min_clip_seconds"] = max(5, s["max_clip_seconds"] - 5)
    save_config(cfg)
    get_live_manager().check_now()
    return s


@app.post("/api/streamers")
def api_save_streamer(req: StreamerReq):
    cfg = load_config()
    sid = req.id or re.sub(r"[^a-z0-9]+", "", req.name.lower()) or f"s{int(time.time())}"
    data = req.model_dump()
    data["id"] = sid
    data["name"] = req.name.strip()
    data["crop_sides"] = max(0.0, min(0.3, float(req.crop_sides)))
    data["layout"] = req.layout if req.layout in ("auto", "split", "crop", "blur") else "auto"
    data["kick"] = data["kick"].strip().rstrip("/").rsplit("/", 1)[-1] if data["kick"] else ""
    try:
        data["youtube"] = sources.normalize_youtube(data["youtube"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Nu am putut citi canalul YouTube: {e}")
    for s in cfg["streamers"]:
        if s["id"] == sid:
            s.update(data)
            break
    else:
        cfg["streamers"].append(data)
    save_config(cfg)
    get_live_manager().check_now()
    return data


@app.delete("/api/streamers/{streamer_id}")
def api_delete_streamer(streamer_id: str):
    cfg = load_config()
    cfg["streamers"] = [s for s in cfg["streamers"] if s["id"] != streamer_id]
    save_config(cfg)
    get_live_manager().stop_streamer(streamer_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel(job_id: str):
    if not manager.cancel(job_id):
        raise HTTPException(404, "Job inexistent")
    return {"ok": True}


app.mount("/media", StaticFiles(directory=CLIPS_DIR), name="media")
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

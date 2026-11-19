"""Public, read-only gallery: visitors can watch and download clips, nothing else.

It is a separate app on its own port, so the admin panel (port 8765, localhost only) is never exposed.
"""

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import CLIPS_DIR, WEB_DIR, load_config

PUBLIC_FIELDS = ("id", "streamer", "platform", "source_kind", "source_url", "source_title", "duration",
                 "title", "transcript", "language", "file", "thumb", "srt", "created_at")

app = FastAPI(title="ClipBot – galerie", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/api/streamers")
def streamers():
    counts = db.clip_counts()
    return [
        {"id": s["id"], "name": s["name"], "color": s.get("color") or "#53fc18", "clips": counts.get(s["id"], 0),
         "youtube": s.get("youtube") or "", "kick": s.get("kick") or ""}
        for s in load_config()["streamers"]
    ]


@app.get("/api/clips")
def clips(streamer: str | None = None, limit: int = 600):
    out = []
    for c in db.list_clips(streamer, max(1, min(limit, 1000))):
        f = CLIPS_DIR / (c.get("file") or "")
        if not c.get("file") or not f.exists():
            continue
        item = {k: c.get(k) for k in PUBLIC_FIELDS}
        item["keywords"] = (c.get("effects") or {}).get("keywords") or []
        item["v"] = int(f.stat().st_mtime)
        out.append(item)
    return out


@app.get("/style.css", include_in_schema=False)
def style():
    return FileResponse(WEB_DIR / "style.css", media_type="text/css")


app.mount("/media", StaticFiles(directory=CLIPS_DIR), name="media")
app.mount("/", StaticFiles(directory=WEB_DIR / "public", html=True), name="gallery")

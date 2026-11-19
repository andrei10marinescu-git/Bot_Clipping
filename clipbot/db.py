import json
import sqlite3
import threading
import time

from .config import DB_PATH

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.executescript(
    """
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    streamer TEXT NOT NULL,
    platform TEXT,
    source_kind TEXT,
    source_id TEXT,
    source_url TEXT,
    source_title TEXT,
    start REAL,
    end REAL,
    duration REAL,
    score REAL,
    reasons TEXT,
    title TEXT,
    transcript TEXT,
    words TEXT,
    language TEXT,
    effects TEXT,
    file TEXT,
    thumb TEXT,
    srt TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_clips_streamer ON clips(streamer, created_at);
CREATE TABLE IF NOT EXISTS sources (
    key TEXT PRIMARY KEY,
    streamer TEXT,
    platform TEXT,
    title TEXT,
    url TEXT,
    status TEXT,
    clips INTEGER DEFAULT 0,
    error TEXT,
    updated_at REAL
);
"""
)
_conn.commit()

_JSON_FIELDS = ("reasons", "words", "effects")


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in _JSON_FIELDS:
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def add_clip(clip: dict) -> None:
    clip = dict(clip)
    clip.setdefault("created_at", time.time())
    for k in _JSON_FIELDS:
        if k in clip and not isinstance(clip[k], str):
            clip[k] = json.dumps(clip[k], ensure_ascii=False)
    cols = ",".join(clip)
    marks = ",".join("?" for _ in clip)
    with _lock:
        _conn.execute(f"INSERT OR REPLACE INTO clips ({cols}) VALUES ({marks})", tuple(clip.values()))
        _conn.commit()


def list_clips(streamer: str | None = None, limit: int = 300) -> list[dict]:
    with _lock:
        if streamer:
            rows = _conn.execute(
                "SELECT * FROM clips WHERE streamer=? ORDER BY created_at DESC LIMIT ?", (streamer, limit)
            ).fetchall()
        else:
            rows = _conn.execute("SELECT * FROM clips ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def get_clip(clip_id: str) -> dict | None:
    with _lock:
        r = _conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    return _row(r) if r else None


def delete_clip(clip_id: str) -> None:
    with _lock:
        _conn.execute("DELETE FROM clips WHERE id=?", (clip_id,))
        _conn.commit()


def clip_counts() -> dict:
    with _lock:
        rows = _conn.execute("SELECT streamer, COUNT(*) AS n FROM clips GROUP BY streamer").fetchall()
    return {r["streamer"]: r["n"] for r in rows}


def get_source(key: str) -> dict | None:
    with _lock:
        r = _conn.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
    return dict(r) if r else None


def set_source(key: str, **fields) -> None:
    fields["updated_at"] = time.time()
    with _lock:
        if _conn.execute("SELECT 1 FROM sources WHERE key=?", (key,)).fetchone():
            sets = ",".join(f"{k}=?" for k in fields)
            _conn.execute(f"UPDATE sources SET {sets} WHERE key=?", (*fields.values(), key))
        else:
            fields["key"] = key
            cols = ",".join(fields)
            marks = ",".join("?" for _ in fields)
            _conn.execute(f"INSERT INTO sources ({cols}) VALUES ({marks})", tuple(fields.values()))
        _conn.commit()


def list_sources(streamer: str | None = None, limit: int = 50) -> list[dict]:
    with _lock:
        if streamer:
            rows = _conn.execute(
                "SELECT * FROM sources WHERE streamer=? ORDER BY updated_at DESC LIMIT ?", (streamer, limit)
            ).fetchall()
        else:
            rows = _conn.execute("SELECT * FROM sources ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

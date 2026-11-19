"""Data sources: YouTube (yt-dlp) and Kick (unofficial web API via curl_cffi)."""

import re
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urljoin

import yt_dlp
from curl_cffi import requests as creq

from .config import DENO

_tls = threading.local()

KICK_PUSHER_FALLBACK = ("34bf7a0ff419a2a775b9", "us2")


def http():
    s = getattr(_tls, "session", None)
    if s is None:
        s = _tls.session = creq.Session(impersonate="chrome", timeout=25)
    return s


def ydl_opts(**extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 10,
        "extractor_retries": 3,
    }
    if DENO.exists():
        opts["js_runtimes"] = {"deno": {"path": str(DENO)}}
    opts.update(extra)
    return opts


def _parse_ts(value) -> float | None:
    if not value:
        return None
    s = str(value).replace("Z", "+00:00")
    if "T" not in s:
        s = s.replace(" ", "T") + "+00:00"
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------- HLS helpers

def hls_variants(master_url: str) -> list[dict]:
    txt = http().get(master_url).text
    lines = txt.splitlines()
    out = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        bw = re.search(r"BANDWIDTH=(\d+)", line)
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j].startswith("#")):
            j += 1
        if j < len(lines):
            out.append({
                "url": urljoin(master_url, lines[j].strip()),
                "height": int(res.group(2)) if res else 0,
                "bandwidth": int(bw.group(1)) if bw else 0,
            })
    return out


def pick_variant(master_url: str, max_height: int = 1080, lowest: bool = False) -> str:
    try:
        variants = hls_variants(master_url)
    except Exception:
        return master_url
    if not variants:
        return master_url
    if lowest:
        return min(variants, key=lambda v: v["bandwidth"])["url"]
    ok = [v for v in variants if v["height"] <= max_height] or variants
    return max(ok, key=lambda v: (v["height"], v["bandwidth"]))["url"]


# ---------------------------------------------------------------- Kick

def kick_get(path: str):
    r = http().get(f"https://kick.com/api/{path}", headers={"Accept": "application/json"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def kick_channel(slug: str) -> dict | None:
    return kick_get(f"v2/channels/{slug}")


def kick_live(slug: str) -> dict | None:
    ch = kick_channel(slug)
    if not ch:
        return None
    ls = ch.get("livestream")
    if not ls or not ls.get("is_live") or not ch.get("playback_url"):
        return None
    return {
        "platform": "kick",
        "id": str(ls["id"]),
        "title": ls.get("session_title") or "",
        "url": f"https://kick.com/{slug}",
        "hls": ch["playback_url"],
        "chatroom_id": (ch.get("chatroom") or {}).get("id"),
        "channel_id": ch.get("id"),
        "viewers": ls.get("viewer_count"),
        "started": _parse_ts(ls.get("start_time")),
    }


def _kick_video_item(slug: str, v: dict) -> dict | None:
    vid = (v.get("video") or {}).get("uuid")
    if not vid or not v.get("source") or v.get("is_live"):
        return None
    return {
        "platform": "kick",
        "kind": "stream",
        "id": vid,
        "slug": slug,
        "livestream_id": str(v.get("id")),
        "title": v.get("session_title") or "",
        "url": f"https://kick.com/{slug}/videos/{vid}",
        "duration": (v.get("duration") or 0) / 1000,
        "hls": v["source"],
        "timestamp": _parse_ts(v.get("start_time")),
        "views": v.get("views"),
        "thumbnail": ((v.get("thumbnail") or {}).get("src") if isinstance(v.get("thumbnail"), dict) else None),
    }


def kick_videos(slug: str, limit: int = 5) -> list[dict]:
    data = kick_get(f"v2/channels/{slug}/videos") or []
    out = []
    for v in data:
        item = _kick_video_item(slug, v)
        if item:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def kick_video(uuid_: str) -> dict | None:
    v = kick_get(f"v1/video/{uuid_}")
    if not v:
        return None
    ls = v.get("livestream") or {}
    slug = ((ls.get("channel") or {}).get("slug")) or ""
    return {
        "platform": "kick",
        "kind": "stream",
        "id": uuid_,
        "slug": slug,
        "livestream_id": str(v.get("live_stream_id") or ls.get("id") or ""),
        "title": ls.get("session_title") or "",
        "url": f"https://kick.com/{slug}/videos/{uuid_}",
        "duration": (ls.get("duration") or 0) / 1000,
        "hls": v.get("source"),
        "timestamp": _parse_ts(ls.get("start_time")),
    }


def kick_clip_moments(slug: str, livestream_id: str, stream_start: float | None) -> list[tuple[float, float]]:
    """Community clips made on Kick for a given stream -> (seconds into VOD, weight)."""
    if not stream_start:
        return []
    moments = []
    for period in ("week", "month"):
        cursor = None
        for _ in range(4):
            q = f"v2/channels/{slug}/clips?sort=view&time={period}" + (f"&cursor={cursor}" if cursor else "")
            try:
                data = kick_get(q) or {}
            except Exception:
                break
            for c in data.get("clips") or []:
                if str(c.get("livestream_id")) != str(livestream_id):
                    continue
                created = _parse_ts(c.get("created_at"))
                if not created:
                    continue
                dur = float(c.get("duration") or 30)
                # Clips cover the moments right before they were created (plus ~5s stream delay).
                t = created - stream_start - 5 - dur / 2
                views = float(c.get("views") or c.get("view_count") or 0)
                moments.append((t, views))
            cursor = data.get("nextCursor") or data.get("next_cursor")
            if not cursor:
                break
        if moments:
            break
    return moments


def kick_pusher_credentials(channel_id: int) -> tuple[str, str]:
    """Kick hands out the realtime (Pusher) app key dynamically."""
    body = {
        "client": {"id": str(uuid.uuid4()), "type": "web"},
        "capabilities": {"accepted_providers": [{"provider": "pusher"}]},
    }
    try:
        r = http().post(
            f"https://web.kick.com/api/v1/realtime/channels/{channel_id}/chat/connection",
            json=body,
            headers={"Origin": "https://kick.com"},
        )
        for conn in (r.json().get("data") or {}).get("connections") or []:
            cred = conn.get("credentials") or {}
            if conn.get("provider") == "pusher" and cred.get("app_key"):
                return cred["app_key"], cred.get("cluster") or "us2"
    except Exception:
        pass
    return KICK_PUSHER_FALLBACK


# ---------------------------------------------------------------- YouTube

def yt_base(channel: str) -> str:
    channel = channel.strip().rstrip("/")
    if channel.startswith("http"):
        return channel
    if channel.startswith("UC") and len(channel) == 24:
        return f"https://www.youtube.com/channel/{channel}"
    return f"https://www.youtube.com/{channel if channel.startswith('@') else '@' + channel}"


def normalize_youtube(value: str) -> str:
    """Accept @handle, channel id, channel URL or even a video URL and return '@handle' or 'UC…' id."""
    v = (value or "").strip()
    if not v:
        return ""
    m = re.search(r"youtube\.com/(@[\w.\-]+)", v)
    if m:
        return m.group(1)
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", v)
    if m:
        return m.group(1)
    if re.search(r"(youtube\.com/(watch|shorts|live)|youtu\.be/)", v):
        with yt_dlp.YoutubeDL(ydl_opts()) as y:
            info = y.extract_info(v, download=False, process=False)
        handle = info.get("uploader_id") or ""
        return handle if handle.startswith("@") else (info.get("channel_id") or v)
    if v.startswith("UC") and len(v) == 24:
        return v
    return v if v.startswith("@") else "@" + v


def yt_channel_url(channel: str) -> str:
    return yt_base(channel)


def yt_latest(channel: str, limit: int = 4) -> list[dict]:
    out = []
    for tab, kind in (("streams", "stream"), ("videos", "video")):
        try:
            with yt_dlp.YoutubeDL(ydl_opts(extract_flat="in_playlist", playlistend=limit)) as y:
                info = y.extract_info(f"{yt_base(channel)}/{tab}", download=False)
        except yt_dlp.utils.DownloadError:
            continue
        for e in info.get("entries") or []:
            if not e.get("id") or e.get("live_status") in ("is_live", "is_upcoming", "post_live"):
                continue
            thumbs = e.get("thumbnails") or []
            out.append({
                "platform": "youtube",
                "kind": kind,
                "id": e["id"],
                "title": e.get("title") or "",
                "url": f"https://www.youtube.com/watch?v={e['id']}",
                "duration": e.get("duration"),
                "views": e.get("view_count"),
                "thumbnail": thumbs[-1]["url"] if thumbs else None,
            })
    return out


def yt_live(channel: str) -> dict | None:
    try:
        with yt_dlp.YoutubeDL(ydl_opts()) as y:
            info = y.extract_info(f"{yt_base(channel)}/live", download=False)
    except yt_dlp.utils.DownloadError:
        return None
    if not info or not info.get("is_live"):
        return None
    hls = [f for f in info.get("formats") or [] if str(f.get("protocol", "")).startswith("m3u8")]
    muxed = [f for f in hls if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")]
    video = [f for f in hls if f.get("vcodec") not in (None, "none")]
    audio = [f for f in hls if f.get("vcodec") == "none"]
    pool = muxed or video
    if not pool:
        return None
    ok = [f for f in pool if (f.get("height") or 0) <= 1080] or pool
    best = max(ok, key=lambda f: ((f.get("height") or 0), f.get("tbr") or 0))
    best_audio = None
    if not muxed and audio:
        best_audio = max(audio, key=lambda f: (f.get("abr") or f.get("tbr") or 0, f.get("format_id") or ""))
    return {
        "platform": "youtube",
        "id": info["id"],
        "title": re.sub(r"\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", "", info.get("title") or ""),
        "url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={info['id']}",
        "hls": best["url"],
        "hls_audio": best_audio["url"] if best_audio else None,
        "viewers": info.get("concurrent_view_count"),
        "started": info.get("release_timestamp"),
    }


def yt_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(ydl_opts()) as y:
        return y.extract_info(url, download=False)


_subs_cache: dict[str, tuple[float, int | None]] = {}


def yt_subscribers(channel: str, max_age: float = 3 * 3600) -> int | None:
    """Public subscriber count of a channel (YouTube rounds it to 3 digits), cached for a few hours."""
    key = (channel or "").strip()
    if not key:
        return None
    hit = _subs_cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    try:
        with yt_dlp.YoutubeDL(ydl_opts(extract_flat="in_playlist", playlistend=1)) as y:
            n = y.extract_info(f"{yt_base(key)}/videos", download=False).get("channel_follower_count")
    except Exception:  # noqa: BLE001 - keep the last known value when YouTube is unreachable
        n = hit[1] if hit else None
    _subs_cache[key] = (time.time(), n)
    return n


def cached_subscribers(channel: str) -> int | None:
    return (_subs_cache.get((channel or "").strip()) or (0, None))[1]


# ---------------------------------------------------------------- generic

def resolve(url: str) -> dict:
    """Return normalized source info for a YouTube or Kick VOD URL."""
    url = url.strip()
    m = re.search(r"kick\.com/([^/?#]+)/videos/([0-9a-f-]{36})", url)
    if m:
        item = kick_video(m.group(2))
        if not item or not item.get("hls"):
            raise RuntimeError("Nu am putut citi VOD-ul de pe Kick (poate e privat sau șters).")
        item["slug"] = item.get("slug") or m.group(1)
        return item
    info = yt_info(url)
    if info.get("is_live"):
        raise RuntimeError("Videoclipul este LIVE acum – folosește monitorizarea live pentru el.")
    return {
        "platform": "youtube",
        "kind": "stream" if info.get("was_live") or info.get("live_status") == "was_live" else "video",
        "id": info["id"],
        "title": info.get("title") or "",
        "url": info.get("webpage_url") or url,
        "duration": info.get("duration") or 0,
        "timestamp": info.get("timestamp"),
        "heatmap": info.get("heatmap") or [],
        "has_live_chat": "live_chat" in (info.get("subtitles") or {}),
        "thumbnail": info.get("thumbnail"),
    }

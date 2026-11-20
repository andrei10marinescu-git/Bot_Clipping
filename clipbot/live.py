"""Live clipping: rolling HLS recording + chat activity -> real-time detection of hype moments."""

import json
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import websocket

from . import analysis, sources, transcribe
from .config import FFMPEG, LIVE_DIR, get_streamer, load_config, main_clip_max, save_config, settings
from .jobs import logger, manager

SEGMENT_S = 6
BUFFER_S = 8 * 60
# ffmpeg can hang without exiting after a network drop; restart it when no new segment arrives for this long.
STALL_S = 60
# A live moment is clipped once no stronger peak followed it for this long (or after the max wait).
BURST_SETTLE_S = 30
BURST_MAX_WAIT_S = 70
# A moment this far above the stream's threshold is clipped right away instead of waiting for the window.
GREAT_RATIO = 1.5
HOP = analysis.HOP
CHAT_REACTION_S = 3.0

_HYPE_CHAT = re.compile(
    r"(KEKW|LUL|OMEGALUL|LMAO|LMFAO|LOL|😂|🤣|💀|😭|🔥|\bW+\b|\bL\b|\?{2,}|!{2,}|POG|HOLY|WTF|NO WAY|CINEMA|AURA|"
    r"HAHA|AHAHA|XD|CLIP IT|CLIP THAT|CLIPPED)",
    re.I,
)


# --------------------------------------------------------------------------- chat readers

class KickChat(threading.Thread):
    def __init__(self, channel_id: int, chatroom_id: int, sink, stop_evt: threading.Event):
        super().__init__(daemon=True, name=f"kick-chat-{chatroom_id}")
        self.channel_id, self.chatroom_id, self.sink, self.stop_evt = channel_id, chatroom_id, sink, stop_evt

    def run(self):
        while not self.stop_evt.is_set():
            ws = None
            try:
                key, cluster = sources.kick_pusher_credentials(self.channel_id)
                ws = websocket.create_connection(
                    f"wss://ws-{cluster}.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false",
                    header=["Origin: https://kick.com"], timeout=30,
                )
                ws.recv()
                ws.send(json.dumps({"event": "pusher:subscribe",
                                    "data": {"auth": "", "channel": f"chatrooms.{self.chatroom_id}.v2"}}))
                while not self.stop_evt.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                        continue
                    msg = json.loads(raw)
                    ev = msg.get("event")
                    if ev == "App\\Events\\ChatMessageEvent":
                        data = json.loads(msg.get("data") or "{}")
                        self.sink(time.time(), data.get("content") or "")
                    elif ev == "pusher:ping":
                        ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
            except Exception as e:  # noqa: BLE001 - reconnect on any failure
                logger.warning("kick chat %s: %s", self.chatroom_id, e)
                self.stop_evt.wait(5)
            finally:
                if ws:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001
                        pass


class YouTubeChat(threading.Thread):
    def __init__(self, video_id: str, sink, stop_evt: threading.Event):
        super().__init__(daemon=True, name=f"yt-chat-{video_id}")
        self.video_id, self.sink, self.stop_evt = video_id, sink, stop_evt

    def run(self):
        while not self.stop_evt.is_set():
            try:
                http = sources.http()
                page = http.get(f"https://www.youtube.com/live_chat?is_popout=1&v={self.video_id}",
                                headers={"Accept-Language": "en-US,en;q=0.9"}).text
                ver = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', page)
                cont = re.search(r'"continuation":"([^"]+)"', page)
                if not ver or not cont:
                    self.stop_evt.wait(30)
                    continue
                version, token = ver.group(1), cont.group(1)
                while token and not self.stop_evt.is_set():
                    data = http.post(
                        "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false",
                        json={"context": {"client": {"clientName": "WEB", "clientVersion": version}},
                              "continuation": token},
                    ).json()
                    lc = (data.get("continuationContents") or {}).get("liveChatContinuation") or {}
                    for act in lc.get("actions") or []:
                        item = (act.get("addChatItemAction") or {}).get("item") or {}
                        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                        if not r:
                            continue
                        ts = int(r.get("timestampUsec") or 0) / 1e6 or time.time()
                        text = "".join(
                            run.get("text") or ((run.get("emoji") or {}).get("shortcuts") or [""])[0]
                            for run in (r.get("message") or {}).get("runs") or []
                        )
                        self.sink(ts, text)
                    conts = lc.get("continuations") or []
                    if not conts:
                        break
                    cd = next(iter(conts[0].values()), {}) or {}
                    token = cd.get("continuation")
                    self.stop_evt.wait(min(10.0, max(1.0, (cd.get("timeoutMs") or 5000) / 1000)))
            except Exception as e:  # noqa: BLE001
                logger.warning("youtube chat %s: %s", self.video_id, e)
                self.stop_evt.wait(10)


# --------------------------------------------------------------------------- monitor

class LiveMonitor(threading.Thread):
    def __init__(self, streamer_id: str, platform: str, info: dict):
        super().__init__(daemon=True, name=f"live-{streamer_id}-{platform}")
        self.streamer_id = streamer_id
        self.platform = platform
        self.info = info
        self.dir = LIVE_DIR / f"{streamer_id}_{platform}"
        self.stop_evt = threading.Event()
        self.status = "pornește…"
        self.segments: list[dict] = []
        self.timeline = 0.0
        self.audio = np.zeros(0, np.float32)
        self._chat: deque = deque()
        self._chat_lock = threading.Lock()
        self.chat_offset: float | None = None
        self.last_clip_peak = -1e9
        self.clip_walls: deque = deque()
        self.clips = 0
        self.started = time.time()
        self.proc: subprocess.Popen | None = None
        self._seg_no = 0
        self._run_no = 0
        self.last_score = 0.0
        self.threshold = 0.0
        self._score_hist: deque = deque(maxlen=int(1800 / HOP))
        self._settled_upto = 0
        self._pending: dict | None = None
        self._pool: list[dict] = []
        self._pool_since: float | None = None

    # chat ---------------------------------------------------------------
    def chat_sink(self, wall: float, text: str) -> None:
        weight = 1.6 if _HYPE_CHAT.search(text or "") else 1.0
        with self._chat_lock:
            self._chat.append((wall, weight))
            cutoff = time.time() - BUFFER_S - 120
            while self._chat and self._chat[0][0] < cutoff:
                self._chat.popleft()

    def chat_rate(self) -> float:
        now = time.time()
        with self._chat_lock:
            return sum(1 for w, _ in self._chat if w > now - 60) / 60.0

    # lifecycle ------------------------------------------------------------
    def stop(self):
        self.stop_evt.set()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def snapshot(self) -> dict:
        return {
            "platform": self.platform,
            "status": self.status,
            "title": self.info.get("title"),
            "url": self.info.get("url"),
            "recorded": round(self.timeline),
            "clips": self.clips,
            "chat_per_min": round(self.chat_rate() * 60),
            "score": round(self.last_score, 2),
            "threshold": round(self.threshold, 2),
            "started": self.started,
            "alive": self.is_alive(),
            "pool": len(self._pool),
        }

    def _start_chat(self):
        if self.platform == "kick" and self.info.get("chatroom_id"):
            KickChat(self.info["channel_id"], self.info["chatroom_id"], self.chat_sink, self.stop_evt).start()
        elif self.platform == "youtube":
            YouTubeChat(self.info["id"], self.chat_sink, self.stop_evt).start()

    def _start_ffmpeg(self) -> Path:
        self._run_no += 1
        url = self.info["hls"]
        if self.platform == "kick":
            url = sources.pick_variant(url, max_height=1080)
        list_path = self.dir / f"list_{self._run_no}.csv"
        inputs = ["-i", url]
        maps = ["-map", "0:v:0", "-map", "0:a:0"]
        ts_flags: list[str] = []
        if self.info.get("hls_audio"):
            # YouTube live sends audio and video as separate HLS playlists that start at different times.
            # Keeping the original timestamps (-copyts) is what keeps lips and voice in sync.
            inputs += ["-i", self.info["hls_audio"]]
            maps = ["-map", "0:v:0", "-map", "1:a:0"]
            ts_flags = ["-copyts"]
        cmd = [
            FFMPEG, "-nostdin", "-v", "error", *ts_flags, *inputs, *maps, "-c", "copy",
            "-f", "segment", "-segment_time", str(SEGMENT_S), "-segment_format", "mpegts",
            "-reset_timestamps", "1", "-segment_list", str(list_path), "-segment_list_type", "csv",
            "-segment_start_number", str(self._seg_no), str(self.dir / "seg_%06d.ts"),
        ]
        self._log = open(self.dir / f"ffmpeg_{self._run_no}.log", "wb")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=self._log)
        return list_path

    def _reset_analysis(self) -> None:
        """Forget everything after a gap (PC sleep, long reconnect) so a backlog can't trigger fake clips."""
        for g in self.segments:
            try:
                g["path"].unlink(missing_ok=True)
            except OSError:
                pass
        self.segments = []
        self.timeline = 0.0
        self.audio = np.zeros(0, np.float32)
        self._score_hist.clear()
        self._settled_upto = 0
        self._pending = None
        self.last_clip_peak = -1e9
        self.chat_offset = None
        with self._chat_lock:
            self._chat.clear()

    def run(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._start_chat()
        try:
            while not self.stop_evt.is_set():
                list_path = self._start_ffmpeg()
                self.status = "înregistrează"
                seen = 0
                last_tick = last_new = time.time()
                resumed = False
                while not self.stop_evt.is_set():
                    if time.time() - last_tick > 30:
                        logger.info("%s/%s: time jump (sleep?) – restarting recorder", self.streamer_id, self.platform)
                        resumed = True
                        break
                    last_tick = time.time()
                    if list_path.exists():
                        lines = list_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                        if len(lines) > seen:
                            last_new = time.time()
                        for idx in range(seen, len(lines)):
                            parts = lines[idx].strip().split(",")
                            if len(parts) < 3:
                                continue
                            seg_path = self.dir / parts[0]
                            dur = float(parts[2]) - float(parts[1])
                            # The first segment of every recorder run has broken timing (and may lack audio).
                            if idx == 0 or not 0 < dur <= SEGMENT_S * 3:
                                seg_path.unlink(missing_ok=True)
                                continue
                            try:
                                self._on_segment(seg_path, dur)
                            except Exception as e:  # noqa: BLE001
                                logger.warning("segment %s: %s", parts[0], e)
                        seen = len(lines)
                    if self.proc.poll() is not None:
                        break
                    if time.time() - last_new > STALL_S:
                        logger.warning("%s/%s: no new video for %ds – restarting recorder",
                                       self.streamer_id, self.platform, STALL_S)
                        resumed = True
                        break
                    self.stop_evt.wait(1.0)
                if self.stop_evt.is_set():
                    break
                if resumed:
                    if self.proc.poll() is None:
                        self.proc.kill()
                        self.proc.wait(timeout=10)
                    self._reset_analysis()
                self._log.close()
                err = (self.dir / f"ffmpeg_{self._run_no}.log").read_text(errors="ignore")[-300:]
                logger.info("recorder for %s/%s exited: %s", self.streamer_id, self.platform, err)
                self.status = "reconectare…"
                st = get_streamer(self.streamer_id) or {}
                fresh = None
                # A network drop looks like "not live" for a while; retry before giving up on the stream.
                for attempt in range(4):
                    try:
                        fresh = (sources.kick_live(st.get("kick", "")) if self.platform == "kick"
                                 else sources.yt_live(st.get("youtube", "")))
                    except Exception as e:  # noqa: BLE001
                        logger.warning("%s/%s reconnect: %s", self.streamer_id, self.platform, e)
                    if fresh or self.stop_evt.wait(20 if attempt < 3 else 0):
                        break
                if not fresh:
                    self.status = "live terminat"
                    break
                self.info.update(fresh)
                self.stop_evt.wait(3)
        except Exception as e:  # noqa: BLE001
            self.status = f"eroare: {e}"
            logger.exception("live monitor crashed")
        finally:
            self.stop()
            try:
                self._maybe_flush(force=True)  # the live ended: still pick the best of the last window
            except Exception:  # noqa: BLE001
                logger.exception("flush live moments")
            if not self.status.startswith(("eroare", "live terminat")):
                self.status = "oprit"
            time.sleep(1)
            try:
                self._log.close()
            except Exception:  # noqa: BLE001
                pass
            shutil.rmtree(self.dir, ignore_errors=True)

    # analysis ------------------------------------------------------------
    def _on_segment(self, path: Path, dur: float) -> None:
        if dur <= 0 or not path.exists():
            return
        self._seg_no += 1
        t0, t1 = self.timeline, self.timeline + dur
        self.timeline = t1
        wall = time.time()
        self.segments.append({"path": path, "t0": t0, "t1": t1})
        self.chat_offset = wall - t1
        db = analysis.rms_db_series(str(path))
        i0 = int(round(t0 / HOP))
        need = i0 + len(db)
        if len(self.audio) < need:
            fill = self.audio[-1] if len(self.audio) else -60.0
            self.audio = np.concatenate([self.audio, np.full(need - len(self.audio), fill, np.float32)])
        self.audio[i0:need] = db
        while self.segments and self.segments[0]["t1"] < self.timeline - BUFFER_S:
            old = self.segments.pop(0)
            try:
                old["path"].unlink(missing_ok=True)
            except OSError:
                pass
        self._detect()

    def _detect(self) -> None:
        self._maybe_flush()
        cfg = settings()
        if self.timeline < 75:
            return
        a0 = max(0, len(self.audio) - int(360 / HOP))
        audio = self.audio[a0:]
        signals = {"audio": analysis.audio_signal(audio)}
        with self._chat_lock:
            chat = list(self._chat)
        if len(chat) >= 30 and self.chat_offset is not None:
            times = [w - self.chat_offset - CHAT_REACTION_S - a0 * HOP for w, _ in chat]
            signals["chat"] = analysis.event_signal(times, [wt for _, wt in chat], len(audio), window_s=240)
        score = analysis.combine(signals)
        t_end = len(audio) * HOP
        lo, hi = int(max(0.0, t_end - 45) / HOP), int(max(0.0, t_end - 14) / HOP)
        if hi <= lo:
            return
        # Remember "settled" scores (enough chat/right context) to learn what is normal for this stream.
        start_rel = max(lo, self._settled_upto - a0)
        if hi > start_rel:
            self._score_hist.extend(float(v) for v in score[start_rel:hi])
            self._settled_upto = a0 + hi
        i = lo + int(np.argmax(score[lo:hi]))
        val = float(score[i])
        self.last_score = float(score[hi - 1])
        sens = max(0.3, float(cfg["live_sensitivity"]))
        floor = (3.0 if "chat" in signals else 2.6) / sens
        if len(self._score_hist) >= int(300 / HOP):
            # A moment must stand out from the last 30 minutes of THIS stream (top ~1% at sensitivity 1).
            pct = min(99.8, max(90.0, 100.0 - 1.0 * sens))
            threshold = max(floor, float(np.percentile(np.fromiter(self._score_hist, np.float32), pct)))
        else:
            threshold = floor * 1.4
        self.threshold = threshold
        peak = (a0 + i) * HOP
        if val >= threshold and peak - self.last_clip_peak >= cfg["live_cooldown_seconds"]:
            p = self._pending
            if p is None or val > p["val"]:
                self._pending = {"peak": peak, "val": val, "reasons": analysis.explain(signals, i * HOP),
                                 "seen": p["seen"] if p else self.timeline}
        p = self._pending
        # Wait until the burst is over, so the clip lands on its strongest moment instead of the first spike.
        if p is None or (self.timeline - p["peak"] < BURST_SETTLE_S and self.timeline - p["seen"] < BURST_MAX_WAIT_S):
            return
        self._pending = None
        cand = self._extract(p["peak"], p["val"], p["reasons"], threshold)
        if cand is None:
            return
        self.last_clip_peak = p["peak"]
        if cand["ratio"] >= GREAT_RATIO or float(cfg.get("live_best_of_minutes", 12)) <= 0:
            self._submit([cand], exceptional=cand["ratio"] >= GREAT_RATIO)
        else:
            self._pool.append(cand)
            self._pool_since = self._pool_since or time.time()
            logger.info("live moment for %s pooled (%d in window)", self.streamer_id, len(self._pool))

    def _maybe_flush(self, force: bool = False) -> None:
        """Every few minutes only the best moment of the window becomes a clip (fewer, stronger clips)."""
        if not self._pool:
            return
        window = float(settings().get("live_best_of_minutes", 12)) * 60
        if force or time.time() - (self._pool_since or 0) >= window:
            pool, self._pool, self._pool_since = self._pool, [], None
            self._submit(pool)

    def _submit(self, cands: list[dict], exceptional: bool = False) -> None:
        now = time.time()
        while self.clip_walls and self.clip_walls[0] < now - 3600:
            self.clip_walls.popleft()
        cap = int(settings().get("live_max_clips_per_hour") or 0)  # 0 = unlimited
        if cap > 0 and len(self.clip_walls) >= cap:
            for c in cands:
                Path(c["raw"]).unlink(missing_ok=True)
            return
        self.clip_walls.append(now)
        self.clips += 1
        st = get_streamer(self.streamer_id) or {"name": self.streamer_id}
        what = "moment uriaș" if exceptional else (
            f"cel mai bun din {len(cands)} momente" if len(cands) > 1 else f"moment la {int(cands[0]['peak'])}s")
        manager.submit(
            "live", self.streamer_id, f"{st['name']} LIVE ({self.platform}) – {what}",
            process_live_moments, self.streamer_id, cands, on_skip=lambda: self._release_slot(now),
            lane="live", priority=1,
        )
        logger.info("live clip job for %s: %s", self.streamer_id, what)

    def _release_slot(self, wall: float) -> None:
        """A skipped moment must not use up one of the clips allowed per hour."""
        try:
            self.clip_walls.remove(wall)
        except ValueError:
            pass

    def _extract(self, peak: float, val: float, reasons: list[str], threshold: float) -> dict | None:
        max_len = float(settings()["max_clip_seconds"])
        s, e = max(0.0, peak - max(38.0, max_len - 4)), min(self.timeline, peak + 16)
        r_end = analysis.reaction_end(self.audio, peak, HOP, max_after=min(16.0, e - peak))
        segs = [g for g in self.segments if g["t1"] > s and g["t0"] < e]
        if not segs:
            return None
        clip_dir = LIVE_DIR / "pending"
        clip_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{self.streamer_id}_{self.platform}_{int(time.time())}"
        lst = clip_dir / f"{stamp}.txt"
        lst.write_text("".join(f"file '{g['path'].as_posix()}'\n" for g in segs), encoding="utf-8")
        raw = clip_dir / f"{stamp}.mp4"
        p = subprocess.run([FFMPEG, "-y", "-nostdin", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-c", "copy", str(raw)], capture_output=True)
        lst.unlink(missing_ok=True)
        if p.returncode != 0 or not raw.exists():
            logger.warning("live concat failed: %s", p.stderr[-300:])
            return None
        started = self.info.get("started")
        src = {
            "platform": self.platform,
            "kind": "live",
            "id": str(self.info["id"]),
            "title": self.info.get("title") or "",
            "url": self.info.get("url"),
            "stream_offset": (self.started - started) if started else None,
        }
        logger.info("live moment for %s at %.1fs (score %.2f, %.2fx threshold)", self.streamer_id, peak, val,
                    val / max(threshold, 1e-6))
        return {"raw": str(raw), "raw_offset": segs[0]["t0"], "peak": peak, "s": s, "e": e, "val": val,
                "ratio": val / max(threshold, 1e-6), "reasons": reasons, "src": src, "r_end": r_end}


def _evaluate_moment(job, st: dict, cfg: dict, c: dict) -> dict | None:
    """Transcribe one live moment, cut it and score it; None when it is not worth a clip."""
    raw, raw_offset, peak, sig = c["raw"], c["raw_offset"], c["peak"], c["val"]
    react_end = c.get("r_end")
    audio = transcribe.load_audio(raw)
    raw_end = raw_offset + len(audio) / transcribe.SAMPLE_RATE
    lang = st.get("language") or None
    tr = transcribe.transcribe(audio, language=lang, offset=raw_offset)
    lo, hi = max(c["s"], raw_offset), min(c["e"], raw_end)
    fine = analysis.rms_db_series(raw, hop=0.1)
    exact = raw_offset + analysis.refine_peak(fine, 0.1, peak - raw_offset)
    if abs(exact - peak) >= 0.5:
        job.log(f"Momentul exact e cu {exact - peak:+.1f}s față de detecție (reacția reală).")
        peak = exact
        react_end = max(react_end or 0.0, peak + 3.0)
    start, end = analysis.choose_bounds(tr["words"], peak, lo, hi, cfg["min_clip_seconds"], main_clip_max(cfg),
                                        react_end=react_end)
    words = [w for w in tr["words"] if start - 1.0 <= w["start"] < end + 8.0]
    spoken = [w for w in words if start <= w["start"] < end]
    if len(spoken) < 4 and sig < 5:
        job.log("Moment fără vorbire (muzică/cutscene) – sărit.")
        return None
    wps = len(spoken) / max(1.0, end - start)
    if wps < 0.4 and sig < 6:
        job.log(f"Prea puțină vorbire ({len(spoken)} cuvinte în {end - start:.0f}s) – sărit.")
        return None
    foreign = analysis.foreign_share(spoken, lang)
    if foreign >= 0.65 and sig < 6.5:
        job.log(f"Vorbesc mai ales personajele din joc/video (~{foreign:.0%} engleză), nu {st['name']} – sărit.")
        return None
    t_score, t_reasons = analysis.text_score(spoken, lang or tr["language"])
    # How strongly it beat this stream's threshold, what is said (hype, laughs, exclamations) and how much is said.
    rank = 2.0 * c["ratio"] + t_score + min(1.0, wps / 2.0) - 4.0 * max(0.0, foreign - 0.35)
    src = c["src"]
    if src.get("stream_offset") is not None:
        src = dict(src, live_t=src["stream_offset"] + start)
    cand = {"peak": peak, "start": start, "end": end, "words": words, "language": lang or tr["language"],
            "score": sig + t_score, "reasons": ["🔴 Prins în LIVE"] + c["reasons"] + t_reasons}
    return {"c": c, "cand": cand, "src": src, "raw_end": raw_end, "rank": rank,
            "preview": " ".join(w["word"] for w in spoken)[:60]}


def process_live_moments(job, streamer_id: str, cands: list[dict], on_skip=None) -> dict:
    """Evaluate the moments of one window and render only the best one."""
    from . import pipeline

    st = get_streamer(streamer_id) or {"id": streamer_id, "name": streamer_id}
    cfg = settings()
    try:
        evaluated = []
        for i, c in enumerate(cands):
            job.check()
            job.set_progress(0.05 + 0.45 * i / len(cands), f"Analizez momentul {i + 1}/{len(cands)} (la {int(c['peak'])}s)…")
            ev = _evaluate_moment(job, st, cfg, c)
            if ev:
                evaluated.append(ev)
        if not evaluated:
            job.log("Niciun moment destul de bun – nu fac clip.")
            if on_skip:
                on_skip()
            return {"clip": None, "skipped": True}
        evaluated.sort(key=lambda e: e["rank"], reverse=True)
        best = evaluated[0]
        if len(cands) > 1:
            job.log("Clasament: " + " | ".join(f"{int(e['c']['peak'])}s → {e['rank']:.1f} („{e['preview']}…”)"
                                                for e in evaluated[:4]))
        job.set_progress(0.55, "Aliniez cuvintele + randez cel mai bun moment…")
        clip = pipeline.finalize_clip(st, best["src"], Path(best["c"]["raw"]), best["c"]["raw_offset"], best["cand"],
                                      log=job.log, raw_end=best["raw_end"])
        job.log(f"Clip live gata ({clip['duration']:.0f}s).")
        return {"clip": clip["id"]}
    finally:
        for c in cands:
            Path(c["raw"]).unlink(missing_ok=True)


# --------------------------------------------------------------------------- manager

class LiveManager:
    def __init__(self):
        self.monitors: dict[tuple[str, str], LiveMonitor] = {}
        self.live: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._last_check: dict[str, float] = {}
        self._force = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="live-manager").start()

    def watching(self, streamer_id: str) -> bool:
        cfg = load_config()
        if cfg["settings"].get("auto_live"):
            return True
        st = next((s for s in cfg["streamers"] if s["id"] == streamer_id), None)
        return bool(st and st.get("watch_live"))

    def set_watch(self, streamer_id: str, on: bool) -> None:
        cfg = load_config()
        for s in cfg["streamers"]:
            if s["id"] == streamer_id:
                s["watch_live"] = bool(on)
        save_config(cfg)
        if not on and not cfg["settings"].get("auto_live"):
            self.stop_streamer(streamer_id)
        self._last_check.pop(streamer_id, None)
        self._force.set()

    def stop_streamer(self, streamer_id: str) -> None:
        with self._lock:
            for key, mon in list(self.monitors.items()):
                if key[0] == streamer_id:
                    mon.stop()
                    self.monitors.pop(key, None)

    def check(self, streamer_id: str) -> dict:
        st = get_streamer(streamer_id) or {}
        res: dict = {}
        if st.get("kick"):
            try:
                res["kick"] = sources.kick_live(st["kick"])
            except Exception as e:  # noqa: BLE001
                logger.warning("kick live check %s: %s", streamer_id, e)
                res["kick"] = (self.live.get(streamer_id) or {}).get("kick")
        if st.get("youtube"):
            try:
                res["youtube"] = sources.yt_live(st["youtube"])
            except Exception as e:  # noqa: BLE001
                logger.warning("youtube live check %s: %s", streamer_id, e)
                res["youtube"] = (self.live.get(streamer_id) or {}).get("youtube")
        res["checked"] = time.time()
        self.live[streamer_id] = res
        self._last_check[streamer_id] = time.time()
        return res

    def check_now(self) -> None:
        self._last_check.clear()
        self._force.set()

    def _loop(self):
        while True:
            try:
                cfg = load_config()
                interval = max(30, int(cfg["settings"]["live_check_interval_seconds"]))
                for st in cfg["streamers"]:
                    sid = st["id"]
                    if time.time() - self._last_check.get(sid, 0) >= interval:
                        self.check(sid)
                    watch = self.watching(sid)
                    for platform in ("kick", "youtube"):
                        info = (self.live.get(sid) or {}).get(platform)
                        key = (sid, platform)
                        with self._lock:
                            mon = self.monitors.get(key)
                            if mon and not mon.is_alive():
                                self.monitors.pop(key, None)
                                mon = None
                            if watch and info and not mon:
                                mon = LiveMonitor(sid, platform, dict(info))
                                self.monitors[key] = mon
                                mon.start()
                                logger.info("live monitor started: %s/%s", sid, platform)
                            elif mon and not watch:
                                mon.stop()
                                self.monitors.pop(key, None)
            except Exception:  # noqa: BLE001
                logger.exception("live manager loop")
            self._force.wait(10)
            self._force.clear()

    def status(self) -> dict:
        out = {}
        with self._lock:
            monitors = list(self.monitors.items())
        for st in load_config()["streamers"]:
            sid = st["id"]
            live = self.live.get(sid) or {}
            out[sid] = {
                "watching": self.watching(sid),
                "checked": live.get("checked"),
                "live": {p: ({k: v for k, v in (live.get(p) or {}).items() if k in ("title", "url", "viewers", "id")}
                             if live.get(p) else None) for p in ("kick", "youtube")},
                "monitors": [m.snapshot() for (s, _), m in monitors if s == sid],
            }
        return out


live_manager: LiveManager | None = None


def get_live_manager() -> LiveManager:
    global live_manager
    if live_manager is None:
        live_manager = LiveManager()
    return live_manager

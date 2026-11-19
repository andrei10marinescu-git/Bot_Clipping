"""VOD pipeline: analyse a YouTube video / Kick VOD, pick the best moments and render clips."""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

from . import align, analysis, db, header, render, sources, titles, transcribe
from .config import CLIPS_DIR, FFMPEG, ROOT, SOURCES_DIR, WORK_DIR, get_streamer, settings
from .jobs import Cancelled, logger, manager

HOP = analysis.HOP


def safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s)[:90]


def fmt_t(t: float) -> str:
    t = int(max(0, t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def source_key(src: dict) -> str:
    return f"{src['platform']}:{src['id']}"


def subs_label(st: dict) -> str | None:
    """'62.8M SUBSCRIBERS' badge for streamers with show_subs on."""
    if not st.get("show_subs") or not st.get("youtube"):
        return None
    n = sources.yt_subscribers(st["youtube"])
    if not n:
        return None
    return f"{header.format_count(n)} {'ABONAȚI' if st.get('language') == 'ro' else 'SUBSCRIBERS'}"


def timestamped_url(src: dict, t: float) -> str:
    if src.get("kind") == "live":
        if src["platform"] == "youtube" and src.get("live_t") is not None:
            return f"https://www.youtube.com/watch?v={src['id']}&t={int(max(0, src['live_t']))}s"
        return src["url"]
    t = int(max(0, t))
    if src["platform"] == "youtube":
        return f"https://www.youtube.com/watch?v={src['id']}&t={t}s"
    return f"{src['url']}?t={t}"


def _progress_hook(job, lo: float, hi: float, label: str):
    state = {"last": 0.0}

    def hook(d):
        if job.cancel_requested:
            raise Cancelled()
        if d.get("status") != "downloading":
            return
        frac = None
        if d.get("fragment_count"):
            frac = (d.get("fragment_index") or 0) / max(1, d["fragment_count"])
        elif d.get("total_bytes") or d.get("total_bytes_estimate"):
            frac = (d.get("downloaded_bytes") or 0) / max(1, d.get("total_bytes") or d.get("total_bytes_estimate"))
        if frac is None:
            return
        now = time.time()
        if now - state["last"] > 2:
            state["last"] = now
            job.set_progress(lo + (hi - lo) * frac)
            job.message = f"{label} {int(frac * 100)}%"

    return hook


def _first(work: Path, stem: str) -> Path:
    files = [p for p in work.glob(f"{stem}.*") if not p.name.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError(f"Descărcarea a eșuat ({stem}).")
    return max(files, key=lambda p: p.stat().st_size)


def _ydl_attempts(job, url: str, attempts: list[dict], stem: str, work: Path) -> Path:
    """Try several yt-dlp configurations; YouTube sometimes serves broken HLS fragments for one format."""
    last_err = None
    for i, extra in enumerate(attempts):
        for p in work.glob(f"{stem}.*"):
            p.unlink(missing_ok=True)
        try:
            with yt_dlp.YoutubeDL(sources.ydl_opts(outtmpl=str(work / f"{stem}.%(ext)s"), **extra)) as y:
                y.download([url])
            return _first(work, stem)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if job and i + 1 < len(attempts):
                job.log(f"Descărcarea a eșuat ({str(e)[:120]}), reîncerc altfel…")
    raise RuntimeError(f"Descărcarea a eșuat: {last_err}")


def download_analysis_audio(job, src: dict, work: Path) -> Path:
    if src["platform"] == "youtube":
        job.set_progress(0.03, "Descarc audio pentru analiză…")
        hook = [_progress_hook(job, 0.03, 0.28, "Descarc audio")]
        a = "[protocol=https][abr<=?100][format_id!*=drc]"
        return _ydl_attempts(job, src["url"], [
            # Original audio track first (videos with YouTube auto-dubs have several tracks).
            {"format": f"bestaudio{a}[language_preference>=10]/bestaudio{a}/worstaudio[protocol=https]",
             "progress_hooks": hook},
            {"format": "worstaudio[language_preference>=10]/worstaudio[format_id!*=drc]/worstaudio/bestaudio",
             "progress_hooks": hook, "concurrent_fragment_downloads": 1},
            {"format": "worst", "progress_hooks": hook},
        ], "audio", work)
    job.set_progress(0.03, "Descarc VOD-ul Kick în calitate mică pentru analiză…")
    variant = sources.pick_variant(src["hls"], lowest=True)
    hook = [_progress_hook(job, 0.03, 0.28, "Descarc VOD")]
    return _ydl_attempts(job, variant, [
        {"format": "worst", "concurrent_fragment_downloads": 16, "fixup": "never", "progress_hooks": hook},
        {"format": "worst", "concurrent_fragment_downloads": 2, "fixup": "never", "progress_hooks": hook},
    ], "low", work)


def download_section(job, src: dict, start: float, end: float, work: Path, stem: str) -> Path:
    if src["platform"] == "youtube":
        common = {"download_ranges": download_range_func(None, [(start, end)]), "force_keyframes_at_cuts": False,
                  "merge_output_format": "mp4", "overwrites": True}
        v = "bv*[height<=1080][vcodec^=avc1][protocol=https]"
        a = "ba[ext=m4a][protocol=https][format_id!*=drc]"
        return _ydl_attempts(job, src["url"], [
            {"format": f"{v}+{a}[language_preference>=10]/{v}+{a}"
                       "/bv*[height<=1080][protocol=https]+ba[protocol=https][format_id!*=drc]", **common},
            {"format": "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a][language_preference>=10]"
                       "/bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b", **common},
        ], stem, work)
    variant = sources.pick_variant(src["hls"], max_height=1080)
    out = work / f"{stem}.mp4"
    cmd = [FFMPEG, "-y", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-i", variant,
           "-t", f"{end - start:.3f}", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", str(out)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 or not out.exists():
        raise RuntimeError(f"Nu am putut descărca secțiunea din Kick: {p.stderr[-400:]}")
    return out


def youtube_chat_times(job, src: dict, work: Path) -> list[float]:
    opts = sources.ydl_opts(
        skip_download=True,
        writesubtitles=True,
        subtitleslangs=["live_chat"],
        outtmpl=str(work / "chat.%(ext)s"),
    )
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([src["url"]])
        files = list(work.glob("chat*.json"))
        return analysis.youtube_chat_replay_times(str(files[0])) if files else []
    except Exception as e:  # noqa: BLE001 - chat is optional
        job.log(f"Chat replay indisponibil: {e}")
        return []


def finalize_clip(st: dict, src: dict, raw: Path, raw_offset: float, cand: dict, log=None,
                  raw_end: float | None = None) -> dict:
    """Render a candidate (times are source-relative) from a raw file starting at raw_offset."""
    start, end = cand["start"], cand["end"]
    lang = cand.get("language")
    if raw_end is None:
        raw_end = raw_offset + render.probe(raw)["duration"]
    max_len = float(settings()["max_clip_seconds"])

    # Precise word timing (forced alignment) on the clip audio, then snap the cut points to real word edges.
    a_lo = max(raw_offset, start - 0.8)
    a_hi = min(raw_end, end + 0.8)
    near = [w for w in cand["words"] if a_lo - 0.5 <= w["start"] < a_hi]
    words = near
    try:
        audio = transcribe.load_audio(str(raw), a_lo - raw_offset, a_hi - a_lo)
        words, stats = align.align(audio, near, offset=a_lo)
        if log:
            log(f"Sincronizare cuvinte: {stats['aligned']}/{len(near)} aliniate precis ({align.device()}).")
    except Exception as e:  # noqa: BLE001 - keep Whisper timing if alignment fails
        if log:
            log(f"Aliniere indisponibilă ({e}); folosesc timpii Whisper.")
    start, end = analysis.refine_bounds(words, start, end, a_lo, a_hi, max_len)
    dur = round(end - start, 2)
    rel = [
        {"start": round(max(0.0, w["start"] - start), 3), "end": round(min(w["end"], end) - start, 3),
         "word": analysis.censor(w["word"])}
        for w in words if start <= w["start"] < end - 0.05
    ]
    hook = titles.make_title(rel, cand["peak"] - start, lang, st["name"], src.get("title") or "",
                             cand.get("reasons"), log=log)
    clip_id = safe(f"{st['id']}_{src['platform']}_{src['id']}_{int(start)}")
    out = CLIPS_DIR / st["id"] / f"{clip_id}.mp4"
    res = render.render_clip(
        str(raw), start - raw_offset, dur, rel, out,
        hook=hook, name=st["name"], credit="", color=st.get("color") or "#FFD400",
        crop_sides=float(st.get("crop_sides") or 0), layout=st.get("layout") or "auto", language=lang, log=log,
        subs=subs_label(st),
    )
    reasons = list(dict.fromkeys(cand.get("reasons", [])))
    fx = res["effects"]
    kept = keep_source(raw, st["id"], clip_id)
    if kept:
        fx.update(source_file=kept, source_offset=round(raw_offset, 3))
    if fx.get("punches"):
        reasons.append("🎬 Efecte: zoom" + (" + shake" if fx.get("shake") is not None else "")
                       + (" + flash" if fx.get("flash") is not None else ""))
    layout_names = {"split": "📐 Split facecam + joc", "crop": "📐 Cadru urmărește fața", "blur": "📐 Cadru complet"}
    reasons.append(layout_names.get(res["layout"], res["layout"]))
    clip = {
        "id": clip_id,
        "streamer": st["id"],
        "platform": src["platform"],
        "source_kind": src.get("kind", "video"),
        "source_id": src["id"],
        "source_url": timestamped_url(src, start),
        "source_title": src.get("title", ""),
        "start": start,
        "end": end,
        "duration": dur,
        "score": round(float(cand["score"]), 2),
        "reasons": reasons,
        "title": hook,
        "transcript": " ".join(w["word"] for w in rel),
        "words": [{k: w[k] for k in ("start", "end", "word", "level", "kind")} for w in res["words"]],
        "language": lang,
        "effects": fx,
        "file": f"{st['id']}/{out.name}",
        "thumb": f"{st['id']}/{Path(res['thumb']).name}",
        "srt": f"{st['id']}/{Path(res['srt']).name}",
    }
    db.add_clip(clip)
    return clip


def process_vod(job, streamer_id: str, url: str, max_clips: int | None = None, auto: bool = False) -> dict:
    st = get_streamer(streamer_id)
    if not st:
        raise RuntimeError(f"Streamer necunoscut: {streamer_id}")
    cfg = settings()
    job.set_progress(0.01, "Citesc informațiile despre video…")
    src = sources.resolve(url)
    key = source_key(src)
    job.label = f"{st['name']}: {src['title'][:60]}"

    if auto:
        age_days = (time.time() - src["timestamp"]) / 86400 if src.get("timestamp") else 0
        if age_days > cfg["max_video_age_days"]:
            db.set_source(key, streamer=streamer_id, platform=src["platform"], title=src["title"],
                          url=src["url"], status="skipped", error="prea vechi")
            job.log("Video prea vechi – sărit.")
            return {"clips": 0, "skipped": True}

    db.set_source(key, streamer=streamer_id, platform=src["platform"], title=src["title"], url=src["url"],
                  status="processing", error=None)
    work = WORK_DIR / safe(key)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        long_vod = (src.get("duration") or 0) > 45 * 60
        n_clips = max_clips or (cfg["clips_per_stream"] if long_vod else cfg["clips_per_video"])

        audio_path = download_analysis_audio(job, src, work)
        job.check()
        job.set_progress(0.3, "Analizez audio (volum, țipete, reacții)…")
        db_series = analysis.rms_db_series(str(audio_path))
        n = len(db_series)
        if n < 20:
            raise RuntimeError("Audio prea scurt sau gol.")
        real_dur = n * HOP
        signals = {"audio": analysis.audio_signal(db_series)}
        if src.get("heatmap"):
            signals["heatmap"] = analysis.heatmap_signal(src["heatmap"], n)
            job.log("Folosesc graficul „Most replayed” de pe YouTube.")
        if (src["platform"] == "youtube" and src.get("has_live_chat")
                and real_dur <= cfg["youtube_chat_replay_max_hours"] * 3600):
            job.set_progress(0.33, "Descarc chat-ul din reluare…")
            times = youtube_chat_times(job, src, work)
            if times:
                signals["chat"] = analysis.event_signal(times, None, n)
                job.log(f"Chat: {len(times)} mesaje analizate.")
        if src["platform"] == "kick":
            moments = sources.kick_clip_moments(src.get("slug") or st.get("kick"), src.get("livestream_id"),
                                                src.get("timestamp"))
            if moments:
                signals["community"] = analysis.community_clip_signal(moments, n)
                job.log(f"Kick: {len(moments)} clipuri făcute de comunitate folosite ca semnal.")

        score = analysis.combine(signals)
        peaks = analysis.pick_peaks(
            score, k=n_clips * 3 + 2, min_gap_s=cfg["max_clip_seconds"] + 10,
            skip_start_s=60 if long_vod else 2, skip_end_s=20 if long_vod else 3,
        )
        lang = st.get("language") or None
        cands = []
        for i, p in enumerate(peaks):
            job.check()
            job.set_progress(0.38 + 0.32 * i / max(1, len(peaks)), f"Transcriu momentul {i + 1}/{len(peaks)} ({fmt_t(p)})…")
            w0, w1 = max(0.0, p - max(36.0, cfg["max_clip_seconds"] - 4)), min(real_dur, p + 20)
            fine = analysis.rms_db_series(str(audio_path), hop=0.1, start=w0, duration=w1 - w0)
            p = w0 + analysis.refine_peak(fine, 0.1, p - w0)
            audio = transcribe.load_audio(str(audio_path), w0, w1 - w0)
            tr = transcribe.transcribe(audio, language=lang, offset=w0)
            r_end = analysis.reaction_end(db_series, p)
            s, e = analysis.choose_bounds(tr["words"], p, w0, w1, cfg["min_clip_seconds"], cfg["max_clip_seconds"],
                                          react_end=r_end)
            words = [w for w in tr["words"] if s - 1.0 <= w["start"] < e + 1.0]
            t_score, t_reasons = analysis.text_score([w for w in words if s <= w["start"] < e], lang or tr["language"])
            q, q_reasons = analysis.clip_quality(score, tr["words"], s, e, p)
            foreign = analysis.foreign_share([w for w in words if s <= w["start"] < e], lang)
            if foreign > 0.35:
                q -= 4.0 * (foreign - 0.35)
                q_reasons = q_reasons + ["🎮 Vorbesc mai ales personajele din joc/video"]
            sig = float(score[min(int(p / HOP), n - 1)])
            cands.append({
                "peak": p, "start": s, "end": e, "words": words, "language": lang or tr["language"],
                "score": sig + t_score + q, "reasons": analysis.explain(signals, p) + t_reasons + q_reasons,
            })

        cands.sort(key=lambda c: c["score"], reverse=True)
        best = cands[0]["score"] if cands else 0
        # Only the real highlights: weak candidates are dropped even if fewer clips come out.
        floor = max(2.0, 0.45 * best)
        chosen = []
        for c in cands:
            if chosen and c["score"] < floor:
                break
            if any(c["start"] < o["end"] + 5 and c["end"] > o["start"] - 5 for o in chosen):
                continue
            chosen.append(c)
            if len(chosen) >= n_clips:
                break
        chosen.sort(key=lambda c: c["start"])
        job.log(f"Am ales {len(chosen)} momente din {len(cands)} candidați "
                f"(scoruri: {', '.join(str(round(c['score'], 1)) for c in chosen)}).")

        made = []
        for i, c in enumerate(chosen):
            job.check()
            job.set_progress(0.72 + 0.27 * i / max(1, len(chosen)),
                             f"Descarc + randez clipul {i + 1}/{len(chosen)} ({fmt_t(c['start'])}, {c['end'] - c['start']:.0f}s)…")
            lo, hi = max(0.0, c["start"] - 1.0), min(real_dur, c["end"] + 1.0)
            raw = download_section(job, src, lo, hi, work, f"raw_{i}")
            clip = finalize_clip(st, src, raw, lo, c, log=job.log, raw_end=hi)
            made.append(clip["id"])
        db.set_source(key, status="done", clips=len(made))
        job.log(f"Gata: {len(made)} clipuri noi pentru {st['name']}.")
        return {"clips": len(made), "ids": made}
    except Cancelled:
        db.set_source(key, status="cancelled")
        raise
    except Exception as e:
        db.set_source(key, status="error", error=str(e)[:500])
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- discovery

def list_recent(streamer_id: str, limit: int = 4) -> list[dict]:
    st = get_streamer(streamer_id)
    if not st:
        return []
    items = []
    if st.get("youtube"):
        try:
            items += sources.yt_latest(st["youtube"], limit)
        except Exception:
            pass
    if st.get("kick"):
        try:
            items += sources.kick_videos(st["kick"], limit)
        except Exception:
            pass
    busy = manager.busy_keys()
    for it in items:
        key = f"{it['platform']}:{it['id']}"
        s = db.get_source(key)
        it["status"] = "queued" if key in busy and not s else (s["status"] if s else None)
        it["clips"] = s["clips"] if s else 0
        it.pop("hls", None)
    return items


def submit_vod(streamer_id: str, url: str, max_clips: int | None = None, auto: bool = False):
    st = get_streamer(streamer_id)
    return manager.submit("vod", streamer_id, f"{st['name'] if st else streamer_id}: {url}", process_vod,
                          streamer_id, url, max_clips=max_clips, auto=auto, lane="vod", key=f"vod:{url}")


def scan_streamer(streamer_id: str) -> int:
    """Queue the newest not-yet-processed upload/stream from each platform."""
    cfg = settings()
    per = max(1, int(cfg["videos_per_scan"]))
    groups: dict[str, int] = {}
    queued = 0
    for it in list_recent(streamer_id, limit=4):
        group = f"{it['platform']}:{it['kind']}"
        if groups.get(group, 0) >= per:
            continue
        if it.get("status") in ("done", "processing", "skipped", "queued", "error", "cancelled"):
            groups[group] = groups.get(group, 0) + 1
            continue
        if it.get("duration") and it["duration"] < cfg["skip_videos_shorter_than"]:
            continue
        submit_vod(streamer_id, it["url"], auto=True)
        groups[group] = groups.get(group, 0) + 1
        queued += 1
    return queued


# --------------------------------------------------------------------------- permanent titles for older clips

# Title positions used by the previous renderer (title shown only for the first seconds in these layouts).
_LEGACY_TITLE_POS = {"split": {"name_y": 64, "title_y": 170}, "crop": {"name_y": 150, "title_y": 280}}


def _rel_words(clip: dict) -> list[dict]:
    return [{"start": w["start"], "end": w["end"], "word": w["word"]} for w in clip.get("words") or []]


# --------------------------------------------------------------------------- kept sources + title editing

def keep_source(raw: Path, streamer_id: str, clip_id: str) -> str | None:
    """Keep the raw footage of a clip (moved out of the temp folders) so it can be re-rendered later."""
    if float(settings().get("keep_sources_days", 14)) <= 0 or not Path(raw).exists():
        return None
    dst = SOURCES_DIR / streamer_id / f"{clip_id}{Path(raw).suffix}"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(raw), dst)
    except OSError as e:
        logger.warning("could not keep source of %s: %s", clip_id, e)
        return None
    return f"{streamer_id}/{dst.name}"


def source_path(clip: dict) -> Path | None:
    rel = (clip.get("effects") or {}).get("source_file")
    p = SOURCES_DIR / rel if rel else None
    return p if p and p.exists() else None


def delete_source(clip: dict) -> None:
    p = source_path(clip)
    if p:
        p.unlink(missing_ok=True)


def cleanup_sources() -> int:
    days = float(settings().get("keep_sources_days", 14))
    cutoff = time.time() - max(1.0, days) * 86400
    removed = 0
    for p in SOURCES_DIR.glob("*/*"):
        if p.is_file() and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    return removed


def title_editable(clip: dict) -> bool:
    return source_path(clip) is not None or (clip.get("source_kind") != "live" and clip.get("platform") in ("youtube", "kick"))


def _replace(src: Path, dst: Path) -> None:
    # The clip may be streaming to a browser right now (file locked on Windows): retry for a few seconds.
    for i in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == 19:
                raise RuntimeError("Fișierul clipului e folosit (se redă în browser?). Oprește playerul și reîncearcă.")
            time.sleep(0.5)


def render_into(clip: dict, st: dict, raw: Path, raw_offset: float, hook: str, log=None) -> dict:
    """Render the clip again from its raw footage with the same cut/words, then swap the files in place."""
    out = CLIPS_DIR / clip["file"]
    tmp = out.with_name(out.stem + ".new.mp4")
    fx_old = clip.get("effects") or {}
    layout = fx_old.get("layout") or st.get("layout") or "auto"
    try:
        res = render.render_clip(
            str(raw), clip["start"] - raw_offset, clip["duration"], _rel_words(clip), tmp,
            hook=hook, name=st["name"], credit="", color=st.get("color") or "#FFD400",
            crop_sides=float(st.get("crop_sides") or 0), layout=layout, language=clip.get("language"), log=log,
            subs=subs_label(st),
        )
        _replace(tmp, out)
        for ext in (".srt", ".jpg"):
            if tmp.with_suffix(ext).exists():
                _replace(tmp.with_suffix(ext), out.with_suffix(ext))
    finally:
        for ext in (".mp4", ".srt", ".jpg"):
            tmp.with_suffix(ext).unlink(missing_ok=True)
    fx = res["effects"]
    for k in ("source_file", "source_offset"):
        if k in fx_old:
            fx[k] = fx_old[k]
    return dict(clip, title=hook, effects=fx, thumb=f"{st['id']}/{out.with_suffix('.jpg').name}",
                srt=f"{st['id']}/{out.with_suffix('.srt').name}",
                words=[{k: w[k] for k in ("start", "end", "word", "level", "kind")} for w in res["words"]])


def retitle_clip(job, clip_id: str, title: str) -> dict:
    clip = db.get_clip(clip_id)
    if not clip:
        raise RuntimeError("Clipul nu mai există")
    st = get_streamer(clip["streamer"]) or {"id": clip["streamer"], "name": clip["streamer"]}
    title = analysis.censor(" ".join(title.split()))[:90]
    raw = source_path(clip)
    if raw:
        job.set_progress(0.1, "Refac clipul cu titlul nou…")
        clip = render_into(clip, st, raw, float((clip.get("effects") or {}).get("source_offset") or 0), title,
                           log=job.log)
        db.add_clip(clip)
    elif clip.get("source_kind") != "live":
        job.set_progress(0.05, "Sursa nu mai e pe PC – descarc din nou bucata din video…")
        clip = rerender_vod_clip(job, clip, hook=title)
    else:
        raise RuntimeError("Clip live mai vechi: sursa lui nu a fost păstrată, titlul din video nu se mai poate schimba.")
    job.log(f"Titlu nou în video: {title}")
    return {"clip": clip["id"]}


def submit_retitle(clip_id: str, title: str):
    clip = db.get_clip(clip_id) or {}
    return manager.submit("edit", clip.get("streamer", "?"), f"Titlu nou: {title[:50]}", retitle_clip, clip_id, title,
                          lane="edit", key=f"retitle:{clip_id}", priority=0)


def rerender_vod_clip(job, clip: dict, hook: str | None = None) -> dict:
    """Re-download the source section of a VOD clip and render it again with the current editor."""
    st = get_streamer(clip["streamer"])
    if not st:
        raise RuntimeError("Streamerul nu mai există")
    if clip["platform"] == "youtube":
        src = {"platform": "youtube", "id": clip["source_id"], "kind": clip.get("source_kind") or "video",
               "url": f"https://www.youtube.com/watch?v={clip['source_id']}", "title": clip.get("source_title") or ""}
    else:
        src = sources.resolve((clip.get("source_url") or "").split("?")[0])
    work = WORK_DIR / safe(f"rr_{clip['id']}")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        lo, hi = max(0.0, clip["start"] - 1.0), clip["end"] + 1.0
        raw = download_section(job, src, lo, hi, work, "raw")
        if hook is None:
            peak = ((clip.get("effects") or {}).get("peak") or clip["duration"] / 2)
            hook = titles.make_title(_rel_words(clip), peak, clip.get("language"), st["name"],
                                     clip.get("source_title") or "", clip.get("reasons"), log=job.log)
        clip = render_into(clip, st, raw, lo, hook, log=job.log)
        kept = keep_source(raw, st["id"], clip["id"])
        if kept:
            clip["effects"].update(source_file=kept, source_offset=round(lo, 3))
        db.add_clip(clip)
        return clip
    finally:
        shutil.rmtree(work, ignore_errors=True)


def overlay_title(clip: dict) -> None:
    """Lay a permanent title over an already rendered clip (its source is gone), exactly over the old one."""
    from . import captions

    st = get_streamer(clip["streamer"]) or {"name": clip["streamer"], "color": "#FFD400"}
    pos = _LEGACY_TITLE_POS[(clip.get("effects") or {}).get("layout")]
    src = CLIPS_DIR / clip["file"]
    dur = render.probe(src)["duration"] or clip["duration"]
    ass = WORK_DIR / f"title_{safe(clip['id'])}.ass"
    events = captions.title_events(clip.get("title") or "", st["name"], dur, pos, legacy=True)
    ass.write_text(captions.ass_header(st.get("color") or "#FFD400") + "\n".join(events) + "\n", encoding="utf-8")
    tmp = src.with_name(src.stem + ".titled.mp4")
    enc = render.pick_encoder()
    rel = ass.relative_to(ROOT).as_posix()
    cmd = [FFMPEG, "-y", "-nostdin", "-v", "error", "-i", str(src), "-vf", f"ass={rel}:fontsdir=assets/fonts",
           *render._encoder_args(enc), "-c:a", "copy", "-movflags", "+faststart", str(tmp)]
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0 and enc == "h264_nvenc":
            cmd = [FFMPEG, "-y", "-nostdin", "-v", "error", "-i", str(src), "-vf", f"ass={rel}:fontsdir=assets/fonts",
                   *render._encoder_args("libx264"), "-c:a", "copy", "-movflags", "+faststart", str(tmp)]
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0 or not tmp.exists():
            raise RuntimeError(p.stderr[-400:])
        tmp.replace(src)
    finally:
        ass.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
    thumb = CLIPS_DIR / clip["thumb"] if clip.get("thumb") else None
    if thumb:
        t = min(max(0.5, ((clip.get("effects") or {}).get("peak") or 1.0) + 0.3), max(0.5, dur - 0.5))
        subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=540:-2", "-q:v", "3", str(thumb)], capture_output=True)


def clips_needing_titles() -> list[dict]:
    return [c for c in db.list_clips(limit=100000) if (c.get("effects") or {}).get("title_v") != 3]


def upgrade_titles(job) -> dict:
    """One-off upgrade: every clip ends up with a title on screen for its whole duration."""
    todo = clips_needing_titles()
    stats = {"rerendered": 0, "overlaid": 0, "already": 0, "failed": 0}
    for i, clip in enumerate(todo):
        job.check()
        job.set_progress(i / max(1, len(todo)), f"Titlu permanent {i + 1}/{len(todo)}: {clip['id']}")
        fx = dict(clip.get("effects") or {})
        layout = fx.get("layout")
        try:
            if not (CLIPS_DIR / clip["file"]).exists():
                continue
            if clip.get("source_kind") != "live" and clip.get("platform") in ("youtube", "kick"):
                try:
                    rerender_vod_clip(job, clip)
                    stats["rerendered"] += 1
                    continue
                except Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001 - fall back to the overlay below
                    job.log(f"Nu am putut reface {clip['id']} din sursă ({str(e)[:120]}); pun titlul peste.")
            if layout in _LEGACY_TITLE_POS:
                overlay_title(clip)
                stats["overlaid"] += 1
            else:
                stats["already"] += 1  # first renderer and the blur layout already kept the title on screen
            fx["title_v"] = 3
            db.add_clip(dict(clip, effects=fx))
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            job.log(f"Eroare la {clip['id']}: {str(e)[:200]}")
    job.log(f"Gata: {stats['rerendered']} refăcute, {stats['overlaid']} cu titlu suprapus, "
            f"{stats['already']} aveau deja titlu, {stats['failed']} erori.")
    return stats


def submit_title_upgrade():
    if not clips_needing_titles():
        return None
    return manager.submit("maint", "toti", "Titlu permanent pe clipurile existente", upgrade_titles,
                          lane="maint", key="upgrade_titles", priority=50)

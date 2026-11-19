"""Vertical 9:16 render: smart framing (split / crop / blur), word-synced captions with emphasis, reaction effects."""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np

from . import analysis, captions, framing, header
from .config import FFMPEG, FFPROBE, ROOT, WORK_DIR, settings

W, H = 1080, 1920
_encoder: str | None = None


def probe(path) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True,
    ).stdout
    data = json.loads(out or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {
        "width": int(v.get("width") or 1920),
        "height": int(v.get("height") or 1080),
        "duration": float((data.get("format") or {}).get("duration") or 0),
        "has_audio": a is not None,
    }


def pick_encoder() -> str:
    global _encoder
    pref = settings().get("encoder", "auto")
    if pref in ("libx264", "h264_nvenc"):
        return pref
    if _encoder is None:
        test = subprocess.run(
            [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.3",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True,
        )
        _encoder = "h264_nvenc" if test.returncode == 0 else "libx264"
    return _encoder


def _encoder_args(enc: str, q: int = 20) -> list[str]:
    if enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0",
                "-maxrate", "16M", "-bufsize", "32M", "-profile:v", "high"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(q - 1), "-profile:v", "high"]


# --------------------------------------------------------------------------- effects


def plan_effects(db: np.ndarray, hop: float, duration: float, words: list[dict]) -> dict:
    """Punch-zooms land on the most important / loudest words; shake + flash only for real screams."""
    if len(db) < 20:
        return {"punches": [], "main": None, "shake": None, "flash": None, "peak": duration / 2, "loudness_z": 0}
    sm = analysis.moving_avg(db, 3)
    med = float(np.median(sm))
    mad = float(np.median(np.abs(sm - med))) * 1.4826 + 1.0
    z = (sm - med) / mad

    def z_at(t: float) -> float:
        i = int(t / hop)
        win = z[max(0, i - 2): i + 6]
        return float(win.max()) if len(win) else 0.0

    cands = []
    for w in words:
        if w.get("level", 0) >= 1:
            cands.append((w["start"], 2.0 * w["level"] + max(0.0, z_at(w["start"]))))
    for i in np.argsort(z)[::-1][:40]:
        if z[i] >= 2.2:
            cands.append((i * hop, float(z[i])))
    punches: list[float] = []
    for t, _ in sorted(cands, key=lambda c: -c[1]):
        if len(punches) >= 4:
            break
        if t < 0.6 or t > duration - 1.0 or any(abs(t - p) < 3.2 for p in punches):
            continue
        punches.append(round(max(0.0, t - 0.06), 2))
    ranked = list(punches)  # strongest first
    zmax = float(z.max())
    main = punches[0] if punches else None
    main_z = z_at(main) if main is not None else 0.0
    peak = main if main is not None else float(np.argmax(z) * hop)
    return {
        "punches": sorted(punches),
        "ranked": ranked,
        "main": main,
        "shake": main if main is not None and main_z >= 3.0 else None,
        "flash": round(main - 0.03, 2) if main is not None and main_z >= 3.8 else None,
        "peak": round(peak, 2),
        "loudness_z": round(zmax, 2),
    }


def _envelope(p: float, amp: float) -> str:
    return (f"{amp}*if(lt(it,{p}),0,if(lt(it,{p + 0.1:.2f}),(it-{p})/0.1,"
            f"if(lt(it,{p + 0.85:.2f}),1,if(lt(it,{p + 1.35:.2f}),1-(it-{p + 0.85:.2f})/0.5,0))))")


def _punch(w: int, h: int, fps: int, fx: dict, strength: float = 1.0) -> str:
    zoom = "1" + "".join(
        "+" + _envelope(p, round((0.16 if p == fx.get("main") else 0.09) * strength, 3)) for p in fx["punches"]
    )
    sx = sy = ""
    if fx.get("shake") is not None:
        s = fx["shake"]
        sx = f"+if(between(it,{s},{s + 0.45:.2f}),{14 * strength:.0f}*sin(it*83),0)"
        sy = f"+if(between(it,{s},{s + 0.45:.2f}),{10 * strength:.0f}*cos(it*71),0)"
    return (f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2){sx}':y='ih/2-(ih/zoom/2){sy}'"
            f":d=1:s={w}x{h}:fps={fps}")


# --------------------------------------------------------------------------- layouts


def _video_graph(frame: dict, fx: dict, fps: int, crop_sides: float, accent: str) -> tuple[str, dict]:
    """Return (filter graph producing [base] at 1080x1920, text layout positions)."""
    lay = frame["layout"]
    src_w, src_h = frame["src_w"], frame["src_h"]
    head = f"[0:v]fps={fps},setsar=1"
    if lay == "split":
        cw, ch, cx, cy = frame["cam"]
        gw, gh, gx, gy = frame["game"]
        band = framing.TITLE_H
        cam_h = int(frame.get("cam_h") or framing.CAM_H)
        game_h = H - band - cam_h
        seam = band + cam_h
        bar = accent.lstrip("#") if len(accent.lstrip("#")) == 6 else "FFD400"
        # A streamer-defined cam area holds overlays at its edges (live sub counter): zoom the gameplay instead.
        cam_fx = "" if frame.get("fixed_cam") else f",{_punch(W, cam_h, fps, fx)}"
        game_fx = f",{_punch(W, game_h, fps, fx, 0.7)}" if frame.get("fixed_cam") else ""
        graph = (
            f"{head},split=3[b0][c0][g0];"
            f"[b0]scale=216:384:force_original_aspect_ratio=increase,crop=216:384,gblur=sigma=8,"
            f"scale={W}:{H},eq=brightness=-0.38:saturation=1.1[bg];"
            f"[c0]crop={cw}:{ch}:{cx}:{cy},scale={W}:{cam_h}:flags=lanczos{cam_fx}[cam];"
            f"[g0]crop={gw}:{gh}:{gx}:{gy},scale={W}:{game_h}:flags=lanczos{game_fx}[game];"
            f"[bg][cam]overlay=x=0:y={band}:shortest=1[t1];[t1][game]overlay=x=0:y={seam}:shortest=1,"
            f"drawbox=x=0:y={band - 4}:w={W}:h=4:color=0x{bar}@1:t=fill,"
            f"drawbox=x=0:y={seam - 3}:w={W}:h=6:color=0x{bar}@1:t=fill,"
            f"unsharp=5:5:0.55[base]"
        )
        return graph, {"caption_y": seam + 6, "name_y": 52, "name_fs": 36, "title_y": 190}
    if lay == "crop":
        cw = frame["crop_w"]
        x = framing.crop_x_expr(frame["track"])
        graph = (
            f"{head},crop=w={cw}:h={src_h}:x='{x}':y=0,scale={W}:{H}:flags=lanczos,"
            f"{_punch(W, H, fps, fx, 0.85)},unsharp=5:5:0.5[base]"
        )
        return graph, {"caption_y": int(H * 0.70), "name_y": 150, "title_y": 280}

    cs = max(0.0, min(0.3, crop_sides))
    cw = src_w * (1 - 2 * cs)
    fh = min(H, int(round(W * src_h / cw / 2) * 2))
    fy = max(0, (H - fh) // 2 - (60 if fh < 1200 else 0))
    graph = (
        f"{head},split=2[b0][f0];"
        f"[b0]scale=216:384:force_original_aspect_ratio=increase,crop=216:384,gblur=sigma=6,"
        f"scale={W}:{H},eq=brightness=-0.22:saturation=1.25[bg];"
        f"[f0]crop=w=iw*{1 - 2 * cs:.4f}:h=ih,scale={W}:{fh}:flags=lanczos,{_punch(W, fh, fps, fx)}[fg];"
        f"[bg][fg]overlay=x=0:y={fy}:shortest=1[base]"
    )
    if fh < 1200:
        pos = {"caption_y": fy + fh + 150, "name_y": int(fy * 0.36), "title_y": int(fy * 0.68), "persistent_title": True}
    else:
        pos = {"caption_y": int(H * 0.72), "name_y": 170, "title_y": 300}
    return graph, pos


def plan_teaser(fx: dict, duration: float) -> list[tuple[float, float]]:
    """Cold open: the strongest action of the clip shown first (optionally preceded by a second one), then the
    clip plays from its setup. Skipped when the action already happens in the first seconds."""
    main = fx.get("main") if fx.get("main") is not None else fx.get("peak")
    if main is None or main < 5.0 or duration < 12:
        return []
    snips = [(round(max(0.0, main - 0.5), 2), round(min(duration - 0.3, main + 1.7), 2))]
    others = [p for p in fx.get("ranked") or [] if abs(p - main) >= 3.5 and 1.0 <= p <= duration - 1.5]
    if others:
        p2 = others[0]
        snips.insert(0, (round(max(0.0, p2 - 0.3), 2), round(min(duration - 0.3, p2 + 0.9), 2)))
    return [s for s in snips if s[1] - s[0] >= 0.6]


def _compose_cold_open(main_mp4: Path, out_mp4: Path, snips: list[tuple[float, float]], duration: float,
                       fps: int, accent: str, has_audio: bool, log=None) -> float:
    """Final video = teaser snippets cut from the rendered clip + the whole clip, flash cuts, one progress bar."""
    teaser = sum(b - a for a, b in snips)
    total = teaser + duration
    inputs, parts, graph = [], [], []
    for i, (a, b) in enumerate(snips):
        inputs += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(main_mp4)]
        graph.append(f"[{i}:v]setpts=PTS-STARTPTS[v{i}]")
        if has_audio:
            graph.append(f"[{i}:a]asetpts=PTS-STARTPTS,afade=t=in:d=0.03,afade=t=out:st={b - a - 0.05:.3f}:d=0.05[a{i}]")
        parts.append(f"[v{i}]" + (f"[a{i}]" if has_audio else ""))
    m = len(snips)
    inputs += ["-i", str(main_mp4)]
    graph.append(f"[{m}:v]setpts=PTS-STARTPTS[v{m}]")
    if has_audio:
        graph.append(f"[{m}:a]asetpts=PTS-STARTPTS,afade=t=in:d=0.05[a{m}]")
    parts.append(f"[v{m}]" + (f"[a{m}]" if has_audio else ""))
    graph.append("".join(parts) + f"concat=n={m + 1}:v=1:a={1 if has_audio else 0}[cv]" + ("[ca]" if has_audio else ""))
    cuts, t = [], 0.0
    for a, b in snips:
        t += b - a
        cuts.append(t)
    flash = "+".join(f"if(between(t,{c:.3f},{c + 0.12:.3f}),0.45*(1-(t-{c:.3f})/0.12),0)" for c in cuts)
    bar = accent.lstrip("#") if len(accent.lstrip("#")) == 6 else "FFD400"
    graph.append(f"[cv]eq=brightness='{flash}':eval=frame[cf]")
    graph.append(f"color=c=0x{bar}:s={W}x12:r={fps}[bar]")
    graph.append(f"[cf][bar]overlay=x='-w+w*t/{total:.3f}':y={H - 12}:shortest=1,format=yuv420p[v]")
    enc = pick_encoder()

    def build(encoder: str) -> list[str]:
        cmd = [FFMPEG, "-y", "-nostdin", "-v", "error", *inputs, "-filter_complex", ";".join(graph), "-map", "[v]"]
        if has_audio:
            cmd += ["-map", "[ca]", "-c:a", "aac", "-b:a", "192k"]
        return cmd + _encoder_args(encoder) + ["-r", str(fps), "-movflags", "+faststart", str(out_mp4)]

    proc = subprocess.run(build(enc), cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 and enc == "h264_nvenc":
        proc = subprocess.run(build("libx264"), cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg cold open failed: {proc.stderr[-1500:]}")
    if log:
        log(f"Început cu acțiune: {len(snips)} secvențe ({teaser:.1f}s), apoi clipul întreg.")
    return teaser


# --------------------------------------------------------------------------- render


def render_clip(src: str, start: float, duration: float, words: list[dict], out_mp4: Path, *,
                hook: str, name: str, credit: str, color: str, crop_sides: float = 0.0,
                layout: str = "auto", language: str | None = None, log=None, subs: str | None = None,
                cam_region: list | None = None) -> dict:
    """Render [start, start+duration] of `src`. `words` must be relative to `start`."""
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    tmp = WORK_DIR / f"render_{out_mp4.stem}"
    tmp.mkdir(parents=True, exist_ok=True)
    accent = color or "#FFD400"
    try:
        info = probe(src)
        fps = int(settings().get("output_fps", 30))
        hop = 0.05
        db = analysis.rms_db_series(src, hop=hop, start=start, duration=duration)
        loud = captions.word_loudness(db, hop, words)
        words_fx = captions.emphasis(words, language, loud)
        fx = plan_effects(db, hop, duration, words_fx)

        frame = framing.analyze(src, start, duration, info["width"], info["height"], layout, cam_region=cam_region)
        graph, pos = _video_graph(frame, fx, fps, crop_sides, accent)
        if log:
            log(f"Layout: {frame['layout']}{' (zona camerei + contoare)' if frame.get('fixed_cam') else ''} · "
                "cuvinte cheie: " + ", ".join(w["word"] for w in words_fx if w["level"] >= 1)[:160])
        snips = plan_teaser(fx, duration) if settings().get("cold_open", True) else []

        header_png = None
        try:
            header_png = header.render(tmp / "header.png", hook, name, accent, pos, subs=subs)
        except Exception as e:  # noqa: BLE001 - fall back to the plain ASS title
            if log:
                log(f"Header grafic indisponibil ({e}); folosesc titlul simplu.")
        ass_path = tmp / "captions.ass"
        cs = captions.build_ass(ass_path, words_fx, duration, hook=hook, name=name, credit=credit,
                                accent=accent, layout=pos, header=header_png is not None)
        srt_path = out_mp4.with_suffix(".srt")
        captions.write_srt(srt_path, cs)

        flash = ""
        if fx.get("flash") is not None:
            f0 = max(0.0, fx["flash"])
            flash = f",eq=brightness='if(between(t,{f0},{f0 + 0.16:.2f}),0.42*(1-(t-{f0})/0.16),0)':eval=frame"
        bar = accent.lstrip("#") if len(accent.lstrip("#")) == 6 else "FFD400"
        rel_ass = ass_path.relative_to(ROOT).as_posix()
        graph += f";[base]eq=contrast=1.05:saturation=1.12{flash},ass={rel_ass}:fontsdir=assets/fonts[txt];"
        if header_png is not None:
            # With a cold open the header must already be there when the clip restarts: no fade.
            hdr = "format=rgba" if snips else "format=rgba,fade=t=in:st=0:d=0.3:alpha=1"
            graph += f"[1:v]{hdr}[hdr];[txt][hdr]overlay=x=0:y=0:shortest=1[txh];"
        else:
            graph += "[txt]null[txh];"
        if snips:  # progress bar is added once over the final video
            graph += "[txh]format=yuv420p[v]"
        else:
            graph += (
                f"color=c=0x{bar}:s={W}x12:r={fps}[bar];"
                f"[txh][bar]overlay=x='-w+w*t/{duration:.3f}':y={H - 12}:shortest=1,format=yuv420p[v]"
            )
        enc = pick_encoder()
        fade_out = max(0.0, duration - 0.2)
        main_mp4 = tmp / "main.mp4" if snips else out_mp4

        def build(encoder: str) -> list[str]:
            cmd = [FFMPEG, "-y", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                   "-i", str(src)]
            if header_png is not None:
                cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{duration:.3f}", "-i", str(header_png)]
            cmd += ["-filter_complex", graph, "-map", "[v]"]
            if info["has_audio"]:
                cmd += ["-map", "0:a:0", "-af",
                        "aresample=async=1:first_pts=0,loudnorm=I=-14:TP=-1.5:LRA=11,"
                        f"afade=t=out:st={fade_out:.3f}:d=0.2,aresample=48000",
                        "-c:a", "aac", "-b:a", "192k"]
            # The intermediate of a cold open is re-encoded once more: keep it near-lossless.
            return cmd + _encoder_args(encoder, 16 if snips else 20) + [
                "-r", str(fps), "-movflags", "+faststart", "-t", f"{duration:.3f}", str(main_mp4)]

        proc = subprocess.run(build(enc), cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0 and enc == "h264_nvenc":
            if log:
                log("NVENC a eșuat, reîncerc cu libx264")
            proc = subprocess.run(build("libx264"), cwd=ROOT, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg render failed: {proc.stderr[-1500:]}")

        teaser = 0.0
        t_thumb = min(max(0.5, (fx.get("peak") or duration / 2) + 0.3), max(0.5, duration - 0.5))
        if snips:
            teaser = _compose_cold_open(main_mp4, out_mp4, snips, duration, fps, accent, info["has_audio"], log)
            # SRT of the final video: teaser words re-timed, then the whole clip shifted.
            tw, t0 = [], 0.0
            for a, b in snips:
                tw += [dict(w, start=w["start"] - a + t0, end=min(w["end"], b) - a + t0)
                       for w in words_fx if a <= w["start"] < b - 0.1]
                t0 += b - a
            tw += [dict(w, start=w["start"] + teaser, end=w["end"] + teaser) for w in words_fx]
            captions.write_srt(srt_path, captions.chunks(tw))
            a_last, b_last = snips[-1]
            main_t = fx.get("main") if fx.get("main") is not None else fx.get("peak")
            t_thumb = teaser - (b_last - a_last) + (main_t - a_last) + 0.3

        thumb = out_mp4.with_suffix(".jpg")
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-ss", f"{t_thumb:.2f}", "-i", str(out_mp4), "-frames:v", "1",
             "-vf", "scale=540:-2", "-q:v", "3", str(thumb)],
            capture_output=True,
        )
        fx["layout"] = frame["layout"]
        fx["title_v"] = 3
        fx["keywords"] = [w["word"] for w in words_fx if w["level"] >= 1]
        fx["teaser"] = [list(s) for s in snips]
        fx["video_duration"] = round(duration + teaser, 2)
        return {"effects": fx, "thumb": thumb, "srt": srt_path, "encoder": enc, "layout": frame["layout"],
                "words": words_fx}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

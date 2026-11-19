"""Finding the interesting moments: audio/chat/heatmap signals, peak picking, transcript scoring."""

import json
import re
import subprocess

import numpy as np

from .config import FFMPEG

HOP = 0.5  # seconds per analysis step
_SR = 8000

# --------------------------------------------------------------------------- signals


def rms_db_series(path: str, hop: float = HOP, start: float | None = None, duration: float | None = None) -> np.ndarray:
    """Loudness (dB) of the voice band per `hop` seconds, streamed so multi-hour VODs stay cheap."""
    cmd = [FFMPEG, "-nostdin", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-af", "highpass=f=250,lowpass=f=3800", "-ac", "1", "-ar", str(_SR), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = int(_SR * hop)
    frame_bytes = n * 2
    out, rest = [], b""
    while True:
        buf = proc.stdout.read(frame_bytes * 1200)
        if not buf:
            break
        data = rest + buf
        k = len(data) // frame_bytes
        if k:
            arr = np.frombuffer(data[: k * frame_bytes], dtype=np.int16).astype(np.float32).reshape(k, n) / 32768.0
            out.append(20 * np.log10(np.sqrt((arr ** 2).mean(axis=1)) + 1e-5))
        rest = data[k * frame_bytes:]
    proc.wait()
    if not out:
        return np.zeros(0, np.float32)
    return np.maximum(np.concatenate(out), -70.0)


def moving_avg(x: np.ndarray, win: int) -> np.ndarray:
    if win <= 1 or len(x) == 0:
        return x
    k = np.ones(win) / win
    return np.convolve(x, k, mode="same")


def _rolling(x: np.ndarray, win: int, step: int, fn) -> np.ndarray:
    n = len(x)
    if n == 0:
        return x
    centers = np.arange(0, n, max(1, step))
    half = win // 2
    vals = np.array([fn(x[max(0, c - half): min(n, c + half + 1)]) for c in centers])
    return np.interp(np.arange(n), centers, vals)


def robust_z(x: np.ndarray, window_s: float, hop: float = HOP) -> np.ndarray:
    """z-score against a rolling median/MAD baseline (handles quiet and loud segments of a stream)."""
    if len(x) == 0:
        return x
    win = max(5, int(window_s / hop))
    step = max(1, int(5 / hop))
    med = _rolling(x, win, step, np.median)
    mad = _rolling(np.abs(x - med), win, step, np.median)
    return (x - med) / (1.4826 * mad + 1.0)


def audio_signal(db: np.ndarray) -> np.ndarray:
    """Excitement from sustained loudness spikes (yelling, laughing, hype) + sudden reaction onsets."""
    smooth = moving_avg(db, 4)  # 2s
    z = robust_z(smooth, 120)
    # Onset: loudness of the next 2s vs. the previous 8s - the classic "calm, then explodes" clip moment.
    n = len(db)
    if n > 30:
        c = np.concatenate(([0.0], np.cumsum(db, dtype=np.float64)))
        idx = np.arange(n)
        a1 = np.minimum(n, idx + 4)
        b0 = np.maximum(0, idx - 16)
        nxt = (c[a1] - c[idx]) / np.maximum(1, a1 - idx)
        prv = (c[idx] - c[b0]) / np.maximum(1, idx - b0)
        jump = np.where(idx >= 4, nxt - prv, 0.0)
        onset = np.clip(robust_z(jump, 180), 0, 5)
        z = z + 0.45 * onset
    return np.clip(z, -2, 6)


def reaction_end(db: np.ndarray, peak: float, hop: float = HOP, max_after: float = 14.0) -> float:
    """When the reaction after `peak` calms down (loudness back near the local baseline)."""
    if len(db) == 0:
        return peak + 6
    sm = moving_avg(db, 3)
    i0 = min(len(sm) - 1, int(peak / hop))
    lo = max(0, i0 - int(60 / hop))
    base = float(np.median(sm[lo:i0 + 1])) if i0 > lo else float(np.median(sm))
    top = float(sm[i0: i0 + int(3 / hop)].max()) if i0 < len(sm) else base
    calm = base + 0.35 * max(0.0, top - base)
    quiet_run = 0
    for i in range(i0 + int(1.5 / hop), min(len(sm), i0 + int(max_after / hop))):
        quiet_run = quiet_run + 1 if sm[i] <= calm else 0
        if quiet_run * hop >= 1.2:
            return (i - quiet_run) * hop + 0.6
    return peak + max_after * 0.6


def event_signal(times: list[float], weights: list[float] | None, n: int, hop: float = HOP,
                 smooth_s: float = 5, window_s: float = 180) -> np.ndarray:
    """Per-step activity (chat messages) -> burst strength relative to the stream's own baseline.

    Uses the ratio to the rolling median (2x = +3, 3x = +4.75) so that huge, naturally noisy chats
    (IShowSpeed, Kai Cenat) don't look like constant explosions.
    """
    counts = np.zeros(n, np.float32)
    if n == 0:
        return counts
    w = weights or [1.0] * len(times)
    for t, wt in zip(times, w):
        i = int(t / hop)
        if 0 <= i < n:
            counts[i] += wt
    counts = moving_avg(counts, max(1, int(smooth_s / hop)))
    win = max(5, int(window_s / hop))
    med = _rolling(counts, win, max(1, int(5 / hop)), np.median)
    ratio = (counts + 0.5) / (med + 0.5)
    return np.clip(3.0 * np.log2(ratio), -2, 6)


def heatmap_signal(heatmap: list[dict], n: int, hop: float = HOP) -> np.ndarray:
    """YouTube 'Most replayed' graph -> 0..4 bonus around the most replayed sections."""
    if not heatmap or n == 0:
        return np.zeros(n, np.float32)
    t = np.arange(n) * hop
    centers = np.array([(h["start_time"] + h["end_time"]) / 2 for h in heatmap])
    vals = np.array([h["value"] for h in heatmap], dtype=np.float32)
    v = np.interp(t, centers, vals)
    med = float(np.median(vals))
    return np.clip(4.0 * (v - med) / max(1e-3, 1.0 - med), 0, 4)


def community_clip_signal(moments: list[tuple[float, float]], n: int, hop: float = HOP) -> np.ndarray:
    """Kick community clips -> gaussian bumps weighted by views."""
    out = np.zeros(n, np.float32)
    if not moments or n == 0:
        return out
    t = np.arange(n) * hop
    for center, views in moments:
        weight = 1.0 + np.log10(1.0 + views)
        out += weight * np.exp(-0.5 * ((t - center) / 15.0) ** 2)
    if out.max() > 0:
        out = 4.0 * out / out.max()
    return out


def youtube_chat_replay_times(path: str) -> list[float]:
    """Parse yt-dlp's .live_chat.json (one JSON object per line) -> message offsets in seconds."""
    times = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            act = obj.get("replayChatItemAction") or {}
            off = act.get("videoOffsetTimeMsec")
            if off is not None:
                times.append(int(off) / 1000.0)
    return times


WEIGHTS = {"audio": 1.0, "chat": 1.1, "heatmap": 1.3, "community": 1.0}


def combine(signals: dict[str, np.ndarray]) -> np.ndarray:
    n = max((len(v) for v in signals.values()), default=0)
    total = np.zeros(n, np.float32)
    for name, sig in signals.items():
        if len(sig) == 0:
            continue
        s = np.zeros(n, np.float32)
        s[: len(sig)] = sig[:n]
        total += WEIGHTS.get(name, 1.0) * s
    return moving_avg(total, 3)


def pick_peaks(score: np.ndarray, k: int, min_gap_s: float, hop: float = HOP,
               skip_start_s: float = 0, skip_end_s: float = 0) -> list[float]:
    n = len(score)
    lo, hi = int(skip_start_s / hop), n - int(skip_end_s / hop)
    if hi <= lo:
        lo, hi = 0, n
    order = np.argsort(score[lo:hi])[::-1] + lo
    chosen: list[float] = []
    for i in order:
        t = i * hop
        if all(abs(t - c) >= min_gap_s for c in chosen):
            chosen.append(t)
            if len(chosen) >= k:
                break
    return chosen


def explain(signals: dict[str, np.ndarray], t: float, hop: float = HOP) -> list[str]:
    i = int(t / hop)
    reasons = []

    def val(name):
        sig = signals.get(name)
        if sig is None or len(sig) == 0:
            return 0.0
        j = min(max(i, 0), len(sig) - 1)
        window = sig[max(0, j - 6): j + 7]
        return float(window.max()) if len(window) else 0.0

    if val("audio") >= 2:
        reasons.append("🔊 Volum ridicat / țipete")
    if val("chat") >= 2:
        reasons.append("💬 Chat-ul a explodat")
    if val("heatmap") >= 1.5:
        reasons.append("🔥 Most replayed pe YouTube")
    if val("community") >= 1.5:
        reasons.append("✂️ Clipuit de comunitate pe Kick")
    return reasons


# --------------------------------------------------------------------------- transcript

HYPE = {
    "en": [
        "what", "bro", "yo", "no way", "oh my god", "omg", "let's go", "lets go", "crazy", "insane",
        "wait", "chat", "bruh", "holy", "damn", "wtf", "what the", "yes", "nah", "hell no", "oh no",
        "run", "help", "dude", "huh", "why", "stop", "goat", "fire", "scared", "shut up", "crying",
        "no no", "oh shit", "shit", "fuck", "unbelievable", "hold on", "look at", "are you serious",
    ],
    "ro": [
        "frate", "bă", "băi", "bai", "doamne", "ce naiba", "ce dracu", "nu se poate", "incredibil",
        "nebun", "nebunie", "hai", "gata", "mamă", "mama", "pe bune", "serios", "wow", "aoleu", "vai",
        "fugi", "ajutor", "uite", "ce-i asta", "ce e asta", "nu nu", "da da", "ce faci", "stai",
        "nu cred", "mor", "mamă mamă", "bravo", "hahaha",
    ],
}
_LAUGH = re.compile(r"\b(ha(ha)+|he(he)+|lol|lmao|lmfao)\b", re.I)
_SLURS = re.compile(r"\b(nigg(?:a|as|az|er|ers|uh)|faggots?|fags?|retards?)\b", re.I)
# Swear words get one star (fu*k, sh*t, motherfu*ker). Romanian roots only match at a word start so
# ordinary words that merely contain them ("populară") stay untouched.
_PROFANE = re.compile(
    r"fuck|shit|bitch|cunt|pussy|\bdick(?:s|head|heads)?\b|whore|slut|bastard|asshole|\bass\b|"
    r"\bpul(?:a|ă|ii|e)\b|\bpizd|\bmuie\b|\bc[aă]c[aă]t|\bfutu|\bcoaie\b",
    re.I,
)


def censor(text: str) -> str:
    text = _SLURS.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 2) + m.group(0)[-1], text)
    return _PROFANE.sub(lambda m: m.group(0)[:2] + "*" + m.group(0)[3:] if len(m.group(0)) > 3
                        else m.group(0)[0] + "*" + m.group(0)[2:], text)


def _norm(w: str) -> str:
    return re.sub(r"[^\w'ăâîșşțţ-]", "", w.lower())


def text_features(words: list[dict], lang: str | None) -> dict:
    if not words:
        return {"n": 0, "wps": 0.0, "hype": 0, "excl": 0, "quest": 0, "laugh": 0, "rep": 0}
    text = " ".join(w["word"] for w in words)
    low = " " + " ".join(_norm(w["word"]) for w in words) + " "
    dur = max(1.0, words[-1]["end"] - words[0]["start"])
    lists = HYPE.get(lang or "", []) or (HYPE["en"] + HYPE["ro"])
    hype = sum(low.count(f" {h} ") for h in lists)
    rep = sum(1 for a, b in zip(words, words[1:]) if _norm(a["word"]) and _norm(a["word"]) == _norm(b["word"]))
    return {
        "n": len(words),
        "wps": len(words) / dur,
        "hype": hype,
        "excl": text.count("!"),
        "quest": text.count("?"),
        "laugh": len(_LAUGH.findall(text)),
        "rep": rep,
    }


def text_score(words: list[dict], lang: str | None) -> tuple[float, list[str]]:
    f = text_features(words, lang)
    reasons = []
    if f["n"] < 6:
        return -1.0, reasons
    s = (0.25 * min(f["hype"], 10) + 0.35 * min(f["excl"], 8) + 0.15 * min(f["quest"], 6)
         + 0.6 * min(f["laugh"], 4) + 0.3 * min(f["rep"], 5) + 0.5 * max(0.0, f["wps"] - 2.6))
    if f["laugh"]:
        reasons.append("😂 Râsete")
    if f["hype"] >= 3 or f["excl"] >= 3:
        reasons.append("🗣️ Reacție intensă")
    if f["wps"] > 3.4:
        reasons.append("⚡ Vorbește foarte repede")
    return min(4.0, s), reasons


def _boundaries(words: list[dict]) -> tuple[list[float], list[float]]:
    """Natural cut points: sentence starts/ends and pauses."""
    starts, ends = [], []
    for i, w in enumerate(words):
        prev = words[i - 1] if i else None
        nxt = words[i + 1] if i + 1 < len(words) else None
        if prev is None or w["start"] - prev["end"] >= 0.45 or prev["word"].rstrip()[-1:] in ".?!":
            starts.append(w["start"])
        if nxt is None or nxt["start"] - w["end"] >= 0.45 or w["word"].rstrip()[-1:] in ".?!":
            ends.append(w["end"])
    return starts, ends


def choose_bounds(words: list[dict], peak: float, lo: float, hi: float,
                  min_len: float = 12, max_len: float = 40, react_end: float | None = None) -> tuple[float, float]:
    """Clip [start, end]: setup that starts on a sentence start, payoff = the reaction, end right after it calms."""
    inside_words = [w for w in words if lo <= w["start"] <= hi]
    starts, ends = _boundaries(inside_words)
    target_e = min(peak + 12, max(peak + 3, react_end if react_end is not None else peak + 8))
    room = max_len - (target_e - peak)
    target_s = max(lo, peak - min(16.0, room - 2))
    cand_s = [s for s in starts if max(lo, peak - (max_len - 4)) <= s <= peak - 4]
    start = min(cand_s, key=lambda s: abs(s - target_s)) if cand_s else target_s
    prev_end = max((w["end"] for w in inside_words if w["end"] <= start), default=lo)
    start = max(lo, prev_end + 0.02, start - 0.25) if cand_s else start

    limit = start + max_len
    cand_e = [e for e in ends if peak + 2 <= e <= min(hi, limit - 0.4)]
    if cand_e:
        end = min(cand_e, key=lambda e: abs(e - target_e) + (0.5 if e < target_e - 1 else 0))
    else:
        inside = [w["end"] for w in inside_words if peak + 2 <= w["end"] <= min(hi, limit - 0.4)]
        end = max(inside) if inside else min(hi, limit, target_e)
    next_start = min((w["start"] for w in inside_words if w["start"] >= end - 0.01), default=end + 0.5)
    end = min(hi, limit, max(end + 0.05, min(end + 0.5, next_start - 0.03)))

    if end - start < min_len:
        missing = min_len - (end - start)
        end = min(hi, limit, end + missing / 2)
        start = max(lo, end - min_len)
    return round(start, 2), round(end, 2)


def refine_bounds(words: list[dict], start: float, end: float, lo: float, hi: float,
                  max_len: float = 40) -> tuple[float, float]:
    """After precise alignment: tight lead-in and never cut into a word at either edge (keeps <= max_len)."""
    straddle = [w for w in words if w["start"] < start < w["end"]]
    if straddle:
        w = straddle[0]
        start = w["start"] if end - w["start"] <= max_len and w["start"] >= lo else w["end"] + 0.02
    first = next((w for w in words if w["start"] >= start - 1e-3), None)
    if first is not None and first["start"] < end:
        prev_end = max((w["end"] for w in words if w["end"] <= first["start"] and w is not first), default=lo)
        start = max(lo, prev_end + 0.01, first["start"] - 0.15)
    straddle = [w for w in words if w["start"] < end < w["end"]]
    if straddle:
        w = straddle[-1]
        end = w["end"] + 0.2 if w["end"] + 0.2 <= hi and w["end"] + 0.2 - start <= max_len else w["start"] - 0.03
    last = max((w["end"] for w in words if start <= w["start"] < end), default=end)
    nxt = min((w["start"] for w in words if w["start"] >= end), default=hi)
    end = min(hi, start + max_len, max(end, min(last + 0.3, nxt - 0.02)))
    return round(max(lo, start), 3), round(end, 3)


def clip_quality(score: np.ndarray, words: list[dict], start: float, end: float, peak: float,
                 hop: float = HOP) -> tuple[float, list[str]]:
    """Whole-clip quality: sustained hype, speech density and a payoff that lands late in the clip."""
    a, b = int(start / hop), max(int(start / hop) + 1, int(end / hop))
    seg = score[max(0, a): min(len(score), b)]
    sustained = float(np.mean(np.sort(seg)[-max(1, len(seg) // 4):])) if len(seg) else 0.0
    dur = max(1.0, end - start)
    inside = [w for w in words if start <= w["start"] < end]
    wps = len(inside) / dur
    reasons = []
    q = 0.5 * sustained
    if wps < 0.9:
        q -= 1.5 * (0.9 - wps)
    elif wps > 1.8:
        q += 0.3
    pos = (peak - start) / dur
    if 0.35 <= pos <= 0.85:
        q += 0.4
    if sustained > 2.5:
        reasons.append("📈 Hype susținut")
    return q, reasons


_FALLBACK_TITLES = {
    "en": ["{n} LOST IT!", "{n} WAS NOT READY FOR THIS", "{n} CAN'T BELIEVE IT", "THIS IS WHY CHAT LOVES {n}",
           "{n}'S CRAZIEST MOMENT"],
    "ro": ["{n} A LUAT-O RAZNA!", "{n} NU-I VINE SĂ CREADĂ", "MOMENT INCREDIBIL CU {n}", "{n} NU ERA PREGĂTIT",
           "CHAT-UL A MURIT DE RÂS"],
}


def make_title(words: list[dict], peak_rel: float, lang: str | None, streamer: str) -> str:
    """Hook title: the punchiest short sentence close to the peak (no LLM needed)."""
    from .captions import HYPE, NUM_WORDS, PROFANITY, STOP

    lang_key = lang if lang in STOP else "en"
    stop = STOP[lang_key]
    hype = HYPE[lang_key] | HYPE["en"]
    sentences, cur = [], []
    for w in words:
        cur.append(w)
        if w["word"].rstrip()[-1:] in ".?!":
            sentences.append(cur)
            cur = []
    if cur:
        sentences.append(cur)
    best, best_score = None, -1e9
    for s in sentences:
        n = len(s)
        if n < 2 or n > 10:
            continue
        txt = " ".join(w["word"] for w in s).strip()
        toks = [_norm(w["word"]) for w in s]
        content = [t for t in toks if t and t not in stop and len(t) > 2]
        if len(content) < 1 or len(set(toks)) <= 1:
            continue
        mid = (s[0]["start"] + s[-1]["end"]) / 2
        sc = text_features(s, lang)
        score = (2 * sc["excl"] + sc["quest"] + 1.5 * sc["hype"] + 2 * sc["laugh"]
                 + 0.6 * len(content) + 1.2 * sum(t in hype for t in toks)
                 + 1.0 * sum(t in PROFANITY or "*" in t for t in toks)
                 + 1.5 * sum(bool(re.match(r"^\$?\d", t)) or t in NUM_WORDS for t in toks)
                 + 0.8 * sum(1 for w in s[1:] if w["word"][:1].isupper())
                 - 0.12 * abs(mid - peak_rel) - 0.25 * abs(n - 5))
        if mid > peak_rel + 6:
            score -= 1.0
        if len(txt) > 50:
            score -= 3
        if score > best_score:
            best, best_score = txt, score
    if best and best_score > 0.5:
        return "\u201c" + best.strip().strip(",.").upper() + "\u201d"
    options = _FALLBACK_TITLES["ro" if lang == "ro" else "en"]
    seed = sum(ord(c) for c in "".join(w["word"] for w in words[:6])) if words else 0
    return options[seed % len(options)].format(n=streamer.upper())

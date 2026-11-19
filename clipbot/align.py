"""Precise word timing: CTC forced alignment (MMS-300m forced aligner, ONNX on CPU) of Whisper's words.

Whisper decides WHAT was said; the CTC model decides exactly WHEN each word starts/ends (≈20 ms precision),
which is what makes the captions lock onto the lips.
"""

import json
import threading
import unicodedata

import numpy as np

from .config import MODELS_DIR

ALIGNER_DIR = MODELS_DIR / "aligner"
MODEL_PATH = ALIGNER_DIR / "onnx" / "model_fp16.onnx"
SR = 16000

_session = None
_input_type = np.float32
_device = "cpu"
_vocab: dict[str, int] = {}
_lock = threading.Lock()


def available() -> bool:
    return MODEL_PATH.exists() and (ALIGNER_DIR / "vocab.json").exists()


def device() -> str:
    return _device


def _load():
    global _session, _vocab, _input_type, _device
    with _lock:
        if _session is None:
            import onnxruntime as ort

            ort.set_default_logger_severity(3)
            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            opts.log_severity_level = 3
            sess = None
            if "CUDAExecutionProvider" in ort.get_available_providers():
                try:
                    if hasattr(ort, "preload_dlls"):
                        ort.preload_dlls()
                    # DEFAULT conv algo: the cuDNN heuristics reject wav2vec2's grouped pos-conv on some GPUs.
                    cuda = ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "DEFAULT"})
                    sess = ort.InferenceSession(str(MODEL_PATH), sess_options=opts,
                                                providers=[cuda, "CPUExecutionProvider"])
                    if sess.get_providers()[0] != "CUDAExecutionProvider":
                        sess = None
                    else:
                        _device = "cuda"
                except Exception:  # noqa: BLE001 - fall back to CPU
                    sess = None
            if sess is None:
                sess = ort.InferenceSession(str(MODEL_PATH), sess_options=opts, providers=["CPUExecutionProvider"])
                _device = "cpu"
            _input_type = np.float16 if "float16" in sess.get_inputs()[0].type else np.float32
            _session = sess
            _vocab = json.loads((ALIGNER_DIR / "vocab.json").read_text(encoding="utf-8"))
    return _session, _vocab


def emissions(audio: np.ndarray) -> tuple[np.ndarray, float]:
    """Log-probabilities [frames, vocab] and seconds per frame."""
    sess, _ = _load()
    x = audio.astype(np.float32)
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    with _lock:
        logits = sess.run(None, {"input_values": x[None, :].astype(_input_type)})[0][0].astype(np.float32)
    m = logits.max(axis=1, keepdims=True)
    lp = logits - m - np.log(np.exp(logits - m).sum(axis=1, keepdims=True))
    return lp, (len(audio) / SR) / max(1, lp.shape[0])


def _word_tokens(word: str, vocab: dict) -> list[int]:
    w = unicodedata.normalize("NFKD", word.lower().replace("’", "'").replace("`", "'"))
    w = "".join(c for c in w if not unicodedata.combining(c))
    return [vocab[c] for c in w if c in vocab and len(c) == 1 and vocab[c] > 3]


def _viterbi(lp: np.ndarray, tokens: list[int]) -> np.ndarray | None:
    """Best CTC path (state per frame) for `tokens` through log-probs `lp`."""
    T, L = lp.shape[0], len(tokens)
    if L == 0 or T < L:
        return None
    S = 2 * L + 1
    labels = np.zeros(S, np.int64)
    labels[1::2] = tokens
    emit = lp[:, labels]
    skip = np.zeros(S, bool)
    if L > 1:
        skip[3::2] = labels[3::2] != labels[1:-2:2]
    neg = np.float32(-1e30)
    alpha = np.full(S, neg, np.float32)
    alpha[0], alpha[1] = emit[0, 0], emit[0, 1]
    back = np.zeros((T, S), np.int8)
    idx = np.arange(S)
    for t in range(1, T):
        a1 = np.empty_like(alpha)
        a1[0] = neg
        a1[1:] = alpha[:-1]
        a2 = np.full_like(alpha, neg)
        a2[2:] = alpha[:-2]
        a2[~skip] = neg
        stack = np.stack((alpha, a1, a2))
        choice = stack.argmax(axis=0)
        alpha = stack[choice, idx] + emit[t]
        back[t] = choice
    s = S - 1 if alpha[S - 1] >= alpha[S - 2] else S - 2
    if alpha[s] <= neg / 2:
        return None
    path = np.empty(T, np.int64)
    for t in range(T - 1, -1, -1):
        path[t] = s
        s -= int(back[t, s])
    return path


def _groups(words: list[dict], max_words: int = 14, gap: float = 0.6) -> list[list[int]]:
    out, cur = [], []
    for i, w in enumerate(words):
        if cur and (len(cur) >= max_words or w["start"] - words[cur[-1]]["end"] > gap):
            out.append(cur)
            cur = []
        cur.append(i)
    if cur:
        out.append(cur)
    return out


def align(audio: np.ndarray, words: list[dict], offset: float = 0.0) -> tuple[list[dict], dict]:
    """Return copies of `words` (absolute times; audio[0] is at `offset`) with CTC-refined start/end."""
    words = [dict(w) for w in words]
    stats = {"aligned": 0, "fallback": 0}
    if not words or not available() or len(audio) < SR // 2:
        stats["fallback"] = len(words)
        return words, stats
    lp, fs = emissions(audio)
    _, vocab = _load()
    n_frames = lp.shape[0]
    groups = _groups(words)
    for gi, g in enumerate(groups):
        g_start, g_end = words[g[0]]["start"] - offset, words[g[-1]]["end"] - offset
        lo = g_start - 0.45
        hi = g_end + 0.45
        if gi > 0:
            prev_end = words[groups[gi - 1][-1]]["end"] - offset
            lo = max(lo, (prev_end + g_start) / 2)
        if gi + 1 < len(groups):
            next_start = words[groups[gi + 1][0]]["start"] - offset
            hi = min(hi, (g_end + next_start) / 2)
        f0, f1 = max(0, int(lo / fs)), min(n_frames, int(np.ceil(hi / fs)))
        tokens, owner = [], []
        for wi in g:
            toks = _word_tokens(words[wi]["word"], vocab)
            tokens += toks
            owner += [wi] * len(toks)
        path = _viterbi(lp[f0:f1], tokens) if f1 - f0 > 2 else None
        if path is None:
            stats["fallback"] += len(g)
            continue
        frames_lp = lp[f0:f1]
        tok_frames: dict[int, list[int]] = {}
        tok_scores = []
        for t, s in enumerate(path):
            if s % 2 == 1:
                k = (s - 1) // 2
                tok_frames.setdefault(k, []).append(t)
                tok_scores.append(frames_lp[t, tokens[k]])
        if not tok_scores or float(np.mean(tok_scores)) < -4.0:
            stats["fallback"] += len(g)
            continue
        spans: dict[int, list[int]] = {}
        for k, frs in tok_frames.items():
            sp = spans.setdefault(owner[k], [frs[0], frs[-1]])
            sp[0], sp[1] = min(sp[0], frs[0]), max(sp[1], frs[-1])
        for wi in g:
            if wi not in spans:
                continue
            a, b = spans[wi]
            st = offset + (f0 + a) * fs - 0.03
            en = offset + (f0 + b + 1) * fs + 0.02
            if abs(st - words[wi]["start"]) > 1.2:
                continue
            words[wi]["start"], words[wi]["end"] = round(st, 3), round(max(en, st + 0.06), 3)
            words[wi]["aligned"] = True
            stats["aligned"] += 1
        stats["fallback"] += sum(1 for wi in g if wi not in spans)

    _fill_unaligned(words)
    return words, stats


def _fill_unaligned(words: list[dict]) -> None:
    """Words without letters (numbers, symbols) sit between their aligned neighbours; keep times monotonic."""
    n = len(words)
    for i, w in enumerate(words):
        if w.get("aligned"):
            continue
        prev_end = next((words[j]["end"] for j in range(i - 1, -1, -1) if words[j].get("aligned")), None)
        next_start = next((words[j]["start"] for j in range(i + 1, n) if words[j].get("aligned")), None)
        if prev_end is not None and next_start is not None and next_start > prev_end:
            dur = max(0.08, w["end"] - w["start"])
            w["start"] = max(prev_end, min(w["start"], next_start - 0.08))
            w["end"] = min(next_start, w["start"] + dur)
    for i in range(1, n):
        if words[i]["start"] < words[i - 1]["start"]:
            words[i]["start"] = words[i - 1]["start"] + 0.01
        if words[i - 1]["end"] > words[i]["start"]:
            words[i - 1]["end"] = max(words[i - 1]["start"] + 0.04, words[i]["start"])
        words[i]["end"] = max(words[i]["end"], words[i]["start"] + 0.04)
    for w in words:
        w.pop("aligned", None)

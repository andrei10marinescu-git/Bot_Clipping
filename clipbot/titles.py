"""Hook titles written by a local LLM (llama.cpp + Qwen3-4B-Instruct, runs on this PC), with a heuristic fallback."""

import difflib
import json
import re
import subprocess
import threading
import time
import urllib.request

from . import analysis, header
from .config import LOG_DIR, MODELS_DIR, TOOLS_DIR, settings
from .jobs import logger

MODEL = MODELS_DIR / "llm" / "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
PORT = 8771

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_failed_until = 0.0

SYSTEM = """You write titles for viral YouTube Shorts / TikTok clips cut from livestreams and videos.
A great title makes people stop scrolling and watch to the end, and it is SPECIFIC to the clip.

The title is ONE short, punchy statement - never a question. It sounds like a friend shouting the news at you,
it builds tension and makes you need to see it happen. Use concrete details from THIS transcript (a name, object,
number, place, food, game, animal, action...). Every title MUST contain at least one such concrete detail.

Hook formulas:
1. the streamer + the crazy / unexpected thing he does or that happens to him, told as breaking news
   (e.g. "SPEED ALMOST GETS ARRESTED IN GTA 6!", "KAI LOSES HIS MIND OVER A CHICKEN!")
2. a tease that promises a payoff without revealing it
   (e.g. "WAIT FOR WHAT JYNXZI DOES WITH THE DOG!", "THE ENDING WITH THE COPS IS INSANE!")
3. extreme emotion or stakes in the moment
   (e.g. "DYLAN GOT DESTROYED BY BIGFOOT!", "THIS $5 TOLL MADE SPEED RAGE!")

Rules:
- 4 to 9 words, spoken style, never reveal the punchline
- no question marks, no "why / what / how" questions; end with "!" and then 1 or 2 emojis that match the
  emotion, e.g. 😱 🤯 😂 💀 🔥 😳 🚨 😭 💰 🤬 ⚠️
- never a flat description like "X talks about ..." / "X shows ..." / "X explains ..."
- only facts that are in the transcript, nothing invented
- no hashtags, no quotation marks, no swear words, no slurs
- do not write about "chat", "stream" or "viewers" unless that is what the clip is about
- if the language is Romanian: natural, grammatically correct Romanian with diacritics
  (e.g. "RAPHAEL A FURAT DUBA GREȘITĂ!", "STAI SĂ VEZI CE FACE CU MAȘINA!")

Reply with JSON only:
{"moment": "<one sentence: what concretely happens in the clip>", "titles": ["...", "...", "..."]}
One title per formula, each title is a separate string."""


def available() -> bool:
    return MODEL.exists() and _server_exe() is not None and settings().get("ai_titles", True)


def _server_exe():
    exes = sorted((TOOLS_DIR / "llama").glob("**/llama-server.exe"))
    return exes[0] if exes else None


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _start() -> bool:
    global _proc
    log = open(LOG_DIR / "llama-server.log", "ab")
    # CPU build on purpose: keeps the GPU memory free for Whisper, the aligner and NVENC (~5 s per title).
    _proc = subprocess.Popen(
        [str(_server_exe()), "-m", str(MODEL), "--host", "127.0.0.1", "--port", str(PORT),
         "-c", "2048", "-np", "1", "--no-webui"],
        stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for _ in range(240):
        if _proc.poll() is not None:
            return False
        if _healthy():
            return True
        time.sleep(0.5)
    _proc.kill()
    return False


def _ensure_server() -> bool:
    global _failed_until
    if _proc is not None and _proc.poll() is None and _healthy():
        return True
    if time.time() < _failed_until:
        return False
    if _start():
        logger.info("llama-server ready")
        return True
    _failed_until = time.time() + 600
    logger.warning("llama-server could not start, using heuristic titles")
    return False


def _clean(title: str) -> str:
    emojis = header.EMOJI_RE.findall(title)
    t = header.EMOJI_RE.sub(" ", title)
    t = re.sub(r"[^\w\s?!.,'’:&$%*-]", "", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip(" .,:-'’\"")
    t = analysis.censor(t).upper()
    if t.endswith("?"):
        t = t.rstrip("?").rstrip() + "!"
    elif t and not t.endswith(("!", "…", "...")):
        t += "!"
    emo = "".join(list(dict.fromkeys(emojis))[:2])  # max 2 different emojis, always at the end
    return f"{t} {emo}".strip()


def _plain(title: str) -> str:
    return re.sub(r"\s+", " ", header.EMOJI_RE.sub(" ", title)).strip()


# Fallback emoji when the title has none: by what the clip sounds like.
def ensure_emoji(title: str, text: str) -> str:
    if not title or header.EMOJI_RE.search(title):
        return title
    low = text.lower()
    if re.search(r"\b(ha(ha)+|lol|lmao|lmfao)\b|😂|🤣", low):
        e = "😂"
    elif re.search(r"\$|\bmoney\b|\bbani\b|\bdollars?\b|\bmillion\b", low):
        e = "💰"
    elif text.count("!") >= 3 or re.search(r"\b(oh my god|no way|what the|bro what|wtf|ce naiba|doamne)\b", low):
        e = "😱"
    else:
        e = "🔥"
    return f"{title} {e}"


# Titles are statements, not questions.
_QUESTION = re.compile(r"^(WHY|WHAT|HOW|WHO|WHEN|WHERE|IS|ARE|DID|DOES|CAN|WOULD|DE CE|CE|CUM|CINE|UNDE|CÂND|CAND)\b")
# Distinctive words of the prompt examples: a title using them without the transcript saying so copied the example.
_EXAMPLE_WORDS = {"ARRESTED", "CHICKEN", "DOG", "COPS", "BIGFOOT", "TOLL", "RAGE", "DUBA", "MAȘINA", "DESTROYED"}


_GENERIC = re.compile(r"\b(CHAT\w*|VIEWERS|STREAM\w*|EXPLOD\w*|INSANE MOMENT|MOMENTUL ACELUI|ON CAMERA|"
                      r"WENT WRONG SO FAST|SHOCKING REACTION|NU SE AȘTEPTA LA ASTA|PE BUNE)\b")
# Flat, descriptive verbs make weak hooks.
_FLAT = re.compile(r"\b(TALKS? ABOUT|SHOWS|SHOWING|DISCUSS\w*|EXPLAINS|MENTIONS|VORBEȘTE DESPRE|PREZINTĂ)\b")


_STOP = {"WHAT", "THAT", "THIS", "WITH", "HAVE", "FROM", "WHEN", "THEY", "YOUR", "JUST", "LIKE", "ABOUT", "BEEN",
         "WERE", "WILL", "THEN", "THERE", "THEIR", "WHY", "HOW", "DOES", "DID", "INTO", "OVER", "AFTER", "REALLY",
         "CARE", "ESTE", "ACUM", "DACĂ", "PENTRU", "CEVA", "ASTA", "DOAR", "FOARTE", "CUM"}


def _specific(text: str) -> set[str]:
    return {w for w in re.findall(r"\w{4,}", text.upper())} - _STOP


def _fix_name(title: str, streamer: str) -> str:
    """The model sometimes misspells the streamer (IRAPHAEHELL): snap near-misses back to the real name."""
    name = streamer.upper().replace(" ", "")
    out = []
    for tok in title.split(" "):
        core = re.sub(r"[^\w'’]", "", tok)
        m = re.match(r"^(\w+?)('S|’S)?$", core) if core else None
        base = m.group(1) if m else core
        if len(base) >= 5 and base != name and difflib.SequenceMatcher(None, base, name).ratio() >= 0.8:
            tok = tok.replace(base, name)
        out.append(tok)
    return " ".join(out)


def _pick(options: list[str], streamer: str, transcript: str) -> str | None:
    words_in_clip = _specific(transcript) - set(streamer.upper().split())
    flat = [part for o in options for part in re.split(r"\s+/\s+|\s*\|\s*", str(o)) if part.strip()]
    best, best_score = None, -1e9
    for o in flat:
        raw_q = "?" in str(o)
        t = _fix_name(_clean(o), streamer)
        p = _plain(t)
        n = len(p.split())
        if not 3 <= n <= 11 or len(p) > 62:
            continue
        specific = len(_specific(p) & words_in_clip)
        score = -abs(n - 6.5) * 0.3 - max(0, len(p) - 48) * 0.2 + 1.0 * min(3, specific)
        if streamer.split()[0].upper() in p:
            score += 0.8
        if p.endswith("!") and not raw_q:
            score += 0.7
        if raw_q or _QUESTION.search(p):
            score -= 3.0
        copied = {w for w in re.findall(r"\w+", p)} & _EXAMPLE_WORDS
        if copied - set(re.findall(r"\w+", transcript.upper())):
            score -= 2.0
        if header.EMOJI_RE.search(t):
            score += 0.3
        if _GENERIC.search(p):
            score -= 2.0
        if _FLAT.search(p):
            score -= 1.2
        if score > best_score:
            best, best_score = t, score
    return best


def ai_title(text: str, streamer: str, lang: str | None, source_title: str = "", log=None) -> str | None:
    if not text.strip() or not available():
        return None
    language = "Romanian (with correct diacritics)" if lang == "ro" else "English"
    user = (f"Streamer: {streamer}\nStream / video title (context only): {source_title or '-'}\n"
            f"Write the titles in: {language}\n\nClip transcript:\n{text[:2000]}")
    try:
        t0 = time.time()
        data = _ask(SYSTEM, user, max_tokens=200, temperature=0.6 if lang == "ro" else 0.75)
        if data is None:
            return None
        options = data.get("titles") or []
        title = _pick(options if isinstance(options, list) else [options], streamer, text)
        if log and title:
            log(f"Titlu AI ({time.time() - t0:.1f}s): {title}  (variante: {' | '.join(map(str, options))[:200]})")
        return title
    except Exception as e:  # noqa: BLE001 - never block a clip because of the title
        logger.warning("ai title failed: %s", e)
        return None


def _ask(system: str, user: str, max_tokens: int, temperature: float) -> dict | None:
    body = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature, "top_p": 0.9, "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    with _lock:
        if not _ensure_server():
            return None
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            content = json.loads(r.read())["choices"][0]["message"]["content"]
    return json.loads(content[content.find("{"): content.rfind("}") + 1])


def make_title(words: list[dict], peak_rel: float, lang: str | None, streamer: str, source_title: str = "",
               reasons: list[str] | None = None, log=None) -> str:
    text = " ".join(w["word"] for w in words)
    title = ai_title(text, streamer, lang, source_title, log=log)
    if not title:  # heuristic fallback, shaped like the AI titles: a statement ending in "!"
        title = analysis.censor(analysis.make_title(words, peak_rel, lang, streamer)).strip().rstrip("?.").strip()
        title = title if title.endswith(("!", "…")) else title + "!"
    return ensure_emoji(title, text)


def shutdown() -> None:
    if _proc is not None and _proc.poll() is None:
        _proc.kill()

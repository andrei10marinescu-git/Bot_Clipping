"""Caption design: word emphasis scoring + animated ASS subtitles (word-by-word karaoke with accented key words)."""

import re
from pathlib import Path

import numpy as np

W, H = 1080, 1920

STOP = {
    "en": set("""a an the and or but so to of in on at by for with from as is are was were be been being am i me my
        you your he him his she her it its we us our they them their this that these those there here then than
        do does did have has had will would can could should just like um uh gonna wanna gotta i'm you're it's
        that's he's she's we're they're i'll i've don't can't isn't wasn't im dont cant into out up down over about
        if when what's who's not too very really also yeah okay ok all some any one get got go going""".split()),
    "ro": set("""a al ale ai și si sau dar de la în in pe cu din că ca e este sunt era fi fost am ai are au o un una
        unei unui ce cum cine când cand unde mă ma te se ne vă va le îi ii îl il lui ei ea el eu tu noi voi ăsta asta
        aia ăla ala acolo aici mai foarte doar tot toți toate să sa nu da deci păi pai gen adică adica chiar care
        sau-mi mi ți ti""".split()),
}
HYPE = {
    "en": set("""what bro yo crazy insane wait chat bruh holy wtf yes nah run help huh why stop goat fire scared
        crying unbelievable never everybody everything nobody wow omg impossible legendary cinema aura lets let's
        dead dying screaming finally won win lost lose broke rich hit clip go""".split()),
    "ro": set("""frate bă ba băi bai doamne nebun nebunie incredibil gata hai mamă mama serios wow aoleu vai fugi
        ajutor uite stai bravo imposibil nebunii cumplit groaznic super tare bestial uau hahaha bine""".split()),
}
PROFANITY = set("""shit fuck fucking fucked damn hell bitch ass crap dick goddamn motherfucker bullshit wtf
    dracu naiba pizda pula muie căcat cacat rahat futu mortii""".split())
NUM_WORDS = set("thousand million billion hundred grand bands racks k mii milioane milion sute miliarde lei euro dollars".split())
_NUM = re.compile(r"^\$?\d[\d,.]*[k%]?$|^\d+(st|nd|rd|th)$", re.I)

COLOR_ACTIVE = "#FFE135"
COLOR_KEY = "#3DFF6E"
COLOR_SCREAM = "#FF4747"


def _norm(w: str) -> str:
    return re.sub(r"[^\w'ăâîșşțţ$%-]", "", w.lower())


def word_loudness(db: np.ndarray, hop: float, words: list[dict]) -> np.ndarray:
    """Per-word loudness z-score (vs. the other words in this clip)."""
    if len(db) == 0 or not words:
        return np.zeros(len(words))
    vals = []
    for w in words:
        a, b = int(w["start"] / hop), max(int(w["start"] / hop) + 1, int(np.ceil(w["end"] / hop)))
        seg = db[max(0, a): min(len(db), b)]
        vals.append(float(np.percentile(seg, 80)) if len(seg) else float(np.median(db)))
    v = np.array(vals)
    med = np.median(v)
    mad = np.median(np.abs(v - med)) * 1.4826 + 1.0
    return (v - med) / mad


def emphasis(words: list[dict], lang: str | None, loud_z: np.ndarray) -> list[dict]:
    """Annotate words with 'level' (0 normal, 1 key, 2 power) and 'kind' (key/scream/money)."""
    lang = lang if lang in STOP else "en"
    stop, hype = STOP[lang], HYPE[lang] | HYPE["en"]
    out = []
    prev = ""
    for i, w in enumerate(words):
        raw = w["word"].strip()
        tok = _norm(raw)
        z = float(loud_z[i]) if i < len(loud_z) else 0.0
        score, kind = 0.0, "key"
        is_num = bool(_NUM.match(tok)) or tok in NUM_WORDS
        if tok and tok not in stop and len(tok.strip("'")) > 1 or is_num or tok in hype:
            if tok in hype:
                score += 1.6
            if tok in PROFANITY or "*" in raw:
                score += 1.4
            if raw.endswith("!"):
                score += 1.5
            if raw.endswith("?"):
                score += 0.6
            if is_num:
                score += 2.2
                kind = "money"
            if lang == "en" and raw[:1].isupper() and i > 0 and not words[i - 1]["word"].rstrip().endswith((".", "!", "?")):
                score += 1.0
            if len(tok) >= 9:
                score += 1.2
            elif len(tok) >= 6:
                score += 0.8
            if lang == "en" and tok.endswith("est") and len(tok) > 5:
                score += 1.0
            if tok == prev:
                score += 0.8
            if z >= 2.5:
                score += 2.4
                kind = "scream" if kind != "money" else kind
            elif z >= 1.4:
                score += 1.2
        out.append({**w, "level": 0, "kind": kind, "z": round(z, 2), "escore": round(score, 2)})
        prev = tok

    # Relative selection, so calm talk still gets its important words accented (~14% of words),
    # while the big single-word "power" style is reserved for real peaks.
    ranked = sorted([w for w in out if w["escore"] >= 0.8], key=lambda x: -x["escore"])
    n_keys = max(1, int(round(len(out) * 0.14)))
    for w in ranked[:n_keys]:
        w["level"] = 1
    for w in ranked[n_keys: int(len(out) * 0.25)]:
        if w["escore"] >= 2.6:
            w["level"] = 1
    powers: list[float] = []
    for w in ranked:
        if w["escore"] >= 3.6 or (not powers and w is ranked[0] and w["escore"] >= 2.8):
            if all(abs(w["start"] - p) >= 5 for p in powers):
                w["level"] = 2
                powers.append(w["start"])
    return out


# --------------------------------------------------------------------------- ASS


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = (hex_rgb or "").lstrip("#")
    if len(h) != 6:
        h = "FFD400"
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _luma(hex_rgb: str) -> float:
    h = (hex_rgb or "#FFD400").lstrip("#")
    if len(h) != 6:
        return 0.8
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _token(word: str) -> str:
    w = word.strip().replace("{", "(").replace("}", ")").replace("\\", "/")
    w = re.sub(r"^[,.;:]+|[,.;:]+$", "", w)
    return w.upper()


def chunks(words: list[dict], max_words: int = 3, max_chars: int = 16, gap: float = 0.45) -> list[list[dict]]:
    out, cur = [], []
    for w in words:
        t = _token(w["word"])
        if not t:
            continue
        item = {**w, "t": t}
        if w.get("level") == 2:
            if cur:
                out.append(cur)
            out.append([item])
            cur = []
            continue
        if cur and (len(cur) >= max_words or sum(len(x["t"]) + 1 for x in cur) + len(t) > max_chars
                    or w["start"] - cur[-1]["end"] > gap):
            out.append(cur)
            cur = []
        cur.append(item)
        if w["word"].rstrip()[-1:] in ".?!":
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _word_color(x: dict) -> str | None:
    if x.get("level", 0) == 0:
        return None
    return COLOR_SCREAM if x.get("kind") == "scream" else COLOR_KEY


def _wrap_lines(text: str, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines


def _wrap(text: str, width: int) -> str:
    return "\\N".join(_wrap_lines(text, width)[:3])


def fit_title(hook: str) -> tuple[str, int]:
    """Title on at most 2 lines: shrink the font for long titles, ellipsize as a last resort."""
    t = " ".join(hook.replace("{", "(").replace("}", ")").split())
    for width, fs in ((24, 60), (28, 54), (32, 48)):
        lines = _wrap_lines(t, width)
        if len(lines) <= 2:
            return "\\N".join(lines), fs
    lines = _wrap_lines(t, 32)[:2]
    lines[-1] = lines[-1].rstrip(" ,.;:") + "…"
    return "\\N".join(lines), 48


STYLE_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Montserrat Black,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,1,0,1,8,4,5,60,60,0,1
Style: Power,Montserrat Black,128,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,2,0,1,10,5,5,40,40,0,1
Style: Title,Montserrat Black,60,&H00111111,&H00FFFFFF,&H00FFFFFF,&H00000000,0,0,0,0,100,100,0,0,3,18,0,5,70,70,0,1
Style: Name,Montserrat Black,44,{name_text},&H00FFFFFF,{name_box},&H00000000,0,0,0,0,100,100,1,0,3,12,0,5,70,70,0,1
Style: Credit,Montserrat ExtraBold,30,&H50FFFFFF,&H00FFFFFF,&H90000000,&H00000000,0,0,0,0,100,100,0,0,1,3,0,2,70,70,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_header(accent: str) -> str:
    name_text = "&H00111111" if _luma(accent) > 0.6 else "&H00FFFFFF"
    return STYLE_HEADER.format(W=W, H=H, name_text=name_text, name_box=ass_color(accent))


def title_events(hook: str, name: str, duration: float, layout: dict, legacy: bool = False) -> list[str]:
    """Streamer badge + title box, visible for the whole clip.

    legacy=True reproduces the exact box of clips rendered before titles were permanent (no entrance
    animation), so it can be laid over them and fully covers the old box.
    """
    end = _ass_time(duration)
    fx = "" if legacy else "{\\fad(180,0)\\fscx80\\fscy80\\t(0,160,\\fscx100\\fscy100)}"
    name_fs = "" if legacy else f"\\fs{layout.get('name_fs', 44)}"
    ev = [f"Dialogue: 3,0:00:00.00,{end},Name,,0,0,0,,{fx}{{\\pos(540,{layout['name_y']}){name_fs}}}{name.upper()}"]
    if hook:
        if legacy:
            txt, fs = _wrap(hook.replace("{", "(").replace("}", ")"), 24), 60
        else:
            txt, fs = fit_title(hook)
        ev.append(f"Dialogue: 3,0:00:00.00,{end},Title,,0,0,0,,{fx}{{\\pos(540,{layout['title_y']})\\fs{fs}}}{txt}")
    return ev


def build_ass(path: Path, words: list[dict], duration: float, *, hook: str, name: str, credit: str,
              accent: str, layout: dict, header: bool = False) -> list[list[dict]]:
    """header=True: badge + title come from the PNG header overlay, the ASS file only has the captions."""
    header_ass = ass_header(accent)
    ev = [] if header else title_events(hook, name, duration, layout)
    if credit:
        ev.append(f"Dialogue: 0,0:00:00.00,{_ass_time(duration)},Credit,,0,0,0,,{{\\pos(540,{H - 34})}}{credit}")

    cs = chunks(words)
    cy = layout["caption_y"]
    for ci, chunk in enumerate(cs):
        nxt = cs[ci + 1][0]["start"] if ci + 1 < len(cs) else duration
        chunk_end = min(chunk[-1]["end"] + 0.35, nxt, duration)
        if len(chunk) == 1 and chunk[0].get("level") == 2:
            x = chunk[0]
            col = ass_color(_word_color(x) or COLOR_KEY)
            st, en = x["start"], max(chunk_end, x["start"] + 0.45)
            anim = "\\fscx50\\fscy50\\t(0,90,\\fscx118\\fscy118)\\t(90,200,\\fscx100\\fscy100)\\frz-4\\t(0,200,\\frz0)"
            ev.append(f"Dialogue: 2,{_ass_time(st)},{_ass_time(min(en, duration))},Power,,0,0,0,,"
                      f"{{\\pos(540,{cy})\\c{col}{anim}}}{x['t']}")
            continue
        for wi, w in enumerate(chunk):
            st = chunk[0]["start"] if wi == 0 else w["start"]
            en = chunk[wi + 1]["start"] if wi + 1 < len(chunk) else chunk_end
            if en - st < 0.04:
                en = st + 0.04
            parts = []
            for k, x in enumerate(chunk):
                col = _word_color(x)
                if k == wi:
                    c = ass_color(col or COLOR_ACTIVE)
                    if x.get("level", 0) >= 1:
                        parts.append(f"{{\\c{c}\\fscx100\\fscy100\\t(0,90,\\fscx124\\fscy124)\\t(90,180,\\fscx114\\fscy114)}}"
                                     f"{x['t']}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                    else:
                        parts.append(f"{{\\c{c}\\fscx106\\fscy106}}{x['t']}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                elif col:
                    parts.append(f"{{\\c{ass_color(col)}}}{x['t']}{{\\c&H00FFFFFF&}}")
                else:
                    parts.append(x["t"])
            pop = "{\\fscx78\\fscy78\\t(0,100,\\fscx100\\fscy100)}" if wi == 0 else ""
            ev.append(f"Dialogue: 1,{_ass_time(st)},{_ass_time(en)},Cap,,0,0,0,,{{\\pos(540,{cy})}}{pop}{' '.join(parts)}")
    path.write_text(header_ass + "\n".join(ev) + "\n", encoding="utf-8")
    return cs


def write_srt(path: Path, cs: list[list[dict]]) -> None:
    def ts(t):
        ms = int(round(max(0.0, t) * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    lines = []
    for i, c in enumerate(cs, 1):
        lines += [str(i), f"{ts(c[0]['start'])} --> {ts(c[-1]['end'])}", " ".join(x["t"] for x in c), ""]
    path.write_text("\n".join(lines), encoding="utf-8")

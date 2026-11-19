"""Clip header drawn as a transparent PNG: streamer badge, subscriber badge and the hook title with color emoji.

libass cannot draw color emoji, so the header is rendered with Pillow (Montserrat + Windows' Segoe UI Emoji)
and laid over the video by ffmpeg.
"""

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import FONTS_DIR

W = 1080
FONT = FONTS_DIR / "Montserrat-Black.ttf"
EMOJI_FONT = Path(r"C:\Windows\Fonts\seguiemj.ttf")

EMOJI_RE = re.compile(
    "(?:[\U0001F1E6-\U0001F1FF]{2}"
    "|[\u203C\u2049\u2194-\u21AA\u231A-\u23FF\u24C2\u25AA-\u27BF\u2934\u2935\u2B05-\u2B55\u3030\u303D"
    "\U0001F000-\U0001FAFF]"
    "(?:\uFE0F|[\U0001F3FB-\U0001F3FF])?"
    "(?:\u200D[\u2600-\u27BF\U0001F000-\U0001FAFF](?:\uFE0F|[\U0001F3FB-\U0001F3FF])?)*)"
)

_fonts: dict = {}


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    key = (str(path), size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(str(path), size)
    return _fonts[key]


def _luma(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return 1.0
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.299 * r + 0.587 * g + 0.114 * b


def _runs(text: str) -> list[tuple[str, bool]]:
    """Split text into (chunk, is_emoji) runs."""
    out, pos = [], 0
    for m in EMOJI_RE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out


class _Typeset:
    def __init__(self, fs: int):
        self.fs = fs
        self.txt = _font(FONT, fs)
        self.emo = _font(EMOJI_FONT, int(fs * 0.95)) if EMOJI_FONT.exists() else None
        self.cap = -self.txt.getbbox("H", anchor="ls")[1]

    def width(self, text: str) -> float:
        w = 0.0
        for chunk, is_emoji in _runs(text):
            if is_emoji:
                w += self.emo.getlength(chunk) + self.fs * 0.06 if self.emo else 0
            else:
                w += self.txt.getlength(chunk)
        return w

    def draw(self, d: ImageDraw.ImageDraw, img: Image.Image, x: float, baseline: float, text: str, fill) -> None:
        for chunk, is_emoji in _runs(text):
            if is_emoji:
                if not self.emo:
                    continue
                # Center the emoji on the middle of the capital letters.
                x0, y0, x1, y1 = self.emo.getbbox(chunk, anchor="lt")
                top = baseline - self.cap / 2 - (y1 + y0) / 2
                d.text((x + self.fs * 0.03, top), chunk, font=self.emo, anchor="lt", embedded_color=True)
                x += self.emo.getlength(chunk) + self.fs * 0.06
            else:
                d.text((x, baseline), chunk, font=self.txt, anchor="ls", fill=fill)
                x += self.txt.getlength(chunk)


def _wrap(ts: _Typeset, text: str, max_w: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        cand = f"{cur} {word}".strip()
        if cur and ts.width(cand) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def fit_title(text: str, max_w: float = W - 2 * 70 - 2 * 24) -> tuple[_Typeset, list[str]]:
    """At most 2 lines: shrink the font for long titles, ellipsize as a last resort."""
    for fs in (64, 58, 52, 46):
        ts = _Typeset(fs)
        lines = _wrap(ts, text, max_w)
        if len(lines) <= 2:
            return ts, lines
    lines = lines[:2]
    while lines[1] and ts.width(lines[1] + "…") > max_w:
        lines[1] = lines[1].rsplit(" ", 1)[0] if " " in lines[1] else lines[1][:-1]
    lines[1] = lines[1].rstrip(" ,.;:") + "…"
    return ts, lines


def _pill(d: ImageDraw.ImageDraw, box, radius: int, fill) -> None:
    d.rounded_rectangle(box, radius=radius, fill=fill)


def format_count(n: int) -> str:
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            v = n / div
            return (f"{v:.1f}".rstrip("0").rstrip(".") if v < 100 else f"{v:.0f}") + suf
    return str(n)


def render(out_png: Path, title: str, name: str, accent: str, layout: dict, subs: str | None = None) -> Path:
    """Draw the header for a 1080-wide frame; it is overlaid at (0, 0). Returns the PNG path."""
    accent = accent if re.fullmatch(r"#[0-9a-fA-F]{6}", accent or "") else "#FFD400"
    name_fs = int(layout.get("name_fs", 40))
    name_y, title_y = int(layout["name_y"]), int(layout["title_y"])

    # Badge row: [STREAMER] [▶ 41.2M SUBSCRIBERS]
    bts = _Typeset(name_fs)
    pad_x, pad_y = int(name_fs * 0.5), int(name_fs * 0.3)
    badge_h = bts.cap + 2 * pad_y
    name_txt = name.upper()
    name_w = bts.width(name_txt) + 2 * pad_x
    subs_w = 0
    if subs:
        icon_w = int(bts.cap * 1.45)
        subs_w = icon_w + int(name_fs * 0.35) + bts.width(subs) + 2 * pad_x
    gap = int(name_fs * 0.35) if subs else 0
    row_x = (W - (name_w + gap + subs_w)) / 2
    row_top = name_y - badge_h / 2

    ts, lines = fit_title(title) if title else (None, [])
    t_pad_x, t_pad_y, line_gap = 24, int(ts.fs * 0.3) if ts else 0, 8
    line_h = (ts.cap + 2 * t_pad_y) if ts else 0
    block_h = len(lines) * line_h + max(0, len(lines) - 1) * line_gap
    block_top = max(title_y - block_h / 2, row_top + badge_h + 14)

    height = int(max(row_top + badge_h, block_top + block_h) + 24)
    shapes = Image.new("RGBA", (W, height), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shapes)
    img = Image.new("RGBA", (W, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    boxes = [((row_x, row_top, row_x + name_w, row_top + badge_h), 12, accent)]
    if subs:
        sx = row_x + name_w + gap
        boxes.append(((sx, row_top, sx + subs_w, row_top + badge_h), 12, "#FFFFFF"))
    for i, line in enumerate(lines):
        lw = ts.width(line) + 2 * t_pad_x
        top = block_top + i * (line_h + line_gap)
        boxes.append((((W - lw) / 2, top, (W + lw) / 2, top + line_h), 14, "#FFFFFF"))

    for box, r, _ in boxes:  # soft drop shadow under every box
        x0, y0, x1, y1 = box
        sd.rounded_rectangle((x0, y0 + 5, x1, y1 + 5), radius=r, fill=(0, 0, 0, 120))
    shapes = shapes.filter(ImageFilter.GaussianBlur(7))
    img = Image.alpha_composite(img, shapes)
    d = ImageDraw.Draw(img)
    for box, r, fill in boxes:
        _pill(d, box, r, fill)

    name_fill = "#111111" if _luma(accent) > 0.6 else "#FFFFFF"
    bts.draw(d, img, row_x + pad_x, row_top + pad_y + bts.cap, name_txt, name_fill)
    if subs:  # YouTube-style logo (red rounded box + white play triangle) and the count
        sx = row_x + name_w + gap + pad_x
        cy = row_top + badge_h / 2
        icon_w, icon_h = int(bts.cap * 1.45), int(bts.cap * 1.05)
        d.rounded_rectangle((sx, cy - icon_h / 2, sx + icon_w, cy + icon_h / 2), radius=int(icon_h * 0.28),
                            fill="#FF0033")
        tri = icon_h * 0.5
        tx = sx + icon_w / 2 - tri * 0.38
        d.polygon([(tx, cy - tri / 2), (tx, cy + tri / 2), (tx + tri * 0.87, cy)], fill="#FFFFFF")
        bts.draw(d, img, sx + icon_w + int(name_fs * 0.35), row_top + pad_y + bts.cap, subs, "#111111")
    for i, line in enumerate(lines):
        lw = ts.width(line)
        top = block_top + i * (line_h + line_gap)
        ts.draw(d, img, (W - lw) / 2, top + t_pad_y + ts.cap, line, "#111111")

    out_png = Path(out_png)
    img.save(out_png)
    return out_png

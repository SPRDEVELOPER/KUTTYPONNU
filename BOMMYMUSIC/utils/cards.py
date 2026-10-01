# BOMMYMUSIC/utils/cards.py
#
# Image cards for the now-playing message and the /queue view.
#
#   now_playing_card()  ->  "Neon Vinyl": blurred-art backdrop, a record sliding
#                           out of the album sleeve, a waveform that is unique
#                           to every song, and an accent colour taken from the art.
#   queue_card()        ->  "Metro Line": the queue drawn as a transit line, the
#                           playing track is the glowing station and every
#                           upcoming track is a stop with its own ETA.
#
# Only Pillow is required. Everything is best-effort: on any failure the callers
# fall back to the plain thumbnail / text UI, so a bad cover can never break
# playback.
import asyncio
import colorsys
import glob
import hashlib
import html
import math
import os
import random
import re
from functools import lru_cache

from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
)

_ASSETS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets"
)
_BOLD = os.path.join(_ASSETS, "font.ttf")  # Raleway Bold
_LIGHT = os.path.join(_ASSETS, "font2.ttf")  # Arial Light

CACHE_DIR = "cache"
KEEP_CARDS = 60  # newest now-playing cards kept on disk

BRAND = (61, 245, 160)  # neon mint, taken from the BOMMY logo
INK = (10, 12, 18)
LANCZOS = Image.Resampling.LANCZOS

NP_SIZE = (1280, 720)
Q_WIDTH = 1280
Q_ROWS = 4  # upcoming tracks drawn on the queue card


# ── fonts & text ─────────────────────────────────────────────────────────────
@lru_cache(maxsize=64)
def _font(path, size):
    return ImageFont.truetype(path, size)


def _glyph_sig(path, ch):
    """Pixels of `ch` drawn at a fixed size (used to spot the 'missing glyph' box)."""
    img = Image.new("L", (56, 56), 0)
    ImageDraw.Draw(img).text((6, 6), ch, font=_font(path, 32), fill=255)
    return img.tobytes()


@lru_cache(maxsize=4)
def _notdef(path):
    return _glyph_sig(path, "\uffff")


@lru_cache(maxsize=4096)
def _has_glyph(path, ch):
    if ch.isspace():
        return True
    return _glyph_sig(path, ch) != _notdef(path)


def _clean(text, fallback, path=_BOLD):
    """Strip HTML, then drop characters the bundled fonts cannot draw (Hindi,
    Arabic, emoji ... would show up as empty boxes on the image)."""
    text = html.unescape(re.sub(r"<[^>]+>", "", str(text or "")))
    text = "".join(ch for ch in text if _has_glyph(path, ch))
    text = " ".join(text.split())
    return text or fallback


def _ellipsis(path):
    return "…" if _has_glyph(path, "…") else "..."


def _fit(text, font, width, path=_BOLD):
    """Single line, trimmed with an ellipsis so it fits `width` pixels."""
    if font.getlength(text) <= width:
        return text
    tail = _ellipsis(path)
    while text and font.getlength(text + tail) > width:
        text = text[:-1]
    return text.rstrip() + tail


def _wrap(text, font, width, max_lines, path=_BOLD):
    lines, cur = [], ""
    for word in text.split(" "):
        trial = f"{cur} {word}".strip()
        if font.getlength(trial) <= width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = word
        while font.getlength(cur) > width and len(cur) > 1:  # very long word
            cut = len(cur)
            while cut > 1 and font.getlength(cur[:cut]) > width:
                cut -= 1
            lines.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        tail = _ellipsis(path)
        last = lines[-1]
        while last and font.getlength(last + tail) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + tail
    return lines


def _tracked(draw, xy, text, font, fill, tracking=0.0, anchor="ls"):
    """Letter-spaced text (left / baseline anchored). Returns the end x."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill, anchor=anchor)
        x += font.getlength(ch) + tracking
    return x - tracking


def _num(draw, xy, text, size, fill, anchor="ls", stroke=1):
    """Times / indices. Raleway's default figures are old-style (a tiny '0'), so
    digits use the lining figures of the second font, thickened with a stroke."""
    draw.text(xy, text, font=_font(_LIGHT, size), fill=fill, anchor=anchor, stroke_width=stroke, stroke_fill=fill)


def _short(value):
    """'03:45' -> '3:45' (keeps '1:02:03' as is)."""
    return re.sub(r"^0(\d:)", r"\1", str(value or "").strip())


def _tracked_width(text, font, tracking):
    return sum(font.getlength(ch) for ch in text) + tracking * max(len(text) - 1, 0)


# ── image helpers ────────────────────────────────────────────────────────────
def _open(path):
    img = Image.open(path)
    img.load()
    return img.convert("RGB")


def _cover(img, width, height):
    """Resize + centre-crop to exactly width x height."""
    scale = max(width / img.width, height / img.height)
    size = (max(int(img.width * scale + 0.5), width), max(int(img.height * scale + 0.5), height))
    img = img.resize(size, LANCZOS)
    left, top = (size[0] - width) // 2, (size[1] - height) // 2
    return img.crop((left, top, left + width, top + height))


def _rr_mask(width, height, radius, ss=4):
    """Anti-aliased rounded-rectangle mask."""
    big = Image.new("L", (width * ss, height * ss), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, width * ss - 1, height * ss - 1], radius=radius * ss, fill=255
    )
    return big.resize((width, height), LANCZOS)


def _circle_mask(diameter, ss=4):
    big = Image.new("L", (diameter * ss, diameter * ss), 0)
    ImageDraw.Draw(big).ellipse([0, 0, diameter * ss - 1, diameter * ss - 1], fill=255)
    return big.resize((diameter, diameter), LANCZOS)


def _scale_mask(mask, factor):
    return mask.point(lambda v: int(v * factor))


def _paste_color(base, box, color, mask, alpha=1.0):
    layer = Image.new("RGB", mask.size, color[:3])
    base.paste(layer, (box[0], box[1]), _scale_mask(mask, alpha) if alpha != 1.0 else mask)


def _rounded(base, box, radius, fill, alpha=1.0, outline=None, outline_alpha=1.0, width=2):
    """Anti-aliased rounded rect: optional translucent fill and outline."""
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = x1 - x0, y1 - y0
    if w < 2 or h < 2:
        return
    mask = _rr_mask(w, h, radius)
    if fill is not None:
        _paste_color(base, (x0, y0), fill, mask, alpha)
    if outline is not None:
        inner = Image.new("L", (w, h), 0)
        inner.paste(_rr_mask(w - 2 * width, h - 2 * width, max(radius - width, 1)), (width, width))
        ring = ImageChops.subtract(mask, inner)
        _paste_color(base, (x0, y0), outline, ring, outline_alpha)


def _shadow(base, box, radius, blur=26, offset=(0, 16), alpha=0.65, ellipse=False):
    x0, y0, x1, y1 = box
    pad = blur * 3
    w, h = x1 - x0, y1 - y0
    layer = Image.new("L", (w + pad * 2, h + pad * 2), 0)
    d = ImageDraw.Draw(layer)
    rect = [pad, pad, pad + w, pad + h]
    if ellipse:
        d.ellipse(rect, fill=255)
    else:
        d.rounded_rectangle(rect, radius=radius, fill=255)
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    _paste_color(base, (x0 - pad + offset[0], y0 - pad + offset[1]), (0, 0, 0), layer, alpha)


def _gradient(size, horizontal=True, reverse=False):
    """0..255 ramp as an 'L' image."""
    g = Image.linear_gradient("L")  # black (top) -> white (bottom)
    if horizontal:
        g = g.rotate(90, expand=True)  # top (black) -> left, bottom (white) -> right
    if reverse:
        g = ImageChops.invert(g)
    return g.resize(size, LANCZOS)


def _glow(base, center, radius, color, strength=0.5):
    """Soft radial light in `color` centred on `center`."""
    # radial_gradient is 0 at the centre but only 181 at the edge midpoints, so
    # rescale to make the light fade to exactly zero at `radius`
    g = ImageChops.invert(Image.radial_gradient("L")).resize((radius * 2, radius * 2), LANCZOS)
    g = g.point(lambda v: int((max(v - 74, 0) / 181) ** 2 * 255))
    _paste_color(base, (center[0] - radius, center[1] - radius), color, g, strength)


def _accent(img):
    """Most vivid dominant colour of the art (BRAND for grey / dark art)."""
    small = img.resize((64, 64), LANCZOS).quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    palette = small.getpalette() or []
    best, best_score = None, 0.0
    for count, idx in small.getcolors() or []:
        r, g, b = palette[idx * 3 : idx * 3 + 3]
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if v < 0.22 or s < 0.22:
            continue
        score = (s**1.3) * (0.4 + v) * math.sqrt(count)
        if score > best_score:
            best, best_score = (h, s, v), score
    if not best:
        return BRAND
    h, s, v = best
    r, g, b = colorsys.hsv_to_rgb(h, max(s, 0.6), max(v, 0.92))
    return int(r * 255), int(g * 255), int(b * 255)


def _lighten(color, amount):
    return tuple(int(c + (255 - c) * amount) for c in color)


def _backdrop(art, size, darkness=0.42):
    """Blurred, darkened, grainy version of the art."""
    w, h = size
    small = _cover(art, w // 2, h // 2).filter(ImageFilter.GaussianBlur(20))
    bg = small.resize(size, LANCZOS)
    bg = ImageEnhance.Color(bg).enhance(1.4)
    bg = ImageEnhance.Brightness(bg).enhance(darkness)
    grain = Image.effect_noise(size, 22).convert("RGB")
    return Image.blend(bg, ImageChops.overlay(bg, grain), 0.07)


def _vignette(base, strength=0.55):
    w, h = base.size
    g = Image.radial_gradient("L").resize((w, h), LANCZOS)  # black centre
    g = g.point(lambda v: int(max(v - 90, 0) / 165 * 255 * strength))
    _paste_color(base, (0, 0), (0, 0, 0), g)


def _note(draw, cx, cy, size, fill):
    """A small beamed pair of eighth notes (the fonts have no ♫ glyph)."""
    r = size * 0.16
    sx = size * 0.30
    for dx in (-sx, sx):
        draw.ellipse([cx + dx - r * 1.2, cy + size * 0.28 - r, cx + dx + r * 1.2, cy + size * 0.28 + r], fill=fill)
        draw.rectangle([cx + dx + r * 0.7, cy - size * 0.36, cx + dx + r * 1.2, cy + size * 0.28], fill=fill)
    draw.polygon(
        [
            (cx - sx + r * 0.7, cy - size * 0.36),
            (cx + sx + r * 1.2, cy - size * 0.46),
            (cx + sx + r * 1.2, cy - size * 0.28),
            (cx - sx + r * 0.7, cy - size * 0.18),
        ],
        fill=fill,
    )


def _placeholder(size, seed, accent):
    """Generated cover for tracks without artwork."""
    rnd = random.Random(seed)
    h, s, v = colorsys.rgb_to_hsv(*[c / 255 for c in accent])
    h2 = (h + rnd.choice((0.08, 0.16, -0.08, -0.16))) % 1.0
    a = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(h, 0.7, 0.55))
    b = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(h2, 0.75, 0.30))
    tile = Image.new("RGB", (size, size), b)
    tile.paste(Image.new("RGB", (size, size), a), (0, 0), _gradient((size, size), horizontal=False, reverse=True))
    d = ImageDraw.Draw(tile, "RGBA")
    _note(d, size / 2, size / 2, size * 0.5, (255, 255, 255, 190))
    return tile


def _waveform(seed, count):
    """Smooth pseudo-random loudness curve, unique per seed (video id)."""
    rnd = random.Random(seed)
    phase = [rnd.uniform(0, math.tau) for _ in range(3)]
    freq = [rnd.uniform(0.12, 0.26), rnd.uniform(0.45, 0.8), rnd.uniform(1.2, 2.0)]
    out = []
    for i in range(count):
        v = (
            0.5 * abs(math.sin(i * freq[0] + phase[0]))
            + 0.3 * abs(math.sin(i * freq[1] + phase[1]))
            + 0.2 * abs(math.sin(i * freq[2] + phase[2]))
        )
        v = (0.16 + 0.84 * v) * (0.86 + 0.14 * rnd.random())
        edge = min(i, count - 1 - i) / 5
        out.append(min(v * min(1.0, 0.45 + edge), 1.0))
    return out


def _draw_waveform(base, box, seed, accent, lit=1.0, ss=3):
    """Mirrored waveform bars. `lit` (0..1) is the played fraction: played bars
    use the accent gradient, the rest are dim white."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    pitch = 10
    count = max(w // pitch, 8)
    vals = _waveform(seed, count)
    layer = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    mid = h * ss / 2
    bar = 6 * ss
    end = _lighten(accent, 0.45)
    for i, v in enumerate(vals):
        t = i / max(count - 1, 1)
        if t <= lit:
            k = t / max(lit, 1e-6)
            color = tuple(int(accent[c] + (end[c] - accent[c]) * k) for c in range(3)) + (255,)
        else:
            color = (255, 255, 255, 70)
        half = max(v * h * ss / 2, 4 * ss)
        x = i * pitch * ss + (pitch * ss - bar) / 2
        d.rounded_rectangle([x, mid - half, x + bar, mid + half], radius=bar / 2, fill=color)
    layer = layer.resize((w, h), LANCZOS)
    base.paste(layer, (x0, y0), layer)


def _vinyl(art, radius, accent, seed):
    """Record with grooves, light sheen and the cover as its centre label."""
    ss = 3
    size = radius * 2 * ss
    c = size // 2
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse([0, 0, size - 1, size - 1], fill=(15, 15, 18, 255))
    rnd = random.Random(seed)
    r = int(radius * ss * 0.97)
    while r > radius * ss * 0.40:
        d.ellipse([c - r, c - r, c + r, c + r], outline=(255, 255, 255, rnd.randint(8, 30)), width=ss)
        r -= rnd.randint(3, 7) * ss // 2
    for gap in (0.52, 0.71, 0.86):  # track gaps
        r = int(radius * ss * gap)
        d.ellipse([c - r, c - r, c + r, c + r], outline=(0, 0, 0, 200), width=ss * 2)
    sheen = Image.new("L", (size, size), 0)
    sd = ImageDraw.Draw(sheen)
    sd.pieslice([0, 0, size - 1, size - 1], -38, 8, fill=255)
    sd.pieslice([0, 0, size - 1, size - 1], 142, 188, fill=255)
    sheen = sheen.filter(ImageFilter.GaussianBlur(size * 0.018)).point(lambda v: int(v * 0.16))
    layer = Image.alpha_composite(layer, Image.merge("RGBA", (*Image.new("RGB", (size, size), (255, 255, 255)).split(), sheen)))
    d = ImageDraw.Draw(layer)
    d.ellipse([0, 0, size - 1, size - 1], outline=(255, 255, 255, 46), width=ss * 2)
    label = int(radius * ss * 0.34)
    d.ellipse([c - label - ss * 5, c - label - ss * 5, c + label + ss * 5, c + label + ss * 5], fill=accent + (255,))
    face = _cover(art, label * 2, label * 2)
    layer.paste(face, (c - label, c - label), _circle_mask(label * 2, ss=2))
    hole = int(radius * ss * 0.05)
    d.ellipse([c - hole * 2, c - hole * 2, c + hole * 2, c + hole * 2], fill=(15, 15, 18, 255))
    d.ellipse([c - hole, c - hole, c + hole, c + hole], fill=(4, 4, 6, 255))
    return layer.resize((radius * 2, radius * 2), LANCZOS)


def _initial(name):
    return (name[:1] or "?").upper()


def _chip(base, x, y, text, accent, font, tracking=3.0, filled=False):
    """Small pill label. Returns the x where the chip ends."""
    d = ImageDraw.Draw(base, "RGBA")
    tw = _tracked_width(text, font, tracking)
    w, h = int(tw + 40), 38
    if filled:
        _rounded(base, (x, y, x + w, y + h), h // 2, accent)
        color = INK + (255,)
    else:
        _rounded(base, (x, y, x + w, y + h), h // 2, None, outline=accent, outline_alpha=0.9, width=2)
        color = accent + (255,)
    _tracked(d, (x + 20, y + h - 12), text, font, color, tracking)
    return x + w


def _eq_icon(base, x, y, color, heights=(10, 18, 13), bar=5, gap=3):
    d = ImageDraw.Draw(base, "RGBA")
    for i, hh in enumerate(heights):
        bx = x + i * (bar + gap)
        d.rounded_rectangle([bx, y - hh, bx + bar - 1, y], radius=2, fill=color)


def _brand(base, right, y, alpha=150):
    d = ImageDraw.Draw(base, "RGBA")
    font = _font(_BOLD, 17)
    text = "BOMMY MUSIC"
    tw = _tracked_width(text, font, 5)
    _tracked(d, (right - tw, y), text, font, (255, 255, 255, alpha), 5)
    d.ellipse([right - tw - 24, y - 12, right - tw - 14, y - 2], fill=BRAND + (255,))


# ── now playing card ─────────────────────────────────────────────────────────
def _title_lines(title, width):
    """Largest size whose wrapped title fits 2 lines (3 for the smallest sizes);
    the final size truncates with an ellipsis."""
    for size, limit in ((64, 2), (58, 2), (52, 2), (46, 3)):
        font = _font(_BOLD, size)
        lines = _wrap(title, font, width, 99)
        if len(lines) <= limit:
            return font, size, lines
    font = _font(_BOLD, 42)
    return font, 42, _wrap(title, font, width, 3)


def _render_now_playing(art_path, title, by, duration, kind, vidid, out_path):
    W, H = NP_SIZE
    art = _open(art_path)
    accent = _accent(art)

    title = _clean(title, "Unknown Track")
    by = _clean(by, "Unknown")
    duration = _short(duration)

    base = _backdrop(art, NP_SIZE)
    base.paste(Image.new("RGB", NP_SIZE, (6, 8, 14)), (0, 0), _scale_mask(_gradient(NP_SIZE, True), 0.55))
    _vignette(base)

    # artwork: album sleeve with the record sliding out of it
    S, R = 410, 196
    sx, sy = 64, (H - S) // 2
    vcx, vcy = sx + S + 34, H // 2
    _glow(base, (vcx + 20, vcy), 520, accent, 0.30)
    _shadow(base, (vcx - R, vcy - R, vcx + R, vcy + R), 0, blur=24, offset=(10, 14), alpha=0.7, ellipse=True)
    disc = _vinyl(art, R, accent, vidid or title)
    base.paste(disc, (vcx - R, vcy - R), disc)

    _shadow(base, (sx, sy, sx + S, sy + S), 30, blur=30, offset=(0, 22), alpha=0.8)
    sleeve = _cover(art, S, S)
    gloss = Image.new("L", (S, S), 0)
    ImageDraw.Draw(gloss).polygon([(0, 0), (S * 0.75, 0), (0, S * 0.75)], fill=255)
    gloss = _scale_mask(gloss.filter(ImageFilter.GaussianBlur(28)), 0.16)
    sleeve.paste(Image.new("RGB", (S, S), (255, 255, 255)), (0, 0), gloss)
    base.paste(sleeve, (sx, sy), _rr_mask(S, S, 30))
    _rounded(base, (sx, sy, sx + S, sy + S), 30, None, outline=(255, 255, 255), outline_alpha=0.22, width=2)

    # text column
    tx, right = 764, W - 64
    width = right - tx

    pill_font = _font(_BOLD, 19)
    label = "NOW PLAYING"
    pw = int(_tracked_width(label, pill_font, 4) + 78)
    _rounded(base, (tx, 86, tx + pw, 86 + 46), 23, accent)
    _eq_icon(base, tx + 22, 86 + 31, INK + (255,))
    d = ImageDraw.Draw(base, "RGBA")
    _tracked(d, (tx + 54, 86 + 31), label, pill_font, INK + (255,), 4)

    font, size, lines = _title_lines(title, width)
    y = 206
    for line in lines:
        d.text((tx, y), line, font=font, fill=(255, 255, 255, 255), anchor="ls")
        y += int(size * 1.16)

    y += 14
    d.ellipse([tx, y, tx + 46, y + 46], fill=accent + (255,))
    d.text((tx + 23, y + 32), _initial(by), font=_font(_BOLD, 26), fill=INK + (255,), anchor="ms")
    d.text((tx + 62, y + 19), "requested by", font=_font(_LIGHT, 20), fill=(255, 255, 255, 150), anchor="ls")
    name_font = _font(_BOLD, 24)
    d.text((tx + 62, y + 44), _fit(by, name_font, width - 70), font=name_font, fill=(255, 255, 255, 235), anchor="ls")

    wy0 = 456
    _draw_waveform(base, (tx, wy0, right, wy0 + 90), vidid or title, accent, lit=1.0)
    d = ImageDraw.Draw(base, "RGBA")
    _num(d, (tx, wy0 + 124), "0:00", 22, (255, 255, 255, 150), stroke=0)
    if duration:
        _num(d, (right, wy0 + 124), duration, 24, (255, 255, 255, 240), anchor="rs")

    chip_font = _font(_BOLD, 15)
    chip_y = 610
    cx = _chip(base, tx, chip_y, (kind or "audio").upper(), accent, chip_font)
    if by.lower() == "autoplay":
        _chip(base, cx + 12, chip_y, "AUTOPLAY", accent, chip_font, filled=True)
    _brand(base, right, chip_y + 26)

    tmp = out_path + ".tmp"
    base.save(tmp, format="JPEG", quality=90, optimize=True, progressive=True)
    os.replace(tmp, out_path)


def _prune(pattern, keep, protect=()):
    """Delete all but the newest `keep` files, never touching `protect`
    (cards that are still on screen and get re-uploaded on every refresh)."""
    try:
        safe = {os.path.abspath(str(p)) for p in protect if p}
        files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            if os.path.abspath(old) not in safe:
                os.remove(old)
    except OSError:
        pass


async def now_playing_card(photo, track, protect=()):
    """Return the path of the styled card for `track`.

    `photo` is the plain cached thumbnail. When it is not a local file (URL
    fall-backs for telegram / live / index streams) or anything goes wrong, the
    original `photo` is returned untouched so playback UI keeps working.
    """
    try:
        if not photo or not os.path.isfile(str(photo)) or not track:
            return photo
        vidid = str(track.get("vidid") or "")
        key = "|".join(
            str(x)
            for x in (vidid, track.get("title"), track.get("by"), track.get("dur"), track.get("streamtype"))
        )
        out = os.path.join(CACHE_DIR, f"np_{hashlib.sha1(key.encode()).hexdigest()[:16]}.jpg")
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        os.makedirs(CACHE_DIR, exist_ok=True)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            _render_now_playing,
            photo,
            track.get("title"),
            track.get("by"),
            track.get("dur"),
            track.get("streamtype"),
            vidid,
            out,
        )
        _prune(os.path.join(CACHE_DIR, "np_*.jpg"), KEEP_CARDS, protect)
        return out
    except Exception:
        return photo


# ── queue card ───────────────────────────────────────────────────────────────
def _cover_tile(path, size, radius, seed, accent):
    try:
        tile = _cover(_open(path), size, size) if path and os.path.isfile(str(path)) else None
    except Exception:
        tile = None
    if tile is None:
        tile = _placeholder(size, seed, accent)
    out = Image.new("RGB", (size, size))
    out.paste(tile, (0, 0))
    return out, _rr_mask(size, size, radius)


def _heart_mask(size, ss=4):
    """Anti-aliased heart shape (parametric curve) filling a size x size box."""
    n = size * ss
    pts = []
    for i in range(361):
        t = math.radians(i)
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((n * (0.5 + x / 35.0), n * (0.47 - y / 35.0)))
    big = Image.new("L", (n, n), 0)
    ImageDraw.Draw(big).polygon(pts, fill=255)
    return big.resize((size, size), LANCZOS)


def _heart_tile(path, size, seed, accent):
    """Cover art cut into a heart, with a soft accent outline."""
    try:
        tile = _cover(_open(path), size, size) if path and os.path.isfile(str(path)) else None
    except Exception:
        tile = None
    if tile is None:
        tile = _placeholder(size, seed, accent)
    mask = _heart_mask(size)
    out = Image.new("RGB", (size, size), (0, 0, 0))
    out.paste(tile, (0, 0))
    ring = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(5)), _scale_mask(mask, 1.0))
    out.paste(Image.new("RGB", (size, size), accent), (0, 0), ring)
    return out, ImageChops.lighter(mask, ring)


def _glass(base, box, radius, accent=None, strong=False):
    _shadow(base, box, radius, blur=16, offset=(0, 8), alpha=0.35)
    _rounded(base, box, radius, (255, 255, 255), alpha=0.13 if strong else 0.08)
    _rounded(base, box, radius, None, outline=accent or (255, 255, 255), outline_alpha=0.45 if accent else 0.14, width=2)


def _render_queue(tracks, covers, out_path):
    """tracks[0] is playing; tracks[1:1+Q_ROWS] are the stops; `covers` maps
    index -> cached cover path (missing -> generated placeholder)."""
    from BOMMYMUSIC.utils.deck_style import eta_seconds, fmt_time, to_seconds, total_seconds

    current = tracks[0]
    upcoming = tracks[1:]
    shown = upcoming[:Q_ROWS]
    more = len(upcoming) - len(shown)

    H = 400 + max(len(shown), 1) * 112 + 96
    size = (Q_WIDTH, H)

    now_art = None
    try:
        if covers.get(0) and os.path.isfile(str(covers[0])):
            now_art = _open(covers[0])
    except Exception:
        now_art = None
    accent = _accent(now_art) if now_art else BRAND
    base = _backdrop(now_art, size, 0.34) if now_art else Image.new("RGB", size, (12, 16, 26))
    base.paste(Image.new("RGB", size, (6, 8, 14)), (0, 0), _scale_mask(_gradient(size, False, reverse=True), 0.45))
    _glow(base, (110, 250), 420, accent, 0.20)
    _vignette(base, 0.5)

    d = ImageDraw.Draw(base, "RGBA")
    L, R = 170, Q_WIDTH - 70
    rail_x = 108

    # header
    ph = _font(_BOLD, 19)
    pw = int(_tracked_width("QUEUE", ph, 5) + 76)
    _rounded(base, (70, 56, 70 + pw, 56 + 46), 23, accent)
    _eq_icon(base, 92, 56 + 31, INK + (255,))
    d = ImageDraw.Draw(base, "RGBA")
    _tracked(d, (124, 56 + 31), "QUEUE", ph, INK + (255,), 5)

    secs, unknown = total_seconds(upcoming)
    stats = f"{len(upcoming)} UP NEXT"
    if upcoming:
        stats += f"   ·   {fmt_time(secs)}{'+' if unknown else ''} TOTAL"
    sf = _font(_BOLD, 18)
    sw = _tracked_width(stats, sf, 3)
    _tracked(d, (R - sw, 56 + 31), stats, sf, (255, 255, 255, 190), 3)

    # rail
    row_top = 400
    centers = [row_top + i * 112 + 48 for i in range(len(shown))]
    now_y = 268
    if centers:
        d.line([(rail_x, now_y), (rail_x, centers[-1] + (36 if more else 0))], fill=(255, 255, 255, 46), width=4)
        d.line([(rail_x, now_y), (rail_x, centers[0])], fill=accent + (255,), width=4)
    if more:
        for k in range(3):
            yy = centers[-1] + 34 + k * 12
            d.ellipse([rail_x - 3, yy - 3, rail_x + 3, yy + 3], fill=(255, 255, 255, 90))
    _glow(base, (rail_x, now_y), 60, accent, 0.55)
    d = ImageDraw.Draw(base, "RGBA")
    d.ellipse([rail_x - 15, now_y - 15, rail_x + 15, now_y + 15], fill=accent + (255,))
    d.ellipse([rail_x - 6, now_y - 6, rail_x + 6, now_y + 6], fill=INK + (255,))

    # now playing card
    box = (L, 168, R, 368)
    _glass(base, box, 30, accent=accent, strong=True)
    tile, tmask = _cover_tile(covers.get(0), 150, 22, current.get("vidid"), accent)
    base.paste(tile, (L + 24, 168 + 25), tmask)
    d = ImageDraw.Draw(base, "RGBA")
    tx = L + 24 + 150 + 30
    tw = R - 30 - tx
    _tracked(d, (tx, 168 + 52), "NOW PLAYING", _font(_BOLD, 16), accent + (255,), 5)
    tfont = _font(_BOLD, 40)
    d.text((tx, 168 + 102), _fit(_clean(current.get("title"), "Unknown Track"), tfont, tw), font=tfont, fill=(255, 255, 255, 255), anchor="ls")
    d.text(
        (tx, 168 + 134),
        "requested by  " + _fit(_clean(current.get("by"), "Unknown"), _font(_LIGHT, 22), 420, _LIGHT),
        font=_font(_LIGHT, 22),
        fill=(255, 255, 255, 165),
        anchor="ls",
    )
    total = to_seconds(current.get("dur"))
    played = int(current.get("played") or 0)
    ratio = min(played / total, 1.0) if total else 0.0
    by0, by1 = 168 + 158, 168 + 166
    d.rounded_rectangle([tx, by0, tw + tx, by1], radius=4, fill=(255, 255, 255, 50))
    if ratio > 0:
        d.rounded_rectangle([tx, by0, tx + max(int(tw * ratio), 8), by1], radius=4, fill=accent + (255,))
    kx = tx + int(tw * ratio)
    d.ellipse([kx - 9, (by0 + by1) // 2 - 9, kx + 9, (by0 + by1) // 2 + 9], fill=(255, 255, 255, 255))
    stamp = f"{fmt_time(played)} / {fmt_time(total)}" if total else f"{fmt_time(played)} / live"
    _num(d, (tw + tx, 168 + 52), stamp, 23, (255, 255, 255, 255), anchor="rs", stroke=0)

    # stops
    for i, track in enumerate(shown):
        top = row_top + i * 112
        y0, y1 = top, top + 96
        cy = centers[i]
        _glass(base, (L, y0, R, y1), 26)
        d = ImageDraw.Draw(base, "RGBA")
        d.ellipse([rail_x - 11, cy - 11, rail_x + 11, cy + 11], fill=(12, 16, 26, 255), outline=accent + (255,), width=3)
        d.ellipse([rail_x - 4, cy - 4, rail_x + 4, cy + 4], fill=accent + (255,))
        _num(d, (L + 26, cy + 17), f"{i + 1:02d}", 46, accent + (240,), stroke=2)
        tile, tmask = _heart_tile(covers.get(i + 1), 76, track.get("vidid") or track.get("title"), accent)
        base.paste(tile, (L + 108, y0 + 10), tmask)
        d = ImageDraw.Draw(base, "RGBA")
        dx = L + 108 + 76 + 24
        right_block = 150
        tf = _font(_BOLD, 29)
        d.text((dx, cy - 4), _fit(_clean(track.get("title"), "Unknown Track"), tf, R - dx - right_block - 20), font=tf, fill=(255, 255, 255, 255), anchor="ls")
        sf2 = _font(_LIGHT, 21)
        d.text((dx, cy + 26), "by  " + _fit(_clean(track.get("by"), "Unknown"), sf2, 380, _LIGHT), font=sf2, fill=(255, 255, 255, 150), anchor="ls")
        _num(d, (R - 28, cy - 2), _short(track.get("dur")) or "--:--", 28, (255, 255, 255, 245), anchor="rs")
        wait = eta_seconds(tracks, i + 1)
        d.text((R - 28, cy + 26), f"in {fmt_time(wait)}" if wait is not None else "up next", font=_font(_LIGHT, 21), fill=accent + (230,), anchor="rs")

    if not shown:
        d.text((L, row_top + 40), "Nothing queued after this track yet.", font=_font(_LIGHT, 26), fill=(255, 255, 255, 170), anchor="ls")
    if more:
        d.text((L, H - 60), f"+ {more} more track{'s' if more != 1 else ''} in the queue", font=_font(_LIGHT, 23), fill=(255, 255, 255, 170), anchor="ls")
    _brand(base, R, H - 58)

    tmp = out_path + ".tmp"
    base.save(tmp, format="JPEG", quality=90, optimize=True)
    os.replace(tmp, out_path)


async def queue_card(chat_id, tracks):
    """Render the queue image for a chat. Returns a file path or None."""
    try:
        if not tracks:
            return None
        from BOMMYMUSIC.utils.thumbnails import get_thumb

        wanted = tracks[: 1 + Q_ROWS]

        async def cover(index, track):
            vid = str(track.get("vidid") or "")
            if not vid or vid in ("telegram", "soundcloud") or "." in vid or "/" in vid:
                return index, None
            try:
                path = await asyncio.wait_for(get_thumb(vid), timeout=6)
            except Exception:
                return index, None
            return index, path if os.path.isfile(str(path)) else None

        covers = dict(await asyncio.gather(*(cover(i, t) for i, t in enumerate(wanted))))
        os.makedirs(CACHE_DIR, exist_ok=True)
        out = os.path.join(CACHE_DIR, f"queue_{str(chat_id).replace('-', 'm')}.jpg")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _render_queue, [dict(t) for t in tracks], covers, out)
        return out
    except Exception:
        return None


# ── "added to queue" ticket ──────────────────────────────────────────────────
def _render_ticket(cover, title, by, position, eta, dur, vidid, out_path):
    W, H = 1280, 580
    art = _open(cover) if cover and os.path.isfile(str(cover)) else None
    accent = _accent(art) if art else BRAND
    base = _backdrop(art, (W, H), 0.38) if art else Image.new("RGB", (W, H), (12, 16, 26))
    base.paste(Image.new("RGB", (W, H), (6, 8, 14)), (0, 0), _scale_mask(_gradient((W, H), True), 0.5))
    _vignette(base, 0.6)

    title = _clean(title, "Unknown Track")
    by = _clean(by, "Unknown")

    # heart cover with glow + shadow
    hs, hx, hy = 400, 70, 90
    _glow(base, (hx + hs // 2, hy + hs // 2), 330, accent, 0.45)
    heart, hmask = _heart_tile(cover, hs, vidid or title, accent)
    sh = hmask.filter(ImageFilter.GaussianBlur(18))
    _paste_color(base, (hx + 6, hy + 22), (0, 0, 0), sh, 0.7)
    base.paste(heart, (hx, hy), hmask)

    d = ImageDraw.Draw(base, "RGBA")
    # ticket perforation
    px = 520
    for y in range(34, H - 20, 26):
        d.rounded_rectangle([px - 2, y, px + 2, y + 12], radius=2, fill=(255, 255, 255, 70))
    for cy in (0, H):
        d.ellipse([px - 24, cy - 24, px + 24, cy + 24], fill=(6, 8, 14, 255))

    tx, right = 580, W - 64
    pf = _font(_BOLD, 19)
    label = "ADDED TO QUEUE"
    pw = int(_tracked_width(label, pf, 4) + 78)
    _rounded(base, (tx, 58, tx + pw, 58 + 46), 23, accent)
    _eq_icon(base, tx + 22, 58 + 31, INK + (255,))
    d = ImageDraw.Draw(base, "RGBA")
    _tracked(d, (tx + 54, 58 + 31), label, pf, INK + (255,), 4)
    _num(d, (right, 160), f"#{position}", 130, accent + (255,), anchor="rs", stroke=3)

    font, size, lines = _title_lines(title, right - tx)
    if size > 46:
        font, size = _font(_BOLD, 46), 46
        lines = _wrap(title, font, right - tx, 2)
    y = 214
    for line in lines[:2]:
        d.text((tx, y), line, font=font, fill=(255, 255, 255, 255), anchor="ls")
        y += int(size * 1.15)

    y += 2
    d.ellipse([tx, y, tx + 42, y + 42], fill=accent + (255,))
    d.text((tx + 21, y + 29), _initial(by), font=_font(_BOLD, 24), fill=INK + (255,), anchor="ms")
    d.text((tx + 56, y + 17), "requested by", font=_font(_LIGHT, 19), fill=(255, 255, 255, 150), anchor="ls")
    nf = _font(_BOLD, 22)
    d.text((tx + 56, y + 40), _fit(by, nf, 420), font=nf, fill=(255, 255, 255, 235), anchor="ls")

    # plays-in + duration
    ey = 456
    if eta:
        _tracked(d, (tx, ey - 30), "PLAYS IN", _font(_BOLD, 15), (255, 255, 255, 150), 4)
        _num(d, (tx, ey + 18), eta, 54, (255, 255, 255, 255), stroke=2)
    if dur:
        dl = _tracked_width("DURATION", _font(_BOLD, 15), 4)
        _tracked(d, (right - dl, ey - 30), "DURATION", _font(_BOLD, 15), (255, 255, 255, 150), 4)
        _num(d, (right, ey + 18), _short(dur), 54, accent + (255,), anchor="rs", stroke=2)

    # lane: now -> stops -> yours
    ly = 508
    between = max(int(position) - 1, 0)
    shown = min(between, 5)
    n = shown + 2
    xs = [tx + 14 + i * ((right - tx - 60) / max(n - 1, 1)) for i in range(n)]
    d.line([(xs[0], ly), (xs[-1], ly)], fill=(255, 255, 255, 60), width=4)
    d.ellipse([xs[0] - 13, ly - 13, xs[0] + 13, ly + 13], fill=accent + (255,))
    d.ellipse([xs[0] - 5, ly - 5, xs[0] + 5, ly + 5], fill=INK + (255,))
    for x in xs[1:-1]:
        d.ellipse([x - 8, ly - 8, x + 8, ly + 8], fill=(12, 16, 26, 255), outline=(255, 255, 255, 150), width=3)
    if between > shown:
        d.text(((xs[-2] + xs[-1]) / 2, ly - 16), f"+{between - shown}", font=_font(_BOLD, 17), fill=(255, 255, 255, 170), anchor="ms")
    hm = _heart_mask(46)
    _glow(base, (int(xs[-1]), ly), 50, accent, 0.6)
    _paste_color(base, (int(xs[-1]) - 23, ly - 23), accent, hm)
    d = ImageDraw.Draw(base, "RGBA")
    lf = _font(_BOLD, 14)
    _tracked(d, (xs[0] - 12, ly + 36), "NOW", lf, (255, 255, 255, 150), 4)
    tw = _tracked_width("YOURS", lf, 4)
    _tracked(d, (xs[-1] + 24 - tw, ly + 36), "YOURS", lf, accent + (255,), 4)
    _brand(base, right, 40, alpha=110)

    tmp = out_path + ".tmp"
    base.save(tmp, format="JPEG", quality=90, optimize=True)
    os.replace(tmp, out_path)


async def queue_added_card(track, position, wait_seconds=None):
    """Ticket image for a track that was just added to the queue (or None)."""
    try:
        from BOMMYMUSIC.utils.deck_style import fmt_time
        from BOMMYMUSIC.utils.thumbnails import get_thumb

        vid = str(track.get("vidid") or "")
        cover = None
        if vid and vid not in ("telegram", "soundcloud") and "." not in vid and "/" not in vid:
            try:
                cover = await asyncio.wait_for(get_thumb(vid), timeout=6)
            except Exception:
                cover = None
        eta = fmt_time(wait_seconds) if wait_seconds is not None else ""
        key = f"{vid}|{track.get('title')}|{track.get('by')}|{position}|{eta}"
        out = os.path.join(CACHE_DIR, f"qa_{hashlib.sha1(key.encode()).hexdigest()[:16]}.jpg")
        os.makedirs(CACHE_DIR, exist_ok=True)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, _render_ticket, cover, track.get("title"), track.get("by"),
            position, eta, track.get("dur"), vid, out,
        )
        _prune(os.path.join(CACHE_DIR, "qa_*.jpg"), 40)
        return out
    except Exception:
        return None

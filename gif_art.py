"""
Animated GIF art for Gondor: status badges ("NOW PLAYING", "DOWNLOADING", ...), cover-art headers and
the ANSI-terminal summon donut.

Discord animates GIF attachments natively, so unlike edited text they are smooth and cost no
rate limit. Everything here is pure standard library - a tiny GIF89a encoder plus a built-in 5x7
pixel font - so there is nothing extra to install (no Pillow, no font files).

Public API (all thread-safe; results cached in memory and, where it makes sense, in ./gif_cache):
    badge_bytes(label, style)            animated badge for a status label, e.g. "now playing"
    header_bytes(label, style, cover)    the badge with a pixel-art cover on its left (LRU cached)
    donut_bytes(style)                   spinning donut in an ANSI terminal window
    cover_from_rgb(data, w, h, key)      raw rgb24 -> Cover (None if the data is unusable)
    terminal_bytes(kind, style, text)    ANSI terminal scenes: "shutdown", "crash", "rain"
    adaptive_style(cover)                a colour style derived from a cover's dominant colours
    prewarm()                            generate the default-style set (run in a background thread)
Styles ("vaporwave", "matrix", "sunset", "synthwave") only change colours; "auto" follows the cover.
"""

import colorsys
import hashlib
import math
import random
import threading
from collections import OrderedDict
from pathlib import Path

# ---------------------------------------------------------------------------
# GIF encoder (GIF89a, global palette, looping, LZW)
# ---------------------------------------------------------------------------


def _lzw(data, min_code):
    """GIF-flavoured LZW over a bytes-like of palette indices (all < 2**min_code)."""
    clear = 1 << min_code
    eoi = clear + 1
    out = bytearray()
    acc = 0
    nbits = 0
    code_size = min_code + 1
    next_code = eoi + 1
    table = {}

    acc |= clear << nbits
    nbits += code_size
    prefix = data[0]
    for px in data[1:]:
        key = (prefix << 8) | px
        code = table.get(key)
        if code is not None:
            prefix = code
            continue
        acc |= prefix << nbits
        nbits += code_size
        while nbits >= 8:
            out.append(acc & 255)
            acc >>= 8
            nbits -= 8
        table[key] = next_code
        if next_code == (1 << code_size) and code_size < 12:
            code_size += 1
        next_code += 1
        if next_code == 4096:  # table full: tell the decoder to start over
            acc |= clear << nbits
            nbits += code_size
            while nbits >= 8:
                out.append(acc & 255)
                acc >>= 8
                nbits -= 8
            table = {}
            code_size = min_code + 1
            next_code = eoi + 1
        prefix = px
    acc |= prefix << nbits
    nbits += code_size
    acc |= eoi << nbits
    nbits += code_size
    while nbits > 0:
        out.append(acc & 255)
        acc >>= 8
        nbits -= 8
    return bytes(out)


def encode_gif(width, height, palette, frames, delay_cs):
    """
    palette: list of (r, g, b), length a power of two.  frames: list of bytes-like (width*height
    palette indices, row-major).  delay_cs: per-frame delay in 1/100 s.  Loops forever.
    """
    n_colors = len(palette)
    bits = max(1, (n_colors - 1).bit_length())
    if (1 << bits) != n_colors:
        raise ValueError("palette size must be a power of two")
    min_code = max(2, bits)
    out = bytearray(b"GIF89a")
    out += width.to_bytes(2, "little") + height.to_bytes(2, "little")
    out += bytes([0x80 | ((bits - 1) << 4) | (bits - 1), 0, 0])
    for r, g, b in palette:
        out += bytes([r, g, b])
    out += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00"  # loop forever
    for frame in frames:
        out += b"\x21\xF9\x04\x04" + int(delay_cs).to_bytes(2, "little") + b"\x00\x00"
        out += b"\x2C\x00\x00\x00\x00" + width.to_bytes(2, "little") + height.to_bytes(2, "little") + b"\x00"
        out.append(min_code)
        data = _lzw(frame, min_code)
        for i in range(0, len(data), 255):
            chunk = data[i:i + 255]
            out.append(len(chunk))
            out += chunk
        out.append(0)
    out.append(0x3B)
    return bytes(out)


# ---------------------------------------------------------------------------
# Styles + palettes
# ---------------------------------------------------------------------------

# Stops of the cyclic gradient (it wraps around), the dark->bright ramp used for shaded things,
# plus a few fixed colours. ansi_ramp maps donut brightness (12 levels) to ANSI colour numbers.
STYLES = {
    "vaporwave": dict(
        bg=0x120A1F, scan=0x1A0E2C, dim=0x4A2A7A, embed=0xB967FF,
        grad=(0xFF71CE, 0xB967FF, 0x01CDFE, 0x05FFA1, 0x01CDFE, 0xB967FF),
        ramp=(0x4A2A8F, 0x9B55E8, 0xFF71CE, 0xFFE9FB),
        ansi_ramp=(8, 8, 4, 12, 5, 5, 13, 13, 6, 14, 14, 15), accent=14, prompt=10,
    ),
    "matrix": dict(
        bg=0x020A04, scan=0x041208, dim=0x0B4D1F, embed=0x00FF41,
        grad=(0x00FF41, 0x00B82E, 0x7CFF9E, 0x00B82E),
        ramp=(0x0B4D1F, 0x00A82C, 0x00FF41, 0xDFFFE6),
        ansi_ramp=(8, 8, 2, 2, 2, 10, 10, 10, 10, 10, 15, 15), accent=10, prompt=10,
    ),
    "sunset": dict(
        bg=0x1A0A12, scan=0x240E19, dim=0x7A2A3A, embed=0xFF9966,
        grad=(0xFF5E5B, 0xFF9966, 0xFFD166, 0xFF9966, 0xFF6F91),
        ramp=(0x6A1B4D, 0xE0457B, 0xFF9966, 0xFFF3C4),
        ansi_ramp=(8, 8, 1, 1, 9, 9, 3, 3, 11, 11, 11, 15), accent=11, prompt=9,
    ),
    "synthwave": dict(
        bg=0x0B0620, scan=0x120A30, dim=0x3A2A8A, embed=0xF72585,
        grad=(0xF72585, 0x7209B7, 0x4361EE, 0x4CC9F0, 0x4361EE, 0x7209B7),
        ramp=(0x3A0CA3, 0xB5179E, 0xF72585, 0xFFD1EB),
        ansi_ramp=(8, 4, 4, 12, 5, 5, 13, 13, 13, 14, 14, 15), accent=14, prompt=13,
    ),
}
DEFAULT_STYLE = "vaporwave"


def style_names():
    return tuple(STYLES)


_DYNAMIC = OrderedDict()   # per-track styles made from cover art ("auto_xxxxxx"), a small LRU
_DYNAMIC_MAX = 24
_style_lock = threading.Lock()


def _style_def(style):
    return STYLES.get(style) or _DYNAMIC.get(style) or STYLES[DEFAULT_STYLE]


def resolve_style(style):
    return style if (style in STYLES or style in _DYNAMIC) else DEFAULT_STYLE


def style_embed_color(style):
    return _style_def(resolve_style(style))["embed"]


IDX_BG, IDX_SCAN, IDX_DIM, IDX_WHITE = 0, 1, 2, 3
GRAD0, GRAD_N = 4, 24      # cyclic gradient
RAMP0, RAMP_N = 28, 16     # dark -> bright ramp (cover art)


def _rgb(c):
    return ((c >> 16) & 255, (c >> 8) & 255, c & 255)


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


_palettes = {}


def palette_for(style):
    """64-colour table for badges / covers in a style."""
    style = resolve_style(style)
    if style in _palettes:
        return _palettes[style]
    st = _style_def(style)
    pal = [_rgb(st["bg"]), _rgb(st["scan"]), _rgb(st["dim"]), (255, 255, 255)]
    stops = [_rgb(c) for c in st["grad"]]
    for i in range(GRAD_N):
        pos = i / GRAD_N * len(stops)
        k = int(pos)
        pal.append(_lerp(stops[k % len(stops)], stops[(k + 1) % len(stops)], pos - k))
    ramp = [_rgb(c) for c in st["ramp"]]
    for i in range(RAMP_N):
        pos = i / (RAMP_N - 1) * (len(ramp) - 1)
        k = min(int(pos), len(ramp) - 2)
        pal.append(_lerp(ramp[k], ramp[k + 1], pos - k))
    while len(pal) < 64:
        pal.append((0, 0, 0))
    _palettes[style] = pal
    return pal


PALETTE = palette_for(DEFAULT_STYLE)  # kept for callers/tests that want "the" palette

# Classic 16-colour VGA/ANSI table (+ terminal background shades) for the terminal donut
ANSI16 = [
    (0, 0, 0), (170, 0, 0), (0, 170, 0), (170, 85, 0), (0, 0, 170), (170, 0, 170), (0, 170, 170), (170, 170, 170),
    (85, 85, 85), (255, 85, 85), (85, 255, 85), (255, 255, 85), (85, 85, 255), (255, 85, 255), (85, 255, 255),
    (255, 255, 255),
]
TERM_BG, TERM_SCAN = 16, 17
TERM_PALETTE = ANSI16 + [(7, 10, 8), (11, 17, 13)] + [(0, 0, 0)] * 14

# ---------------------------------------------------------------------------
# 5x7 pixel font (rows top->bottom, bit 4 = leftmost column)
# ---------------------------------------------------------------------------

_FONT = {
    "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11), "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E), "D": (0x1E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1E),
    "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F), "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F), "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E), "J": (0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C),
    "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11), "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11), "N": (0x11, 0x11, 0x19, 0x15, 0x13, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E), "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "Q": (0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D), "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E), "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E), "V": (0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0A), "X": (0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11),
    "Y": (0x11, 0x11, 0x11, 0x0A, 0x04, 0x04, 0x04), "Z": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
    "a": (0x00, 0x00, 0x0E, 0x01, 0x0F, 0x11, 0x0F), "b": (0x10, 0x10, 0x16, 0x19, 0x11, 0x11, 0x1E),
    "c": (0x00, 0x00, 0x0E, 0x10, 0x10, 0x11, 0x0E), "d": (0x01, 0x01, 0x0D, 0x13, 0x11, 0x11, 0x0F),
    "e": (0x00, 0x00, 0x0E, 0x11, 0x1F, 0x10, 0x0E), "f": (0x06, 0x09, 0x08, 0x1C, 0x08, 0x08, 0x08),
    "g": (0x00, 0x00, 0x0F, 0x11, 0x11, 0x0F, 0x01, 0x0E),  # 8 rows: the tail drops below the baseline
    "h": (0x10, 0x10, 0x16, 0x19, 0x11, 0x11, 0x11),
    "i": (0x04, 0x00, 0x0C, 0x04, 0x04, 0x04, 0x0E), "j": (0x02, 0x00, 0x06, 0x02, 0x02, 0x12, 0x0C),
    "k": (0x10, 0x10, 0x12, 0x14, 0x18, 0x14, 0x12), "l": (0x0C, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "m": (0x00, 0x00, 0x1A, 0x15, 0x15, 0x11, 0x11), "n": (0x00, 0x00, 0x16, 0x19, 0x11, 0x11, 0x11),
    "o": (0x00, 0x00, 0x0E, 0x11, 0x11, 0x11, 0x0E), "p": (0x00, 0x00, 0x1E, 0x11, 0x1E, 0x10, 0x10),
    "q": (0x00, 0x00, 0x0D, 0x13, 0x0F, 0x01, 0x01), "r": (0x00, 0x00, 0x16, 0x19, 0x10, 0x10, 0x10),
    "s": (0x00, 0x00, 0x0E, 0x10, 0x0E, 0x01, 0x1E), "t": (0x08, 0x08, 0x1C, 0x08, 0x08, 0x09, 0x06),
    "u": (0x00, 0x00, 0x11, 0x11, 0x11, 0x13, 0x0D), "v": (0x00, 0x00, 0x11, 0x11, 0x11, 0x0A, 0x04),
    "w": (0x00, 0x00, 0x11, 0x11, 0x15, 0x15, 0x0A), "x": (0x00, 0x00, 0x11, 0x0A, 0x04, 0x0A, 0x11),
    "y": (0x00, 0x00, 0x11, 0x11, 0x0F, 0x01, 0x0E), "z": (0x00, 0x00, 0x1F, 0x02, 0x04, 0x08, 0x1F),
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E), "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F), "3": (0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02), "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E), "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E), "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    ".": (0, 0, 0, 0, 0, 0x0C, 0x0C), ":": (0, 0x0C, 0x0C, 0, 0x0C, 0x0C, 0),
    "-": (0, 0, 0, 0x1F, 0, 0, 0), "/": (0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10),
    "!": (0x04, 0x04, 0x04, 0x04, 0x04, 0, 0x04), "?": (0x0E, 0x11, 0x01, 0x02, 0x04, 0, 0x04),
    "%": (0x18, 0x19, 0x02, 0x04, 0x08, 0x13, 0x03),
    ",": (0, 0, 0, 0, 0x0C, 0x04, 0x08), ";": (0, 0x0C, 0x0C, 0, 0x0C, 0x04, 0x08),
    "=": (0, 0, 0x1F, 0, 0x1F, 0, 0), "~": (0, 0, 0x08, 0x15, 0x02, 0, 0),
    "*": (0, 0x04, 0x15, 0x0E, 0x15, 0x04, 0), "#": (0x0A, 0x0A, 0x1F, 0x0A, 0x1F, 0x0A, 0x0A),
    "$": (0x04, 0x0F, 0x14, 0x0E, 0x05, 0x1E, 0x04), "@": (0x0E, 0x11, 0x17, 0x15, 0x17, 0x10, 0x0E),
    "_": (0, 0, 0, 0, 0, 0, 0x1F), ">": (0x10, 0x08, 0x04, 0x02, 0x04, 0x08, 0x10),
    "<": (0x01, 0x02, 0x04, 0x08, 0x04, 0x02, 0x01), "+": (0, 0x04, 0x04, 0x1F, 0x04, 0x04, 0),
    "(": (0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02), ")": (0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08),
    "[": (0x0E, 0x08, 0x08, 0x08, 0x08, 0x08, 0x0E), "]": (0x0E, 0x02, 0x02, 0x02, 0x02, 0x02, 0x0E),
    "|": (0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04), "\\": (0x10, 0x10, 0x08, 0x04, 0x02, 0x01, 0x01),
    "'": (0x04, 0x04, 0x08, 0, 0, 0, 0), "\u2588": (0x1F,) * 7,
}


def _text_pixels(text, keep_case=False):
    """-> list per character of the lit (col, row) cells in the 5x7 grid."""
    cells = []
    for ch in (text if keep_case else text.upper()):
        rows = _FONT.get(ch)
        lit = []
        if rows:
            for r, bits in enumerate(rows):
                for c in range(5):
                    if bits & (1 << (4 - c)):
                        lit.append((c, r))
        cells.append(lit)
    return cells


class _Canvas:
    def __init__(self, w, h, fill=IDX_BG):
        self.w, self.h = w, h
        self.px = bytearray([fill]) * (w * h)

    def rect(self, x0, y0, x1, y1, color):
        """Fill [x0, x1) x [y0, y1), clipped."""
        x0, x1 = max(0, x0), min(self.w, x1)
        if x1 <= x0:
            return
        run = bytes([color]) * (x1 - x0)
        for y in range(max(0, y0), min(self.h, y1)):
            o = y * self.w
            self.px[o + x0:o + x1] = run

    def scanlines(self, color=IDX_SCAN):
        for y in range(1, self.h, 2):
            self.rect(0, y, self.w, y + 1, color)

    def marquee(self, shift):
        """Moving gradient line along the top and bottom edges."""
        for x in range(self.w):
            c = GRAD0 + (x // 3 + shift) % GRAD_N
            self.px[x] = c
            self.px[(self.h - 1) * self.w + x] = c

    def glyph(self, lit, x, y, scale, color):
        for c, r in lit:
            self.rect(x + c * scale, y + r * scale, x + (c + 1) * scale, y + (r + 1) * scale, color)

    def text(self, text, x, y, scale, color, keep_case=True, advance=None):
        adv = advance or 6 * scale
        for i, lit in enumerate(_text_pixels(text, keep_case)):
            self.glyph(lit, x + i * adv, y, scale, color)


# ---------------------------------------------------------------------------
# Cover art (pixel-art version of the video thumbnail)
# ---------------------------------------------------------------------------

COVER_W, COVER_H, COVER_SCALE = 48, 27, 2   # grid -> drawn at 96x54
_BAYER = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))


class Cover:
    """A small rgb image (default 48x27) to be turned into pixel art."""

    def __init__(self, rgb, w, h, key):
        self.rgb, self.w, self.h, self.key = rgb, w, h, key
        self._levels = None

    def levels(self):
        """w*h dithered brightness levels 0..RAMP_N-1 (auto-contrast + 4x4 Bayer dither)."""
        if self._levels is None:
            lum = [(self.rgb[i] * 299 + self.rgb[i + 1] * 587 + self.rgb[i + 2] * 114) / 1000
                   for i in range(0, self.w * self.h * 3, 3)]
            srt = sorted(lum)
            lo = srt[int(len(srt) * 0.03)]
            hi = srt[min(len(srt) - 1, int(len(srt) * 0.97))]
            span = max(hi - lo, 24.0)
            out = []
            for i, v in enumerate(lum):
                x, y = i % self.w, i // self.w
                n = min(1.0, max(0.0, (v - lo) / span)) ** 0.9
                lv = int(n * (RAMP_N - 1) + _BAYER[y % 4][x % 4] / 16.0)
                out.append(min(RAMP_N - 1, max(0, lv)))
            self._levels = out
        return self._levels


def cover_from_rgb(data, w=COVER_W, h=COVER_H, key=None):
    """Raw rgb24 bytes -> Cover, or None if the data isn't a usable image."""
    try:
        if not data or len(data) < w * h * 3:
            return None
        return Cover(bytes(data[:w * h * 3]), w, h, key or hashlib.md5(data).hexdigest()[:10])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Adaptive colours: a style made from the cover's dominant colours ("!style auto")
# ---------------------------------------------------------------------------

def _from_hsv(h, s_, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s_)), max(0.0, min(1.0, v)))
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


def _dominant_colors(rgb, w, h, n=3):
    """Up to n distinct, vivid-ish (hue, sat) colours of an image, most prominent first."""
    buckets = {}
    for i in range(0, w * h * 3, 3):
        r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
        hh, ss, vv = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if vv < 0.18 or ss < 0.12:
            continue                      # near-black and grey pixels carry no colour
        weight = ss * ss * vv + 0.02
        acc = buckets.setdefault(int(hh * 18) % 18, [0.0, 0.0, 0.0, 0.0])
        acc[0] += r * weight
        acc[1] += g * weight
        acc[2] += b * weight
        acc[3] += weight
    ranked = sorted(buckets.values(), key=lambda a: -a[3])
    picked = []
    for acc in ranked:
        col = colorsys.rgb_to_hsv(acc[0] / acc[3] / 255, acc[1] / acc[3] / 255, acc[2] / acc[3] / 255)
        if all(min(abs(col[0] - o[0]), 1 - abs(col[0] - o[0])) >= 0.09 for o in picked):
            picked.append(col)
        if len(picked) == n:
            break
    return picked


def _make_adaptive(cover):
    cols = _dominant_colors(cover.rgb, cover.w, cover.h)
    if not cols:
        return None                       # greyscale cover: keep the default look
    while len(cols) < 3:                  # a one/two-colour cover: invent neighbours by shifting the hue
        hh, ss, vv = cols[-1]
        cols.append(((hh + 0.14) % 1.0, ss, vv))
    (h1, s1, _), (h2, s2, _), (h3, s3, _) = cols
    v1, v2, v3 = (_from_hsv(h, max(sv, 0.55), 1.0) for h, sv in ((h1, s1), (h2, s2), (h3, s3)))
    st = dict(STYLES[DEFAULT_STYLE])
    st.update(
        bg=_from_hsv(h1, 0.65, 0.09), scan=_from_hsv(h1, 0.65, 0.14), dim=_from_hsv(h1, 0.6, 0.45),
        grad=(v1, v2, v3, v2),
        ramp=(_from_hsv(h1, 0.85, 0.40), v1, _from_hsv(h2, 0.55, 1.0), _from_hsv(h2, 0.12, 1.0)),
        embed=v1,
    )
    return st


def adaptive_style(cover):
    """Name of a (cached) style coloured like `cover`; the default style if there's no usable colour."""
    if cover is None:
        return DEFAULT_STYLE
    name = "auto_" + hashlib.md5(str(cover.key).encode()).hexdigest()[:8]
    with _style_lock:
        if name in _DYNAMIC:
            _DYNAMIC.move_to_end(name)
            return name
    st = _make_adaptive(cover)
    if st is None:
        return DEFAULT_STYLE
    with _style_lock:
        _DYNAMIC[name] = st
        while len(_DYNAMIC) > _DYNAMIC_MAX:
            old, _ = _DYNAMIC.popitem(last=False)
            _palettes.pop(old, None)
    return name


def effective_style(pref, cover=None):
    """Style actually drawn: a preset name, or (pref == "auto") the one derived from the cover."""
    if pref == "auto":
        return adaptive_style(cover)
    return resolve_style(pref)


# ---------------------------------------------------------------------------
# Status badge (optionally with a cover on the left)
# ---------------------------------------------------------------------------

BADGE_FRAMES = 16
BADGE_DELAY = 7  # centiseconds
LABELS = (
    "searching", "reading playlist", "queued", "extracting", "downloading", "finishing",
    "buffering", "now playing", "paused", "played", "stopped", "importing", "buffer ready", "error",
    "skipped", "resumed", "shuffled", "volume", "seeking", "playlist", "added to queue", "style",
    "sleep timer", "effect", "voted", "liked", "stats", "autoplay", "guess", "winner", "history",
    "lyrics", "queue", "fade", "dashboard",
)


def _render_badge(label, style=DEFAULT_STYLE, cover=None):
    style = resolve_style(style)
    text = label.upper()
    cells = _text_pixels(text)
    scale = 3
    adv = 6 * scale
    cov_w, cov_h = COVER_W * COVER_SCALE, COVER_H * COVER_SCALE
    height = 62 if cover else 40
    ox = (6 + cov_w + 8) if cover else 0           # everything else shifts right of the cover
    bar_x0 = ox + 10
    text_x = bar_x0 + 5 * 6 + 12
    width = text_x + len(text) * adv - scale + 14
    text_y = (height - 7 * scale) // 2
    base = (height + 24) // 2                        # equalizer baseline
    rng = random.Random(label)
    phases = [rng.random() * 6.2832 for _ in range(5)]
    glitches = {5: (3, 13, 3), 11: (9, 21, -3), 12: (5, 11, 2)}  # frame -> (band rows from text top, dx)
    levels = cover.levels() if cover else None
    frames = []
    for f in range(BADGE_FRAMES):
        cv = _Canvas(width, height)
        cv.scanlines()
        shift = f * GRAD_N // BADGE_FRAMES
        cv.marquee(shift)
        cv.rect(0, 1, 1, height - 1, IDX_DIM)
        cv.rect(width - 1, 1, width, height - 1, IDX_DIM)

        if cover:
            top = (height - cov_h) // 2
            band_c = int(f / BADGE_FRAMES * (cov_h + 16)) - 8     # shimmer band sweeping down
            glitch = glitches.get(f)
            for y in range(cov_h):
                gy = y // COVER_SCALE
                dx = (6 if (f in (5, 11) and 18 <= y < 30) else 0)
                row = bytearray(cov_w)
                for x in range(COVER_W):
                    lv = levels[gy * COVER_W + x]
                    if abs(y - band_c) < 6:
                        lv = min(RAMP_N - 1, lv + 4)
                    c = RAMP0 + lv
                    row[x * 2] = c
                    row[x * 2 + 1] = c
                if dx:
                    row = row[-dx:] + row[:-dx]
                o = (top + y) * width + 6
                cv.px[o:o + cov_w] = row
            cv.rect(5, top - 1, 6 + cov_w + 1, top, IDX_DIM)
            cv.rect(5, top + cov_h, 6 + cov_w + 1, top + cov_h + 1, IDX_DIM)
            cv.rect(5, top, 6, top + cov_h, IDX_DIM)
            cv.rect(6 + cov_w, top, 6 + cov_w + 1, top + cov_h, IDX_DIM)

        # little equalizer: loops seamlessly (whole number of cycles per loop)
        for i in range(5):
            cycles = 1 + (i % 2)
            h = 4 + int((math.sin(f / BADGE_FRAMES * 6.2832 * cycles + phases[i]) * 0.5 + 0.5) * 20)
            x = bar_x0 + i * 6
            cv.rect(x, base - h, x + 4, base, GRAD0 + (shift + i * 4) % GRAD_N)

        band = glitches.get(f)
        sweep = (f * (len(text) + 3)) // BADGE_FRAMES  # which letter is "shining" this frame
        for ghost in (True, False):
            if ghost and not band:
                continue
            for li, lit in enumerate(cells):
                color = GRAD0 + (li * 2 + shift + (12 if ghost else 0)) % GRAD_N
                if not ghost and li == sweep:
                    color = IDX_WHITE
                tx = text_x + li * adv + (-2 if ghost else 0)
                for c, r in lit:
                    y = text_y + r * scale
                    dx = band[2] if (band and not ghost and band[0] <= r * scale < band[1]) else 0
                    x = tx + c * scale + dx
                    cv.rect(x, y, x + scale, y + scale, color)
        frames.append(bytes(cv.px))
    return encode_gif(width, height, palette_for(style), frames, BADGE_DELAY)


# ---------------------------------------------------------------------------
# Summon donut: donut.c running in an ANSI terminal window
# ---------------------------------------------------------------------------

DONUT_COLS, DONUT_ROWS = 32, 16
DONUT_CELL_W, DONUT_CELL_H, DONUT_SCALE = 12, 16, 2   # 5x7 glyphs drawn 2x in 12x16 character cells
DONUT_FRAMES = 24
DONUT_DELAY = 11           # centiseconds per frame -> one cycle = 2.64 s (was 6 = 1.44 s)
DONUT_A_TURNS = 1
DONUT_B_TURNS = 1
CHROME_H = 20
_LUMINANCE = ".,-~:;=!*#$@"   # same shading characters as the classic donut.c


def _render_donut(style=DEFAULT_STYLE):
    import donut  # local import: only needed when generating

    st = STYLES[style if style in STYLES else DEFAULT_STYLE]  # the terminal donut only uses the preset styles
    ramp = st["ansi_ramp"]
    cols, rows, cw, ch, sc = DONUT_COLS, DONUT_ROWS, DONUT_CELL_W, DONUT_CELL_H, DONUT_SCALE
    width = cols * cw
    body_rows = rows + 2                                   # prompt row + donut + status row
    height = CHROME_H + body_rows * ch
    y_scale = rows / 2 * 1.08
    x_scale = y_scale * ch / cw                            # character cells are taller than wide
    glyphs = [_text_pixels(c, True)[0] for c in _LUMINANCE]
    gx = (cw - 5 * sc) // 2
    gy = (ch - 7 * sc) // 2
    spinner = "|/-\\"
    frames = []
    for f in range(DONUT_FRAMES):
        a = f / DONUT_FRAMES * 2 * math.pi * DONUT_A_TURNS
        b = f / DONUT_FRAMES * 2 * math.pi * DONUT_B_TURNS
        levels = donut.render_levels(a, b, cols, rows, x_scale, y_scale)
        cv = _Canvas(width, height, TERM_BG)
        cv.scanlines(TERM_SCAN)
        # window chrome: traffic-light dots + title
        cv.rect(0, 0, width, CHROME_H, 8)
        for i, colr in enumerate((9, 11, 10)):
            cx, cy = 14 + i * 16, CHROME_H // 2
            cv.rect(cx - 3, cy - 2, cx + 4, cy + 3, colr)
            cv.rect(cx - 2, cy - 3, cx + 3, cy + 4, colr)
        title = "gondor@discord: ~"
        cv.text(title, (width - len(title) * 6) // 2, (CHROME_H - 7) // 2, 1, 7)
        top = CHROME_H

        def put(col, row, text, color):
            for i, lit in enumerate(_text_pixels(text, True)):
                cv.glyph(lit, (col + i) * cw + gx, top + row * ch + gy, sc, color)

        # prompt line:  gondor@discord:~$ ./donut
        put(0, 0, "gondor@discord", st["prompt"])
        put(14, 0, ":", 15)
        put(15, 0, "~", 12)
        put(16, 0, "$ ./donut", 15)
        # the donut: one ANSI-coloured character per cell
        for i, lv in enumerate(levels):
            if lv < 0:
                continue
            col, row = i % cols, i // cols
            cv.glyph(glyphs[lv], col * cw + gx, top + (row + 1) * ch + gy, sc, ramp[lv])
        # status line: spinner, text with cycling dots, blinking cursor
        dots = "." * (1 + (f // 4) % 3)
        line = f"{spinner[(f // 3) % 4]} summoning gondor{dots}"
        put(0, rows + 1, line[0], st["accent"])
        put(2, rows + 1, line[2:], 7)
        if (f // 6) % 2 == 0:
            put(2 + len(line) - 2 + 1, rows + 1, "\u2588", 15)
        frames.append(bytes(cv.px))
    return encode_gif(width, height, TERM_PALETTE, frames, DONUT_DELAY)


# ---------------------------------------------------------------------------
# Terminal scenes: shutdown (!kill), crash (a failed request), rain (standing by)
# ---------------------------------------------------------------------------

def _term_scene(style, rows, nframes, delay, draw, title="gondor@discord: ~"):
    """Render an ANSI terminal window; draw(frame, put, cv, style_dict) paints each frame's text."""
    st = STYLES[style if style in STYLES else DEFAULT_STYLE]
    cols, cw, ch, sc = DONUT_COLS, DONUT_CELL_W, DONUT_CELL_H, DONUT_SCALE
    width, height = cols * cw, CHROME_H + rows * ch
    gx, gy = (cw - 5 * sc) // 2, (ch - 7 * sc) // 2
    frames = []
    for f in range(nframes):
        cv = _Canvas(width, height, TERM_BG)
        cv.scanlines(TERM_SCAN)
        cv.rect(0, 0, width, CHROME_H, 8)
        for i, colr in enumerate((9, 11, 10)):
            cx, cy = 14 + i * 16, CHROME_H // 2
            cv.rect(cx - 3, cy - 2, cx + 4, cy + 3, colr)
            cv.rect(cx - 2, cy - 3, cx + 3, cy + 4, colr)
        cv.text(title, (width - len(title) * 6) // 2, (CHROME_H - 7) // 2, 1, 7)

        def put(col, row, text, color, _cv=cv):
            for i, lit in enumerate(_text_pixels(text, True)):
                _cv.glyph(lit, (col + i) * cw + gx, CHROME_H + row * ch + gy, sc, color)

        draw(f, put, cv, st)
        frames.append(bytes(cv.px))
    return encode_gif(width, height, TERM_PALETTE, frames, delay)


def _segments(put, row, segments, visible=None, col=0):
    """Draw [(text, colour), ...] on a row, only the first `visible` characters (typewriter effect)."""
    left = 10 ** 6 if visible is None else visible
    for text, colour in segments:
        if left <= 0:
            break
        piece = text[:left]
        put(col, row, piece, colour)
        col += len(piece)
        left -= len(piece)
    return col


def _prompt(st, command):
    return [("gondor@discord", st["prompt"]), (":", 15), ("~", 12), ("$ " + command, 15)]


def _render_shutdown(style=DEFAULT_STYLE):
    steps = [  # (first frame, characters per frame, segments)
        (0, 2, None),
        (14, 99, [("[ ", 7), ("ok", 10), (" ] stopping playback", 7)]),
        (18, 99, [("[ ", 7), ("ok", 10), (" ] clearing queue", 7)]),
        (22, 99, [("[ ", 7), ("ok", 10), (" ] leaving voice channel", 7)]),
        (28, 1, "goodbye."),
    ]

    def draw(f, put, cv, st):
        last_row, last_col = 0, 0
        for row, (start, speed, seg) in enumerate(steps):
            if f < start:
                break
            if seg is None:
                seg = _prompt(st, "shutdown")
            elif isinstance(seg, str):
                seg = [(seg, st["accent"])]
            col = _segments(put, row, seg, (f - start + 1) * speed)
            last_row, last_col = row, col
        if (f // 3) % 2 == 0:
            put(last_col + 1, last_row, "\u2588", 15)

    return _term_scene(style, 8, 44, 8, draw)


def _ascii_line(text, width=30, lines=3):
    """Fold arbitrary text into the pixel font's safe ASCII subset and wrap it."""
    import textwrap
    safe = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in " .:/_-,!?()'")) else " " for ch in str(text))
    out = textwrap.wrap(" ".join(safe.split()) or "unknown error", width=width)[:lines]
    return out or ["unknown error"]


def _render_crash(style=DEFAULT_STYLE, text="something went wrong"):
    msg = _ascii_line(text)
    rng = random.Random(hash(text) & 0xFFFF)
    noise = "#@%*$=+<>/|"
    plan = [(rng.random() < 0.45, rng.randrange(1, 7), rng.randrange(0, 20), rng.randrange(6, 14)) for _ in range(16)]

    def draw(f, put, cv, st):
        glitch, row, col, n = plan[f]
        put(0, 0, "gondor@discord", st["prompt"])
        put(14, 0, ":", 15)
        put(15, 0, "~", 12)
        put(16, 0, "$ ./play", 15)
        put(0, 1, "fatal error", 9 if f % 4 < 3 else 15)
        for i, line in enumerate(msg):
            put(0, 2 + i, line, 11)
        put(0, 2 + len(msg), "core dumped", 8)
        put(0, 3 + len(msg), "try again with !play", 7)
        if glitch:
            put(col, row, "".join(rng.choice(noise) for _ in range(n)), 9 if f % 2 else 12)

    return _term_scene(style, 8, 16, 7, draw, title="gondor@discord: ~ [crashed]")


def _render_rain(style=DEFAULT_STYLE):
    rows, cols = 12, DONUT_COLS
    trail, cycle = 8, 12 + 8 + 4
    rng0 = random.Random(7)
    drops = [(rng0.randrange(cycle), rng0.randrange(5, trail + 1)) for _ in range(cols)]
    glyph_chars = "0123456789ABCDEFXYZ$#@%=+<>/"

    def draw(f, put, cv, st):
        rng = random.Random(f * 977)
        ramp = st["ansi_ramp"]
        shades = (15, ramp[11], ramp[9], ramp[9], ramp[6], ramp[6], ramp[3], 8, 8)
        for c, (off, tlen) in enumerate(drops):
            head = (f + off) % cycle
            for k in range(tlen):
                r = head - k
                if 0 <= r < rows:
                    put(c, r, rng.choice(glyph_chars), shades[min(k, len(shades) - 1)])
        put(0, rows, "standing by", st["accent"])
        if (f // 4) % 2 == 0:
            put(12, rows, "\u2588", 15)

    return _term_scene(style, rows + 1, cycle, 9, draw, title="gondor@discord: ~ [idle]")


_TERM_KINDS = {"shutdown": _render_shutdown, "crash": _render_crash, "rain": _render_rain}


def terminal_bytes(kind, style=None, text=None):
    """GIF of a terminal scene. 'crash' takes the (short) error text and is kept in the small LRU."""
    style = style if style in STYLES else DEFAULT_STYLE
    if kind == "crash":
        key = ("term", kind, style, text)
        with _lock:
            if key in _header_lru:
                _header_lru.move_to_end(key)
                return _header_lru[key]
        data = _render_crash(style, text or "something went wrong")
        with _lock:
            _header_lru[key] = data
            while len(_header_lru) > HEADER_LRU_SIZE:
                _header_lru.popitem(last=False)
        return data
    return _cached(f"term_{kind}__{style}", lambda: _TERM_KINDS[kind](style))


# ---------------------------------------------------------------------------
# Caching: memory + ./gif_cache (the file name carries a hash of the code, so edits regenerate)
# ---------------------------------------------------------------------------

_CACHE_DIR = Path(__file__).resolve().parent / "gif_cache"
_mem = {}
_lock = threading.Lock()
_header_lru = OrderedDict()   # (label, style, cover key) -> bytes (per-track headers: memory only)
HEADER_LRU_SIZE = 48


def _code_hash():
    h = hashlib.md5()
    for name in ("gif_art.py", "donut.py"):
        try:
            h.update((Path(__file__).resolve().parent / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:8]


_HASH = _code_hash()


def _cached(name, build):
    with _lock:
        if name in _mem:
            return _mem[name]
        path = _CACHE_DIR / f"{name}-{_HASH}.gif"
        try:
            data = path.read_bytes()
            if data[:6] == b"GIF89a" and data[-1:] == b"\x3B":
                _mem[name] = data
                return data
        except OSError:
            pass
        data = build()
        _mem[name] = data
        try:
            _CACHE_DIR.mkdir(exist_ok=True)
            for old in _CACHE_DIR.glob(f"{name}-*.gif"):
                try:
                    old.unlink()
                except OSError:
                    pass
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError:
            pass  # cache is only an optimisation
        return data


def slug(label):
    return "".join(ch if ch.isalnum() else "_" for ch in label.lower()).strip("_") or "badge"


def badge_bytes(label, style=None):
    style = resolve_style(style)
    return _cached(f"badge_{slug(label)}__{style}", lambda: _render_badge(label, style))


def header_bytes(label, style=None, cover=None):
    """Badge GIF; with a Cover it gets the pixel-art cover on its left (memory-cached per track)."""
    style = resolve_style(style)
    if cover is None:
        return badge_bytes(label, style)
    key = (slug(label), style, cover.key)
    with _lock:
        if key in _header_lru:
            _header_lru.move_to_end(key)
            return _header_lru[key]
    data = _render_badge(label, style, cover)
    with _lock:
        _header_lru[key] = data
        while len(_header_lru) > HEADER_LRU_SIZE:
            _header_lru.popitem(last=False)
    return data


def donut_bytes(style=None):
    style = resolve_style(style)
    return _cached(f"donut__{style}", lambda: _render_donut(style))


def prewarm(style=DEFAULT_STYLE):
    """Generate one style's full set (default: the default style); safe to call repeatedly / from a thread."""
    style = resolve_style(style)
    for label in LABELS:
        badge_bytes(label, style)
    donut_bytes(style)
    for kind in ("shutdown", "rain"):
        terminal_bytes(kind, style)

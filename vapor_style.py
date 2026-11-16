"""
Vaporwave x breakcore text helpers for Gondor's Discord status messages.

Only *labels* are styled. Song titles and real error details are always passed
through untouched so they stay readable / reportable.

Set GONDOR_VAPOR=0 in .env to turn all styling off (plain text fallbacks).
"""

import os
import random
import textwrap
import unicodedata

ENABLED = os.environ.get("GONDOR_VAPOR", "1").strip().lower() not in ("0", "false", "no", "off")

# Animated GIF badges replace the text header line of status messages (see gif_art.py).
# GONDOR_GIFS=0 turns them off (the text header is used instead).
CRASH_GIF = os.environ.get("GONDOR_CRASH_GIF", "1").strip().lower() not in ("0", "false", "no", "off")
BADGES = ENABLED and os.environ.get("GONDOR_GIFS", "1").strip().lower() not in ("0", "false", "no", "off")

# Classic vaporwave pastels (embed colours)
PALETTE = [0xFF71CE, 0x01CDFE, 0x05FFA1, 0xB967FF, 0xFFFB96]

NOISE_CHARS = "░▒▓"

_rng = random.Random()


def vw(text: str) -> str:
    """Fullwidth (vaporwave) conversion: ASCII 0x21-0x7E -> U+FF01.., space -> U+3000."""
    if not ENABLED:
        return text
    out = []
    for ch in str(text):
        code = ord(ch)
        if ch == " ":
            out.append("\u3000")
        elif 0x21 <= code <= 0x7E:
            out.append(chr(code + 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def noise(n: int = 3, rng: random.Random | None = None) -> str:
    r = rng or _rng
    return "".join(r.choice(NOISE_CHARS) for _ in range(n))


def glitch(text: str, intensity: float = 0.12, rng: random.Random | None = None) -> str:
    """
    Breakcore corruption using ONLY characters that render reliably in Discord on every
    platform (no combining marks - those show up offset / as boxes depending on the font):
      - at most one letter swapped for a ▒/░ noise block
      - occasional fullwidth stutter of the first letter ("ｓ－ｓ－ｓｅａｒｃｈ")
    """
    if not ENABLED:
        return text
    r = rng or _rng
    chars = list(text)
    letters = [i for i, ch in enumerate(chars) if ch.isalpha()]
    if len(letters) > 3 and r.random() < min(0.6, intensity * 2.5):
        # never the first letter, so the word stays recognisable
        chars[r.choice(letters[1:])] = r.choice("▒░")
    result = "".join(chars)
    if text and text[0].isalpha() and r.random() < intensity * 1.5:
        result = f"{text[0]}－{text[0]}－{result}"
    return result


def label(text: str, intensity: float = 0.1, rng: random.Random | None = None) -> str:
    """Styled lowercase label: fullwidth + light glitch."""
    if not ENABLED:
        return text
    return glitch(vw(text.lower()), intensity, rng)


def title(text: str) -> str:
    """Embed title (label only - never pass a song title through this)."""
    if not ENABLED:
        return text
    return f"♪ {vw(text.lower())}"


def color(default: int = 0x9C27B0) -> int:
    if not ENABLED:
        return default
    return _rng.choice(PALETTE)


def pct_text(pct: float) -> str:
    return vw(f"{int(round(pct))}%")


def bar(pct: float | None, width: int = 10, rng: random.Random | None = None) -> str:
    """
    Progress bar: solid ▓ for the filled part, flickering ░▒ noise for the rest.
    pct=None -> fully indeterminate noise (used while extracting).
    Re-rolling the noise on every edit makes the bar 'animate' for free.
    """
    r = rng or _rng
    if not ENABLED:
        if pct is None:
            return "[" + "." * width + "]"
        filled = max(0, min(width, int(round(pct / 100 * width))))
        return "[" + "#" * filled + "-" * (width - filled) + "]"
    if pct is None:
        return "".join(r.choice("░░▒▓") for _ in range(width))
    filled = max(0, min(width, int(round(pct / 100 * width))))
    rest = "".join(r.choice("░░░░▒") for _ in range(width - filled))
    return "▓" * filled + rest


def _fmt_speed(bps: float | None) -> str:
    if not bps:
        return ""
    return f"{bps / 1_000_000:.1f} MB/s"


def _fmt_eta(eta: float | None) -> str:
    if eta is None:
        return ""
    eta = int(eta)
    return f"{eta}s left"


# ---------------------------------------------------------------------------
# Frames + equalizer bar (code-block based: needs a monospace font to line up)
#
# The frame is deliberately OPEN on the right: song titles / errors have unpredictable
# display widths (CJK, emoji), which would push a closed right border out of line.
# ---------------------------------------------------------------------------

FRAME_WIDTH = 30
WRAP_WIDTH = 40
_EQ_LEVELS = "▁▂▃▄▅▆▇█"


def spaced(text: str) -> str:
    """ASCII letter-spacing (aligns in monospace, unlike fullwidth): 'now playing' -> 'n o w   p l a y i n g'."""
    if not ENABLED:
        return text
    return "   ".join(" ".join(word) for word in text.split())


def _clean(text) -> str:
    """Keep arbitrary text (titles, errors) from breaking the code fence."""
    out = _tame_marks(str(text)).replace("`", "'")
    return out.replace("\r", " ").replace("\n", " ").strip()


def _tame_marks(text: str, keep: int = 1) -> str:
    """Zalgo / stacked combining marks make a line several rows tall and smear its neighbours:
    keep at most `keep` combining mark per letter (real accents survive, glitch text gets cleaned)."""
    text = unicodedata.normalize("NFC", text)
    out, run = [], 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Me"):
            run += 1
            if run > keep:
                continue
        else:
            run = 0
        out.append(ch)
    return "".join(out)


def _wrap(text, width: int = WRAP_WIDTH, max_lines: int = 3) -> list:
    lines = textwrap.wrap(_clean(text), width=width, break_long_words=True) or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(0, width - 1)] + "…"
    return lines


def frame(lines) -> str:
    """Wrap lines in an open-right box-drawing frame inside a code block."""
    if not ENABLED:
        return "\n".join(str(line) for line in lines)
    rule = "═" * FRAME_WIDTH
    body = "\n".join(f"║ {line}" for line in lines)
    fence = "`" * 3
    return f"{fence}\n╔{rule}\n{body}\n╚{rule}\n{fence}"


def _level_char(level) -> str:
    """0..1 -> one of the 8 block heights (never the lowest, so a filled bar always looks 'on')."""
    level = 0.0 if level is None else max(0.0, min(1.0, float(level)))
    return _EQ_LEVELS[1 + int(round(level * (len(_EQ_LEVELS) - 2)))]


def eq_bar(pct, width: int = 12, rng: random.Random | None = None, idle: bool = True, levels=None) -> str:
    """
    Equalizer progress bar, re-rolled on every message edit so it animates for free:
      filled part   -> tall bars of random height
      leading edge  -> one mid-height bar (so even 0% is never an empty-looking bar)
      unfilled part -> low, flickering bars (idle=False makes it a flat baseline, for paused/finished)
    pct=None -> whole bar is live (indeterminate, e.g. while extracting).
    levels: optional list of 0..1 values (a real audio spectrum); the filled part then shows those heights
    (resampled to fit) instead of random ones.
    """
    r = rng or _rng
    if not ENABLED:
        if pct is None:
            return "[" + "." * width + "]"
        filled = max(0, min(width, int(round(pct / 100 * width))))
        return "[" + "#" * filled + "-" * (width - filled) + "]"
    if pct is None:
        return "".join(r.choice(_EQ_LEVELS[1:]) for _ in range(width))
    filled = max(0, min(width, int(round(pct / 100 * width))))
    if levels and filled:
        live = "".join(_level_char(levels[min(len(levels) - 1, int(i * len(levels) / filled))]) for i in range(filled))
    else:
        live = "".join(r.choice(_EQ_LEVELS[3:]) for _ in range(filled))
    rest = width - filled
    if not idle or rest <= 0:
        return live + _EQ_LEVELS[0] * rest
    edge = r.choice(_EQ_LEVELS[2:5])
    flicker = "".join(r.choice(_EQ_LEVELS[:3]) for _ in range(rest - 1))
    return live + edge + flicker


def _head(text: str) -> str:
    return f"{noise(2)} {spaced(text)}" if ENABLED else text


def _extra(note, skipped) -> str:
    return " · ".join(x for x in (note, f"skipped {skipped}" if skipped else "") if x)


# ---------------------------------------------------------------------------
# Status messages
# ---------------------------------------------------------------------------

class StatusText(str):
    """
    A status message body. With badges on, the text has NO header line: `badge` names the animated
    GIF label (gif_art.LABELS) that goes with it, and `fallback` is the same message with a text
    header, used if the GIF can't be attached. Without badges, `badge` is None and the text is complete.
    Being a str subclass, it still works anywhere a plain string does.
    """
    badge = None
    fallback = None
    scene = None  # (kind, text): draw a terminal scene GIF (gif_art.terminal_bytes) instead of the badge


def _status(label: str, lines, head: str | None = None, footer: str | None = None) -> StatusText:
    """footer: plain markdown placed under the frame (outside the code fence, so <t:..> timestamps render)."""
    tail = f"\n{footer}" if footer else ""
    if BADGES:
        text = StatusText(frame(lines) + tail)
        text.badge = label
        text.fallback = frame([head or _head(label), *lines]) + tail
        return text
    text = StatusText(frame([head or _head(label), *lines]) + tail)
    text.fallback = str(text)
    return text


def searching() -> str:
    return _status("searching", [eq_bar(None)])


def reading_playlist() -> str:
    return _status("reading playlist", [eq_bar(None)])


def queued(ahead: int = 0) -> str:
    lines = [eq_bar(None)]
    if ahead:
        lines.append(f"{ahead} ahead")
    return _status("queued", lines)


def extracting() -> str:
    return _status("extracting", [eq_bar(None)])


def downloading(pct: float | None, speed: float | None = None, eta: float | None = None,
                downloaded: float | None = None) -> str:
    # No real progress yet (0% / unknown): whole bar bounces, like "extracting", instead of looking empty.
    bar = eq_bar(pct if (pct is not None and pct >= 1) else None)
    if pct is not None:
        bar += f"  {int(round(pct))}%"
    elif downloaded:
        bar += f"  {downloaded / 1_000_000:.1f} MB"
    lines = [bar]
    info = " · ".join(x for x in (_fmt_speed(speed), _fmt_eta(eta)) if x)
    if info:
        lines.append(info)
    return _status("downloading", lines)


def finishing() -> str:
    return _status("finishing", [eq_bar(100)])


def buffering(song_title: str) -> str:
    return _status("buffering", [*_wrap(song_title, max_lines=2), eq_bar(None)])


def now_playing(song_title: str, duration_str: str = "", cached: bool = False) -> str:
    tail = " · ".join(x for x in (duration_str.strip(" •"), "cached" if cached else "") if x)
    lines = [*_wrap(song_title), eq_bar(100) + (f"  {tail}" if tail else "")]
    return _status("now playing", lines)


def _fmt_clock(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


NP_BAR_WIDTH = 14


def now_playing_live(song_title: str, elapsed: float, duration: float = 0, cached: bool = False,
                     paused: bool = False, ended: bool = False, levels=None, ends_at: int | None = None,
                     hint: str | None = None, lyric=None, meta: str | None = None) -> str:
    """
    Now-playing frame with a real progress bar. The bar's filled part is the share of the track
    already played (equalizer bars of random height, re-rolled on every edit so it looks alive);
    the rest is a flat baseline. Unknown duration -> whole bar is live and only elapsed time shows.
    ended=True is the final, static state ("played" if it ran to the end, otherwise "stopped").
    levels: real audio spectrum (0..1 per band) to draw in the filled part instead of random heights.
    ends_at: unix time the track will end; adds a live "ends in ..." line (Discord renders <t:..:R> itself).
    lyric: None (off) or the current lyric line ("" = instrumental gap): shown as two fixed rows under the bar.
    meta: small plain line under the frame, e.g. who requested the track and the active effect.
    """
    if duration and duration > 0:
        elapsed = min(elapsed, duration)
        pct = elapsed / duration * 100
        clock = f"{_fmt_clock(elapsed)} / {_fmt_clock(duration)}"
    else:
        pct = None
        clock = _fmt_clock(elapsed)
    if ended:
        finished = bool(duration) and elapsed >= duration - 4  # metadata durations are rounded; natural ends land within ~1s
        head = "played" if finished else "stopped"
        if finished:
            pct = 100
            clock = _fmt_clock(duration)
        elif pct is None:
            pct = 0  # unknown length: don't leave an "indeterminate" bar on a finished track
    elif paused:
        head = "paused"
    else:
        head = "now playing"
    bar = eq_bar(pct, NP_BAR_WIDTH, idle=not (paused or ended), levels=None if (paused or ended) else levels)
    tail = f"{bar}  {clock}" + ("  · cached" if cached and not ended else "")
    if ENABLED and paused and not ended:
        tail += "  ⏸"
    parts = []
    if ends_at and not (paused or ended):
        parts.append(f"⏳ ends <t:{int(ends_at)}:R>" + (f" · {hint}" if hint else ""))
    if meta and not ended:
        parts.append(meta)
    lines = [*_wrap(song_title), tail]
    if lyric is not None and not ended:
        lines.extend(lyric_block(lyric))
    return _status(head, lines, footer="\n".join(parts) or None)


def lyric_block(text) -> list:
    """Two fixed rows for the live lyric (fixed height so the message doesn't jump around)."""
    text = (text or "").strip()
    rows = _wrap(text, width=max(12, WRAP_WIDTH - 2), max_lines=2) if text else ["♪ ♪ ♪"]
    if text:
        rows = [("♪ " if i == 0 else "  ") + r for i, r in enumerate(rows)]
    return (rows + [""] * 2)[:2]


def import_status(name: str, ready: int, total: int, pct: float | None = None,
                  skipped: int = 0, note: str | None = None) -> str:
    overall = (ready / total * 100) if total else 0
    count = f"{ready}/{total} ready"
    if pct is not None:
        count += f" · track {int(round(pct))}%"
    lines = [*_wrap(name, max_lines=1), f"{eq_bar(overall)}  {count}"]
    extra = _extra(note, skipped)
    if extra:
        lines.append(extra)
    return _status("importing", lines)


def import_idle(name: str, ready: int, total: int, skipped: int = 0, note: str | None = None) -> str:
    overall = (ready / total * 100) if total else 0
    lines = [*_wrap(name, max_lines=1), f"{eq_bar(overall)}  {ready}/{total} ready"]
    extra = _extra(note, skipped)
    if extra:
        lines.append(extra)
    return _status("buffer ready", lines)


def error_label(detail: str) -> str:
    """Styled header, REAL error text kept plain (just wrapped inside the frame)."""
    head = ("✖ " + spaced("error")) if ENABLED else "error"
    text = _status("error", [*_wrap(detail, max_lines=4)], head=head)
    if BADGES and CRASH_GIF and text.badge:
        text.scene = ("crash", detail)
    return text


# ---------------------------------------------------------------------------
# Command notes (short-lived replies with an animated badge) and the playlist card
# ---------------------------------------------------------------------------

def note(label: str, *lines) -> str:
    """Generic short reply: a badge (gif_art label) plus a few framed lines."""
    body = []
    for line in lines:
        body.extend(_wrap(line, max_lines=2))
    return _status(label, body or [""])


def volume_note(pct: int) -> str:
    pct = int(max(0, min(200, pct)))
    cells = 14
    on = int(round(min(pct, 100) / 100 * cells))
    over = max(0, pct - 100)
    bar = "▮" * on + "▯" * (cells - on)
    extra = f"  +{over}% boost" if over else ""
    return _status("volume", [f"{bar}  {pct}%{extra}"])


def seek_note(frm: float, to: float, duration: float = 0) -> str:
    arrow = f"{_fmt_clock(frm)} → {_fmt_clock(to)}"
    if duration:
        arrow += f"  / {_fmt_clock(duration)}"
    return _status("seeking", [arrow, eq_bar(100 * to / duration if duration else None, NP_BAR_WIDTH)])


def fmt_long_duration(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m" if m else f"{seconds}s"


def playlist_card(name: str, total: int, total_seconds: float = 0, skipped: int = 0, shuffled: bool = False,
                  keep: bool = False) -> str:
    lines = [*_wrap(name, max_lines=2)]
    stats = f"{total} track" + ("" if total == 1 else "s")
    if total_seconds:
        stats += f" · {fmt_long_duration(total_seconds)}"
    lines.append(stats)
    flags = " · ".join(x for x in ("shuffled" if shuffled else "", "kept" if keep else "", f"{skipped} unavailable" if skipped else "") if x)
    if flags:
        lines.append(flags)
    return _status("playlist", lines)


# ---------------------------------------------------------------------------
# Queue / stats / list cards
# ---------------------------------------------------------------------------

def _row(prefix: str, title: str, suffix: str = "") -> str:
    room = max(8, WRAP_WIDTH - len(prefix) - len(suffix) - 1)
    return f"{prefix}{_wrap(title, width=room, max_lines=1)[0]}{(' ' + suffix) if suffix else ''}"


def queue_card(now_title, rows, page: int, pages: int, total: int, total_seconds: float = 0,
               selected: int | None = None, playlist: str | None = None) -> str:
    """rows: [(number, title, seconds)] for the visible page; `selected` marks one number."""
    lines = []
    if now_title:
        lines.append(_row("▶ ", now_title))
    if playlist:
        lines.append(_row("🎵 playlist: ", playlist))
    if not rows:
        lines.append("(queue is empty)")
    for number, title, secs in rows:
        mark = "➤" if number == selected else " "
        lines.append(_row(f"{mark}{number:>2} ", title, _fmt_clock(secs) if secs else ""))
    foot = f"page {page + 1}/{max(1, pages)} · {total} queued"
    if total_seconds:
        foot += f" · {fmt_long_duration(total_seconds)}"
    lines.append(foot)
    return _status("queue", lines)


def stats_card(period: str, tracks: int, seconds: float, top_songs, top_people) -> str:
    lines = [f"{period}: {tracks} track" + ("" if tracks == 1 else "s") + (f" · {fmt_long_duration(seconds)}" if seconds else "")]
    if top_songs:
        lines.append("top songs")
        lines += [_row(f" {i}. ", title, f"x{n}") for i, (title, n) in enumerate(top_songs, 1)]
    if top_people:
        lines.append("top DJs")
        lines += [_row(f" {i}. ", name, f"x{n}") for i, (name, n) in enumerate(top_people, 1)]
    if not top_songs:
        lines.append("nothing played yet")
    return _status("stats", lines)


def list_card(label: str, title: str, rows) -> str:
    """rows: [(number, text)]  (favourites, history...)."""
    lines = [title] + ([_row(f"{n:>2}. ", t) for n, t in rows] or ["(nothing here yet)"])
    return _status(label, lines)

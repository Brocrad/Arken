"""
Spinning ASCII donut (a port of Andy Sloane's donut.c) for Arken's summon animation.

Frames are rendered once, in a background thread, and cached - the math is ~25k loop
iterations per frame, which is fine off the event loop but not something to repeat per summon.
"""

import math
import threading

WIDTH = 28   # Discord code blocks wrap past ~30 cols on narrow windows/mobile; stay under
HEIGHT = 14
FRAME_COUNT = 24
# One animation cycle = FRAME_COUNT frames = whole turns on both axes, so the loop is seamless.
# A turns twice per cycle (30 degrees/frame), B once (15 degrees/frame).
A_STEP = 2 * math.pi * 2 / FRAME_COUNT
B_STEP = 2 * math.pi / FRAME_COUNT
X_SCALE = 16.0   # classic donut.c uses 30/15 on an 80x22 grid; scaled to fit with margin on every side
Y_SCALE = 8.0    # keep X:Y at 2:1 because character cells are about twice as tall as wide

_LUMINANCE = ".,-~:;=!*#$@"
_frames = None
_lock = threading.Lock()


def render_levels(A: float, B: float, width: int, height: int, x_scale: float, y_scale: float) -> list:
    """
    One frame of the donut rotated by (A, B) as a flat row-major list of brightness levels:
    -1 = empty, 0..11 = dark..bright. Used by the GIF renderer (square pixels -> x_scale == y_scale).
    """
    out = [-1] * (width * height)
    zbuf = [0.0] * (width * height)
    cos_a, sin_a = math.cos(A), math.sin(A)
    cos_b, sin_b = math.cos(B), math.sin(B)
    cx, cy = width / 2, height / 2

    for theta_step in range(90):                      # small circle
        theta = theta_step * 0.07
        sin_t, cos_t = math.sin(theta), math.cos(theta)
        h = cos_t + 2
        for phi_step in range(315):                   # sweep around the centre
            phi = phi_step * 0.02
            sin_p, cos_p = math.sin(phi), math.cos(phi)
            d = 1 / (sin_p * h * sin_a + sin_t * cos_a + 5)
            t = sin_p * h * cos_a - sin_t * sin_a
            x = int(cx + x_scale * d * (cos_p * h * cos_b - t * sin_b))
            y = int(cy + y_scale * d * (cos_p * h * sin_b + t * cos_b))
            if 0 < y < height and 0 < x < width:
                o = x + width * y
                if d > zbuf[o]:
                    zbuf[o] = d
                    n = int(8 * ((sin_t * sin_a - sin_p * cos_t * cos_a) * cos_b
                                 - sin_p * cos_t * sin_a - sin_t * cos_a - cos_p * cos_t * sin_b))
                    out[o] = min(max(n, 0), len(_LUMINANCE) - 1)
    return out


def render(A: float, B: float, width: int = WIDTH, height: int = HEIGHT) -> str:
    """One frame of the donut rotated by (A, B) as text. Rows are padded to full width."""
    levels = render_levels(A, B, width, height, X_SCALE, Y_SCALE)
    chars = [" " if v < 0 else _LUMINANCE[v] for v in levels]
    rows = ["".join(chars[r * width:(r + 1) * width]) for r in range(height)]
    return "\n".join(rows)


def get_frames() -> list:
    """All animation frames (rendered once, then cached; safe to call from any thread)."""
    global _frames
    if _frames is None:
        with _lock:
            if _frames is None:
                _frames = [render(i * A_STEP, i * B_STEP) for i in range(FRAME_COUNT)]
    return _frames


if __name__ == "__main__":
    import sys
    import time
    t0 = time.time()
    frames = get_frames()
    print(f"rendered {len(frames)} frames in {time.time() - t0:.2f}s", file=sys.stderr)
    for f in (frames[0], frames[5]):
        print(f)
        print("=" * WIDTH)

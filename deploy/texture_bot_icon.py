#!/usr/bin/env python3
"""Composite a pin/emblem photo onto the Gondor leather plate for Discord app icons."""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageChops, ImageEnhance, ImageFilter


def _median_rgb(pixels: list[tuple[int, int, int]]) -> tuple[float, float, float]:
    if not pixels:
        return (128.0, 128.0, 128.0)
    rs = sorted(p[0] for p in pixels)
    gs = sorted(p[1] for p in pixels)
    bs = sorted(p[2] for p in pixels)
    mid = len(pixels) // 2
    return (float(rs[mid]), float(gs[mid]), float(bs[mid]))


def _bg_mask(size: tuple[int, int], get_px, thresh: float = 38.0) -> Image.Image:
    """Mask foreground (pin) vs grey stone background from corner colour."""
    w, h = size
    pad = min(48, w // 8, h // 8)
    samples: list[tuple[int, int, int]] = []
    for y in range(pad):
        for x in range(pad):
            samples.append(get_px(x, y))
            samples.append(get_px(w - 1 - x, y))
            samples.append(get_px(x, h - 1 - y))
            samples.append(get_px(w - 1 - x, h - 1 - y))
    bg = _median_rgb(samples)
    mask = Image.new("L", (w, h))
    mp = mask.load()
    src = None
    for y in range(h):
        for x in range(w):
            r, g, b = get_px(x, y)
            dist = ((r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2) ** 0.5
            mp[x, y] = 255 if dist > thresh else 0
    return mask


def _leather_plate(gondor: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Build a navy leather field using Gondor's lower area (mostly background)."""
    g = gondor.convert("RGB")
    # Bias crop toward bottom: more plain leather, less tree.
    gh, gw = g.size[1], g.size[0]
    patch = g.crop((0, int(gh * 0.72), gw, gh))
    plate = patch.resize(size, Image.Resampling.LANCZOS)
    blur = g.resize(size, Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(6))
    plate = Image.blend(plate, blur, 0.12)
    plate = ImageEnhance.Brightness(plate).enhance(1.12)
    plate = ImageEnhance.Contrast(plate).enhance(1.05)
    return plate


def _extract_rgba(src: Image.Image) -> Image.Image:
    rgb = src.convert("RGB")
    px = rgb.load()
    w, h = rgb.size

    def get_px(x, y):
        return px[x, y]

    mask = _bg_mask((w, h), get_px)
    rgba = rgb.convert("RGBA")
    rgba.putalpha(mask)
    bb = rgba.getbbox()
    if bb:
        rgba = rgba.crop(bb)
    alpha = rgba.split()[3].filter(ImageFilter.GaussianBlur(1.2))
    rgba.putalpha(alpha)
    return rgba


def compose(gondor_path: Path, emblem_path: Path, out_path: Path, *, size: int = 1024) -> None:
    gondor = Image.open(gondor_path)
    src = Image.open(emblem_path)
    emblem = _extract_rgba(src)
    emblem = ImageEnhance.Brightness(emblem).enhance(1.22)
    emblem = ImageEnhance.Contrast(emblem).enhance(1.15)
    emblem = ImageEnhance.Color(emblem).enhance(1.2)

    plate = _leather_plate(gondor, (size, size))

    # Scale emblem to fit with margin (shield is tall).
    mw, mh = int(size * 0.78), int(size * 0.88)
    ew, eh = emblem.size
    scale = min(mw / ew, mh / eh)
    nw, nh = max(1, int(ew * scale)), max(1, int(eh * scale))
    emblem = emblem.resize((nw, nh), Image.Resampling.LANCZOS)

    # Emboss: multiply with plate luminance so colours pick up leather grain.
    plate_rgb = plate.convert("RGB")
    em_rgb = emblem.convert("RGB")
    grain = plate_rgb.resize(emblem.size, Image.Resampling.LANCZOS)
    grain = ImageEnhance.Brightness(grain).enhance(1.18)
    embossed = ImageChops.soft_light(em_rgb, grain)
    emblem_done = Image.merge("RGBA", (*embossed.split(), emblem.split()[3]))

    canvas = plate.convert("RGBA")
    x = (size - nw) // 2
    y = (size - nh) // 2 + int(size * 0.02)
    canvas.alpha_composite(emblem_done, (x, y))
    final = canvas

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() in (".jpg", ".jpeg"):
        final.convert("RGB").save(out_path, quality=92)
    else:
        final.save(out_path, lossless=True)
    png = out_path.with_suffix(".png")
    final.save(png)
    print(f"Wrote {out_path} and {png} ({size}x{size})")


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    default_assets = Path(r"C:\Users\Jasper\.cursor\projects\c-project-folder-Gondor\assets")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gondor", type=Path, default=default_assets / "GondorianEmblem-d05ff944-642d-425f-afea-3e626c3fdcd0.webp")
    ap.add_argument("--emblem", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=default_assets / "osgiliath-discord-icon.webp")
    ap.add_argument("--size", type=int, default=1024)
    args = ap.parse_args()
    compose(args.gondor, args.emblem, args.out, size=args.size)


if __name__ == "__main__":
    main()
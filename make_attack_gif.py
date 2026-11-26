#!/usr/bin/env python3
"""Stitch numbered hitbox PNGs into an animated GIF, APNG, or WebM.

Usage:
    python make_attack_gif.py Rivals2/HitboxFrames/Loxodont/ATT_Lox_Bair_Aerial
    python make_attack_gif.py path/to/attack --format apng
    python make_attack_gif.py path/to/attack --out bair.gif --fps 30
"""
from __future__ import annotations

import argparse
from collections import Counter
import re
import shutil
import subprocess
from pathlib import Path

try:
    from PIL import Image
except ImportError as exc:
    raise SystemExit("GIF creation requires Pillow: python -m pip install pillow") from exc


FRAME_NUMBER = re.compile(
    r"^(?P<prefix>.*?)(?P<marker>frame_|_HB_)(?P<number>\d+)\.png$",
    re.IGNORECASE,
)


def numbered_frames(source: Path):
    """Return one consistently named, numerically sorted PNG sequence."""
    groups = {}
    for path in source.iterdir():
        match = FRAME_NUMBER.match(path.name)
        if path.is_file() and match:
            key = (match.group("prefix"), match.group("marker"))
            groups.setdefault(key, []).append((int(match.group("number")), path))
    if not groups:
        raise SystemExit(f"No numbered hitbox PNG images found in {source}")
    if len(groups) > 1:
        names = ", ".join(f"{prefix}{marker}..." for prefix, marker in sorted(groups))
        raise SystemExit(f"Found multiple PNG naming patterns in {source}: {names}")
    (prefix, marker), frames = next(iter(groups.items()))
    frames.sort(key=lambda item: item[0])
    padding = len(FRAME_NUMBER.match(frames[0][1].name).group("number"))
    return frames, prefix, marker, padding


def gif_alpha(image, dither=True, matte_color=None):
    """Map PNG alpha to GIF's binary transparency channel.

    GIF cannot store partial alpha. Error diffusion approximates coverage with
    a fine-grained transparent/opaque pattern; no-dither mode makes every
    nontransparent pixel opaque instead.
    """
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    if matte_color is not None:
        matte = Image.new("RGBA", rgba.size, (*matte_color, 255))
        flattened = Image.alpha_composite(matte, rgba)
        visible = alpha.point([0] + [255] * 255)
        flattened.putalpha(visible)
        matte.close()
        alpha.close()
        visible.close()
        rgba.close()
        return flattened
    if dither:
        visible = alpha.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")
    else:
        visible = alpha.point([0] + [255] * 255)
    rgba.putalpha(visible)
    alpha.close()
    visible.close()
    return rgba


def indexed_frames(images):
    """Use one move-wide palette and reserve colors that identify box classes."""
    try:
        # Single source of truth: the color settings at the top of the
        # frame renderer.
        from rivals2_move_boxes import semantic_colors
        reserved = semantic_colors()
    except ImportError:
        reserved = [(0, 150, 255), (235, 240, 245), (213, 0, 249),
                    (255, 176, 0), (255, 23, 68)]
    frequencies = Counter()
    for rgba in images:
        for count, color in rgba.getcolors(maxcolors=1_000_000) or []:
            if color[3] > 0:
                frequencies[color[:3]] += count

    # Index 0 is always transparent. Keep semantic colors at stable indices,
    # then fill the remaining palette with the most common colors across all
    # frames so later orange frames are not mapped through the first frame's
    # red/blue palette.
    colors = [(0, 0, 0)] + reserved
    for color, _ in frequencies.most_common():
        if color not in colors:
            colors.append(color)
            if len(colors) == 256:
                break
    colors.extend([(0, 0, 0)] * (256 - len(colors)))
    palette = [channel for color in colors for channel in color]
    palette_image = Image.new("P", (1, 1))
    palette_image.putpalette(palette)

    result = []
    for rgba in images:
        indexed = rgba.convert("RGB").quantize(palette=palette_image,
                                               dither=Image.Dither.NONE)
        alpha = rgba.getchannel("A")
        transparent = alpha.point([255 if value == 0 else 0 for value in range(256)])
        indexed.paste(0, mask=transparent)
        result.append(indexed)
        alpha.close()
        transparent.close()
    palette_image.close()
    return result


def create_gif(source: Path, destination: Path, fps: float = 60, loop: int = 0,
               dither: bool = True, matte_color=None) -> int:
    if not source.is_dir():
        raise SystemExit(f"Attack frame folder does not exist: {source}")
    frames, _, _, _ = numbered_frames(source)

    images = []
    indexed = []
    try:
        for _, path in frames:
            with Image.open(path) as image:
                images.append(gif_alpha(image, dither=dither, matte_color=matte_color))
        indexed = indexed_frames(images)
        destination.parent.mkdir(parents=True, exist_ok=True)
        duration_ms = max(1, round(1000 / fps))
        # Keep a full, independently decodable frame sequence. GIF palette
        # optimization can emit partial rectangles that leave transparent
        # blocks behind in viewers when the image also uses transparency.
        indexed[0].save(destination, save_all=True, append_images=indexed[1:],
                       duration=duration_ms, loop=loop, optimize=False,
                       transparency=0, disposal=2)
    finally:
        for image in images + indexed:
            image.close()
    return len(frames)


def create_apng(source: Path, destination: Path, fps: float = 60, loop: int = 0) -> int:
    """Write animated PNG frames with full per-pixel alpha support."""
    if not source.is_dir():
        raise SystemExit(f"Attack frame folder does not exist: {source}")
    paths, _, _, _ = numbered_frames(source)

    images = []
    try:
        for _, path in paths:
            with Image.open(path) as image:
                images.append(image.convert("RGBA"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        duration_ms = max(1, round(1000 / fps))
        images[0].save(destination, format="PNG", save_all=True,
                       append_images=images[1:], duration=duration_ms,
                       loop=loop, disposal=0, blend=0, optimize=True)
    finally:
        for image in images:
            image.close()
    return len(paths)


def create_webm(source: Path, destination: Path, fps: float = 60, loop: int = 0) -> int:
    """Encode the numbered PNG sequence as VP9 WebM with an alpha channel."""
    if not source.is_dir():
        raise SystemExit(f"Attack frame folder does not exist: {source}")
    frames, prefix, marker, padding = numbered_frames(source)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("WebM creation requires ffmpeg on PATH")

    first_frame = frames[0][0]
    image_pattern = source.resolve() / f"{prefix}{marker}%0{padding}d.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-framerate", f"{fps:g}", "-start_number", str(first_frame),
               "-i", str(image_pattern), "-c:v", "libvpx-vp9",
               "-pix_fmt", "yuva420p", "-auto-alt-ref", "0",
               "-lossless", "1", "-b:v", "0", "-an",
               "-metadata:s:v:0", "alpha_mode=1", str(destination)]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or str(exc)
        raise SystemExit(f"ffmpeg could not encode alpha WebM:\n{detail}") from exc
    return len(frames)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("attack_folder", type=Path, help="folder containing numbered frame PNGs")
    parser.add_argument("--format", choices=("gif", "apng", "webm"),
                        help="output format (default: gif; APNG/WebM preserve alpha)")
    parser.add_argument("--out", type=Path, help="output path (default: animation.gif, animation.png, or animation.webm)")
    parser.add_argument("--fps", type=float, default=60, help="playback speed (default: 60)")
    parser.add_argument("--loop", type=int, default=0, help="number of repeats; 0 loops forever (default: 0)")
    alpha_group = parser.add_mutually_exclusive_group()
    alpha_group.add_argument("--dither", action="store_true",
                             help="approximate partial opacity with dithering on transparency (default)")
    alpha_group.add_argument("--no-dither", action="store_true",
                             help="make all visible GIF pixels opaque; keeps only the outside background transparent")
    alpha_group.add_argument("--matte-color", metavar="RRGGBB",
                             help="smoothly blend opacity against this background color; transparent outside the artwork")
    args = parser.parse_args()
    if args.fps <= 0:
        parser.error("--fps must be greater than zero")
    if args.loop < 0:
        parser.error("--loop cannot be negative")
    source = args.attack_folder
    output_format = args.format or (
        "apng" if args.out and args.out.suffix.lower() in (".png", ".apng") else
        "webm" if args.out and args.out.suffix.lower() == ".webm" else "gif")
    default_name = {"apng": "animation.png", "webm": "animation.webm",
                    "gif": "animation.gif"}[output_format]
    destination = args.out or source / default_name
    creator = {"apng": create_apng, "webm": create_webm,
               "gif": create_gif}[output_format]
    if output_format == "gif":
        if args.matte_color:
            value = args.matte_color.removeprefix("#")
            if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
                parser.error("--matte-color must be six hexadecimal digits, e.g. 313338")
            matte_color = tuple(int(value[i:i+2], 16) for i in (0, 2, 4))
            dither = False
        else:
            matte_color = None
            dither = not args.no_dither
        count = creator(source, destination, args.fps, args.loop,
                        dither=dither, matte_color=matte_color)
    else:
        if args.no_dither or args.dither or args.matte_color:
            parser.error("--dither, --no-dither, and --matte-color apply only to GIF output")
        count = creator(source, destination, args.fps, args.loop)
    print(f"Created {output_format.upper()} {destination} from {count} frames at {args.fps:g} fps")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Draw the hitbox/hurtbox color legend image used by the README.

Colors, opacities, outline styling, and the box-drawing routine all come
from rivals2_move_boxes.py, so the legend always matches rendered frames.
Edit colors there; edit the plain-language explanations here.

Usage:
    python make_legend.py                      # writes images/box_legend.png
    python make_legend.py --out /tmp/legend.png
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from rivals2_move_boxes import (
    HEIGHT, WIDTH,
    HITBOX_FILL_ALPHA, HITBOX_OUTLINE_ALPHA, HITBOX_OUTLINE_WIDTH,
    HITBOX_FILL_COLOR_NORMAL, HITBOX_FILL_COLOR_SWEET, HITBOX_FILL_COLOR_SOUR,
    HITBOX_FILL_COLOR_GRAB, HITBOX_FILL_COLOR_HITGRAB, HITBOX_FILL_COLOR_DETECT,
    HITBOX_FILL_COLOR_KILL_PROJECTILES,
    HURTBOX_FILL_ALPHA, HURTBOX_OUTLINE_ALPHA, HURTBOX_OUTLINE_WIDTH,
    HURTBOX_FILL_COLOR_DEFAULT, HURTBOX_FILL_COLOR_UNGRABBABLE,
    HURTBOX_FILL_COLOR_UNHITGRABBABLE, HURTBOX_FILL_COLOR_INTANGIBLE,
    HURTBOX_FILL_COLOR_PROJECTILE_INTANGIBLE, HURTBOX_FILL_COLOR_INVULNERABLE,
    HURTBOX_FILL_COLOR_COUNTERING, HURTBOX_FILL_COLOR_PARRYING,
    HURTBOX_FILL_COLOR_INACTIVE, HURTBOX_FILL_COLOR_DISABLED,
    composite_box, hurtbox_outline_color,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "images/box_legend.png"

# (label, kind, color, plain-language meaning). kind None is a section header.
# Keep README's "Box colors and states" tables in step when editing.
LEGEND_ENTRIES = (
    ("Hitboxes", None, None, "The attack: touching an opponent's hurtbox hits them."),
    ("Hit", "hit", HITBOX_FILL_COLOR_NORMAL,
     "A normal attack. Deals damage and knockback."),
    ("Sweetspot", "hit", HITBOX_FILL_COLOR_SWEET,
     "The strongest part of the attack. Hitting with it deals extra damage and knockback."),
    ("Sourspot", "hit", HITBOX_FILL_COLOR_SOUR,
     "A weaker part of the attack. Hitting with it deals less damage and knockback."),
    ("Grab", "hit", HITBOX_FILL_COLOR_GRAB,
     "Grabs the opponent instead of hitting them. Can't grab Ungrabbable hurtboxes."),
    ("Hit grab", "hit", HITBOX_FILL_COLOR_HITGRAB,
     "An attack that grabs whoever it hits. Can't grab Unhitgrabbable hurtboxes."),
    ("Detect", "hit", HITBOX_FILL_COLOR_DETECT,
     "Senses contact without dealing damage, e.g. to start a follow-up."),
    ("Kills projectiles", "hit", HITBOX_FILL_COLOR_KILL_PROJECTILES,
     "Destroys projectiles. Does not hit fighters."),
    ("Hurtboxes", None, None, "The character's body: where they can be hit."),
    ("Normal (Default / Vulnerable)", "hurt", HURTBOX_FILL_COLOR_DEFAULT,
     "Can be hit and grabbed as usual."),
    ("Ungrabbable", "hurt", HURTBOX_FILL_COLOR_UNGRABBABLE,
     "Can be hit, but can't be grabbed. Often the arms and legs, or the whole body while holding someone."),
    ("Unhitgrabbable", "hurt", HURTBOX_FILL_COLOR_UNHITGRABBABLE,
     "Can be hit, but hit grabs won't grab this part."),
    ("Intangible", "hurt", HURTBOX_FILL_COLOR_INTANGIBLE,
     "Attacks pass straight through. Seen on getup and ledge options."),
    ("Projectile intangible", "hurt", HURTBOX_FILL_COLOR_PROJECTILE_INTANGIBLE,
     "Projectiles pass through; melee attacks still hit."),
    ("Invulnerable", "hurt", HURTBOX_FILL_COLOR_INVULNERABLE,
     "Attacks connect but deal no damage or knockback.*"),
    ("Countering", "hurt", HURTBOX_FILL_COLOR_COUNTERING,
     "Getting hit here triggers the character's counterattack.*"),
    ("Parrying", "hurt", HURTBOX_FILL_COLOR_PARRYING,
     "Getting hit here triggers a parry.*"),
    ("Inactive", "hurt", HURTBOX_FILL_COLOR_INACTIVE,
     "Switched off by the move, e.g. while traveling or hidden. Can't be hit.*"),
    ("Disabled", "hurt", HURTBOX_FILL_COLOR_DISABLED,
     "Hurtbox turned off entirely. Hidden in frames by default."),
)
LEGEND_FOOTNOTE = ("* This needs to be revised, I'm not that smart.")

#LEGEND_FOOTNOTE = ("* Inferred from the game's state names and how moves use them; the game files don't describe these states.")


def wrap(text, font, limit):
    """Greedy word wrap to a pixel width."""
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and font.getlength(trial) > limit:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + ([line] if line else [])


def legend_columns():
    """Split LEGEND_ENTRIES into one column per section header."""
    columns = []
    for entry in LEGEND_ENTRIES:
        if entry[1] is None or not columns:
            columns.append([])
        columns[-1].append(entry)
    return columns


def write_legend(path):
    """Draw each section (hitboxes, hurtboxes) as its own column, with
    swatches styled like the rendered frames."""
    try:
        label_font = ImageFont.load_default(size=17)
        body_font = ImageFont.load_default(size=14)
        header_font = ImageFont.load_default(size=21)
    except TypeError:  # Pillow < 10.1 has no sized default font
        label_font = body_font = header_font = ImageFont.load_default()
    column_width, column_gap, pad, swatch, gap = 440, 28, 16, 26, 12
    text_offset = swatch + gap
    text_width = column_width - text_offset
    label_color, body_color = (240, 240, 240, 255), (175, 180, 188, 255)
    columns = legend_columns()
    width = pad*2 + column_width*len(columns) + column_gap*(len(columns) - 1)

    # Lay out every column first so the image height fits the tallest one.
    layout, bottom = [], pad
    for index, column in enumerate(columns):
        left, y = pad + index*(column_width + column_gap), pad
        for label, kind, color, meaning in column:
            if kind is None:
                lines = wrap(meaning, body_font, column_width)
                layout.append((label, kind, color, lines, left, y))
                y += 28 + 19*len(lines) + 10
            else:
                lines = wrap(meaning, body_font, text_width)
                layout.append((label, kind, color, lines, left, y))
                y += max(swatch + 6, 22 + 18*len(lines)) + 8
        bottom = max(bottom, y)
    footnote = wrap(LEGEND_FOOTNOTE, body_font, width - 2*pad)
    height = bottom + 6 + 18*len(footnote) + pad

    image = Image.new("RGBA", (width, height), (32, 34, 37, 255))
    # The renderer's projection centers the frame at (WIDTH/2, HEIGHT/2);
    # offset each swatch by that so it lands at legend pixel (cx, cy).
    camera = {"scale": 1.0, "center_horizontal": 0.0, "center_z": 0.0, "right": (1.0, 0.0)}
    for label, kind, color, lines, left, top in layout:
        draw = ImageDraw.Draw(image)
        if kind is None:
            draw.text((left, top), label, font=header_font, fill=label_color)
            for k, line in enumerate(lines):
                draw.text((left, top + 28 + 19*k), line, font=body_font, fill=body_color)
            continue
        text_x = left + text_offset
        cx, cy = left + swatch/2, top + swatch/2 + 2
        box = {"shape": "circle", "r": swatch/2, "c": (cx - WIDTH/2, 0.0, HEIGHT/2 - cy)}
        if kind == "hit":
            image = composite_box(image, box, camera, color, HITBOX_FILL_ALPHA,
                                  color, HITBOX_OUTLINE_ALPHA, HITBOX_OUTLINE_WIDTH)
        else:
            image = composite_box(image, box, camera, color, HURTBOX_FILL_ALPHA,
                                  hurtbox_outline_color(color), HURTBOX_OUTLINE_ALPHA,
                                  HURTBOX_OUTLINE_WIDTH)
        draw = ImageDraw.Draw(image)
        draw.text((text_x, top), label, font=label_font, fill=label_color)
        for k, line in enumerate(lines):
            draw.text((text_x, top + 22 + 18*k), line, font=body_font, fill=body_color)
    draw = ImageDraw.Draw(image)
    for k, line in enumerate(footnote):
        draw.text((pad, bottom + 6 + 18*k), line, font=body_font, fill=body_color)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="PNG to write (default: images/box_legend.png)")
    args = parser.parse_args()
    write_legend(args.out)
    print(f"Wrote color legend {args.out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Export character assets from a Rivals 2 Unreal pak via CUE4Parse-Python.

Examples:
    python export_characters_from_pak.py
    python export_characters_from_pak.py --format raw
    python export_characters_from_pak.py --pak /path/to/Rivals2-Windows.pak

The default JSON mode asks CUE4Parse to deserialize each character .uasset
using the supplied .usmap. Raw mode copies all archive entries beneath the
characters content folder, including .uasset/.uexp pairs.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_PAK = ROOT / "Rivals2-Windows.pak"
DEFAULT_MAP = ROOT / "Rivals2-5.8.0-0+UE5-5418fa4e.usmap"
DEFAULT_OUT = ROOT / "Rivals2/Exports/Characters"
CHARACTER_ASSETS = "Rivals2/Content/Characters"


def find_cli(explicit: str | None) -> list[str]:
    """Return an executable prefix, allowing an explicitly selected CLI path."""
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.exists():
            if candidate.suffix.lower() == ".py":
                return [sys.executable, str(candidate)]
            return [str(candidate)]
        found = shutil.which(explicit)
        if found:
            return [found]
        raise SystemExit(f"CUE4Parse command not found: {explicit}")

    found = shutil.which("cue4parse")
    if found:
        return [found]
    raise SystemExit(
        "CUE4Parse-Python is not installed or cue4parse is not on PATH.\n"
        "Install it once with: python -m pip install cue4parse"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pak", type=Path, default=DEFAULT_PAK,
                        help=f"input pak (default: {DEFAULT_PAK.name})")
    parser.add_argument("--usmap", type=Path, default=DEFAULT_MAP,
                        help=f"UE mappings file (default: {DEFAULT_MAP.name})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"export directory (default: {DEFAULT_OUT.relative_to(ROOT)})")
    parser.add_argument("--game", default="GAME_UE5_8",
                        help="CUE4Parse game version (default: GAME_UE5_8)")
    parser.add_argument("--format", choices=("json", "raw", "list"), default="json",
                        help="JSON package exports, raw archive entries, or list matches")
    parser.add_argument("--cli", help="cue4parse executable or path to its Python CLI script")
    parser.add_argument("--key", action="append", default=[],
                        help="AES key in hex, if the pak requires one (repeatable)")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace files already present in the output directory")
    args = parser.parse_args()

    for label, path in (("pak", args.pak), ("usmap", args.usmap)):
        if not path.is_file():
            raise SystemExit(f"{label} file not found: {path}")

    command = find_cli(args.cli)
    cmd = command + ["--pak", str(args.pak.resolve()), "--mappings", str(args.usmap.resolve()),
                     "--game", args.game]
    if args.format == "json":
        cmd += ["--package", f"{CHARACTER_ASSETS}/*.uasset", "--format", "json",
                "--output", str(args.out.resolve())]
    elif args.format == "raw":
        cmd += ["--package", f"{CHARACTER_ASSETS}/*", "--format", "raw",
                "--output", str(args.out.resolve())]
    else:
        cmd += ["--package", f"{CHARACTER_ASSETS}/*", "--list"]
    for key in args.key:
        cmd += ["--key", key]
    if args.overwrite:
        cmd.append("--yes")

    if args.format != "list":
        args.out.mkdir(parents=True, exist_ok=True)
    print("Running:", " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())

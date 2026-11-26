#!/usr/bin/env python3
"""Batch-export Rivals 2 character data from the game's .pak, no FModel clicking.

Three commands:

  setup    One-time: fetch CUE4Parse (the library FModel is built on) at a
           pinned commit and build tools/pak_exporter. Needs git and the
           .NET 10 SDK (`dotnet`). Re-run after changing --ref.
  export   Export everything rivals2_move_boxes.py reads, preserving the
           Rivals2/Content/Characters/... layout:
             json      properties of every package (ATT_, HB_, CD_, skeletons,
                       materials, palettes, ...) like FModel "Save Properties"
             anims     Animation/**/AN_*            -> .ueanim (UEFormat)
             meshes    Skins/Default/SK_*_Default   -> .glb (+ materials/textures)
             textures  every texture in the character folders plus
                       Shared/ and DefaultMasks/ -> .png (curve atlases .hdr)
           Only the default skin is exported: packages under any other
           Skins/<Name>/ folder are skipped (--all-skins to include them).
           Existing files are skipped unless --overwrite, so FModel exports
           already on disk are kept.
  check    Report, per character, which renderer inputs are present or
           missing. Works on any export tree (FModel or this tool).

Examples:
  python export_characters_from_pak.py setup
  python export_characters_from_pak.py export --list            # dry run
  python export_characters_from_pak.py export
  python export_characters_from_pak.py export --character Loxodont --steps json,anims
  python export_characters_from_pak.py check
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_PAK = ROOT / "Rivals2-Windows.pak"
DEFAULT_MAP = ROOT / "Rivals2-5.8.0-0+UE5-5418fa4e.usmap"
CHARACTERS = ROOT / "Rivals2/Content/Characters"
TOOL_DIR = ROOT / "tools/pak_exporter"
TOOL_PROJECT = TOOL_DIR / "Rivals2PakExporter.csproj"
TOOL_BIN = TOOL_DIR / "bin"
TOOL_DLL = TOOL_BIN / "Rivals2PakExporter.dll"
VENDOR = ROOT / "tools/vendor/CUE4Parse"
CUE4PARSE_URL = "https://github.com/FabianFG/CUE4Parse.git"
# Commit the exporter was written against (2026-11-23). Newer commits may
# change CUE4Parse's API; pass --ref to try one.
CUE4PARSE_REF = "5bc69da12e83de97d811962a69f90ce3c5904737"
STEPS = ("json", "anims", "meshes", "textures")


def run(command, **kwargs):
    print("+", " ".join(str(part) for part in command), file=sys.stderr)
    return subprocess.run([str(part) for part in command], check=True, **kwargs)


def require(tool, hint):
    if not shutil.which(tool):
        raise SystemExit(f"{tool!r} is not on PATH. {hint}")


# --------------------------------------------------------------------- setup
def cmd_setup(args):
    require("git", "Install git first.")
    require("dotnet", "Install the .NET 10 SDK: https://dotnet.microsoft.com/download")
    sdks = subprocess.run(["dotnet", "--list-sdks"], capture_output=True, text=True).stdout
    if not any(line.split(".")[0].isdigit() and int(line.split(".")[0]) >= 10 for line in sdks.splitlines()):
        raise SystemExit(f"CUE4Parse targets .NET 10; `dotnet --list-sdks` shows:\n{sdks or '  (none)'}")

    # Fetch exactly one commit instead of the full history.
    if not (VENDOR / ".git").is_dir():
        VENDOR.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--quiet", VENDOR])
        run(["git", "-C", VENDOR, "remote", "add", "origin", CUE4PARSE_URL])
    run(["git", "-C", VENDOR, "fetch", "--depth", "1", "origin", args.ref])
    run(["git", "-C", VENDOR, "checkout", "--quiet", "--force", "FETCH_HEAD"])

    env = dict(os.environ)
    if args.no_natives:
        env["CUE4PARSE_SKIP_NATIVE"] = "1"
    else:
        # CUE4Parse's C++ helpers. Rivals 2 needs them: most of its animations
        # are ACL-compressed, and without the helper they fail with
        # "Unsupported compressed data type". Needs cmake and a C++ compiler.
        require("cmake", "Install cmake and a C++ compiler (gcc/clang, or MSVC on Windows).")
        run(["git", "-C", VENDOR, "submodule", "update", "--init", "--recursive", "--depth", "1"])
    run(["dotnet", "publish", TOOL_PROJECT, "-c", "Release", "-o", TOOL_BIN], env=env)

    # CUE4Parse's build ignores a failed C++ build and carries on, so verify
    # that the helper library really landed next to the exporter.
    natives = sorted(TOOL_BIN.glob("CUE4Parse-Natives.*"))
    if not args.no_natives and not natives:
        print("\nWARNING: CUE4Parse-Natives was not built (check the cmake output above). "
              "ACL-compressed animations will fail to export with 'Unsupported compressed data type'.",
              file=sys.stderr)
    elif natives:
        print(f"\nNative helpers: {', '.join(p.name for p in natives)}")
    print(f"Built {TOOL_DLL.relative_to(ROOT)}. Next: python {Path(__file__).name} export --list")


# -------------------------------------------------------------------- export
def cmd_export(args):
    require("dotnet", "Install the .NET 10 SDK and run the setup command first.")
    if not TOOL_DLL.is_file():
        raise SystemExit(f"{TOOL_DLL.relative_to(ROOT)} not found; run: python {Path(__file__).name} setup")
    for label, path in (("pak", args.pak), ("usmap", args.usmap)):
        if not path.is_file():
            raise SystemExit(f"{label} file not found: {path}")
    command = ["dotnet", TOOL_DLL, "--pak", args.pak.resolve(), "--usmap", args.usmap.resolve(),
               "--game", args.game, "--out", args.out.resolve(), "--steps", ",".join(args.steps)]
    for character in args.character:
        command += ["--character", character]
    for key in args.aes:
        command += ["--aes", key]
    if args.overwrite:
        command.append("--overwrite")
    if args.all_skins:
        command.append("--all-skins")
    if args.list:
        command.append("--list")
    if args.verbose:
        command.append("--verbose")
    result = subprocess.run([str(part) for part in command])
    if args.list:
        return result.returncode
    if result.returncode not in (0, 3):  # 3 = finished with some per-file failures
        raise SystemExit(f"exporter failed with exit code {result.returncode}")
    if not args.no_check:
        print()
        cmd_check(args)
    return result.returncode


# --------------------------------------------------------------------- check
def attack_animation_names(attack_path):
    """Animation basenames an attack asset references (Grounded/Aerial)."""
    try:
        data = json.loads(attack_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    attack = next((e for e in data if str(e.get("Type", "")).startswith("ATT_")), None)
    if attack is None or "Properties" not in attack:
        return None
    names = []
    for key in ("GroundedAnimation", "AerialAnimation"):
        ref = attack["Properties"].get(key)
        if isinstance(ref, dict) and ref.get("ObjectPath"):
            names.append(ref["ObjectPath"].rsplit("/", 1)[-1].split(".", 1)[0])
    return names


def check_character(char_dir):
    attacks = sorted((char_dir / "Attacks").glob("ATT_*.json"))
    skeletons = sorted((char_dir / "Animation").glob("*Skeleton.json"))
    if not attacks:
        return None  # not a playable character folder (Shared, Orby, ...)
    row = {"attacks": len(attacks), "skeleton": bool(skeletons)}
    cd = char_dir / f"CD_{char_dir.name}.json"
    if not cd.is_file():
        cd = next(iter(sorted(char_dir.glob("CD_*.json"))), None)
    row["character_data"] = bool(cd)
    physics = None
    if cd:
        for entry in json.loads(cd.read_text(encoding="utf-8")):
            ref = entry.get("Properties", {}).get("HurtboxesPhysicsAsset") or {}
            if ref.get("ObjectPath"):
                physics = ROOT / (ref["ObjectPath"].rsplit(".", 1)[0] + ".json")
                break
    row["physics"] = bool(physics and physics.is_file()) or any(char_dir.glob("HB_*.json"))

    have = {p.stem for p in char_dir.rglob("*.ueanim")}
    needed, empty = set(), 0
    for attack in attacks:
        names = attack_animation_names(attack)
        if names is None:
            empty += 1
        else:
            needed.update(names)
    row["empty_attacks"] = empty
    row["anims_needed"] = len(needed)
    row["anims_missing"] = sorted(n for n in needed if n not in have)
    glbs = sorted((char_dir / "Skins/Default").glob("SK_*_Default.glb"))
    row["glb"] = glbs[0].name if glbs else None
    # Textures the default skin's material instances point at (masks,
    # normals, ...). Render targets (RT_*) have no exportable content.
    referenced = set()
    for material in (char_dir / "Skins/Default").glob("MI_*.json"):
        try:
            entries = json.loads(material.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in entries if isinstance(entries, list) else [entries]:
            textures = entry.get("Textures") if isinstance(entry, dict) else None
            for ref in (textures or {}).values():
                path = ref.get("ObjectPath", "") if isinstance(ref, dict) else ""
                if path.startswith("Rivals2/Content/Characters/") and "/RT_" not in path:
                    referenced.add(path.rsplit(".", 1)[0])
    row["textures_needed"] = len(referenced)
    row["textures_missing"] = sorted(p for p in referenced
                                     if not ((ROOT / f"{p}.png").is_file() or (ROOT / f"{p}.hdr").is_file()))
    return row


def cmd_check(args):
    root = (args.out / "Rivals2/Content/Characters") if getattr(args, "out", None) else CHARACTERS
    names = args.character or sorted(p.name for p in root.iterdir() if p.is_dir())
    print(f"Renderer inputs under {root}:")
    print(f"{'character':12s} {'ATT':>4s} {'skel':>4s} {'CD':>3s} {'HB':>3s} {'anims':>9s} "
          f"{'skin tex':>9s}  default mesh")
    problems = 0
    for name in names:
        row = check_character(root / name)
        if row is None:
            continue
        anim = f"{row['anims_needed'] - len(row['anims_missing'])}/{row['anims_needed']}"
        tex = (f"{row['textures_needed'] - len(row['textures_missing'])}/{row['textures_needed']}"
               if row["textures_needed"] else "-")
        flag = lambda ok: "yes" if ok else "NO"
        print(f"{name:12s} {row['attacks']:4d} {flag(row['skeleton']):>4s} {flag(row['character_data']):>3s} "
              f"{flag(row['physics']):>3s} {anim:>9s} {tex:>9s}  {row['glb'] or '-'}")
        notes = []
        if row["empty_attacks"]:
            notes.append(f"{row['empty_attacks']} ATT files have no Properties (stub export; frames will be empty)")
        if row["anims_missing"]:
            shown = ", ".join(row["anims_missing"][:4]) + (" ..." if len(row["anims_missing"]) > 4 else "")
            notes.append(f"missing .ueanim: {shown}")
        if row["textures_missing"] and row["glb"]:
            shown = ", ".join(p.rsplit("/", 1)[-1] for p in row["textures_missing"][:3])
            notes.append(f"{len(row['textures_missing'])} default-skin textures missing "
                         f"(--skin-textures needs them): {shown} ...")
        for note in notes:
            print(f"{'':12s}   - {note}")
        problems += bool(notes) or not (row["skeleton"] and row["physics"])
    print("\nNotes: 'anims' counts animations referenced by ATT files; a reference to another"
          " character's animation (e.g. Gouie -> AN_Kra_FAir) shows as missing. A default mesh is"
          " only needed for --show-character/--skin-textures.")
    return 1 if problems else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="fetch CUE4Parse and build the exporter (one time)")
    setup.add_argument("--ref", default=CUE4PARSE_REF,
                       help=f"CUE4Parse commit/branch/tag to build against (default: {CUE4PARSE_REF[:10]})")
    setup.add_argument("--no-natives", action="store_true",
                       help="skip CUE4Parse's C++ helpers (no cmake needed, but ACL-compressed "
                            "animations, most of Rivals 2's, will fail to export)")

    export = sub.add_parser("export", help="export character data from the pak")
    export.add_argument("--pak", type=Path, default=DEFAULT_PAK, help=f"game archive (default: {DEFAULT_PAK.name})")
    export.add_argument("--usmap", type=Path, default=DEFAULT_MAP, help=f"mappings file (default: {DEFAULT_MAP.name})")
    export.add_argument("--game", default="GAME_UE5_8", help="CUE4Parse game version (default: GAME_UE5_8)")
    export.add_argument("--out", type=Path, default=ROOT,
                        help="output root; files land in OUT/Rivals2/Content/Characters/... (default: project root)")
    export.add_argument("--character", action="append", default=[],
                        help="only this character folder, e.g. Loxodont (repeatable; default: all)")
    export.add_argument("--steps", type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                        default=list(STEPS), help="comma list of json,anims,meshes,textures (default: all)")
    export.add_argument("--aes", action="append", default=[], help="AES key in hex if the pak is encrypted (repeatable)")
    export.add_argument("--overwrite", action="store_true", help="replace existing files instead of skipping them")
    export.add_argument("--all-skins", action="store_true",
                        help="also export non-default skins (default: only Skins/Default; other skins are skipped)")
    export.add_argument("--list", action="store_true", help="dry run: print what each step would export")
    export.add_argument("--verbose", action="store_true", help="log every exported file")
    export.add_argument("--no-check", action="store_true",
                        help="don't run the check report after exporting (run_pipeline.py runs it once itself)")

    check = sub.add_parser("check", help="report missing renderer inputs per character")
    check.add_argument("--character", action="append", default=[], help="only this character (repeatable)")

    args = parser.parse_args()
    if args.command == "export":
        bad = [s for s in args.steps if s not in STEPS]
        if bad:
            parser.error(f"unknown --steps value(s): {', '.join(bad)}")
    handler = {"setup": cmd_setup, "export": cmd_export, "check": cmd_check}[args.command]
    return handler(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())

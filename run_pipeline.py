#!/usr/bin/env python3
"""Regenerate every character's hitbox frames from the game pak, with a timed report.

Runs these steps in order (pick a subset with --steps):

  setup    Build the pak exporter. Runs automatically if it isn't built yet
           or its source is newer than the build; list it in --steps to force
           a rebuild (e.g. after a CUE4Parse bump).
  usmap    Generate Rivals2-build<Steam build>.usmap from the game's executable
           with UsmapGen if this game build has none yet (skipped if usmapgen
           isn't built or --usmap is given). export uses the newest .usmap.
  export   Export json, anims, meshes, textures from the pak, one exporter run
           per kind so each one is timed separately. Existing files are
           REPLACED by default, since the point is picking up a pak update
           (--keep-existing to only fill in missing files).
  check    The exporter's per-character input report (missing anims, ...).
  render   rivals2_move_boxes.py once per character, --jobs at a time. Only
           moves whose inputs changed since their last render are re-rendered
           (--full-render or --clean renders everything). Renders with
           --skin-textures (the textured default skin under the boxes) unless
           --no-skin-textures. Intros, taunts and *_OLD assets are skipped
           (--no-default-skips renders them; --skip REGEX skips more).
  gifs     make_attack_gif.py for every rendered move folder (not in the
           default steps; add it with --steps ...,gifs or --gifs).

On a terminal, progress is one block redrawn in place: every step with its
status and time, a bar for the running step (characters, moves, frames,
ETA) and a bar per character being worked on. When output goes to a file
or pipe (or with --no-progress) it prints plain lines instead.

Everything goes into a report folder (default
Rivals2/PipelineReports/<date_time>/): report.md, report.json, and one log
per step/character under logs/. The report has per-step and per-character
times, frame and move counts, failures, and the slowest moves.

The pak is read straight from the game install, found through Steam (any
library, Windows or Linux, Flatpak/Snap too). For other installs pass
--game-dir "/path/to/Rivals 2" or set RIVALS2_GAME_DIR; --pak picks one file.

Examples:
  python run_pipeline.py                                  # export + check + render all
  python run_pipeline.py --game-dir "/mnt/games/SteamLibrary/steamapps/common/Rivals 2"
  python run_pipeline.py --jobs 4                         # render 4 characters at once
  python run_pipeline.py --character Loxodont --character Absa
  python run_pipeline.py --steps render --clean           # re-render only, from fresh folders
  python run_pipeline.py --gifs --gif-args=--no-dither    # also make GIFs
  python run_pipeline.py --no-skin-textures               # boxes only, much faster
  python run_pipeline.py --skip Pummel --skip Throw       # skip more moves (regex)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import export_characters_from_pak as exporter

ROOT = Path(__file__).resolve().parent
CHARACTERS = ROOT / "Rivals2/Content/Characters"
DEFAULT_FRAMES = ROOT / "Rivals2/HitboxFrames"
DEFAULT_GIFS = ROOT / "Rivals2/GIFs"
DEFAULT_REPORTS = ROOT / "Rivals2/PipelineReports"
ALL_STEPS = ("setup", "usmap", "export", "check", "render", "gifs")
DEFAULT_STEPS = ("usmap", "export", "check", "render")
EXPORT_KINDS = exporter.STEPS  # json, anims, meshes, textures
EXPORTER_SOURCES = (exporter.TOOL_DIR / "Program.cs", exporter.TOOL_PROJECT)
# Moves the render step skips unless --no-default-skips: regular expressions
# matched case-insensitively against the attack asset name (ATT_Lox_Intro).
# Intros and taunts carry no hit/hurt data worth a wiki page; *_OLD assets are
# leftovers the game no longer uses (ATT_Orc_LedgeSpecial_OLD).
DEFAULT_SKIPS = ("Intro", "Taunt", "_OLD$")
try:  # render_settings.json "pipeline.default_skips" overrides the list above
    DEFAULT_SKIPS = tuple(json.loads((ROOT / "render_settings.json").read_text(encoding="utf-8"))
                          .get("pipeline", {}).get("default_skips", DEFAULT_SKIPS))
except (OSError, ValueError):
    pass

# Always run project scripts with the project venv when there is one.
_venv = ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
PYTHON = str(_venv) if _venv.is_file() else sys.executable


def fmt_time(seconds):
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


# ------------------------------------------------------------------- display
class Display:
    """Pipeline progress: a block of step lines and bars redrawn in place.

    live=False prints plain, permanent lines instead (pipes, log files, CI).
    All methods are thread-safe; render/gif workers call them concurrently.
    """

    BAR_WIDTH = 24
    TASK_BAR_WIDTH = 14

    def __init__(self, steps, live):
        self.steps = [{"name": name, "status": "pending", "seconds": None, "note": ""} for name in steps]
        self.live = live
        self.lock = threading.RLock()
        self.current = None        # index of the running step
        self.step_start = None
        self.bar = None            # running step's bar: done, total, unit, extra, eta_done, eta_total
        self.tasks = {}            # name -> done, total, unit, detail, start
        self.drawn = 0             # lines of the block currently on screen
        self.start = time.monotonic()
        try:
            "█░✓▶·✗".encode(sys.stdout.encoding or "ascii")
            self.glyph = {"full": "█", "empty": "░", "done": "✓", "running": "▶", "pending": "·", "failed": "✗"}
        except (LookupError, UnicodeEncodeError):
            self.glyph = {"full": "#", "empty": "-", "done": "+", "running": ">", "pending": ".", "failed": "x"}
        if live:
            if os.name == "nt":
                os.system("")  # makes the Windows console interpret ANSI escape codes
            sys.stdout.write("\x1b[?25l")  # hide the cursor while redrawing
            self._stop = threading.Event()
            self._ticker = threading.Thread(target=self._tick, daemon=True)
            self._ticker.start()

    # -- public API --------------------------------------------------------
    def log(self, message=""):
        """A permanent line (printed above the live block)."""
        with self.lock:
            if self.live:
                self._erase()
                print(message, flush=True)
                self._draw()
            else:
                print(message, flush=True)

    def step_started(self, name):
        with self.lock:
            index = next(i for i, s in enumerate(self.steps) if s["name"] == name)
            self.current, self.step_start = index, time.monotonic()
            self.steps[index]["status"] = "running"
            self.bar, self.tasks = None, {}
            if not self.live:
                print(f"\n== step {index + 1}/{len(self.steps)}: {name} ==", flush=True)
            self._redraw()

    def step_finished(self, name, result):
        with self.lock:
            step = next(s for s in self.steps if s["name"] == name)
            step.update(status=result["status"], seconds=result.get("seconds"), note=result.get("note", ""))
            self.current, self.bar, self.tasks = None, None, {}
            if not self.live:
                print(f"== {name}: {result['status']} in {fmt_time(result.get('seconds'))}"
                      + (f" ({result['note']})" if result.get("note") else ""), flush=True)
            self._redraw()

    def set_bar(self, done, total, unit, extra="", eta_done=None, eta_total=None, eta=True):
        with self.lock:
            self.bar = {"done": done, "total": total, "unit": unit, "extra": extra, "eta": eta,
                        "eta_done": done if eta_done is None else eta_done,
                        "eta_total": total if eta_total is None else eta_total}
            self._redraw()

    def task(self, name, done=None, total=None, unit=None, detail=None):
        """Create or update a sub-task line (a character, an export kind)."""
        with self.lock:
            task = self.tasks.setdefault(name, {"done": 0, "total": None, "unit": "", "detail": "",
                                                "start": time.monotonic()})
            for key, value in (("done", done), ("total", total), ("unit", unit), ("detail", detail)):
                if value is not None:
                    task[key] = value
            self._redraw()

    def task_finished(self, name, summary):
        with self.lock:
            self.tasks.pop(name, None)
            if not self.live:
                print(f"  {summary}", flush=True)
            self._redraw()

    def close(self):
        with self.lock:
            if self.live:
                self._stop.set()
                self._erase()
                self.live = False
                sys.stdout.write("\x1b[?25h")  # show the cursor again
                sys.stdout.flush()

    def final_block(self):
        """The finished step list as plain lines (printed once at the end)."""
        return self._lines(width=10_000)

    # -- drawing -------------------------------------------------------------
    def _tick(self):
        while not self._stop.wait(0.5):  # keeps elapsed times and ETAs moving
            self._redraw()

    def _redraw(self):
        if self.live:
            with self.lock:
                self._erase()
                self._draw()

    def _erase(self):
        if self.drawn:
            sys.stdout.write(f"\x1b[{self.drawn}F\x1b[J")  # up to the block's first line, clear below
            self.drawn = 0

    def _draw(self):
        size = shutil.get_terminal_size((100, 24))
        lines = self._lines(size.columns - 1, max_lines=max(4, size.lines - 2))
        sys.stdout.write("".join(line + "\n" for line in lines))
        sys.stdout.flush()
        self.drawn = len(lines)

    def _bar(self, done, total, width):
        filled = int(width * min(done / total, 1.0)) if total else 0
        return "[" + self.glyph["full"] * filled + self.glyph["empty"] * (width - filled) + "]"

    def _lines(self, width, max_lines=10_000):
        now = time.monotonic()
        count = len(self.steps)
        head = f"Rivals 2 pipeline · {fmt_time(now - self.start)}"
        if self.current is not None:
            head += f" · step {self.current + 1} of {count}: {self.steps[self.current]['name']}"
        lines = [head]
        task_lines = []
        for i, step in enumerate(self.steps):
            mark = self.glyph["failed"] if step["status"] in ("failed", "interrupted") else self.glyph.get(step["status"], self.glyph["done"])
            label = f" {mark} {i + 1}/{count} {step['name']:<7}"
            if step["status"] == "pending":
                lines.append(label)
            elif step["status"] == "running":
                text = label + f" {fmt_time(now - self.step_start):>8}"
                bar = self.bar
                if bar and bar["total"]:
                    text += (f"  {self._bar(bar['done'], bar['total'], self.BAR_WIDTH)}"
                             f" {100 * bar['eta_done'] / max(bar['eta_total'], 1):3.0f}%")
                    if bar["eta"] and 0 < bar["eta_done"] < bar["eta_total"]:
                        elapsed = now - self.step_start
                        text += f"  ETA {fmt_time(elapsed * (bar['eta_total'] - bar['eta_done']) / bar['eta_done'])}"
                    text += f"  {bar['done']}/{bar['total']} {bar['unit']}"
                    if bar["extra"]:
                        text += f" · {bar['extra']}"
                lines.append(text)
                for name, task in self.tasks.items():
                    task_text = f"     {name:<11}"
                    if task["total"]:
                        task_text += (f" {self._bar(task['done'], task['total'], self.TASK_BAR_WIDTH)}"
                                      f" {task['done']:>4}/{task['total']:<4} {task['unit']:<7}")
                    task_text += f" {fmt_time(now - task['start']):>7}  {task['detail']}"
                    task_lines.append(task_text)
                lines.append(None)  # where the task lines go
            else:
                lines.append(f"{label} {step['status']:<11} {fmt_time(step['seconds']):>8}  {step['note']}")
        room = max_lines - (len(lines) - 1)
        if len(task_lines) > room:
            hidden = len(task_lines) - (room - 1)
            task_lines = task_lines[:room - 1] + [f"     … {hidden} more"]
        out = []
        for line in lines:
            out.extend(task_lines if line is None else [line])
        return [line[:width] for line in out]


DISPLAY: Display | None = None
RUNNING: set = set()  # child processes, stopped on Ctrl+C
RUNNING_LOCK = threading.Lock()


INTERRUPTED = threading.Event()


def stop_children():
    INTERRUPTED.set()
    with RUNNING_LOCK:
        for process in list(RUNNING):
            try:
                process.terminate()
            except OSError:
                pass


def say(message=""):
    if DISPLAY:
        DISPLAY.log(message)
    else:
        print(message, flush=True)


# ------------------------------------------------------------------- helpers
def run_logged(command, log_path, echo=False, on_line=None, on_progress=None):
    """Run a command, writing its combined output to log_path.

    echo           also print each line (plain mode only)
    on_line        called with (line, seconds since start) for each line
    on_progress    called with the fields of exporter "PROGRESS ..." lines,
                   which are kept out of the log
    Returns (exit code, seconds).
    """
    command = [str(part) for part in command]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    start = time.monotonic()
    with open(log_path, "w", encoding="utf-8", buffering=1) as log:  # line-buffered: tail -f works
        log.write("$ " + shlex.join(command) + "\n\n")
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                   errors="replace", bufsize=1)
        with RUNNING_LOCK:
            RUNNING.add(process)
        try:
            for line in process.stdout:
                if line.startswith("PROGRESS "):
                    if on_progress:
                        on_progress(line.split())
                    continue
                log.write(line)
                line = line.rstrip("\n")
                if echo and not (DISPLAY and DISPLAY.live):
                    say("    " + line)
                if on_line:
                    on_line(line, time.monotonic() - start)
            code = process.wait()
        finally:
            with RUNNING_LOCK:
                RUNNING.discard(process)
    return code, time.monotonic() - start


def plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def relative(path):
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def renderable_characters():
    """Character folders rivals2_move_boxes.py can render (attacks + skeleton)."""
    if not CHARACTERS.is_dir():
        return []
    return sorted(p.name for p in CHARACTERS.iterdir()
                  if p.is_dir() and any((p / "Attacks").glob("ATT_*.json"))
                  and any((p / "Animation").glob("*Skeleton.json")))


def skipped(stem, patterns):
    return any(re.search(pattern, stem, re.IGNORECASE) for pattern in patterns)


def skipped_moves(character, patterns):
    """Attack assets the render step skips for a character, with the pattern
    that matched each, for the report."""
    rows = []
    for path in sorted((CHARACTERS / character / "Attacks").glob("ATT_*.json")):
        pattern = next((p for p in patterns if re.search(p, path.stem, re.IGNORECASE)), None)
        if pattern:
            rows.append({"move": path.stem, "pattern": pattern, "variants": count_move_variants_for(path)})
    return rows


def count_move_variants_for(path):
    """Variants (folders) one attack asset would render: see count_move_variants."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    attack = next((e for e in data if str(e.get("Type", "")).startswith("ATT_")), None)
    if not attack:
        return 0
    props = attack.get("Properties", {}) or {}
    refs = [key for key in ("GroundedAnimation", "AerialAnimation")
            if isinstance(props.get(key), dict)
            and props[key].get("ObjectPath", "").rsplit("/", 1)[-1].split(".", 1)[0]]
    return len(refs) or 1


def count_move_variants(character, skips=()):
    """How many move folders rivals2_move_boxes.py will write for a character.

    Mirrors its loop: every ATT_*.json with an ATT_ export gives one variant
    per Grounded/Aerial animation reference, or one "Default" variant.
    Attack assets matching a --skip pattern are left out, as the renderer does.
    """
    return sum(count_move_variants_for(path)
               for path in sorted((CHARACTERS / character / "Attacks").glob("ATT_*.json"))
               if not skipped(path.stem, skips))


def exporter_needs_build():
    """Why the exporter must be (re)built before exporting, or None."""
    if not exporter.TOOL_DLL.is_file():
        return f"{relative(exporter.TOOL_DLL)} is not built yet"
    built = exporter.TOOL_DLL.stat().st_mtime
    newer = [p.name for p in EXPORTER_SOURCES if p.is_file() and p.stat().st_mtime > built]
    if newer:
        return f"{', '.join(newer)} changed since the exporter was built"
    return None


# --------------------------------------------------------------------- steps
def step_setup(args, report, logs):
    DISPLAY.task("build", detail="fetching CUE4Parse and building (first time takes a few minutes)")
    code, seconds = run_logged([PYTHON, "export_characters_from_pak.py", "setup"], logs / "setup.log",
                               echo=True, on_line=lambda line, _t: line.strip() and DISPLAY.task(
                                   "build", detail=line.strip()))
    DISPLAY.task_finished("build", f"setup exited {code}")
    ok = code == 0 and exporter.TOOL_DLL.is_file()
    natives = list(exporter.TOOL_BIN.glob("CUE4Parse-Natives.*"))
    note = "" if natives else "CUE4Parse-Natives missing: ACL animations will fail"
    return {"status": "ok" if ok else "failed", "seconds": seconds, "exit_code": code,
            "note": note, "log": relative(logs / "setup.log")}


EXPORT_SUMMARY = re.compile(r"\[(\w+)\] (?:wrote (\d+), skipped (\d+) existing, of (\d+)"
                            r"|exported (\d+) of (\d+) objects.*skipped (\d+)"
                            r"|nothing to export \((\d+) already present\))")
EXPORT_FAILURES = re.compile(r"^(\d+) failures:")
MOUNTED = re.compile(r"^Mounted (\d+) files")
PHASES = {"write": ("packages", "writing json"), "load": ("packages", "reading packages"),
          "export": ("objects", "converting")}


def step_usmap(args, report, logs):
    """Generate a .usmap for the installed game build if there isn't one yet."""
    if args.usmap:
        return {"status": "skipped", "seconds": 0, "note": f"using --usmap {relative(args.usmap)}"}
    if not exporter.USMAPGEN_BIN.is_file():
        return {"status": "skipped", "seconds": 0,
                "note": f"usmapgen not built (setup builds it); using {exporter.default_usmap().name}"}
    DISPLAY.task("usmapgen", detail="reading the game executable")
    log = logs / "usmap.log"
    code, seconds = run_logged([PYTHON, "export_characters_from_pak.py", "usmap", "--pak", args.resolved_pak],
                               log, echo=True)
    DISPLAY.task_finished("usmapgen", f"usmap exited {code}")
    chosen = exporter.default_usmap()
    report["usmap"] = file_info(chosen)
    return {"status": "ok" if code == 0 else "failed", "seconds": seconds, "exit_code": code,
            "log": relative(log), "note": f"mappings: {chosen.name}"}


def step_export(args, report, logs):
    kinds = []
    DISPLAY.set_bar(0, len(EXPORT_KINDS), "kinds", eta=False)  # kinds take very different times
    for index, kind in enumerate(EXPORT_KINDS):
        command = [PYTHON, "export_characters_from_pak.py", "export", "--steps", kind, "--no-check",
                   "--progress", "--pak", args.resolved_pak, "--usmap", args.usmap or exporter.default_usmap()]
        for character in args.character:
            command += ["--character", character]
        if not args.keep_existing:
            command.append("--overwrite")
        if args.all_skins:
            command.append("--all-skins")
        row = {"kind": kind, "written": None, "skipped": None, "failures": 0}

        def on_line(line, t, row=row, kind=kind):
            line = line.strip()
            match = EXPORT_SUMMARY.search(line)
            if match:
                g = match.groups()
                if g[1] is not None:
                    row["written"], row["skipped"] = int(g[1]), int(g[2])
                elif g[4] is not None:
                    row["written"], row["skipped"] = int(g[4]), int(g[6])
                else:
                    row["written"], row["skipped"] = 0, int(g[7])
            match = EXPORT_FAILURES.match(line)
            if match:
                row["failures"] = int(match.group(1))
            match = MOUNTED.match(line)
            if match:
                row["mounted_after"] = round(t, 1)
                DISPLAY.task(kind, detail=f"mounted {int(match.group(1)):,} files")

        def on_progress(fields, kind=kind):
            # PROGRESS <kind> <phase> <done> <total>
            if len(fields) == 5:
                unit, label = PHASES.get(fields[2], ("items", fields[2]))
                DISPLAY.task(kind, done=int(fields[3]), total=int(fields[4]), unit=unit, detail=label)

        DISPLAY.task(kind, detail="mounting the pak")
        log = logs / f"export_{kind}.log"
        code, seconds = run_logged(command, log, echo=args.verbose, on_line=on_line, on_progress=on_progress)
        row.update(seconds=seconds, exit_code=code, log=relative(log),
                   status="ok" if code == 0 else "partial" if code == 3 else "failed")
        DISPLAY.task_finished(kind, f"export {kind}: {row['status']} in {fmt_time(seconds)} "
                                    f"(written {row['written']}, skipped {row['skipped']}, "
                                    f"failures {row['failures']})")
        kinds.append(row)
        DISPLAY.set_bar(index + 1, len(EXPORT_KINDS), "kinds",
                        extra=", ".join(f"{k['kind']} {fmt_time(k['seconds'])}" for k in kinds), eta=False)
        if row["status"] == "failed":
            say(f"Exporting {kind} failed; see {row['log']}. Stopping the export step.")
            break
    status = ("failed" if any(k["status"] == "failed" for k in kinds) else
              "partial" if any(k["status"] == "partial" for k in kinds) else "ok")
    total_fail = sum(k["failures"] for k in kinds)
    return {"status": status, "seconds": sum(k["seconds"] for k in kinds), "kinds": kinds,
            "note": f"{total_fail} per-file failures (see logs)" if total_fail else ""}


def step_check(args, report, logs):
    command = [PYTHON, "export_characters_from_pak.py", "check"]
    for character in args.character:
        command += ["--character", character]
    lines = []
    DISPLAY.task("check", detail="reading exported inputs")
    code, seconds = run_logged(command, logs / "check.log", echo=True,
                               on_line=lambda line, _t: lines.append(line))
    DISPLAY.task_finished("check", f"check exited {code}")
    problems = [line.split()[0] for line in lines
                if line[:1].isalpha() and not line.startswith(("Renderer", "character", "Notes"))
                and len(line.split()) > 1]
    noted = sorted({lines[i - 1].split()[0] for i, line in enumerate(lines)
                    if line.strip().startswith("- ") and i and lines[i - 1][:1].isalpha()})
    # check exits 1 when it has notes (missing anims etc.); that's information, not a failure.
    return {"status": "ok" if code == 0 else "notes" if code == 1 else "failed",
            "seconds": seconds, "exit_code": code, "output": "\n".join(lines),
            "log": relative(logs / "check.log"),
            "note": (f"{plural(len(problems), 'character')}; inputs missing for {', '.join(noted)}"
                     if noted else f"{plural(len(problems), 'character')}, all inputs present")}


MOVE_LINE = re.compile(r"^(?P<path>.+): (?P<frames>\d+) frames(?P<unchanged> unchanged)?$")
WHY_LINE = re.compile(r"^(?P<path>.+): re-rendered \((?P<why>.*)\)$")
CHAR_TOTAL = re.compile(r": rendered (\d+) PNG frames across (\d+) move variants")
UNCHANGED_TOTAL = re.compile(r"; (\d+) unchanged \((\d+) frames kept\)")
OLD_FOLDERS = re.compile(r": folders with no matching move this run: (.*)$")


class Counter:
    """Step-wide totals shared by the worker threads."""

    def __init__(self, characters, units_total):
        self.lock = threading.Lock()
        self.characters, self.done_characters = characters, 0
        self.units_total, self.units_done, self.frames = units_total, 0, 0


def render_one(character, args, logs, out_root, totals, counter):
    out_dir = out_root / character
    if args.clean and out_dir.is_dir():
        shutil.rmtree(out_dir)
    command = [PYTHON, "rivals2_move_boxes.py", "--character", character, "--out", out_dir,
               "--camera-rotation", args.camera_rotation]
    if args.show_skeleton:
        command.append("--show-skeleton")
    for pattern in args.skips:
        command += ["--skip", pattern]
    if args.skin_textures:
        command.append("--skin-textures")  # implies --show-character
    elif args.show_character:
        command.append("--show-character")
    if not args.full_render:
        command.append("--changed-only")
    moves, last = [], [0.0]
    summary = {"unchanged": 0, "unchanged_frames": 0, "rerendered": [], "old_folders": []}
    reasons = {}
    seen = [0]
    DISPLAY.task(character, done=0, total=totals[character], unit="moves", detail="loading")

    def on_line(line, t):
        match = WHY_LINE.match(line)
        if match:
            reasons[Path(match["path"]).name] = match["why"]
            return
        match = MOVE_LINE.match(line)
        if match:
            frames = int(match["frames"])
            move = Path(match["path"]).name
            seen[0] += 1
            if match["unchanged"]:
                detail = f"{move} unchanged"
            else:
                moves.append({"move": move, "frames": frames, "seconds": round(t - last[0], 2)})
                if move in reasons:
                    summary["rerendered"].append({"move": move, "why": reasons[move]})
                detail = f"{move} ({frames} frames)"
                with counter.lock:
                    counter.frames += frames
            last[0] = t
            DISPLAY.task(character, done=seen[0], total=max(totals[character], seen[0]), detail=detail)
            with counter.lock:
                counter.units_done += 1
            update_render_bar(counter)
        match = CHAR_TOTAL.search(line)
        if match:
            summary["frames"], summary["moves"] = int(match.group(1)), int(match.group(2))
        match = UNCHANGED_TOTAL.search(line)
        if match:
            summary["unchanged"], summary["unchanged_frames"] = int(match.group(1)), int(match.group(2))
        match = OLD_FOLDERS.search(line)
        if match:
            summary["old_folders"] = [name.strip() for name in match.group(1).split(",")]

    log = logs / f"render_{character}.log"
    code, seconds = run_logged(command, log, on_line=on_line)
    missing_file = out_dir / "missing_animation_ueanim.txt"
    missing = ([n for n in missing_file.read_text(encoding="utf-8").split() if n]
               if missing_file.is_file() else [])
    status = "ok" if code == 0 else "interrupted" if INTERRUPTED.is_set() else "failed"
    row = {"character": character, "status": status, "exit_code": code,
           "seconds": seconds, "moves": summary.get("moves", len(moves)),
           "frames": summary.get("frames", sum(m["frames"] for m in moves)),
           "unchanged": summary["unchanged"], "unchanged_frames": summary["unchanged_frames"],
           "rerendered": summary["rerendered"], "old_folders": summary["old_folders"],
           "missing_anims": len(missing), "missing_anim_names": missing,
           "move_times": moves, "log": relative(log),
           "out": relative(out_dir)}
    with counter.lock:
        counter.done_characters += 1
        # Keep the ETA honest when the count was off (e.g. a crash mid-character).
        counter.units_done += totals[character] - seen[0] if code == 0 else 0
    update_render_bar(counter)
    DISPLAY.task_finished(character, f"{character}: {row['status']} in {fmt_time(seconds)} "
                                     f"({row['frames']} frames, {row['moves']} moves"
                                     + (f", {row['unchanged']} unchanged" if row["unchanged"] else "") + ")")
    if code != 0 and not INTERRUPTED.is_set():
        say(f"{character}: renderer exited with code {code}; see {row['log']}")
    return row


def update_render_bar(counter):
    with counter.lock:
        DISPLAY.set_bar(counter.done_characters, len(counter.characters), "characters",
                        extra=f"{counter.units_done}/{counter.units_total} moves · {counter.frames:,} frames",
                        eta_done=counter.units_done, eta_total=counter.units_total)


def run_per_character(func, characters, args):
    """Run func(character) with --jobs workers; results in character order."""
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(func, c): c for c in characters}
        try:
            for future in as_completed(futures):
                rows.append(future.result())
        except KeyboardInterrupt:
            # Ctrl+C in a terminal also reaches the children, but stop them
            # explicitly so the pool doesn't wait out whole characters.
            pool.shutdown(wait=False, cancel_futures=True)
            stop_children()
            raise
    order = {c: i for i, c in enumerate(characters)}
    return sorted(rows, key=lambda r: order[r["character"]])


def step_render(args, report, logs):
    characters = args.character or renderable_characters()
    if not characters:
        return {"status": "failed", "seconds": 0, "characters": [],
                "note": f"no renderable characters under {relative(CHARACTERS)}"}
    totals = {c: count_move_variants(c, args.skips) for c in characters}
    counter = Counter(characters, sum(totals.values()))
    update_render_bar(counter)
    skipped = {c: skipped_moves(c, args.skips) for c in characters}
    skipped_count = sum(row["variants"] for rows in skipped.values() for row in rows)
    if not DISPLAY.live:
        say(f"  rendering {plural(len(characters), 'character')} ({counter.units_total} moves), "
            f"{args.jobs} at a time -> {relative(args.frames_out)}")
    start = time.monotonic()
    rows = run_per_character(lambda c: render_one(c, args, logs, args.frames_out, totals, counter),
                             characters, args)
    failed = [r["character"] for r in rows if r["status"] != "ok"]
    frames = sum(r["frames"] for r in rows)
    note = f"{frames:,} frames, {sum(r['moves'] for r in rows)} moves"
    unchanged = sum(r["unchanged"] for r in rows)
    if not args.full_render:
        note = f"{sum(r['moves'] for r in rows)} moves re-rendered ({frames:,} frames), {unchanged} unchanged"
    if skipped_count:
        note += f" ({skipped_count} skipped, listed in the report)"
    return {"status": "failed" if failed else "ok", "seconds": time.monotonic() - start,
            "characters": rows, "skips": list(args.skips), "skipped_moves": skipped_count,
            "skipped": {c: rows for c, rows in skipped.items() if rows},
            "note": note + (f"; failed: {', '.join(failed)}" if failed else "")}


def gif_move_dirs(character, args):
    frames_dir = args.frames_out / character
    return (sorted(d for d in frames_dir.iterdir() if d.is_dir() and any(d.glob("*.png")))
            if frames_dir.is_dir() else [])


def gifs_one(character, args, logs, totals, counter):
    out_dir = args.gifs_out / character
    move_dirs = gif_move_dirs(character, args)
    extra = shlex.split(args.gif_args or "")
    suffix = {"apng": ".png", "webm": ".webm"}.get(next(
        (extra[i + 1] for i, a in enumerate(extra[:-1]) if a == "--format"), "gif"), ".gif")
    log = logs / f"gifs_{character}.log"
    created = failed = skipped_gifs = 0
    start = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    DISPLAY.task(character, done=0, total=len(move_dirs), unit="moves", detail="")
    with open(log, "w", encoding="utf-8") as handle:
        for done, move_dir in enumerate(move_dirs, 1):
            out_dir.mkdir(parents=True, exist_ok=True)
            destination = out_dir / (move_dir.name + suffix)
            newest_png = max((p.stat().st_mtime for p in move_dir.glob("*.png")), default=0)
            if (not args.full_render and destination.is_file()
                    and destination.stat().st_mtime >= newest_png):
                skipped_gifs += 1  # its frames haven't been re-rendered since
                DISPLAY.task(character, done=done, detail=f"{move_dir.name} unchanged")
                with counter.lock:
                    counter.units_done += 1
                update_gif_bar(counter)
                continue
            command = [PYTHON, "make_attack_gif.py", move_dir, "--out", destination, *extra]
            result = subprocess.run([str(c) for c in command], cwd=ROOT, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace")
            handle.write(f"$ {shlex.join(str(c) for c in command)}\n{result.stdout}{result.stderr}\n")
            if result.returncode == 0:
                created += 1
            else:
                failed += 1
            DISPLAY.task(character, done=done, detail=move_dir.name)
            with counter.lock:
                counter.units_done += 1
            update_gif_bar(counter)
    with counter.lock:
        counter.done_characters += 1
    update_gif_bar(counter)
    DISPLAY.task_finished(character, f"{character}: {created} created, {skipped_gifs} unchanged, {failed} failed "
                                     f"in {fmt_time(time.monotonic() - start)}")
    return {"character": character, "status": "ok" if not failed else "failed",
            "seconds": time.monotonic() - start, "created": created, "failed": failed, "unchanged": skipped_gifs,
            "log": relative(log), "out": relative(out_dir)}


def update_gif_bar(counter):
    with counter.lock:
        DISPLAY.set_bar(counter.done_characters, len(counter.characters), "characters",
                        extra=f"{counter.units_done}/{counter.units_total} moves",
                        eta_done=counter.units_done, eta_total=counter.units_total)


def step_gifs(args, report, logs):
    characters = args.character
    if not characters and args.frames_out.is_dir():
        characters = sorted(p.name for p in args.frames_out.iterdir() if p.is_dir())
    if not characters:
        return {"status": "failed", "seconds": 0, "characters": [],
                "note": f"no rendered characters under {relative(args.frames_out)}"}
    totals = {c: len(gif_move_dirs(c, args)) for c in characters}
    counter = Counter(characters, sum(totals.values()))
    update_gif_bar(counter)
    if not DISPLAY.live:
        say(f"  making animations for {len(characters)} characters -> {relative(args.gifs_out)}")
    start = time.monotonic()
    rows = run_per_character(lambda c: gifs_one(c, args, logs, totals, counter), characters, args)
    failed = sum(r["failed"] for r in rows)
    note = f"{sum(r['created'] for r in rows)} created" + (f", {failed} failed" if failed else "")
    return {"status": "failed" if failed else "ok", "seconds": time.monotonic() - start,
            "characters": rows, "note": note}


STEP_FUNCS = {"setup": step_setup, "usmap": step_usmap, "export": step_export, "check": step_check,
              "render": step_render, "gifs": step_gifs}


# -------------------------------------------------------------------- report
def file_info(path):
    path = Path(path)
    if not path.is_file():
        return {"path": relative(path), "exists": False}
    stat = path.stat()
    return {"path": relative(path), "exists": True, "size_gb": round(stat.st_size / 1e9, 2),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")}


def md_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def write_report(report, report_dir):
    (report_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    out = [f"# Pipeline run {report['started']}", ""]
    out.append(f"- **Status:** {report['status']}")
    out.append(f"- **Total time:** {fmt_time(report.get('seconds'))}")
    out.append(f"- **Finished:** {report.get('finished', '-')}")
    out.append(f"- **Command:** `{report['command']}`")
    if {"usmap", "export"} & set(report.get("steps_requested", [])):
        out.append(f"- **Pak:** {report['pak']['path']}" + (
            f" ({report['pak']['size_gb']} GB, modified {report['pak']['modified']})"
            if report['pak'].get('exists') else " (missing)"))
        if report["pak"].get("found_via"):
            out.append(f"- **Found via:** {report['pak']['found_via']}"
                       + (f", Steam build {report['pak']['steam_build']}" if report["pak"].get("steam_build") else ""))
        out.append(f"- **Mappings:** {report['usmap']['path']}")
    else:
        out.append("- **Game data:** already exported (this run didn't read the pak)")
    out.append(f"- **Host:** {report['host']}")
    out.append("")
    out.append("## Steps")
    out.append("")
    out.append(md_table(["step", "status", "time", "notes"],
                        [(name, s["status"], fmt_time(s.get("seconds")), s.get("note", ""))
                         for name, s in report["steps"].items()]))
    export = report["steps"].get("export")
    if export and export.get("kinds"):
        out += ["", "## Export", "",
                md_table(["kind", "status", "time", "written", "skipped", "failures", "log"],
                         [(k["kind"], k["status"], fmt_time(k["seconds"]), k["written"], k["skipped"],
                           k["failures"], k["log"]) for k in export["kinds"]])]
    check = report["steps"].get("check")
    if check and check.get("output"):
        out += ["", "## Input check", "", "```", check["output"].strip(), "```"]
    render = report["steps"].get("render")
    if render and render.get("characters"):
        rows = render["characters"]
        out += ["", "## Render", "",
                md_table(["character", "status", "time", "moves rendered", "frames", "frames/s",
                          "unchanged", "missing anims", "log"],
                         [(r["character"], r["status"], fmt_time(r["seconds"]), r["moves"], r["frames"],
                           f"{r['frames'] / r['seconds']:.1f}" if r["seconds"] and r["frames"] else "-",
                           r.get("unchanged", 0), r["missing_anims"], r["log"]) for r in rows])]
        out.append("")
        out.append(f"Totals: {sum(r['moves'] for r in rows)} moves, {sum(r['frames'] for r in rows)} frames. "
                   "Character times are wall-clock; with --jobs above 1 they overlap.")
        rerendered = [(r["character"], m["move"], m["why"]) for r in rows for m in r.get("rerendered", [])]
        if rerendered:
            first_time = [row for row in rerendered if row[2] == "no previous render"]
            out += ["", "### Re-rendered moves", "",
                    "Moves whose inputs changed since their last render (`render_inputs.json` in each move folder), "
                    "and what changed:", ""]
            listed = [row for row in rerendered if row[2] != "no previous render"]
            if listed:
                out.append(md_table(["character", "move", "changed"], listed))
            if first_time:
                out += ["", f"{len(first_time)} moves had no previous render and were rendered for the first time."]
        old = [(r["character"], name) for r in rows for name in r.get("old_folders", [])]
        if old:
            out += ["", "### Old move folders", "",
                    "Folders in the output that no move produced this run: moves a patch removed or renamed, "
                    "or moves now skipped. They were left alone; delete them or run with `--clean`.", "",
                    md_table(["character", "folder"], old)]
        if render.get("skipped"):
            out += ["", "### Skipped moves", "",
                    f"Not rendered because their names match a skip pattern ({', '.join(f'`{p}`' for p in render.get('skips', []))}; "
                    "`--no-default-skips` renders them, `pipeline.default_skips` in render_settings.json sets the list):", "",
                    md_table(["character", "attack asset", "variants", "matched"],
                             [(c, m["move"], m["variants"], f"`{m['pattern']}`")
                              for c, moves in render["skipped"].items() for m in moves])]
        missing = [(r["character"], name) for r in rows for name in r.get("missing_anim_names", [])]
        if missing:
            out += ["", "### Missing animations", "",
                    "These .ueanim files are referenced by attacks but weren't found, so those moves were "
                    "rendered in the reference pose. Re-export (`export --steps anims`) or check the character "
                    "folder; a reference to another character's animation also shows up here.", "",
                    md_table(["character", "animation"], missing)]
        slow = sorted(((m, r["character"]) for r in rows for m in r["move_times"]),
                      key=lambda x: x[0]["seconds"], reverse=True)[:15]
        if slow:
            out += ["", "### Slowest moves", "",
                    md_table(["character", "move", "frames", "time"],
                             [(c, m["move"], m["frames"], fmt_time(m["seconds"])) for m, c in slow])]
    gifs = report["steps"].get("gifs")
    if gifs and gifs.get("characters"):
        out += ["", "## Animations", "",
                md_table(["character", "status", "time", "created", "unchanged", "failed", "log"],
                         [(r["character"], r["status"], fmt_time(r["seconds"]), r["created"],
                           r.get("unchanged", 0), r["failed"], r["log"]) for r in gifs["characters"]])]
    (report_dir / "report.md").write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------- main
def parse_steps(value):
    steps = [s.strip() for s in value.split(",") if s.strip()]
    bad = [s for s in steps if s not in ALL_STEPS]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown step(s) {', '.join(bad)}; choose from {', '.join(ALL_STEPS)}")
    return steps


def main():
    global DISPLAY
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--steps", type=parse_steps, default=list(DEFAULT_STEPS),
                        help=f"comma list from {','.join(ALL_STEPS)} (default: {','.join(DEFAULT_STEPS)})")
    parser.add_argument("--gifs", action="store_true", help="also run the gifs step")
    parser.add_argument("--character", action="append", default=[],
                        help="only this character folder (repeatable; default: all)")
    parser.add_argument("--jobs", type=int, default=1,
                        help="characters to render/animate at the same time (default: 1)")
    group = parser.add_argument_group("export")
    group.add_argument("--game-dir", type=Path,
                       help=f"Rivals 2 install folder (default: ${exporter.GAME_DIR_ENV}, else found through Steam)")
    group.add_argument("--pak", type=Path,
                       help="one specific .pak file instead (overrides --game-dir)")
    group.add_argument("--usmap", type=Path,
                       help="mappings file (default: the newest .usmap in the project root; the usmap step generates one per game build)")
    group.add_argument("--keep-existing", action="store_true",
                       help="skip files already on disk instead of replacing them")
    group.add_argument("--all-skins", action="store_true", help="also export non-default skins")
    group = parser.add_argument_group("render (passed to rivals2_move_boxes.py)")
    group.add_argument("--frames-out", type=Path, default=DEFAULT_FRAMES,
                       help=f"frame output root (default: {relative(DEFAULT_FRAMES)})")
    group.add_argument("--full-render", action="store_true",
                       help="re-render every move (and remake every GIF). By default only moves whose "
                            "inputs changed are re-rendered: game data, animations, articles, mesh and "
                            "textures, render_settings.json, article rules, renderer code or options")
    group.add_argument("--clean", action="store_true",
                       help="delete each character's old frame folder before rendering, so moves "
                            "removed by a patch don't linger")
    group.add_argument("--show-skeleton", action="store_true", help="draw the skeleton too")
    group.add_argument("--skin-textures", action=argparse.BooleanOptionalAction, default=True,
                       help="render the textured default-skin mesh under the boxes (default: on). "
                            "--no-skin-textures renders boxes only, about 6x faster")
    group.add_argument("--show-character", action="store_true",
                       help="with --no-skin-textures: plain untextured mesh under the boxes")
    group.add_argument("--camera-rotation", type=float, default=90)
    group.add_argument("--skip", action="append", default=[], metavar="REGEX",
                       help="also skip attack assets whose name matches this case-insensitive "
                            "regular expression (repeatable), e.g. --skip Pummel")
    group.add_argument("--no-default-skips", action="store_true",
                       help=f"render the moves skipped by default too ({', '.join(DEFAULT_SKIPS)})")
    group = parser.add_argument_group("gifs (passed to make_attack_gif.py)")
    group.add_argument("--gifs-out", type=Path, default=DEFAULT_GIFS,
                       help=f"animation output root (default: {relative(DEFAULT_GIFS)})")
    group.add_argument("--gif-args", default="",
                       help="extra make_attack_gif.py options as one string; write it with '=' "
                            "so it isn't read as an option: --gif-args=--no-dither")
    group = parser.add_argument_group("output")
    group.add_argument("--report-dir", type=Path,
                       help=f"where report.md/json and logs go (default: {relative(DEFAULT_REPORTS)}/<date_time>)")
    group.add_argument("--no-progress", action="store_true",
                       help="print plain lines instead of the live progress display "
                            "(automatic when output isn't a terminal)")
    group.add_argument("--verbose", action="store_true",
                       help="echo the exporter's output (implies --no-progress)")
    args = parser.parse_args()

    args.skips = ([] if args.no_default_skips else list(DEFAULT_SKIPS)) + args.skip
    args.full_render = args.full_render or args.clean  # --clean deletes the old frames anyway
    steps = list(args.steps) + (["gifs"] if args.gifs and "gifs" not in args.steps else [])
    notices = []
    if "export" in steps and "setup" not in steps:
        reason = exporter_needs_build()
        if reason:
            notices.append(f"{reason}; adding the setup step.")
            steps.insert(0, "setup")
    if "export" not in steps and "usmap" in steps and "usmap" not in args.steps:
        steps.remove("usmap")
    steps = [s for s in ALL_STEPS if s in steps]  # always run in pipeline order
    if args.clean:
        frames_root = args.frames_out.resolve()
        if ROOT not in frames_root.parents:
            parser.error("--clean only works with a --frames-out folder inside the project")

    # Find the game once, so every export step reads the same pak and the
    # report records which install/build it was. Steps that don't export
    # don't need it, so a missing install only stops export runs.
    args.resolved_pak, pak_info = None, {"path": "-", "exists": False}
    try:
        args.resolved_pak, source, build = exporter.locate_pak(args.pak, args.game_dir)
        pak_info = dict(file_info(args.resolved_pak), found_via=source, steam_build=build,
                        path=str(args.resolved_pak))
    except SystemExit as exc:
        if "export" in steps or "usmap" in steps:
            parser.error(str(exc))

    started = datetime.now()
    report_dir = args.report_dir or DEFAULT_REPORTS / started.strftime("%Y-%m-%d_%H%M%S")
    logs = report_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    report = {"started": started.isoformat(timespec="seconds"), "status": "running",
              "command": shlex.join(["python", *sys.argv]), "steps_requested": steps,
              "pak": pak_info, "usmap": file_info(args.usmap or exporter.default_usmap()),
              "host": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs, "
                      f"Python {platform.python_version()}", "steps": {}}

    for notice in notices:
        print(notice)
    if args.resolved_pak and "export" in steps:
        print(f"Game: {args.resolved_pak}" + (f" (Steam build {pak_info['steam_build']})"
                                               if pak_info.get("steam_build") else ""))
    if "render" in steps:
        print("Render: " + ("textured default skin (--skin-textures)" if args.skin_textures else
                            "untextured mesh" if args.show_character else "boxes only")
              + (f"; skipping moves matching {', '.join(args.skips)}" if args.skips else ""))
    print(f"Logs and report: {relative(report_dir)}   (full output of each step is in logs/)")
    live = (sys.stdout.isatty() and not args.no_progress and not args.verbose
            and os.environ.get("TERM") != "dumb")
    DISPLAY = Display(steps, live)
    clock = time.monotonic()
    exit_code = 0
    running = None
    try:
        for name in steps:
            running = name
            DISPLAY.step_started(name)
            result = STEP_FUNCS[name](args, report, logs)
            report["steps"][name] = result
            DISPLAY.step_finished(name, result)
            running = None
            write_report(report, report_dir)  # keep a partial report if a later step dies
            if result["status"] == "failed":
                exit_code = 1
                if name in ("setup", "export"):
                    say("Stopping: later steps would use stale or missing inputs.")
                    break
        report["status"] = "failed" if exit_code else "ok"
    except KeyboardInterrupt:
        stop_children()
        report["status"] = "interrupted"
        exit_code = 130
        if running:
            result = {"status": "interrupted", "seconds": time.monotonic() - DISPLAY.step_start,
                      "note": "stopped with Ctrl+C; partial results in logs/"}
            report["steps"][running] = result
            DISPLAY.step_finished(running, result)
    finally:
        DISPLAY.close()
        report["seconds"] = time.monotonic() - clock
        report["finished"] = datetime.now().isoformat(timespec="seconds")
        write_report(report, report_dir)
    print("\n".join(DISPLAY.final_block()))
    print(f"\nPipeline {report['status']} in {fmt_time(report['seconds'])}. "
          f"Report: {relative(report_dir / 'report.md')}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

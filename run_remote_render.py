#!/usr/bin/env python3
"""Render hitbox frames on another machine (e.g. a Mac Studio) over SSH.

Export stays on this machine (it needs the game install and usmapgen); only
rendering, the slow part, runs remotely. Steps, each timed:

  1. push    rsync the scripts, settings files, exported game data
             (Rivals2/Content/Characters) and the existing frames
             (Rivals2/HitboxFrames, so the remote --changed-only check knows
             what's already rendered) into the remote folder. Only changed
             files are sent after the first run.
  2. venv    create .venv there with numpy and Pillow if it doesn't work yet.
  3. render  run_pipeline.py --steps render on the remote machine, with its
             live progress display shown here.
  4. pull    rsync the frames (and GIFs with --gifs) back, plus the run's
             report, which gets a "Remote run" section with these timings.

Defaults come from the "remote" section of render_settings.json (host,
folder, Python, jobs) and can be overridden with flags. Any other options
(--full-render, --clean, --character, --no-skin-textures, --skip, ...) are
passed to run_pipeline.py unchanged.

Needs ssh and rsync on this machine (Linux/macOS) and key-based SSH login to
the remote one. Ctrl+C stops the remote render; the frames finished so far
are still pulled back (a second Ctrl+C skips that).

Examples:
  python run_remote_render.py                       # render changed moves on the Mac
  python run_remote_render.py --full-render         # re-render everything there
  python run_remote_render.py --character Absa --gifs
  python run_remote_render.py --host other-mac --jobs 10
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / "render_settings.json"
CODE_FILES = ("run_pipeline.py", "export_characters_from_pak.py", "rivals2_move_boxes.py",
              "make_attack_gif.py", "requirements.txt", "render_settings.json", "article_rules.json")
DATA = "Rivals2/Content/Characters"
FRAMES = "Rivals2/HitboxFrames"
GIFS = "Rivals2/GIFs"
REPORTS = "Rivals2/PipelineReports"
DEFAULTS = {"host": "smackers", "dir": "~/projects/rivals2-render",
            "python": "/opt/homebrew/bin/python3", "jobs": None}


def remote_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **{k: v for k, v in (data.get("remote") or {}).items() if not k.startswith("_")}}


def fmt_time(seconds):
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{seconds:.1f}s"


def ssh(host, command, tty=False, capture=False):
    args = ["ssh", *(["-t"] if tty else ["-o", "BatchMode=yes"]), host, command]
    if capture:
        return subprocess.run(args, capture_output=True, text=True)
    return subprocess.run(args)


def folder_size(path):
    total = sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
    return f"{total / 1e9:.1f} GB" if total >= 1e9 else f"{total / 1e6:.0f} MB"


def rsync(source, destination, delete=False, excludes=(), label=""):
    """rsync -a over ssh (works with macOS's openrsync on the other end).
    On a terminal, shows one updating progress line (local rsync 3.1+)."""
    if label:
        print(f"  {label}", flush=True)
    progress = ["--info=progress2", "--human-readable"] if sys.stdout.isatty() else []
    command = ["rsync", "-a", "-e", "ssh", *progress, *(["--delete"] if delete else []),
               *(f"--exclude={pattern}" for pattern in excludes), source, destination]
    result = subprocess.run(command)
    if result.returncode != 0:
        raise SystemExit(f"rsync failed ({result.returncode}): {' '.join(command)}")


def main():
    config = remote_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=config["host"],
                        help=f"SSH host to render on (default: {config['host']}, from render_settings.json remote.host)")
    parser.add_argument("--remote-dir", default=config["dir"],
                        help=f"folder on the remote machine (default: {config['dir']})")
    parser.add_argument("--remote-python", default=config["python"],
                        help=f"Python used to create the remote venv (default: {config['python']})")
    parser.add_argument("--jobs", type=int, default=config["jobs"],
                        help="characters rendered at once there (default: remote CPU count minus 1)")
    parser.add_argument("--gifs", action="store_true", help="also make GIFs there and pull them back")
    parser.add_argument("--no-push-frames", action="store_true",
                        help="don't send this machine's frames first (the remote copy from the last run is used)")
    args, passthrough = parser.parse_known_args()
    if any(arg == "--steps" or arg.startswith("--steps=") for arg in passthrough):
        parser.error("--steps can't be used here: export runs on this machine; this renders only")
    if any(arg == "--report-dir" or arg.startswith("--report-dir=") for arg in passthrough):
        parser.error("--report-dir is set by this script")

    host = args.host
    timings = {}
    start_all = time.monotonic()

    # The remote folder, as an absolute path (~ expanded by the remote shell).
    folder = args.remote_dir
    shell_folder = ('"$HOME"/' + shlex.quote(folder[2:])) if folder.startswith("~/") else shlex.quote(folder)
    probe = ssh(host, f"mkdir -p {shell_folder}/{FRAMES} {shell_folder}/{DATA} {shell_folder}/{REPORTS} "
                      f"&& cd {shell_folder} && pwd && (sysctl -n hw.ncpu 2>/dev/null || getconf _NPROCESSORS_ONLN)",
                capture=True)
    if probe.returncode != 0:
        raise SystemExit(f"Can't reach {host} over SSH (key login needed): {probe.stderr.strip()}")
    remote_dir, cpus = probe.stdout.split()[:2]
    jobs = args.jobs or max(1, int(cpus) - 1)
    target = f"{host}:{remote_dir}"
    print(f"Remote: {host}:{remote_dir} ({cpus} CPUs, --jobs {jobs})")

    # 1. push
    started = time.monotonic()
    print("\n== push: scripts, settings, exported data" + ("" if args.no_push_frames else ", existing frames"))
    code_files = [str(ROOT / name) for name in CODE_FILES if (ROOT / name).is_file()]
    if subprocess.run(["rsync", "-a", "-e", "ssh", *code_files, f"{target}/"]).returncode != 0:
        raise SystemExit("rsync of the scripts failed")
    print("  (only files that differ from the remote copy are sent; the first push sends everything)")
    rsync(f"{ROOT / DATA}/", f"{target}/{DATA}/", delete=True,
          label=f"exported data, {folder_size(ROOT / DATA)}")
    if not args.no_push_frames and (ROOT / FRAMES).is_dir():
        # The remote frames mirror this machine's, so its fingerprints
        # (render_inputs.json) decide what's already up to date.
        rsync(f"{ROOT / FRAMES}/", f"{target}/{FRAMES}/", delete=True,
              label=f"existing frames, {folder_size(ROOT / FRAMES)}")
    timings["push"] = time.monotonic() - started

    # 2. venv
    started = time.monotonic()
    check = ssh(host, f"cd {shlex.quote(remote_dir)} && .venv/bin/python -c 'import numpy, PIL'", capture=True)
    if check.returncode != 0:
        print(f"\n== venv: creating {remote_dir}/.venv with {args.remote_python}")
        result = ssh(host, f"cd {shlex.quote(remote_dir)} && {shlex.quote(args.remote_python)} -m venv .venv "
                           f"&& .venv/bin/pip install -q -r requirements.txt")
        if result.returncode != 0:
            raise SystemExit("Creating the remote venv failed (see above); set remote.python in render_settings.json")
    timings["venv"] = time.monotonic() - started

    # 3. render
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S") + "-remote"
    report_dir = f"{REPORTS}/{stamp}"
    steps = "render,gifs" if args.gifs else "render"
    command = (f"cd {shlex.quote(remote_dir)} && .venv/bin/python run_pipeline.py --steps {steps} "
               f"--jobs {jobs} --report-dir {report_dir} {shlex.join(passthrough)}")
    print(f"\n== render on {host}\n$ {command}\n")
    started = time.monotonic()
    tty = sys.stdout.isatty()
    process = subprocess.Popen(["ssh", *(["-t"] if tty else []), host, command])
    pull = True
    try:
        render_code = process.wait()
    except KeyboardInterrupt:
        # Ctrl+C also reached the remote pipeline through the terminal; wait
        # for it to stop cleanly so its report and finished frames are kept.
        print("\nStopping the remote render... (Ctrl+C again to skip pulling frames back)")
        try:
            render_code = process.wait()
        except KeyboardInterrupt:
            process.kill()
            render_code, pull = 130, False
    timings["render"] = time.monotonic() - started

    # 4. pull
    if pull:
        started = time.monotonic()
        print(f"\n== pull: frames{' and GIFs' if args.gifs else ''} and the report")
        # No --delete: frames here that the remote run didn't touch stay as they
        # are (the report lists old folders no move produced).
        rsync(f"{target}/{FRAMES}/", f"{ROOT / FRAMES}/", label="frames (only new or changed ones)")
        if args.gifs:
            rsync(f"{target}/{GIFS}/", f"{ROOT / GIFS}/", label="GIFs")
        (ROOT / REPORTS).mkdir(parents=True, exist_ok=True)
        rsync(f"{target}/{report_dir}", f"{ROOT / REPORTS}/")
        timings["pull"] = time.monotonic() - started
    timings["total"] = time.monotonic() - start_all

    summary = [f"## Remote run", "",
               f"- **Rendered on:** {host}:{remote_dir} ({cpus} CPUs, --jobs {jobs})",
               f"- **Times:** " + ", ".join(f"{name} {fmt_time(seconds)}" for name, seconds in timings.items()),
               f"- **Remote exit code:** {render_code}", ""]
    local_report = ROOT / report_dir / "report.md"
    if local_report.is_file():
        local_report.write_text(local_report.read_text(encoding="utf-8").rstrip("\n") + "\n\n"
                                + "\n".join(summary), encoding="utf-8")
    print("\n" + "\n".join(line for line in summary if line))
    if local_report.is_file():
        print(f"Report: {local_report.relative_to(ROOT)}")
    return render_code


if __name__ == "__main__":
    sys.exit(main())

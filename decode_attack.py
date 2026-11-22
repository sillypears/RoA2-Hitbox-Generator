#!/usr/bin/env python3
"""
attack_info.py - summarise exported Rivals 2 attack assets (.uasset + .uexp pairs).

Usage:
    python attack_info.py <folder> [--csv out.csv] [--json out.json] [--verbose]

What it reads (reliable):
    - asset class / parent class, animations referenced, VFX paths, other FName values
What it infers (heuristic):
    - frame data: the int32s stored right after the "Startup" / "Active" /
      "Recovery" strings in the .uexp. Field names are NOT in the file (the game
      uses unversioned properties), so labels are inferred, not decoded. Matched
      to the Dragdown Wiki for Loxodont Bair/Bthrow/Dair (3 of 3, all columns
      below); other moves (specials, multi-hit) are untested.

Inferred layout per phase (consistent across the 3 files this was built from):
    [duration, ?, start_frame, duration_again]   (Startup's 3rd value differs)
The script cross-checks start_frame against the sum of the earlier durations and
flags MISMATCH when it doesn't line up, so you can see where the guess fails.

Standard library only. Tested on Python 3.8+.
"""
import argparse, csv, json, os, re, struct, sys

PHASES = ("Startup", "Active", "Recovery")


class Pkg:
    """Minimal parser for the cooked, unversioned UE5 package header (legacy -9)."""

    def __init__(self, path):
        self.d = open(path, "rb").read()
        d = self.d
        if self.u32(0) != 0x9E2A83C1:
            raise ValueError("not a .uasset (bad magic)")
        self.legacy = self.i32(4)
        o = 0x18 + 20                      # 3 version ints, then 20-byte saved hash
        self.header_size = self.u32(o)
        o += 4
        o += 4 + self.u32(o) * 20          # custom version container (0 when unversioned)
        _, o = self.fstr(o)                # package name
        self.flags = self.u32(o)
        o += 4
        nc, no = self.u32(o), self.u32(o + 4)
        ec, eo = self.u32(o + 0x18), self.u32(o + 0x1C)
        ic, io = self.u32(o + 0x20), self.u32(o + 0x24)
        self.names = []
        p = no
        for _ in range(nc):
            s, p = self.fstr(p)
            p += 4                          # name hashes
            self.names.append(s)
        self.imports = []
        for k in range(ic):
            b = io + k * 40
            self.imports.append(dict(
                cls=self.fname(b + 8), outer=self.i32(b + 16), name=self.fname(b + 20)))
        self.exports = []
        for k in range(ec):
            b = eo + k * 96
            self.exports.append(dict(
                cls=self.i32(b), sup=self.i32(b + 4), tmpl=self.i32(b + 8),
                name=self.fname(b + 16), size=self.i64(b + 28), offset=self.i64(b + 36)))

    def u32(self, o): return struct.unpack_from("<I", self.d, o)[0]
    def i32(self, o): return struct.unpack_from("<i", self.d, o)[0]
    def i64(self, o): return struct.unpack_from("<q", self.d, o)[0]

    def fstr(self, o):
        n = self.i32(o)
        o += 4
        if n < 0:
            s = self.d[o:o + -n * 2 - 2].decode("utf-16le")
            return s, o + -n * 2
        return self.d[o:o + n - 1].decode("latin1"), o + n

    def fname(self, o):
        idx, num = self.u32(o), self.u32(o + 4)
        return self.names[idx] + ("_%d" % (num - 1) if num else "")

    def ref(self, v):
        if v < 0:
            return self.imports[-v - 1]["name"]
        if v > 0:
            return self.exports[v - 1]["name"]
        return None


def inline_strings(blob):
    """Length-prefixed ASCII strings (FString) embedded in export data."""
    out = []
    for m in re.finditer(rb"([\x02-\x40])\x00\x00\x00([ -~]{1,63})\x00", blob):
        if len(m.group(2)) == m.group(1)[0] - 1:
            out.append((m.start(), m.group(2).decode()))
    return out


def ints_after(blob, pos, n=4):
    """n int32 values starting at pos (the byte just past a string's length prefix+text+NUL)."""
    vals = []
    for k in range(n):
        if pos + 4 * (k + 1) > len(blob):
            break
        vals.append(struct.unpack_from("<i", blob, pos + 4 * k)[0])
    return vals


def analyse(base):
    pkg = Pkg(base + ".uasset")
    blob = open(base + ".uexp", "rb").read()
    # The default-object export is the first one; its data starts at offset - header size.
    cdo = pkg.exports[0]
    start = cdo["offset"] - pkg.header_size
    data = blob[start:start + cdo["size"]]

    info = {
        "file": os.path.basename(base),
        "cdo": cdo["name"],
        "parent_class": None,
        "unversioned": bool(pkg.flags & 0x2000),
        "animations": [i["name"] for i in pkg.imports if i["cls"] == "AnimSequence"],
        "paths": [n for n in pkg.names if n.startswith("/Game/")],
        "fnames": sorted(n for n in pkg.names if not n.startswith(("/", "Default__"))
                         and n not in ("None", "Engine", "BlueprintType", "Class", "Object",
                                       "Package", "AnimSequence", "BlueprintGeneratedClass")
                         and not n.startswith(("AN_", "ATT_", "BP_"))
                         and not n.endswith("_C")),
    }
    for e in pkg.exports[1:]:
        if e["sup"] < 0:
            info["parent_class"] = pkg.ref(e["sup"])

    strs = inline_strings(data)
    info["inline_strings"] = [s for _, s in strs]
    # BPTYPE_* lives in the class export (after the default object), so search the whole file.
    info["attack_type"] = next((s for _, s in inline_strings(blob) if s.startswith("BPTYPE_")), None)

    # First occurrence of each phase name whose next int32 is non-zero.
    phases = {}
    for off, s in strs:
        if s in PHASES and s not in phases:
            # offset points at the length prefix: 4 bytes + text + NUL
            after = off + 4 + len(s) + 1
            vals = ints_after(data, after, 4)
            if vals and vals[0] != 0:
                phases[s] = vals
    info["phases_raw"] = phases

    dur = {p: (v[0] if v else None) for p, v in phases.items()}
    info["startup"], info["active"], info["recovery"] = (dur.get(p) for p in PHASES)
    total = [d for d in (info["startup"], info["active"], info["recovery"]) if d is not None]
    info["total"] = sum(total) if len(total) == 3 else None

    checks = []
    if "Active" in phases and info["startup"] is not None and len(phases["Active"]) > 2:
        checks.append(("Active start == Startup", phases["Active"][2] == info["startup"]))
    if "Recovery" in phases and None not in (info["startup"], info["active"]) \
            and len(phases["Recovery"]) > 2:
        checks.append(("Recovery start == Startup+Active",
                       phases["Recovery"][2] == info["startup"] + info["active"]))
    info["checks"] = checks
    info["recovery_extra"] = phases["Recovery"][1] if "Recovery" in phases and len(phases["Recovery"]) > 1 else None

    # Dragdown-wiki convention (matched against Loxodont Bair/Bthrow/Dair, 3 of 3):
    #   wiki startup = first active frame = Startup + 1
    #   endlag       = Recovery's 2nd stored number
    #   iasa         = first active frame + active frames + endlag
    #   total        = Startup + Active + Recovery
    info["first_active"] = info["last_active"] = info["endlag"] = info["iasa"] = None
    if info["startup"] is not None and info["active"] is not None:
        info["first_active"] = info["startup"] + 1
        info["last_active"] = info["startup"] + info["active"]
    info["endlag"] = info["recovery_extra"]
    if None not in (info["first_active"], info["endlag"]):
        info["iasa"] = info["first_active"] + info["active"] + info["endlag"]
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--csv")
    ap.add_argument("--json")
    ap.add_argument("--verbose", action="store_true", help="also print names, strings and raw phase ints")
    a = ap.parse_args()

    bases = []
    for root, _, files in os.walk(a.folder):
        for f in sorted(files):
            if f.endswith(".uasset") and os.path.exists(os.path.join(root, f[:-7] + ".uexp")):
                bases.append(os.path.join(root, f[:-7]))
    if not bases:
        sys.exit("No .uasset/.uexp pairs found in %s" % a.folder)

    results = []
    for b in bases:
        try:
            results.append(analyse(b))
        except Exception as ex:                      # keep going; report the failures
            print("!! %s: %s" % (os.path.basename(b), ex), file=sys.stderr)

    print("%-22s %-8s %-9s %-7s %-5s %-6s %-10s %s" % (
        "file", "startup", "active", "endlag", "iasa", "total", "checks", "type"))
    for r in results:
        ck = ",".join("ok" if ok else "MISMATCH" for _, ok in r["checks"]) or "n/a"
        act = "%s-%s" % (r["first_active"], r["last_active"]) if r["first_active"] else None
        print("%-22s %-8s %-9s %-7s %-5s %-6s %-10s %s" % (
            r["file"], r["first_active"], act, r["endlag"], r["iasa"], r["total"], ck, r["attack_type"]))
    print("\nColumns follow the Dragdown Wiki convention (startup = first active frame).")
    print("Checked against Loxodont Bair/Bthrow/Dair only. Landing lag, cancel and ledge-grab frame are not decoded.")

    for r in results:
        print("\n== %s  (%s, parent: %s, unversioned: %s)" % (
            r["file"], r["cdo"], r["parent_class"], r["unversioned"]))
        print("   animations: %s" % (", ".join(r["animations"]) or "none"))
        if r["paths"]:
            print("   assets:     %s" % ", ".join(r["paths"]))
        print("   names:      %s" % ", ".join(r["fnames"]))
        if a.verbose:
            print("   phases raw: %s" % r["phases_raw"])
            print("   strings:    %s" % r["inline_strings"])

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["file", "startup", "active_frames", "active_range", "endlag", "iasa",
                        "total_duration", "attack_type", "animations", "checks_ok"])
            for r in results:
                act = "%s-%s" % (r["first_active"], r["last_active"]) if r["first_active"] else ""
                w.writerow([r["file"], r["first_active"], r["active"], act, r["endlag"], r["iasa"],
                            r["total"], r["attack_type"], ";".join(r["animations"]),
                            all(ok for _, ok in r["checks"]) if r["checks"] else ""])
        print("\nwrote", a.csv)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(results, f, indent=2)
        print("wrote", a.json)


if __name__ == "__main__":
    main()
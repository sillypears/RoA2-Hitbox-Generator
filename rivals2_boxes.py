#!/usr/bin/env python3
"""Inspect Loxodont UE packages and render per-frame box tracks.

Run from this directory, for example:
    python rivals2_boxes.py inspect
    python rivals2_boxes.py render loxodont_boxes.json --out rendered_frames

The renderer consumes per-frame box tracks from JSON. Package inspection is
included to help decode source assets. Unreal's cooked physics and animation
serialization still needs decoding before source assets can fill those tracks.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
from pathlib import Path

from decode_attack import Pkg as AttackPackage


ROOT = Path(__file__).resolve().parent
LOX = ROOT / "Rivals2/Content/Characters/Loxodont"


class Package:
    """Small reader for the cooked package table layout used by decode_attack.py."""

    def __init__(self, path: Path):
        parsed = AttackPackage(str(path))
        self.path = path
        self.data = parsed.d
        self.header_size = parsed.header_size
        self.flags = parsed.flags
        self.names = parsed.names
        self.imports = [{"class": item["cls"], "name": item["name"]}
                        for item in parsed.imports]
        self.exports = [{"class_index": item["cls"], "super_index": item["sup"],
                         "name": item["name"], "size": item["size"],
                         "offset": item["offset"]} for item in parsed.exports]

    def u32(self, p): return struct.unpack_from("<I", self.data, p)[0]
    def i32(self, p): return struct.unpack_from("<i", self.data, p)[0]
    def i64(self, p): return struct.unpack_from("<q", self.data, p)[0]

    def fstring(self, p):
        n = self.i32(p)
        p += 4
        if n < 0:
            return self.data[p:p - 2 + (-n * 2)].decode("utf-16le"), p + -n * 2
        return self.data[p:p + n - 1].decode("latin1"), p + n

    def fname(self, p):
        index, number = self.u32(p), self.u32(p + 4)
        if index >= len(self.names):
            return f"<bad-name-{index}>"
        return self.names[index] + (f"_{number - 1}" if number else "")

    def reference(self, index):
        if index < 0:
            return self.imports[-index - 1]["name"]
        if index > 0:
            return self.exports[index - 1]["name"]
        return None


class MapReader:
    """Reader for the uncompressed community .usmap payload used by this dump."""
    TYPES = {
        0: "ByteProperty", 1: "BoolProperty", 2: "IntProperty", 3: "FloatProperty",
        4: "ObjectProperty", 5: "NameProperty", 6: "DelegateProperty", 7: "DoubleProperty",
        8: "ArrayProperty", 9: "StructProperty", 10: "StrProperty", 11: "TextProperty",
        12: "InterfaceProperty", 13: "MulticastDelegateProperty", 14: "WeakObjectProperty",
        15: "LazyObjectProperty", 16: "AssetObjectProperty", 17: "SoftObjectProperty",
        18: "UInt64Property", 19: "UInt32Property", 20: "UInt16Property", 21: "Int64Property",
        22: "Int16Property", 23: "Int8Property", 24: "MapProperty", 25: "SetProperty",
        26: "EnumProperty", 27: "FieldPathProperty", 28: "OptionalProperty",
        29: "Utf8StrProperty", 30: "AnsiStrProperty", 31: "ClassProperty",
        32: "MulticastInlineDelegateProperty", 33: "SoftClassProperty",
    }

    def __init__(self, path: Path):
        raw = path.read_bytes()
        if raw[:2] != b"\xc4\x30":
            raise ValueError(f"{path} is not a .usmap (bad magic)")
        self.version = raw[2]
        # UE's .usmap header stores the version-info flag in a 32-bit slot.
        has_versioning = struct.unpack_from("<i", raw, 3)[0]
        if has_versioning:
            raise ValueError("Versioned .usmap headers are not yet supported by this inspector")
        compression = raw[7]
        comp_size, data_size = struct.unpack_from("<II", raw, 8)
        payload = raw[16:16 + comp_size]
        if compression == 0:
            if comp_size != data_size or len(payload) != data_size:
                raise ValueError("Uncompressed .usmap size fields do not match the file")
        elif compression == 3:
            import zstandard
            payload = zstandard.ZstdDecompressor().decompress(payload, max_output_size=data_size)
        else:
            raise ValueError(f"Unsupported .usmap compression method {compression}")
        self.data = payload
        self.offset = 0
        name_count = self.u32()
        self.names = []
        for _ in range(name_count):
            length = self.u16() if self.version >= 2 else self.u8()
            self.names.append(self.read(length).decode("utf-8"))
        self.enums = {}
        for _ in range(self.u32()):
            enum_name = self.name()
            entry_count = self.u16() if self.version >= 3 else self.u8()
            enum_entries = []
            for index in range(entry_count):
                value = self.u64() if self.version >= 4 else index
                enum_entries.append((value, self.name()))
            self.enums[enum_name] = enum_entries
        self.schemas = {}
        for _ in range(self.u32()):
            name, parent = self.name(), self.name(allow_none=True)
            property_count, serializable_count = self.u16(), self.u16()
            properties = []
            for _ in range(serializable_count):
                index, array_dim, prop_name = self.u16(), self.u8(), self.name()
                properties.append({"index": index, "array_dim": array_dim,
                                   "name": prop_name, "type": self.property_type()})
            self.schemas[name] = {"parent": parent, "property_count": property_count,
                                  "properties": properties}

    def read(self, count):
        result = self.data[self.offset:self.offset + count]
        if len(result) != count:
            raise ValueError(f"Truncated .usmap at payload offset 0x{self.offset:x}")
        self.offset += count
        return result

    def u8(self): return self.read(1)[0]
    def u16(self): return struct.unpack("<H", self.read(2))[0]
    def u32(self): return struct.unpack("<I", self.read(4))[0]
    def i32(self): return struct.unpack("<i", self.read(4))[0]
    def u64(self): return struct.unpack("<Q", self.read(8))[0]

    def name(self, allow_none=False):
        index = self.i32()
        if allow_none and index < 0:
            return None
        if index < 0 or index >= len(self.names):
            raise ValueError(f"Invalid .usmap name index {index}")
        return self.names[index]

    def property_type(self):
        code = self.u8()
        kind = self.TYPES.get(code, f"Unknown_{code}")
        result = {"kind": kind}
        if kind == "StructProperty":
            result["struct"] = self.name()
        elif kind in ("ArrayProperty", "SetProperty", "OptionalProperty"):
            result["inner"] = self.property_type()
        elif kind == "MapProperty":
            result["key"] = self.property_type()
            result["value"] = self.property_type()
        elif kind == "EnumProperty":
            result["underlying"] = self.property_type()
            result["enum"] = self.name()
        return result


class UnversionedReader:
    """Partial schema-driven UE reader for the PhysicsAsset property subset."""
    def __init__(self, mapping: MapReader, package: Package, blob: bytes):
        self.mapping, self.package, self.blob, self.offset = mapping, package, blob, 0

    def take(self, count):
        result = self.blob[self.offset:self.offset + count]
        if len(result) != count:
            raise ValueError(f"truncated export at 0x{self.offset:x} reading {count} bytes")
        self.offset += count
        return result

    def unpack(self, fmt):
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, self.take(size))[0]

    def schema_properties(self, schema_name):
        schema = self.mapping.schemas[schema_name]
        parent = schema["parent"]
        props = self.schema_properties(parent) if parent in self.mapping.schemas else []
        base = len(props)
        for prop in schema["properties"]:
            for array_index in range(prop["array_dim"]):
                item = dict(prop)
                item["index"] = base + prop["index"] + array_index
                props.append(item)
        return props

    def header(self, properties):
        fragments, current = [], 0
        zero_count = 0
        while True:
            frag = self.unpack("<H")
            skip = frag & 0x7f
            has_zeroes = bool(frag & 0x80)
            is_last = bool(frag & 0x100)
            value_count = frag >> 9
            start = current + skip
            indexes = list(range(start, start + value_count))
            fragments.append((indexes, has_zeroes))
            if has_zeroes:
                zero_count += value_count
            current = start + value_count
            if is_last:
                break
        mask = b""
        if zero_count:
            byte_count = 1 if zero_count <= 8 else 2 if zero_count <= 16 else ((zero_count + 31)//32)*4
            mask = self.take(byte_count)
        values, mask_index = [], 0
        for indexes, has_zeroes in fragments:
            for index in indexes:
                zero = False
                if has_zeroes:
                    zero = bool(mask[mask_index // 8] & (1 << (mask_index % 8)))
                    mask_index += 1
                values.append((index, zero))
        by_index = {p["index"]: p for p in properties}
        return [(by_index[i], zero) for i, zero in values if i in by_index]

    def unversioned_struct(self, schema_name):
        if schema_name not in self.mapping.schemas:
            raise ValueError(f"no .usmap schema for struct {schema_name}")
        props = self.schema_properties(schema_name)
        result = {}
        for prop, zero in self.header(props):
            result[prop["name"]] = None if zero else self.value(prop["type"])
        return result

    def value(self, typ):
        kind = typ["kind"]
        if kind == "BoolProperty": return bool(self.unpack("<B"))
        if kind in ("ByteProperty", "Int8Property"): return self.unpack("<b" if kind == "Int8Property" else "<B")
        if kind in ("Int16Property", "UInt16Property"): return self.unpack("<h" if kind == "Int16Property" else "<H")
        if kind in ("IntProperty", "UInt32Property", "ObjectProperty", "ClassProperty",
                    "WeakObjectProperty", "LazyObjectProperty", "AssetObjectProperty",
                    "SoftObjectProperty", "SoftClassProperty"):
            return self.unpack("<I" if kind == "UInt32Property" else "<i")
        if kind == "UInt64Property": return self.unpack("<Q")
        if kind == "Int64Property": return self.unpack("<q")
        if kind == "FloatProperty": return self.unpack("<f")
        if kind == "DoubleProperty": return self.unpack("<d")
        if kind == "NameProperty":
            name_index, number = struct.unpack("<II", self.take(8))
            name = self.package.names[name_index] if name_index < len(self.package.names) else f"<name:{name_index}>"
            return name + (f"_{number-1}" if number else "")
        if kind == "EnumProperty": return self.value(typ.get("underlying", {"kind": "ByteProperty"}))
        if kind == "ArrayProperty":
            count = self.unpack("<i")
            if count < 0 or count > 100000:
                raise ValueError(f"implausible {kind} count {count} at 0x{self.offset-4:x}")
            return [self.value(typ["inner"]) for _ in range(count)]
        if kind == "StructProperty":
            name = typ.get("struct", "")
            if name in ("Vector", "Vector3f"):
                return list(struct.unpack("<3f", self.take(12)))
            if name in ("Rotator", "Rotator3f"):
                return list(struct.unpack("<3f", self.take(12)))
            if name in ("Quat", "Quaternion"):
                return list(struct.unpack("<4f", self.take(16)))
            if name == "Guid": return self.take(16).hex()
            if name == "Transform":
                return {"rotation": list(struct.unpack("<4f", self.take(16))),
                        "translation": list(struct.unpack("<3f", self.take(12))),
                        "scale": list(struct.unpack("<3f", self.take(12)))}
            return self.unversioned_struct(name)
        raise ValueError(f"unsupported property type {kind}")


def inspect_asset(base: Path):
    package = Package(base.with_suffix(".uasset"))
    payload_path = base.with_suffix(".uexp")
    payload = payload_path.read_bytes() if payload_path.exists() else b""
    result = {
        "asset": str(base.relative_to(ROOT)),
        "header_size": package.header_size,
        "flags": f"0x{package.flags:08x}",
        "names": package.names,
        "imports": package.imports,
        "exports": [],
        "payload_size": len(payload),
        "payload_ascii": [s.decode("ascii", "replace") for s in
                          re.findall(rb"[\x20-\x7e]{5,}", payload)],
    }
    for entry in package.exports:
        start = entry["offset"] - package.header_size
        end = start + entry["size"]
        result["exports"].append({
            **entry,
            "class": package.reference(entry["class_index"]),
            "super": package.reference(entry["super_index"]),
            "payload_range": [start, end],
            "payload_hex": payload[start:end].hex(),
        })
    return result


def decode_physics_asset(base: Path, mapping_path: Path):
    package = Package(base.with_suffix(".uasset"))
    payload = base.with_suffix(".uexp").read_bytes()
    mapping = MapReader(mapping_path)
    decoded = []
    for export in package.exports:
        class_name = package.reference(export["class_index"])
        if class_name not in ("SkeletalBodySetup", "BodySetup"):
            continue
        start = export["offset"] - package.header_size
        blob = payload[start:start + export["size"]]
        reader = UnversionedReader(mapping, package, blob)
        props = reader.unversioned_struct(class_name)
        decoded.append({"export": export["name"], "properties": props,
                        "bytes_consumed": reader.offset, "export_bytes": len(blob)})
    return {"asset": str(base.relative_to(ROOT)), "bodies": decoded}


def candidate_assets():
    choices = [LOX / "HB_Lox"]
    for folder in (LOX / "Attacks", LOX / "Animation/Attacks"):
        choices.extend(sorted(p.with_suffix("") for p in folder.glob("*.uasset")))
    return choices


def export_asset_records(asset: str, destination: Path):
    """Extract source-asset records to JSON for review; never invent geometry.

    This is deliberately a data-extraction entry point rather than a renderer
    alias. At present the package parser can reliably expose object names and
    serialized export bytes, but the UE 5.8 unversioned property schema needed
    to turn those bytes into positioned hit/hurt shapes has not been recovered.
    The JSON therefore contains the raw body/setup exports and the animation
    references as an auditable intermediate for continuing the decoder.
    """
    matches = [p for p in candidate_assets() if p.name == asset]
    if not matches:
        raise SystemExit(f"Unknown Loxodont candidate {asset!r}; try HB_Lox or AN_Lox_BAir")
    result = inspect_asset(matches[0])
    data = {
        "status": "source_records_only_geometry_schema_pending",
        "asset": result["asset"],
        "names": result["names"],
        "imports": result["imports"],
        "exports": result["exports"],
        "frames": [],
    }
    destination.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Wrote asset records to {destination}")
    print("UE geometry/animation decoding is not complete; no plotted boxes were fabricated.")


def render_tracks(source: Path, output: Path):
    """Render explicit frame-space shape tracks to PNGs and an animated GIF.

    Coordinates use x right, y up, and box dimensions in arbitrary consistent
    units. Supported shapes: capsule {a:[x,y], b:[x,y], r}, circle {c:[x,y], r},
    rect {c:[x,y], w, h}. Each frame has a `boxes` list; `kind` is hit or hurt.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise SystemExit("Rendering needs Pillow: python -m pip install pillow") from exc
    data = json.loads(source.read_text())
    frames = data["frames"]
    width, height = data.get("canvas", [640, 480])
    scale = float(data.get("pixels_per_unit", 12))
    origin = data.get("origin", [width / (2 * scale), height / (2 * scale)])
    fps = float(data.get("fps", 60))
    output.mkdir(parents=True, exist_ok=True)
    images = []

    def xy(point):
        return (round(width / 2 + (point[0] - origin[0]) * scale),
                round(height / 2 - (point[1] - origin[1]) * scale))

    for index, frame in enumerate(frames):
        image = Image.new("RGB", (width, height), "#171b24")
        draw = ImageDraw.Draw(image, "RGBA")
        ox, oy = xy([0, 0])
        draw.line((0, oy, width, oy), fill=(105, 115, 130, 110), width=1)
        draw.line((ox, 0, ox, height), fill=(105, 115, 130, 75), width=1)
        draw.text((16, 14), f"{data.get('name', source.stem)}  frame {frame.get('index', index)}",
                  fill="#f1f3f5")
        for box in frame.get("boxes", []):
            color = (65, 190, 255, 105) if box.get("kind", "hurt") == "hurt" else (255, 70, 70, 115)
            edge = (105, 220, 255, 255) if box.get("kind", "hurt") == "hurt" else (255, 120, 100, 255)
            if box["shape"] == "capsule":
                a, b, r = xy(box["a"]), xy(box["b"]), box["r"] * scale
                draw.line((a, b), fill=edge, width=max(1, round(2 * r)))
                for cx, cy in (a, b):
                    draw.ellipse((cx-r, cy-r, cx+r, cy+r), fill=color, outline=edge, width=2)
            elif box["shape"] == "circle":
                cx, cy = xy(box["c"]); r = box["r"] * scale
                draw.ellipse((cx-r, cy-r, cx+r, cy+r), fill=color, outline=edge, width=2)
            elif box["shape"] == "rect":
                cx, cy = xy(box["c"]); w, h = box["w"]*scale/2, box["h"]*scale/2
                draw.rectangle((cx-w, cy-h, cx+w, cy+h), fill=color, outline=edge, width=2)
            else:
                raise ValueError(f"Unsupported shape: {box['shape']}")
            label = box.get("name", box.get("bone", box.get("kind", "box")))
            point = box.get("c", box.get("a", box.get("center", [0, 0])))
            draw.text((xy(point)[0] + 5, xy(point)[1] - 14), label, fill=edge)
        path = output / f"frame_{index:04d}.png"
        image.save(path)
        images.append(image)
    if images:
        duration = max(1, round(1000 / fps))
        images[0].save(output / "animation.gif", save_all=True, append_images=images[1:],
                       duration=duration, loop=0, disposal=2)
    print(f"Rendered {len(images)} frames to {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inspect", "render", "dump", "template", "map-info", "physics"],
                        help="inspect assets, dump raw records, render tracks, or inspect a .usmap")
    parser.add_argument("input", nargs="?", help="JSON box-track input for render")
    parser.add_argument("--out", default="loxodont_frames", help="render output directory")
    parser.add_argument("--asset", help="asset basename, such as HB_Lox or AN_Lox_BAir")
    parser.add_argument("--schema", help="print one mapping schema with property indices")
    parser.add_argument("--json", help="write full inspection data to this JSON file")
    args = parser.parse_args()

    if args.command == "map-info":
        mapping_path = ROOT / "Rivals2-5.8.0-0+UE5-5418fa4e.usmap"
        if args.input:
            mapping_path = Path(args.input)
        mapping = MapReader(mapping_path)
        print(f"{mapping_path.name}: version {mapping.version}, {len(mapping.names)} names, "
              f"{len(mapping.enums)} enums, {len(mapping.schemas)} schemas")
        needles = ("SkeletalBodySetup", "BodySetup", "PhysicsAsset", "AggregateGeom",
                   "SphylElem", "AnimSequence", "RivalsCharacterAttackData")
        if args.schema:
            key = next((k for k in mapping.schemas if k.casefold() == args.schema.casefold()), None)
            if key is None:
                raise SystemExit(f"No schema named {args.schema!r}")
            print(json.dumps({"name": key, **mapping.schemas[key]}, indent=2))
            return
        for name, schema in mapping.schemas.items():
            if any(needle.casefold() in name.casefold() for needle in needles):
                fields = ", ".join(f"{p['name']}:{p['type']['kind']}" +
                                    (f"<{p['type']['struct']}>" if "struct" in p["type"] else "")
                                    for p in schema["properties"])
                print(f"  {name} : {schema['parent']} [{schema['property_count']} fields] {fields}")
        return

    if args.command == "physics":
        base = LOX / "HB_Lox"
        if args.asset:
            matching = next((p for p in candidate_assets() if p.name == args.asset), None)
            if matching is None:
                raise SystemExit(f"No candidate asset named {args.asset!r}")
            base = matching
        mapping_path = ROOT / "Rivals2-5.8.0-0+UE5-5418fa4e.usmap"
        result = decode_physics_asset(base, mapping_path)
        output = Path(args.json) if args.json else ROOT / f"{base.name}_physics.json"
        output.write_text(json.dumps(result, indent=2) + "\n")
        print(f"Wrote {output} ({len(result['bodies'])} body setup exports)")
        for body in result["bodies"]:
            agg = body["properties"].get("AggGeom") or {}
            print(f"  {body['export']} bone={body['properties'].get('BoneName')} "
                  f"sphyl={len(agg.get('SphylElems') or [])} "
                  f"sphere={len(agg.get('SphereElems') or [])} "
                  f"box={len(agg.get('BoxElems') or [])}")
        return

    if args.command == "dump":
        if not args.asset:
            parser.error("dump requires --asset (for example HB_Lox or AN_Lox_BAir)")
        export_asset_records(args.asset, Path(args.input or f"{args.asset}_records.json"))
        return

    if args.command == "template":
        target = Path(args.input or "boxes.template.json")
        template = {
            "name": "Loxodont move name",
            "fps": 60,
            "canvas": [640, 480],
            "pixels_per_unit": 12,
            "origin": [0, 0],
            "frames": [{"index": 0, "boxes": []}],
        }
        target.write_text(json.dumps(template, indent=2) + "\n")
        print(f"Wrote {target}; add decoded per-frame boxes before rendering.")
        return

    if args.command == "render":
        if not args.input:
            parser.error("render requires a JSON input path")
        source = Path(args.input)
        if not source.is_file():
            raise SystemExit(
                f"Input file not found: {source}\n"
                "The renderer needs decoded per-frame box tracks. "
                "Use `python rivals2_boxes.py template boxes.template.json` "
                "to create the JSON structure; direct Unreal asset extraction is still in progress."
            )
        render_tracks(source, Path(args.out))
        return

    paths = candidate_assets()
    if args.asset:
        paths = [p for p in paths if p.name == args.asset]
        if not paths:
            raise SystemExit(f"No candidate asset named {args.asset!r} under {LOX}")
    results = [inspect_asset(path) for path in paths]
    if args.json:
        output = Path(args.json)
        output.write_text(json.dumps(results, indent=2))
        print(f"Wrote {output}")
    for result in results:
        print(f"\n{result['asset']} ({result['payload_size']} payload bytes)")
        for entry in result["exports"]:
            print(f"  {entry['name']}: class={entry['class']}, bytes={entry['size']}, "
                  f"payload={entry['payload_range']}")
        print("  imported assets:")
        for item in result["imports"]:
            if item["class"] == "Package":
                print(f"    {item['name']}")


if __name__ == "__main__":
    main()

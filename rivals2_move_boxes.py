#!/usr/bin/env python3
"""Extract character move hitbox windows and render hit/hurt boxes per frame.

The extracted FModel JSON contains move timing, hitbox attributes, and the
Physics Asset capsules. Per-frame transforms are read from UEFormat .ueanim
files exported by FModel. If a move's .ueanim is missing, its boxes fall back
to the skeleton reference pose and the PNG is clearly marked.

Usage:
    python rivals2_move_boxes.py                         # Loxodont (default)
    python rivals2_move_boxes.py --character Absa
    python rivals2_move_boxes.py --hide-skeleton          # omit rig lines
    python rivals2_move_boxes.py --all                    # every supported character
    python rivals2_move_boxes.py --character Absa --move ATT_Abs_Bair
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import functools
import json
import math
import struct
from pathlib import Path

import numpy as np

try:
    from PIL import Image, ImageDraw
except ImportError as exc:
    raise SystemExit("PNG rendering requires Pillow: python -m pip install pillow") from exc


ROOT = Path(__file__).resolve().parent
CHAR = ROOT / "Rivals2/Content/Characters/Loxodont"
ATTACKS = CHAR / "Attacks"
PHYSICS = CHAR / "HB_Lox.json"
SKELETON = CHAR / "Animation/SK_Lox_Skeleton.json"
OUT_DEFAULT = ROOT / "Rivals2/HitboxFrames/Loxodont"
CHARACTERS_ROOT = ROOT / "Rivals2/Content/Characters"
FPS = 60
WIDTH, HEIGHT = 900, 680
SCALE = 1.8
VIEW_CENTER_Z = 95
REFERENCE_WARNING = "REFERENCE POSE ONLY — matching .ueanim not found"


class BinaryReader:
    def __init__(self, data, label):
        self.data, self.offset, self.label = data, 0, label

    def take(self, count):
        if count < 0 or self.offset + count > len(self.data):
            raise ValueError(f"truncated {self.label} at byte 0x{self.offset:x}")
        result = self.data[self.offset:self.offset + count]
        self.offset += count
        return result

    def unpack(self, fmt):
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, self.take(size))

    def string(self):
        (length,) = self.unpack("<i")
        if length < 0 or length > 16_000_000:
            raise ValueError(f"invalid UEFormat string length {length} in {self.label}")
        return self.take(length).decode("utf-8")


def decompress_zstd(data, expected_size):
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=expected_size)
    except ImportError:
        library = ctypes.util.find_library("zstd")
        if not library:
            raise SystemExit("Reading compressed .ueanim requires zstandard or the system libzstd")
        zstd = ctypes.CDLL(library)
        zstd.ZSTD_decompress.argtypes = (ctypes.c_void_p, ctypes.c_size_t,
                                         ctypes.c_void_p, ctypes.c_size_t)
        zstd.ZSTD_decompress.restype = ctypes.c_size_t
        zstd.ZSTD_isError.argtypes = (ctypes.c_size_t,)
        zstd.ZSTD_isError.restype = ctypes.c_uint
        zstd.ZSTD_getErrorName.argtypes = (ctypes.c_size_t,)
        zstd.ZSTD_getErrorName.restype = ctypes.c_char_p
        destination = ctypes.create_string_buffer(expected_size)
        source = ctypes.create_string_buffer(data)
        size = zstd.ZSTD_decompress(destination, expected_size, source, len(data))
        if zstd.ZSTD_isError(size):
            message = zstd.ZSTD_getErrorName(size).decode("utf-8", "replace")
            raise ValueError(f"ZSTD decompression failed: {message}")
        return destination.raw[:size]


@functools.lru_cache(maxsize=256)
def read_ueanim(path_string):
    path = Path(path_string)
    raw = path.read_bytes()
    reader = BinaryReader(raw, str(path))
    if reader.take(8) != b"UEFORMAT":
        raise ValueError(f"{path} does not begin with UEFORMAT")
    identifier = reader.string()
    if identifier != "UEANIM":
        raise ValueError(f"{path} is {identifier}, expected UEANIM")
    (version,) = reader.unpack("<B")
    if version != 10:
        raise ValueError(f"unsupported UEFormat version {version} in {path}")
    object_name = reader.string()
    object_path = reader.string()
    (compressed,) = reader.unpack("<?")
    if compressed:
        compression = reader.string()
        uncompressed_size, compressed_size = reader.unpack("<ii")
        compressed_data = reader.take(compressed_size)
        if compression == "ZSTD":
            body = decompress_zstd(compressed_data, uncompressed_size)
        elif compression == "GZIP":
            import gzip
            body = gzip.decompress(compressed_data)
        else:
            raise ValueError(f"unsupported UEFormat compression {compression!r} in {path}")
        if len(body) != uncompressed_size:
            raise ValueError(f"UEFormat size mismatch in {path}: expected {uncompressed_size}, got {len(body)}")
    else:
        body = reader.take(len(raw) - reader.offset)

    attrs_reader = BinaryReader(body, f"{path} UEAnim body")
    (attribute_count,) = attrs_reader.unpack("<i")
    attrs = {}
    for _ in range(attribute_count):
        name = attrs_reader.string()
        (size,) = attrs_reader.unpack("<i")
        attrs[name] = attrs_reader.take(size)

    metadata_reader = BinaryReader(attrs.get("METADATA", b""), f"{path} metadata")
    num_frames, fps = metadata_reader.unpack("<if")
    ref_pose_path = metadata_reader.string()
    additive_type, ref_pose_type = metadata_reader.unpack("<BB")
    (ref_frame_index,) = metadata_reader.unpack("<i")

    tracks_reader = BinaryReader(attrs.get("TRACKS", b""), f"{path} tracks")
    (track_count,) = tracks_reader.unpack("<i")
    tracks = {}
    for _ in range(track_count):
        bone_name = tracks_reader.string()
        positions = read_vector_keys(tracks_reader)
        rotations = read_quat_keys(tracks_reader)
        scales = read_vector_keys(tracks_reader)
        tracks[bone_name] = {"position": positions, "rotation": rotations, "scale": scales}
    if tracks_reader.offset != len(tracks_reader.data):
        raise ValueError(f"unparsed bytes in {path} TRACKS: {len(tracks_reader.data)-tracks_reader.offset}")

    return {"name": object_name, "path": object_path, "frames": num_frames, "fps": fps,
            "ref_pose_path": ref_pose_path, "additive_type": additive_type,
            "ref_pose_type": ref_pose_type, "ref_frame_index": ref_frame_index,
            "tracks": tracks}


def read_vector_keys(reader):
    (count,) = reader.unpack("<i")
    if count < 0 or count > 10_000_000:
        raise ValueError(f"invalid vector key count {count} in {reader.label}")
    return [reader.unpack("<i3f") for _ in range(count)]


def read_quat_keys(reader):
    (count,) = reader.unpack("<i")
    if count < 0 or count > 10_000_000:
        raise ValueError(f"invalid quaternion key count {count} in {reader.label}")
    return [reader.unpack("<i4f") for _ in range(count)]


def vec(obj):
    return (float(obj.get("X", 0)), float(obj.get("Y", 0)), float(obj.get("Z", 0)))


def qmul(a, b):
    x, y, z, w = a
    X, Y, Z, W = b
    return (w*X + x*W + y*Z - z*Y,
            w*Y - x*Z + y*W + z*X,
            w*Z + x*Y - y*X + z*W,
            w*W - x*X - y*Y - z*Z)


def qrot(q, v):
    x, y, z, w = q
    vx, vy, vz = v
    tx, ty, tz = 2*(y*vz-z*vy), 2*(z*vx-x*vz), 2*(x*vy-y*vx)
    return (vx + w*tx + y*tz-z*ty,
            vy + w*ty + z*tx-x*tz,
            vz + w*tz + x*ty-y*tx)


def rotator_quat(rot):
    """Convert Unreal Pitch/Yaw/Roll degrees to a quaternion."""
    p, y, r = (math.radians(float(rot.get(k, 0))) / 2 for k in ("Pitch", "Yaw", "Roll"))
    sp, cp, sy, cy, sr, cr = math.sin(p), math.cos(p), math.sin(y), math.cos(y), math.sin(r), math.cos(r)
    return (cr*sp*sy - sr*cp*cy,
            -cr*sp*cy - sr*cp*sy,
            cr*cp*sy - sr*sp*cy,
            cr*cp*cy + sr*sp*sy)


def read_skeleton():
    data = json.loads(SKELETON.read_text(encoding="utf-8"))
    skeleton = next(x for x in data if x.get("Type") == "Skeleton")
    ref = skeleton["ReferenceSkeleton"]
    infos, poses = ref["FinalRefBoneInfo"], ref["FinalRefBonePose"]
    if len(infos) != len(poses):
        raise ValueError("Skeleton reference bone and pose arrays have different lengths")
    local, parents, indices = [], [], {}
    for i, (info, pose) in enumerate(zip(infos, poses)):
        local_t = vec(pose["Translation"])
        q = pose["Rotation"]
        local_q = (float(q["X"]), float(q["Y"]), float(q["Z"]), float(q["W"]))
        sc = vec(pose.get("Scale3D", {"X": 1, "Y": 1, "Z": 1}))
        parent = int(info["ParentIndex"])
        local.append((local_t, local_q, sc))
        parents.append(parent)
        indices[info["Name"]] = i
    return compose_world(local, parents), indices, local, parents


class GLBCharacter:
    """Small GLB skinning reader for rendering a character behind the boxes."""
    COMPONENTS = {5121: np.dtype("u1"), 5123: np.dtype("<u2"),
                  5125: np.dtype("<u4"), 5126: np.dtype("<f4")}
    WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}

    def __init__(self, path, skeleton_indices, skin_textures=False):
        self.path = Path(path)
        raw = self.path.read_bytes()
        if raw[:4] != b"glTF":
            raise ValueError(f"{path} is not a binary glTF file")
        offset = 12
        document, binary = None, b""
        while offset < len(raw):
            length, kind = struct.unpack_from("<I4s", raw, offset)
            offset += 8
            chunk = raw[offset:offset+length]
            offset += length
            if kind == b"JSON":
                document = json.loads(chunk)
            elif kind == b"BIN\0":
                binary = chunk
        if document is None:
            raise ValueError(f"{path} has no GLB JSON chunk")
        self.doc = document
        self.binary = binary
        self.skeleton_indices = skeleton_indices
        skins = document.get("skins", [])
        if not skins:
            raise ValueError(f"{path} has no skeletal skin")
        self.joints = [document["nodes"][i].get("name", "") for i in skins[0]["joints"]]
        missing = [name for name in self.joints if name not in skeleton_indices]
        if missing:
            raise ValueError(f"{path} has joints absent from the reference skeleton: {missing[:5]}")
        self.inverse_bind = self.accessor(skins[0]["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)
        self.primitives = []
        self.skin_textures = skin_textures
        for mesh in document.get("meshes", []):
            for primitive in mesh.get("primitives", []):
                attributes = primitive["attributes"]
                positions = self.accessor(attributes["POSITION"]).astype(np.float32, copy=False)
                normals = self.accessor(attributes["NORMAL"]).astype(np.float32, copy=False)
                joints = self.accessor(attributes["JOINTS_0"]).astype(np.int32, copy=False)
                weights = self.accessor(attributes["WEIGHTS_0"]).astype(np.float32, copy=False)
                uv = (self.accessor(attributes["TEXCOORD_0"]).astype(np.float32, copy=False)
                      if skin_textures and "TEXCOORD_0" in attributes else None)
                indices = (self.accessor(primitive["indices"]).reshape(-1).astype(np.int32)
                           if "indices" in primitive else np.arange(len(positions), dtype=np.int32))
                material = int(primitive.get("material", 0))
                material_name = document.get("materials", [])[material].get("name", "")
                # Use stable, opaque neutral colors for the first mesh overlay.
                # FModel's exported CM textures are palette masks, not final
                # base-color maps, so sampling them would show misleading colors.
                colors = ((196, 145, 102), (105, 92, 78), (220, 190, 145),
                          (175, 125, 90), (90, 70, 55), (140, 100, 72), (205, 165, 125))
                color = colors[material % len(colors)]
                baked_texture = self.bake_palette_texture(material_name) if skin_textures else None
                normal_texture = self.load_normal_texture(material_name) if skin_textures else None
                if skin_textures and uv is None:
                    raise ValueError(f"{material_name} has no TEXCOORD_0 UV channel")
                self.primitives.append((positions, normals, joints, weights, indices,
                                        color, uv, baked_texture, normal_texture))
        if not self.primitives:
            raise ValueError(f"{path} has no mesh primitives")

    def accessor(self, index):
        accessor = self.doc["accessors"][index]
        if "sparse" in accessor:
            raise ValueError("Sparse GLB accessors are not supported")
        view = self.doc["bufferViews"][accessor["bufferView"]]
        dtype = self.COMPONENTS[accessor["componentType"]]
        width = self.WIDTHS[accessor["type"]]
        itemsize = dtype.itemsize
        stride = view.get("byteStride", width * itemsize)
        start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        return np.ndarray((accessor["count"], width), dtype=dtype, buffer=self.binary,
                          offset=start, strides=(stride, itemsize))

    def bake_palette_texture(self, material_name):
        """Bake mask slots using the material defaults and skin color-slot palette."""
        instance_path = self.path.parent / f"{material_name}.json"
        if not instance_path.is_file():
            instance_path = next((CHARACTERS_ROOT.rglob(f"{material_name}.json")), None)
        if instance_path is None or not instance_path.is_file():
            raise ValueError(f"missing material instance properties {instance_path}")
        instance = json.loads(instance_path.read_text(encoding="utf-8"))
        textures = instance.get("Textures", {})
        params = instance.get("Colors", {})
        color_mask = textures.get("ColorRegionMask") or textures.get("PM_Diffuse")
        if not isinstance(color_mask, dict):
            base = params.get("BaseColorTint", {}).get("Hex", "B0B0B0")
            rgb = np.array([int(base[i:i+2], 16) for i in (0, 2, 4)], dtype=np.uint8)
            return rgb.reshape(1, 1, 3)
        object_path = color_mask.get("ObjectPath", "").rsplit(".", 1)[0]
        mask_path = ROOT / f"{object_path}.png"
        if not mask_path.is_file():
            raise ValueError(f"missing color-region mask PNG {mask_path}")

        mask = np.asarray(Image.open(mask_path).convert("RGB"), dtype=np.uint8)
        # CM RGB bits encode a one-based material color slot. The material
        # parameters are zero-based for the first three slots; slot four uses
        # the special CustomColorX parameter, followed by CustomColor3+.
        codes = ((mask[:, :, 0] >= 128).astype(np.uint8) |
                 ((mask[:, :, 1] >= 128).astype(np.uint8) << 1) |
                 ((mask[:, :, 2] >= 128).astype(np.uint8) << 2))
        palette = np.zeros((8, 3), dtype=np.uint8)
        tint = params.get("BaseColorTint", {}).get("Hex", "FFFFFF")
        tint_rgb = np.array([int(tint[i:i+2], 16) for i in (0, 2, 4)], dtype=np.float32) / 255
        slot_parameter = {1: "CustomColor0", 2: "CustomColor1", 3: "CustomColor2",
                          4: "CustomColorX", 5: "CustomColor3", 6: "CustomColor4",
                          7: "CustomColor5"}
        palette_colors, names_by_default_color = self.read_skin_palette()
        palette[0] = (128, 128, 128)  # unassigned/black CM regions
        for slot, parameter in slot_parameter.items():
            color = params.get(parameter, {})
            hex_color = color.get("Hex", "808080")
            # FModel's MI JSON contains the slot's fallback/default color.
            # The selected CS -> PS palette contains the player-facing color;
            # find a matching named definition automatically when available.
            palette_name = names_by_default_color.get(hex_color.upper())
            hex_color = palette_colors.get(palette_name, hex_color)
            rgb = np.array([int(hex_color[i:i+2], 16) for i in (0, 2, 4)], dtype=np.float32)
            palette[slot] = np.clip(np.rint(rgb * tint_rgb), 0, 255).astype(np.uint8)
        return palette[codes]

    def load_normal_texture(self, material_name):
        """Load the per-material tangent-space normal map when the export has it."""
        instance_path = self.path.parent / f"{material_name}.json"
        if not instance_path.is_file():
            instance_path = next((CHARACTERS_ROOT.rglob(f"{material_name}.json")), None)
        if instance_path is None or not instance_path.is_file():
            return None
        instance = json.loads(instance_path.read_text(encoding="utf-8"))
        textures = instance.get("Textures", {})
        normal = textures.get("Normal") or textures.get("PM_Normals")
        if not isinstance(normal, dict) or not normal.get("ObjectPath"):
            return None
        object_path = normal["ObjectPath"].rsplit(".", 1)[0]
        normal_path = ROOT / f"{object_path}.png"
        if not normal_path.is_file():
            return None
        return np.asarray(Image.open(normal_path).convert("RGB"), dtype=np.uint8)

    def read_skin_palette(self):
        """Resolve this skin's default CS asset to its PS named color values."""
        neutral = self.path.parent / "Data/Palettes/Neutral"
        slot_assets = sorted(neutral.glob("CS_*.json")) if neutral.is_dir() else []
        if not slot_assets:
            palettes = self.path.parent / "Data/Palettes"
            slot_assets = sorted(palettes.glob("*/CS_*.json")) if palettes.is_dir() else []
        for slot_path in slot_assets:
            slot_data = json.loads(slot_path.read_text(encoding="utf-8"))
            slot_object = next((entry for entry in slot_data
                                if entry.get("Name", "").startswith("Default__")), None)
            if not slot_object:
                continue
            ref = slot_object.get("Properties", {}).get("BaseColorPalette", {}).get("ObjectPath", "")
            asset_path = ref.rsplit(".", 1)[0]
            palette_path = ROOT / f"{asset_path}.json"
            if not palette_path.is_file():
                continue
            palette_data = json.loads(palette_path.read_text(encoding="utf-8"))
            palette_object = next((entry for entry in palette_data
                                   if entry.get("Name", "").startswith("Default__")), None)
            definitions = (palette_object or {}).get("Properties", {}).get("CustomColorSlotDefinitions", [])
            palette_colors = {}
            names_by_default_color = {}
            for entry in definitions:
                name = entry.get("Key", "")
                value = entry.get("Value", {}).get("Hex", "")
                if name and value:
                    palette_colors[name] = value
                    names_by_default_color.setdefault(value.upper(), name)
            return palette_colors, names_by_default_color
        return {}, {}

    @staticmethod
    def ue_world_to_gltf(position, rotation):
        """Convert Unreal cm coordinates (X forward, Z up) to GLB meters."""
        x, y, z, w = rotation
        ue_rotation = np.array([
            [1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
            [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
            [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)],
        ], dtype=np.float32)
        # GLB basis = (Unreal Y, Unreal Z, Unreal X), a cyclic permutation.
        basis = np.array([[0, 1, 0], [0, 0, 1], [1, 0, 0]], dtype=np.float32)
        result = np.eye(4, dtype=np.float32)
        result[:3, :3] = basis @ ue_rotation @ basis.T
        result[:3, 3] = (position[1] * .01, position[2] * .01, position[0] * .01)
        return result

    def posed_primitives(self, world_pose):
        joint_matrices = np.stack([
            self.ue_world_to_gltf(*world_pose[self.skeleton_indices[name]][:2])
            for name in self.joints
        ]) @ self.inverse_bind
        result = []
        for positions, normals, joints, weights, indices, color, uv, texture, normal_texture in self.primitives:
            skinned = np.zeros((len(positions), 3), dtype=np.float32)
            skinned_normals = np.zeros((len(normals), 3), dtype=np.float32)
            homogeneous = np.concatenate((positions, np.ones((len(positions), 1), dtype=np.float32)), axis=1)
            for influence in range(joints.shape[1]):
                joint_ids = joints[:, influence]
                influence_weights = weights[:, influence:influence+1]
                matrices = joint_matrices[joint_ids]
                transformed = np.einsum("nij,nj->ni", matrices, homogeneous)[:, :3]
                skinned += transformed * influence_weights
                transformed_normals = np.einsum("nij,nj->ni", matrices[:, :3, :3], normals)
                skinned_normals += transformed_normals * influence_weights
            lengths = np.linalg.norm(skinned_normals, axis=1, keepdims=True)
            skinned_normals /= np.maximum(lengths, 1e-8)
            result.append((skinned, skinned_normals, indices, color, uv, texture, normal_texture))
        return result


def compose_world(local_pose, parents):
    world = []
    for i, ((local_t, local_q, sc), parent) in enumerate(zip(local_pose, parents)):
        if parent < 0:
            t, rot, scale = local_t, local_q, sc
        else:
            pt, pr, ps = world[parent]
            scaled = tuple(local_t[k] * ps[k] for k in range(3))
            rt = qrot(pr, scaled)
            t = tuple(pt[k] + rt[k] for k in range(3))
            rot = qmul(pr, local_q)
            scale = tuple(ps[k] * sc[k] for k in range(3))
        world.append((t, rot, scale))
    return world


def normalize_quat(q):
    length = math.sqrt(sum(v*v for v in q))
    return tuple(v / length for v in q) if length else (0, 0, 0, 1)


def sample_vector(keys, frame, default):
    if not keys:
        return default
    if frame <= keys[0][0]:
        return tuple(keys[0][1:])
    if frame >= keys[-1][0]:
        return tuple(keys[-1][1:])
    for left, right in zip(keys, keys[1:]):
        if left[0] <= frame <= right[0]:
            span = right[0] - left[0]
            alpha = (frame - left[0]) / span if span else 0.0
            return tuple(left[k] + (right[k]-left[k])*alpha for k in range(1, 4))
    return tuple(keys[-1][1:])


def sample_quat(keys, frame, default):
    if not keys:
        return default
    if frame <= keys[0][0]:
        return normalize_quat(keys[0][1:])
    if frame >= keys[-1][0]:
        return normalize_quat(keys[-1][1:])
    for left, right in zip(keys, keys[1:]):
        if left[0] <= frame <= right[0]:
            span = right[0] - left[0]
            alpha = (frame - left[0]) / span if span else 0.0
            a, b = normalize_quat(left[1:]), normalize_quat(right[1:])
            dot = sum(a[i]*b[i] for i in range(4))
            if dot < 0:
                b = tuple(-v for v in b)
                dot = -dot
            if dot > 0.9995:
                return normalize_quat(tuple(a[i] + alpha*(b[i]-a[i]) for i in range(4)))
            theta = math.acos(max(-1.0, min(1.0, dot)))
            sin_theta = math.sin(theta)
            wa = math.sin((1-alpha)*theta) / sin_theta
            wb = math.sin(alpha*theta) / sin_theta
            return normalize_quat(tuple(wa*a[i] + wb*b[i] for i in range(4)))
    return normalize_quat(keys[-1][1:])


def animation_local_pose(animation, ref_local, indices, frame):
    local = list(ref_local)
    for bone_name, track in animation["tracks"].items():
        index = indices.get(bone_name)
        if index is None:
            continue
        ref_t, ref_q, ref_s = ref_local[index]
        local[index] = (sample_vector(track["position"], frame, ref_t),
                        sample_quat(track["rotation"], frame, ref_q),
                        sample_vector(track["scale"], frame, ref_s))
    return local


def bone_point(bones, indices, bone_name, offset=(0, 0, 0)):
    # Rivals attack data refers to the character root as "Root"; the exported
    # Loxodont skeleton uses Unreal's actual bone name "Root_M".
    if bone_name == "Root" and bone_name not in indices:
        bone_name = "Root_M"
    idx = indices.get(bone_name)
    if idx is None:
        return None
    t, q, sc = bones[idx]
    scaled = tuple(offset[k] * sc[k] for k in range(3))
    r = qrot(q, scaled)
    return tuple(t[k] + r[k] for k in range(3))


def hurt_shapes(bones, indices):
    if not PHYSICS or not PHYSICS.is_file():
        return []
    data = json.loads(PHYSICS.read_text(encoding="utf-8"))
    shapes = []
    for entry in data:
        if entry.get("Type") != "SkeletalBodySetup":
            continue
        props = entry.get("Properties", {})
        bone = props.get("BoneName", "")
        body = bones[indices[bone]] if bone in indices else None
        if body is None:
            continue
        body_t, body_q, body_scale = body
        for shape_type, elems in props.get("AggGeom", {}).items():
            for elem in elems:
                center = vec(elem.get("Center", {}))
                local_center = tuple(center[k] * body_scale[k] for k in range(3))
                cr = qrot(body_q, local_center)
                c = tuple(body_t[k] + cr[k] for k in range(3))
                if shape_type == "SphylElems":
                    q = qmul(body_q, rotator_quat(elem.get("Rotation", {})))
                    axis = qrot(q, (0, 0, 1))
                    half = float(elem.get("Length", 0)) * .5
                    a = tuple(c[k] - axis[k]*half for k in range(3))
                    b = tuple(c[k] + axis[k]*half for k in range(3))
                    shapes.append({"shape": "capsule", "name": elem.get("Name", bone),
                                   "bone": bone, "a": a, "b": b,
                                   "r": float(elem.get("Radius", 0)),
                                   "collision": elem.get("CollisionEnabled", "unknown")})
                elif shape_type == "SphereElems":
                    shapes.append({"shape": "circle", "name": elem.get("Name", bone),
                                   "bone": bone, "c": c,
                                   "r": float(elem.get("Radius", 0)),
                                   "collision": elem.get("CollisionEnabled", "unknown")})
    return shapes


def load_attack(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return next((entry for entry in data if entry.get("Type", "").startswith("ATT_")), None)


def animation_basename(ref):
    if not isinstance(ref, dict):
        return None
    path = ref.get("ObjectPath", "")
    if path:
        base = path.rsplit("/", 1)[-1].split(".", 1)[0]
        if base:
            return base
    name = ref.get("ObjectName", "")
    return name.rsplit("'", 1)[-1].rstrip("'") or None


def find_animation(name):
    if not name:
        return None
    return next(CHAR.rglob(name + ".ueanim"), None) or next(CHAR.rglob(name + ".json"), None)


def list_animation_variants(props):
    variants = []
    for key in ("GroundedAnimation", "AerialAnimation"):
        name = animation_basename(props.get(key))
        if name:
            variants.append((key.removesuffix("Animation"), name, find_animation(name)))
    return variants or [("Default", None, None)]


def hitbox_attributes(props):
    attrs = {}
    for attr in props.get("HitboxAttributes", []) or []:
        if attr.get("Name"):
            attrs[attr["Name"]] = attr
    return attrs


def hitbox_spot(name, damage_property):
    """Infer sweet/sour classification from the game's hitbox/property names."""
    label = f"{name} {damage_property}".casefold()
    if "sweet" in label:
        return "sweet"
    if "sour" in label:
        return "sour"
    return "normal"


def format_frame_ranges(frames):
    frames = sorted(set(frames))
    if not frames:
        return None
    ranges = []
    start = previous = frames[0]
    for frame in frames[1:]:
        if frame != previous + 1:
            ranges.append((start, previous))
            start = frame
        previous = frame
    ranges.append((start, previous))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)


def move_info(attack, name, variant, animation):
    props = attack.get("Properties", {})
    windows = props.get("Windows", []) or []
    frame_cursor = 0
    active_frames = set()
    sets_by_start = {}
    iasa_values = []
    for window in windows:
        length = max(0, int(window.get("WindowLengthFrames", 0)))
        start = frame_cursor
        iasa = int(window.get("IasaFrame", -1))
        if iasa >= 0:
            iasa_values.append(start + iasa)
        for hit_window in window.get("HitboxWindows", []) or []:
            relative = max(0, int(hit_window.get("StartFrame", 0)))
            hit_length = int(hit_window.get("LengthInFrames", 0))
            if hit_length == 0:
                hit_length = max(0, length - relative)
            if hit_length <= 0:
                continue
            first = start + relative + 1  # display as 1-based gameplay frames
            last = start + relative + hit_length
            hit_frames = list(range(first, last + 1))
            active_frames.update(hit_frames)
            group = sets_by_start.setdefault(first, {"hit_frame": first, "hitboxes": set(), "frames": set()})
            group["hitboxes"].add(hit_window.get("HitboxName", "Hitbox"))
            group["frames"].update(hit_frames)
        frame_cursor += length

    iasa_frame = min(iasa_values) if iasa_values else None
    landing_lag = props.get("LandingLagFrames")
    hitbox_sets = []
    for index, start in enumerate(sorted(sets_by_start)):
        group = sets_by_start[start]
        hitbox_sets.append({"set": index, "hits_on_frame": start,
                            "active_frames": format_frame_ranges(group["frames"]),
                            "hitboxes": sorted(group["hitboxes"])})

    values = {}
    if iasa_frame is not None:
        values["IASA"] = iasa_frame
    if landing_lag is not None:
        values["Landing Lag"] = landing_lag
    hitbox_active = format_frame_ranges(active_frames)
    if hitbox_active is not None:
        values["Hitboxes active"] = hitbox_active
    return {
        "character": CHAR.name,
        "attack": name,
        "variant": variant,
        "animation": animation.stem if animation else None,
        "frame_numbering": "1-based gameplay frames",
        "stats": values,
        "hitbox_sets": hitbox_sets,
    }


def write_move_info(move_dir, info):
    json_path = move_dir / "info.json"
    json_path.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    lines = [f"# {info['attack']} ({info['variant']})", "", "## Stats", ""]
    for label, value in info["stats"].items():
        lines.append(f"- **{label}:** {value}")
    lines.extend(["", "## Hitbox sets", ""])
    if info["hitbox_sets"]:
        for group in info["hitbox_sets"]:
            lines.append(f"- **Hitbox set {group['set']} hits:** {group['hits_on_frame']}"
                         f" (active {group['active_frames']}; {', '.join(group['hitboxes'])})")
    else:
        lines.append("No hitbox windows are defined in this attack asset.")
    lines.append("")
    (move_dir / "info.md").write_text("\n".join(lines), encoding="utf-8")


def move_frames(attack, animation_path, ref_local, parents, indices, character_mesh=None):
    props = attack.get("Properties", {})
    windows = props.get("Windows", []) or []
    frame_count = max((int(w.get("AnimationStartFrame", 0)) +
                       int(w.get("AnimationLengthFrames", w.get("WindowLengthFrames", 0)))
                       for w in windows), default=1)
    animation = None
    if animation_path and animation_path.suffix.lower() == ".ueanim":
        animation = read_ueanim(str(animation_path.resolve()))
        frame_count = max(frame_count, int(animation["frames"]))
    elif animation_path and animation_path.is_file() and animation_path.suffix.lower() == ".json":
        try:
            anim = json.loads(animation_path.read_text(encoding="utf-8"))[0]
            frame_count = max(frame_count, int(anim.get("CompressedDataStructure", {}).get("CompressedNumberOfFrames", 0)))
        except (ValueError, IndexError, OSError, TypeError):
            pass

    attrs = hitbox_attributes(props)
    active_hitboxes = [[] for _ in range(frame_count)]
    events = [[] for _ in range(frame_count)]
    window_names = [None] * frame_count
    for window in windows:
        start = int(window.get("AnimationStartFrame", 0))
        length = int(window.get("AnimationLengthFrames", window.get("WindowLengthFrames", 0)))
        for frame in range(max(0, start), min(frame_count, start + length)):
            window_names[frame] = window.get("StringTableKey", "")
        for hwin in window.get("HitboxWindows", []) or []:
            name = hwin.get("HitboxName", "")
            relative_start = int(hwin.get("StartFrame", 0))
            begin = start + relative_start
            active_length = int(hwin.get("LengthInFrames", 0))
            # Workshop data defines zero as “remain active for the rest of
            # this attack window”, rather than an empty active interval.
            if active_length == 0:
                active_length = max(0, length - relative_start)
            end = begin + active_length
            for frame in range(max(0, begin), min(frame_count, end)):
                attr = attrs.get(name)
                if attr:
                    active_hitboxes[frame].append(attr)
        for event in window.get("HurtboxStateChanges", []) or []:
            frame = start + int(event.get("WindowFrame", 0))
            frame = min(frame_count - 1, max(0, frame))
            events[frame].append({"shape": "event", "kind": "hurtbox_state",
                                  "name": event.get("HurtboxName", "None"),
                                  "state": event.get("HurtboxState", "unknown"),
                                  "active": event.get("HurtboxActive", True)})

    frames = []
    animation_fps = float(animation["fps"]) if animation else FPS
    for i in range(frame_count):
        if animation:
            animation_frame = min(float(animation["frames"] - 1), i * animation_fps / FPS)
            local_pose = animation_local_pose(animation, ref_local, indices, animation_frame)
            pose_source = "ueanim"
        else:
            animation_frame = None
            local_pose = ref_local
            pose_source = "reference_skeleton"
        world_pose = compose_world(local_pose, parents)
        if animation and i > 0:
            previous_frame = max(0.0, (i - 1) * animation_fps / FPS)
            previous_local = animation_local_pose(animation, ref_local, indices, previous_frame)
            previous_world = compose_world(previous_local, parents)
        else:
            previous_world = world_pose
        hurt = hurt_shapes(world_pose, indices)
        for box in hurt:
            box["kind"] = "hurt"
        hits = []
        for attr in active_hitboxes[i]:
            attach = attr.get("OffsetBoneName")
            if not attach or attach == "None":
                attach = attr.get("BoneName", "")
            center = bone_point(world_pose, indices, attach, vec(attr.get("Offset", {})))
            if center is not None:
                interpolation = str(attr.get("InitialInterpolationMode", "None")).split("::")[-1]
                if interpolation == "Auto":
                    source_bone = attr.get("InitialInterpolationBoneName")
                    if not source_bone or source_bone == "None":
                        source_bone = attach
                    source_offset = attr.get("InitialInterpolationOffset", attr.get("Offset", {}))
                    start = bone_point(previous_world, indices, source_bone, vec(source_offset))
                elif interpolation == "Manual":
                    source_bone = attr.get("InitialInterpolationBoneName") or attach
                    if source_bone == "None":
                        source_bone = attach
                    source_offset = attr.get("InitialInterpolationOffset", {})
                    start = bone_point(world_pose, indices, source_bone, vec(source_offset))
                else:
                    start = None
                shape = {"shape": "circle", "kind": "hit", "c": center}
                if start is not None and interpolation in ("Auto", "Manual"):
                    shape = {"shape": "capsule", "kind": "hit", "a": start, "b": center}
                hits.append({**shape, "name": attr.get("Name", "Hitbox"), "bone": attach,
                             "r": float(attr.get("Radius", 0)),
                             "interpolation": interpolation,
                             "hit_response": str(attr.get("HitResponse", "")),
                             "damage_property": attr.get("OnHitPropertiesName", ""),
                             "spot": hitbox_spot(attr.get("Name", ""),
                                                 attr.get("OnHitPropertiesName", ""))})
        skeleton_lines = [{"a": world_pose[parent][0], "b": world_pose[index][0]}
                          for index, parent in enumerate(parents) if parent >= 0]
        frame_data = {"index": i + 1, "game_frame_zero_based": i,
                       "animation_frame": animation_frame,
                       "pose_source": pose_source, "window": window_names[i],
                       "skeleton": skeleton_lines,
                       "boxes": hurt + hits + events[i]}
        if character_mesh:
            frame_data["character_mesh"] = character_mesh.posed_primitives(world_pose)
        frames.append(frame_data)
    return frames


def camera_for_frames(frames, rotation_clockwise=90):
    # Fit the union of every sampled hurtbox and hitbox so later active frames
    # cannot be clipped as the weapon moves away from the character.
    theta = math.radians(-rotation_clockwise)
    right = (-math.sin(theta), math.cos(theta))
    min_h = min_z = float("inf")
    max_h = max_z = float("-inf")
    for frame in frames:
        for vertices, _, _, _, _, _, _ in frame.get("character_mesh", []):
            if len(vertices):
                # Convert GLB (X=UE Y, Y=UE Z, Z=UE X) meters back to UE cm,
                # then fit the rotated camera's horizontal axis.
                h = vertices[:, 2] * (100 * right[0]) + vertices[:, 0] * (100 * right[1])
                min_h = min(min_h, float(h.min()) - 5)
                max_h = max(max_h, float(h.max()) + 5)
                min_z = min(min_z, float(vertices[:, 1].min()) * 100 - 5)
                max_z = max(max_z, float(vertices[:, 1].max()) * 100 + 5)
        for box in frame["boxes"]:
            if box.get("kind") not in ("hit", "hurt"):
                continue
            radius = float(box.get("r", 0))
            points = [box["a"], box["b"]] if box["shape"] == "capsule" else [box["c"]]
            for point in points:
                horizontal = point[0] * right[0] + point[1] * right[1]
                min_h = min(min_h, horizontal - radius)
                max_h = max(max_h, horizontal + radius)
                min_z = min(min_z, point[2] - radius)
                max_z = max(max_z, point[2] + radius)
    if not math.isfinite(min_h):
        return {"scale": SCALE, "center_horizontal": 0.0, "center_z": VIEW_CENTER_Z,
                "right": right, "rotation_clockwise": rotation_clockwise}
    span_h, span_z = max(1.0, max_h-min_h), max(1.0, max_z-min_z)
    scale = min((WIDTH-110)/span_h, (HEIGHT-150)/span_z)
    return {"scale": scale, "center_horizontal": (min_h+max_h)*.5,
            "center_z": (min_z+max_z)*.5, "right": right,
            "rotation_clockwise": rotation_clockwise}


def project(point, camera):
    horizontal = point[0]*camera["right"][0] + point[1]*camera["right"][1]
    return (round(WIDTH / 2 + (horizontal-camera["center_horizontal"]) * camera["scale"]),
            round(HEIGHT / 2 - (point[2]-camera["center_z"]) * camera["scale"]))


def draw_textured_mesh(layer, primitives, camera):
    """Rasterize palette and normal mapped skinned triangles with a z-buffer."""
    color_buffer = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    depth_buffer = np.full((HEIGHT, WIDTH), -np.inf, dtype=np.float32)
    theta = math.radians(-camera["rotation_clockwise"])
    view_x, view_y = math.cos(theta), math.sin(theta)
    light = np.array((-0.35, 0.65, 0.68), dtype=np.float32)
    light /= np.linalg.norm(light)
    for vertices, normals, indices, _, uvs, texture, normal_texture in primitives:
        if texture is None or uvs is None:
            continue
        tex_h, tex_w = texture.shape[:2]
        if normal_texture is not None:
            norm_h, norm_w = normal_texture.shape[:2]
        for tri in indices.reshape(-1, 3):
            v = vertices[tri]
            n = normals[tri]
            uv = uvs[tri]
            horizontal = v[:, 2] * (100 * camera["right"][0]) + v[:, 0] * (100 * camera["right"][1])
            screen_x = WIDTH / 2 + (horizontal-camera["center_horizontal"]) * camera["scale"]
            screen_y = HEIGHT / 2 - (v[:, 1]*100-camera["center_z"]) * camera["scale"]
            x0 = max(0, int(math.floor(float(screen_x.min()))))
            x1 = min(WIDTH, int(math.ceil(float(screen_x.max()))) + 1)
            y0 = max(0, int(math.floor(float(screen_y.min()))))
            y1 = min(HEIGHT, int(math.ceil(float(screen_y.max()))) + 1)
            if x1 <= x0 or y1 <= y0:
                continue
            x_a, y_a = float(screen_x[0]), float(screen_y[0])
            x_b, y_b = float(screen_x[1]), float(screen_y[1])
            x_c, y_c = float(screen_x[2]), float(screen_y[2])
            denom = (y_b-y_c)*(x_a-x_c) + (x_c-x_b)*(y_a-y_c)
            if abs(denom) < 1e-8:
                continue
            gx, gy = np.meshgrid(np.arange(x0, x1, dtype=np.float32) + .5,
                                 np.arange(y0, y1, dtype=np.float32) + .5)
            wa = ((y_b-y_c)*(gx-x_c) + (x_c-x_b)*(gy-y_c)) / denom
            wb = ((y_c-y_a)*(gx-x_c) + (x_a-x_c)*(gy-y_c)) / denom
            wc = 1.0 - wa - wb
            inside = (wa >= -1e-5) & (wb >= -1e-5) & (wc >= -1e-5)
            if not inside.any():
                continue
            depth = wa*(v[0, 2]*view_x+v[0, 0]*view_y) + \
                    wb*(v[1, 2]*view_x+v[1, 0]*view_y) + \
                    wc*(v[2, 2]*view_x+v[2, 0]*view_y)
            region = depth_buffer[y0:y1, x0:x1]
            visible = inside & (depth >= region)
            if not visible.any():
                continue
            u = wa*uv[0, 0] + wb*uv[1, 0] + wc*uv[2, 0]
            vcoord = wa*uv[0, 1] + wb*uv[1, 1] + wc*uv[2, 1]
            tx = np.clip((u * tex_w).astype(np.int32), 0, tex_w-1)
            ty = np.clip((vcoord * tex_h).astype(np.int32), 0, tex_h-1)
            base_rgb = texture[ty, tx].astype(np.float32)
            if normal_texture is not None:
                du1, dv1 = uv[1]-uv[0]
                du2, dv2 = uv[2]-uv[0]
                determinant = du1*dv2-dv1*du2
                if abs(float(determinant)) > 1e-8:
                    edge1, edge2 = v[1]-v[0], v[2]-v[0]
                    tangent = (edge1*dv2-edge2*dv1) / determinant
                    bitangent = (edge2*du1-edge1*du2) / determinant
                    norm = np.linalg.norm(tangent)
                    if norm > 1e-8:
                        tangent /= norm
                    norm = np.linalg.norm(bitangent)
                    if norm > 1e-8:
                        bitangent /= norm
                    geom_n = wa[..., None]*n[0] + wb[..., None]*n[1] + wc[..., None]*n[2]
                    geom_n /= np.maximum(np.linalg.norm(geom_n, axis=2, keepdims=True), 1e-8)
                    tangent_grid = tangent - geom_n*np.sum(geom_n*tangent, axis=2, keepdims=True)
                    tangent_grid /= np.maximum(np.linalg.norm(tangent_grid, axis=2, keepdims=True), 1e-8)
                    handedness = 1.0 if np.dot(np.cross(edge1, edge2), np.cross(tangent, bitangent)) >= 0 else -1.0
                    bitangent_grid = np.cross(geom_n, tangent_grid) * handedness
                    ntex = normal_texture[np.clip((vcoord*norm_h).astype(np.int32), 0, norm_h-1),
                                          np.clip((u*norm_w).astype(np.int32), 0, norm_w-1)].astype(np.float32) / 127.5 - 1
                    # Rivals normal maps are Unreal/DirectX convention: invert green for this renderer.
                    ntex[:, :, 1] *= -1
                    mapped_n = (tangent_grid*ntex[:, :, 0:1] +
                                bitangent_grid*ntex[:, :, 1:2] + geom_n*ntex[:, :, 2:3])
                    mapped_n /= np.maximum(np.linalg.norm(mapped_n, axis=2, keepdims=True), 1e-8)
                    diffuse = np.maximum(0, np.sum(mapped_n*light, axis=2))
                    shade = np.where(diffuse < .30, .42,
                                     np.where(diffuse < .62, .70, 1.0)).astype(np.float32)
                    base_rgb *= shade[..., None]
            pixels = color_buffer[y0:y1, x0:x1]
            pixels[visible] = np.clip(base_rgb[visible], 0, 255).astype(np.uint8)
            region[visible] = depth[visible]
    rgba = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    rgba[:, :, :3] = color_buffer
    rgba[:, :, 3] = (depth_buffer > -np.inf).astype(np.uint8) * 255
    layer.alpha_composite(Image.fromarray(rgba, "RGBA"))


def render_frame(image_path, move_name, frame, camera, show_skeleton=True, show_character=False):
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    if show_character and frame.get("character_mesh"):
        theta = math.radians(-camera["rotation_clockwise"])
        mesh_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        if any(texture is not None for _, _, _, _, _, texture, _ in frame["character_mesh"]):
            draw_textured_mesh(mesh_layer, frame["character_mesh"], camera)
        else:
            draw = ImageDraw.Draw(mesh_layer, "RGBA")
            triangles = []
            for vertices, _, indices, color, _, _, _ in frame["character_mesh"]:
                for tri in indices.reshape(-1, 3):
                    points = vertices[tri]
                    # Painter's order: draw the far side before the near side.
                    depth = float((points[:, 2]*math.cos(theta) +
                                   points[:, 0]*math.sin(theta)).mean())
                    screen = [project((float(v[2])*100, float(v[0])*100, float(v[1])*100), camera)
                              for v in points]
                    triangles.append((depth, screen, color))
            triangles.sort(key=lambda item: item[0])
            for _, points, color in triangles:
                draw.polygon(points, fill=(*color, 255))
        image = Image.alpha_composite(image, mesh_layer)
        mesh_layer.close()
    # Draw the animated rig first so it remains visible through translucent
    # hit/hurt shapes. ImageDraw writes RGBA pixels directly; alpha_composite
    # is needed for proper source-over transparency between layers.
    if show_skeleton:
        skeleton_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(skeleton_layer, "RGBA")
        for link in frame.get("skeleton", []):
            a, b = project(link["a"], camera), project(link["b"], camera)
            draw.line((a, b), fill=(235, 240, 245, 128), width=2)
        image = Image.alpha_composite(image, skeleton_layer)
        skeleton_layer.close()

    for box in (b for b in frame["boxes"] if b.get("kind") == "hurt"):
        # Keep hurtboxes visible while making the underlying transparent
        # background and overlapping shapes easy to see through.
        edge, fill = (0, 220, 255, 128), (0, 150, 255, 128)
        if "NoCollision" in box.get("collision", ""):
            edge, fill = (45, 105, 180, 128), (15, 55, 115, 128)
        hurt_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(hurt_layer, "RGBA")
        if box["shape"] == "capsule":
            a, b, r = project(box["a"], camera), project(box["b"], camera), box["r"]*camera["scale"]
            draw.line((a, b), fill=edge, width=max(1, round(2*r)))
            for cx, cy in (a, b):
                draw.ellipse((cx-r, cy-r, cx+r, cy+r), fill=fill, outline=edge, width=2)
        else:
            c, r = project(box["c"], camera), box["r"]*camera["scale"]
            draw.ellipse((c[0]-r, c[1]-r, c[0]+r, c[1]+r), fill=fill, outline=edge, width=2)
        # Composite each hurtbox separately so overlap increases opacity while
        # the animated skeleton remains visible beneath translucent fills.
        image = Image.alpha_composite(image, hurt_layer)
        hurt_layer.close()

    hitboxes = [b for b in frame["boxes"] if b.get("kind") == "hit"]
    for box in hitboxes:
        r = box["r"]*camera["scale"]
        response = box.get("hit_response", "").casefold()
        if "hitgrab" in response:
            color = (255, 0, 200, 255)  # hit grab
        elif "grab" in response:
            color = (170, 45, 255, 255)  # normal or command grab
        else:
            color = {
                "sweet": (213, 0, 249, 255),
                "sour": (255, 176, 0, 255),
            }.get(box.get("spot"), (255, 23, 68, 255))
        edge, fill = color, color
        layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer, "RGBA")
        if box["shape"] == "capsule":
            a, b = project(box["a"], camera), project(box["b"], camera)
            draw.line((a, b), fill=edge, width=max(1, round(2*r)))
            for cx, cy in (a, b):
                draw.ellipse((cx-r, cy-r, cx+r, cy+r), fill=fill, outline=edge, width=3)
            c = b
        else:
            c = project(box["c"], camera)
            draw.ellipse((c[0]-r, c[1]-r, c[0]+r, c[1]+r),
                         fill=fill, outline=edge, width=3)
        image = Image.alpha_composite(image, layer)
        layer.close()
    image.save(image_path)
    image.close()


def export_variant(attack, name, variant, animation, ref_local, parents, indices, out_dir,
                   show_skeleton=True, show_character=False, character_mesh=None,
                   camera_rotation=90):
    move_dir = out_dir / f"{name}_{variant}"
    frames = move_frames(attack, animation, ref_local, parents, indices, character_mesh)
    camera = camera_for_frames(frames, camera_rotation)
    move_dir.mkdir(parents=True, exist_ok=True)
    frame_stem = f"RoA2_{CHAR.name}_{name}_{variant}_HB_"
    for stale in (*move_dir.glob("frame_*.png"), *move_dir.glob(f"{frame_stem}*.png")):
        stale.unlink()
    for frame in frames:
        filename = f"{frame_stem}{frame['index']:04d}.png"
        render_frame(move_dir / filename, f"{name} ({variant})",
                     frame, camera, show_skeleton, show_character)
    write_move_info(move_dir, move_info(attack, name, variant, animation))
    pose_source = "ueanim" if animation and animation.suffix.lower() == ".ueanim" else "reference_skeleton"
    data = {"move": name, "variant": variant, "animation": animation.stem if animation else None,
            "fps": FPS, "animation_source_fps": read_ueanim(str(animation.resolve()))["fps"]
            if pose_source == "ueanim" else None,
            "pose_source": pose_source,
            "pose_warning": None if pose_source == "ueanim" else REFERENCE_WARNING,
            "camera": camera,
            "hurtbox_asset": str(PHYSICS.relative_to(ROOT)) if PHYSICS else None,
            "frames": [{key: value for key, value in frame.items() if key != "character_mesh"}
                       for frame in frames]}
    (move_dir / "boxes.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return move_dir, len(frames)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--character", default="Loxodont",
                        help="character folder name (default: Loxodont)")
    parser.add_argument("--all", action="store_true", dest="all_characters",
                        help="process every character folder with attack and skeleton data")
    parser.add_argument("--move", help="one attack asset basename, e.g. ATT_Lox_Bair (default: all)")
    skeleton_group = parser.add_mutually_exclusive_group()
    skeleton_group.add_argument("--show-skeleton", action="store_true", dest="show_skeleton",
                                help="draw animated bone connections (default)")
    skeleton_group.add_argument("--hide-skeleton", action="store_false", dest="show_skeleton",
                                help="omit animated bone connections from PNGs")
    parser.set_defaults(show_skeleton=True)
    parser.add_argument("--show-character", action="store_true",
                        help="render the character mesh from Skins/Default/*.glb beneath boxes")
    parser.add_argument("--skin-textures", "--skin-texture", action="store_true",
                        help="bake the exported color-region masks and material palette onto the mesh (implies --show-character)")
    parser.add_argument("--camera-rotation", type=float,
                        help="rotate view clockwise around the character in degrees (mesh overlay defaults to 90)")
    parser.add_argument("--out", type=Path,
                        help="output folder (default: Rivals2/HitboxFrames/<character>)")
    args = parser.parse_args()
    show_character = args.show_character or args.skin_textures
    global CHAR, ATTACKS, PHYSICS, SKELETON

    def discover(character):
        char_dir = CHARACTERS_ROOT / character
        attacks_dir = char_dir / "Attacks"
        attack_paths = sorted(attacks_dir.glob("ATT_*.json"))
        animation_dir = char_dir / "Animation"
        skeletons = sorted(animation_dir.glob("*Skeleton.json"))
        if not attack_paths or not skeletons:
            return None
        # Prefer the body physics asset whose basename best matches the attack
        # prefix (e.g. ATT_Lox_* -> HB_Lox); accessory physics files are ignored.
        attack_prefix = attack_paths[0].stem.removeprefix("ATT_").split("_")[0].casefold()
        physics = list(char_dir.glob("HB_*.json"))
        physics.sort(key=lambda p: (not p.stem.removeprefix("HB_").casefold().startswith(attack_prefix),
                                   len(p.stem), p.name))
        return char_dir, attacks_dir, physics[0] if physics else None, skeletons[0], attack_paths

    characters = sorted(p.name for p in CHARACTERS_ROOT.iterdir() if p.is_dir()) if args.all_characters else [args.character]
    overall_moves = overall_frames = 0
    for character in characters:
        found = discover(character)
        if not found:
            if args.all_characters:
                print(f"Skipping {character}: no attack JSONs or primary Animation/*Skeleton.json")
                continue
            raise SystemExit(f"Could not find attack JSONs and a skeleton under {CHARACTERS_ROOT / character}")
        CHAR, ATTACKS, PHYSICS, SKELETON, paths = found
        default_out = ROOT / "Rivals2/HitboxFrames"
        out_dir = (args.out / character if args.all_characters else args.out) if args.out else default_out / character
        if args.move:
            paths = [p for p in paths if p.stem.casefold() == args.move.casefold()]
            if not paths:
                if args.all_characters:
                    continue
                raise SystemExit(f"No attack JSON named {args.move!r} under {ATTACKS}")
        _, indices, ref_local, parents = read_skeleton()
        character_mesh = None
        if show_character:
            glb_candidates = sorted((CHAR / "Skins/Default").glob("SK_*_Default.glb"))
            if not glb_candidates:
                print(f"{character}: --show-character requested, but no Skins/Default/SK_*_Default.glb was found")
            else:
                try:
                    character_mesh = GLBCharacter(glb_candidates[0], indices,
                                                  skin_textures=args.skin_textures)
                    print(f"{character}: rendering mesh {glb_candidates[0].relative_to(ROOT)}")
                except (OSError, ValueError, KeyError, IndexError) as exc:
                    raise SystemExit(f"Could not load character GLB {glb_candidates[0]}: {exc}") from exc
        total_moves = total_frames = 0
        needed_ueanim, missing_ueanim = set(), set()
        for path in paths:
            attack = load_attack(path)
            if not attack:
                continue
            props = attack.get("Properties", {})
            for variant, _, animation in list_animation_variants(props):
                anim_name = animation_basename(props.get(variant + "Animation"))
                if anim_name:
                    needed_ueanim.add(anim_name + ".ueanim")
                    if not animation or animation.suffix.lower() != ".ueanim":
                        missing_ueanim.add(anim_name + ".ueanim")
                move_dir, count = export_variant(attack, path.stem, variant, animation,
                                                 ref_local, parents, indices, out_dir,
                                                 args.show_skeleton, show_character,
                                                 character_mesh,
                                                 args.camera_rotation if args.camera_rotation is not None
                                                 else (90 if show_character else 0))
                total_moves += 1
                total_frames += count
                try:
                    display_path = move_dir.relative_to(ROOT)
                except ValueError:
                    display_path = move_dir
                print(f"{display_path}: {count} frames")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "required_animation_ueanim.txt").write_text(
            "\n".join(sorted(needed_ueanim)) + "\n", encoding="utf-8")
        missing_path = out_dir / "missing_animation_ueanim.txt"
        missing_path.write_text("\n".join(sorted(missing_ueanim)) + ("\n" if missing_ueanim else ""), encoding="utf-8")
        print(f"{character}: rendered {total_frames} PNG frames across {total_moves} move variants to {out_dir}")
        if not PHYSICS:
            print(f"{character}: no HB_*.json physics asset found; output will contain hitboxes and hurtbox-state events only")
        if missing_ueanim:
            print(f"Missing {len(missing_ueanim)} animation files; see {missing_path}")
        overall_moves += total_moves
        overall_frames += total_frames
    if args.all_characters:
        print(f"All-character run: {overall_frames} frames across {overall_moves} move variants")


if __name__ == "__main__":
    main()

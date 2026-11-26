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
import re
import struct
from pathlib import Path

import numpy as np

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
except ImportError as exc:
    raise SystemExit("PNG rendering requires Pillow: python -m pip install pillow") from exc


ROOT = Path(__file__).resolve().parent
CHAR = ROOT / "Rivals2/Content/Characters/Loxodont"
ATTACKS = CHAR / "Attacks"
PHYSICS = CHAR / "HB_Lox.json"
CHARACTER_DATA = CHAR / "CD_Loxodont.json"   # hurtbox definitions and physics asset
SKELETON = CHAR / "Animation/SK_Lox_Skeleton.json"
OUT_DEFAULT = ROOT / "Rivals2/HitboxFrames/Loxodont"
CHARACTERS_ROOT = ROOT / "Rivals2/Content/Characters"
FPS = 60
WIDTH, HEIGHT = 900, 680
SCALE = 1.8
VIEW_CENTER_Z = 95
REFERENCE_WARNING = "REFERENCE POSE ONLY — matching .ueanim not found"
# FModel glTF axes: GLB (X, Y, Z) = Unreal (X, Z, Y), in meters instead of cm.
UE_TO_GLTF_BASIS = np.array([[1, 0, 0], [0, 0, 1], [0, 1, 0]], dtype=np.float32)

# ---------------------------------------------------------------------------
# Box styling and other user settings come from render_settings.json (edit
# that file, not this one). The values below are only fallbacks for entries
# the file doesn't have. Colors are (R, G, B) 0-255; alpha is 0-255.
# ---------------------------------------------------------------------------
SETTINGS_FILE = ROOT / "render_settings.json"


def _load_settings():
    if not SETTINGS_FILE.is_file():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"{SETTINGS_FILE.name} is not valid JSON: {exc}") from exc


SETTINGS = _load_settings()


def setting(path, default):
    """render_settings.json value at a dotted path, else the default."""
    node = SETTINGS
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def color_setting(path, default):
    """An (R, G, B) tuple from [R, G, B] or "#RRGGBB" in the settings."""
    value = setting(path, default)
    try:
        if isinstance(value, str):
            text = value.strip().lstrip("#")
            rgb = tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
        else:
            rgb = tuple(int(channel) for channel in value[:3])
        if len(rgb) != 3 or not all(0 <= channel <= 255 for channel in rgb):
            raise ValueError
        return rgb
    except (TypeError, ValueError, IndexError):
        raise SystemExit(f"{SETTINGS_FILE.name}: {path} must be [R, G, B] (0-255) or \"#RRGGBB\", got {value!r}")


HITBOX_FILL_ALPHA = int(setting("hitbox.fill_alpha", 150))
HITBOX_OUTLINE_ALPHA = int(setting("hitbox.outline_alpha", 255))
HITBOX_OUTLINE_WIDTH = int(setting("hitbox.outline_width", 3))  # px

# Hitboxes are chosen by the attack's HitResponse first, then projectile,
# tipper, then a sweet/sour label in the hitbox or damage-property name.
HITBOX_FILL_COLOR_NORMAL = color_setting("hitbox.colors.normal", (237, 56, 92))
HITBOX_FILL_COLOR_SWEET = color_setting("hitbox.colors.sweet", (213, 0, 249))
HITBOX_FILL_COLOR_SOUR = color_setting("hitbox.colors.sour", (255, 176, 0))
HITBOX_FILL_COLOR_TIPPER = color_setting("hitbox.colors.tipper", (79, 195, 247))
HITBOX_FILL_COLOR_PROJECTILE = color_setting("hitbox.colors.projectile", (255, 235, 59))
HITBOX_FILL_COLOR_GRAB = color_setting("hitbox.colors.grab", (153, 0, 255))
HITBOX_FILL_COLOR_HITGRAB = color_setting("hitbox.colors.hitgrab", (255, 0, 200))
HITBOX_FILL_COLOR_DETECT = color_setting("hitbox.colors.detect", (164, 121, 20))
HITBOX_FILL_COLOR_KILL_PROJECTILES = color_setting("hitbox.colors.kill_projectiles", (141, 110, 99))

HURTBOX_FILL_ALPHA = int(setting("hurtbox.fill_alpha", 80))  # overlaps stack
HURTBOX_OUTLINE_ALPHA = int(setting("hurtbox.outline_alpha", 160))
HURTBOX_OUTLINE_WIDTH = int(setting("hurtbox.outline_width", 2))  # px
HURTBOX_OUTLINE_LIGHTEN = float(setting("hurtbox.outline_lighten", 0.45))  # 0 = fill, 1 = white
HURTBOX_SHOW_DISABLED = bool(setting("hurtbox.show_disabled", False))  # draw HurtboxActive false?

# Hurtbox gameplay states (ERivalsHurtboxState). Each hurtbox starts in its
# default state from CD_<Char>.json HurtboxDefinitions (else Default) and
# moves change it with Windows[].HurtboxStateChanges in ATT_*.json.
HURTBOX_FILL_COLOR_DEFAULT = color_setting("hurtbox.colors.Default", (66, 255, 37))
HURTBOX_FILL_COLOR_VULNERABLE = color_setting("hurtbox.colors.Vulnerable", (0, 150, 255))
HURTBOX_FILL_COLOR_UNGRABBABLE = color_setting("hurtbox.colors.Ungrabbable", (244, 255, 41))
HURTBOX_FILL_COLOR_UNHITGRABBABLE = color_setting("hurtbox.colors.Unhitgrabbable", (0, 255, 242))
HURTBOX_FILL_COLOR_INTANGIBLE = color_setting("hurtbox.colors.Intangible", (190, 190, 205))
HURTBOX_FILL_COLOR_PROJECTILE_INTANGIBLE = color_setting("hurtbox.colors.ProjectileIntangible", (0, 79, 163))
HURTBOX_FILL_COLOR_INVULNERABLE = color_setting("hurtbox.colors.Invulnerable", (28, 255, 145))
HURTBOX_FILL_COLOR_COUNTERING = color_setting("hurtbox.colors.Countering", (255, 120, 40))
HURTBOX_FILL_COLOR_PARRYING = color_setting("hurtbox.colors.Parrying", (115, 255, 0))
HURTBOX_FILL_COLOR_INACTIVE = color_setting("hurtbox.colors.Inactive", (70, 70, 80))
HURTBOX_FILL_COLOR_DISABLED = color_setting("hurtbox.colors.Disabled", (40, 40, 40))

HURTBOX_STATE_COLORS = {
    "Default": HURTBOX_FILL_COLOR_DEFAULT,
    "Vulnerable": HURTBOX_FILL_COLOR_VULNERABLE,
    "Ungrabbable": HURTBOX_FILL_COLOR_UNGRABBABLE,
    "Unhitgrabbable": HURTBOX_FILL_COLOR_UNHITGRABBABLE,
    "Intangible": HURTBOX_FILL_COLOR_INTANGIBLE,
    "ProjectileIntangible": HURTBOX_FILL_COLOR_PROJECTILE_INTANGIBLE,
    "Invulnerable": HURTBOX_FILL_COLOR_INVULNERABLE,
    "Countering": HURTBOX_FILL_COLOR_COUNTERING,
    "Parrying": HURTBOX_FILL_COLOR_PARRYING,
    "Inactive": HURTBOX_FILL_COLOR_INACTIVE,
}
SKELETON_COLOR = color_setting("skeleton.color", (235, 240, 245))
SKELETON_ALPHA = int(setting("skeleton.alpha", 128))

def gltf_to_ue_cm(vertices):
    """Return Unreal (X, Y, Z) centimeter components of GLB-space vertices."""
    return vertices[..., 0] * 100, vertices[..., 2] * 100, vertices[..., 1] * 100


def short_enum(value, default=""):
    """'ERivalsHurtboxState::Intangible' -> 'Intangible'."""
    return str(value).split("::")[-1] if value is not None else default


# On-hit SpecialEffect values drawn as tippers. Training mode colors these
# boxes (light blue) and no other effect; AutoTipperOnly is the throw variant.
TIPPER_EFFECTS = {str(effect).casefold() for effect in setting("tipper_effects", ["TipperStun", "AutoTipperOnly"])}


def hitbox_color(box):
    """Pick a hitbox color from its HitResponse, then projectile, tipper, sweet/sour."""
    response = short_enum(box.get("hit_response")).casefold()
    if response == "hitgrab":
        return HITBOX_FILL_COLOR_HITGRAB
    if response == "grab":
        return HITBOX_FILL_COLOR_GRAB
    if response in ("detect", "detectall"):
        return HITBOX_FILL_COLOR_DETECT
    if response == "killprojectiles":
        return HITBOX_FILL_COLOR_KILL_PROJECTILES
    # Training mode shows every projectile hit yellow (Zetterburn's fireball),
    # whatever its sweet/sour naming, so this comes before those.
    if box.get("article_projectile"):
        return HITBOX_FILL_COLOR_PROJECTILE
    if short_enum(box.get("effect")).casefold() in TIPPER_EFFECTS:
        return HITBOX_FILL_COLOR_TIPPER
    return {"sweet": HITBOX_FILL_COLOR_SWEET,
            "sour": HITBOX_FILL_COLOR_SOUR}.get(box.get("spot"), HITBOX_FILL_COLOR_NORMAL)


def hurtbox_color(box):
    if not box.get("active", True):
        return HURTBOX_FILL_COLOR_DISABLED
    return HURTBOX_STATE_COLORS.get(box.get("state", "Default"), HURTBOX_FILL_COLOR_DEFAULT)


def hurtbox_outline_color(fill):
    """Hurtbox outlines are the fill tinted toward white by HURTBOX_OUTLINE_LIGHTEN."""
    return tuple(round(c + (255 - c) * HURTBOX_OUTLINE_LIGHTEN) for c in fill)


def semantic_colors():
    """Every configured box color, for GIF palette reservation."""
    hit = [HITBOX_FILL_COLOR_NORMAL, HITBOX_FILL_COLOR_SWEET, HITBOX_FILL_COLOR_SOUR,
           HITBOX_FILL_COLOR_TIPPER, HITBOX_FILL_COLOR_PROJECTILE, HITBOX_FILL_COLOR_GRAB, HITBOX_FILL_COLOR_HITGRAB, HITBOX_FILL_COLOR_DETECT,
           HITBOX_FILL_COLOR_KILL_PROJECTILES]
    return list(dict.fromkeys([*hit, *HURTBOX_STATE_COLORS.values(),
                               HURTBOX_FILL_COLOR_DISABLED, SKELETON_COLOR]))


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


def read_skeleton(path=None):
    data = json.loads(Path(path or SKELETON).read_text(encoding="utf-8"))
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

    def __init__(self, path, skeleton_indices, skin_textures=False, palette_dir=None):
        self.path = Path(path)
        # Where Data/Palettes lives: the skin folder. Article meshes sit in a
        # subfolder (Skins/Default/ChainHand) but use the skin's palette.
        self.palette_dir = Path(palette_dir) if palette_dir else self.path.parent
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
        neutral = self.palette_dir / "Data/Palettes/Neutral"
        slot_assets = sorted(neutral.glob("CS_*.json")) if neutral.is_dir() else []
        if not slot_assets:
            palettes = self.palette_dir / "Data/Palettes"
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
        """Convert Unreal cm coordinates (left-handed, Z up) to GLB meters.

        FModel's glTF export swaps Unreal Y and Z (GLB = UE X, UE Z, UE Y).
        That swap is a reflection, which is what converts Unreal's
        left-handed basis to glTF's right-handed one. A cyclic permutation
        (no reflection) mirrors the posed skeleton against the mesh: centered
        bones still line up, but left/right bones trade sides and asymmetric
        parts such as the axe head fold onto themselves.
        """
        x, y, z, w = rotation
        ue_rotation = np.array([
            [1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
            [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
            [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)],
        ], dtype=np.float32)
        result = np.eye(4, dtype=np.float32)
        result[:3, :3] = UE_TO_GLTF_BASIS @ ue_rotation @ UE_TO_GLTF_BASIS.T
        result[:3, 3] = (position[0] * .01, position[2] * .01, position[1] * .01)
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
    # Attack data uses "Root" for the character origin, not a skeleton bone:
    # no exported skeleton has a bone with that name. Its offsets are in
    # gameplay axes (X forward, Z up; Y is almost always 0). The skeleton's
    # top bone "Group" is that origin: at the feet with identity rotation in
    # every animation. "Root_M" is the pelvis control, ~1 m up with a rotated
    # frame, and would turn vertical offsets sideways.
    if bone_name == "Root" and bone_name not in indices:
        if "Group" not in indices:
            return tuple(float(v) for v in offset)
        bone_name = "Group"
    idx = indices.get(bone_name)
    if idx is None:
        return None
    t, q, sc = bones[idx]
    scaled = tuple(offset[k] * sc[k] for k in range(3))
    r = qrot(q, scaled)
    return tuple(t[k] + r[k] for k in range(3))


@functools.lru_cache(maxsize=32)
def load_physics_bodies(path_string):
    """Parse a PhysicsAsset JSON once: [(bone name, AggGeom dict), ...]."""
    path = Path(path_string)
    if not path.is_file():
        return ()
    bodies = []
    for entry in json.loads(path.read_text(encoding="utf-8")):
        if entry.get("Type") == "SkeletalBodySetup":
            props = entry.get("Properties", {})
            bodies.append((props.get("BoneName", ""), props.get("AggGeom", {})))
    return tuple(bodies)


@functools.lru_cache(maxsize=32)
def load_character_data(path_string):
    """Return the CD_<Char>.json properties holding hurtbox setup, or {}."""
    path = Path(path_string)
    if not path.is_file():
        return {}
    for entry in json.loads(path.read_text(encoding="utf-8")):
        props = entry.get("Properties", {})
        if "HurtboxDefinitions" in props or "HurtboxesPhysicsAsset" in props:
            return props
    return {}


def hurtbox_definitions():
    """HurtboxDefinitions from the character data asset, keyed by name.

    "Existing" entries override a physics-asset body's default state/active
    flag. "NewSphere"/"NewCapsule" entries add hurtboxes positioned from the
    character origin (gameplay axes: X forward, Z up); temporary ones start
    disabled until a move's HurtboxStateChanges enables them by name.
    """
    data = load_character_data(str(CHARACTER_DATA)) if CHARACTER_DATA else {}
    definitions = {}
    for entry in data.get("HurtboxDefinitions", []) or []:
        name = entry.get("HurtboxName", "")
        if name:
            definitions[name] = {
                "type": short_enum(entry.get("HurtboxDefinitionType"), "Existing"),
                "state": short_enum(entry.get("HurtboxDefaultState"), "Default"),
                "active": bool(entry.get("HurtboxActive", True)),
                "temporary": bool(entry.get("bIsTemporary", False)),
                "a": vec(entry.get("OffsetFromOrigin", {})),
                "b": vec(entry.get("OffsetFromOrigin2", {})),
                "r": float(entry.get("HurtboxRadius", 0)),
            }
    return definitions


def hurtbox_defaults():
    """Starting {name: {"state", "active", "temporary"}} for every hurtbox."""
    defaults = {}
    for bone, _ in load_physics_bodies(str(PHYSICS)) if PHYSICS else ():
        defaults[bone] = {"state": "Default", "active": True, "temporary": False}
    for name, definition in hurtbox_definitions().items():
        if definition["type"] == "Existing" and name not in defaults:
            continue  # override for a body this physics asset does not have
        defaults[name] = {key: definition[key] for key in ("state", "active", "temporary")}
    return defaults


def apply_hurtbox_event(states, defaults, event):
    """Apply one ATT HurtboxStateChanges entry to the running state table.

    A blank/"None" name targets every hurtbox and only changes state (in the
    exported data these events never disable anything, and enabling would
    wrongly switch on other moves' temporary hurtboxes). State "Default"
    restores each hurtbox's own configured default, e.g. Ungrabbable limbs.
    """
    name = event.get("HurtboxName") or "None"
    new_state = short_enum(event.get("HurtboxState"), "Default")
    wildcard = name == "None"
    for target in (list(states) if wildcard else [name]):
        if target not in states:
            continue
        entry = states[target]
        entry["state"] = defaults[target]["state"] if new_state == "Default" else new_state
        if not wildcard:
            entry["active"] = bool(event.get("HurtboxActive", True))


def hurt_shapes(bones, indices, states=None):
    states = states if states is not None else hurtbox_defaults()
    shapes = []

    def tagged(shape, name):
        status = states.get(name, {"state": "Default", "active": True})
        return {**shape, "kind": "hurt", "hurtbox": name,
                "state": status["state"], "active": status["active"]}

    for name, definition in hurtbox_definitions().items():
        if definition["type"] == "NewSphere":
            shapes.append(tagged({"shape": "circle", "name": name, "bone": "Root",
                                  "c": bone_point(bones, indices, "Root", definition["a"]),
                                  "r": definition["r"]}, name))
        elif definition["type"] == "NewCapsule":
            shapes.append(tagged({"shape": "capsule", "name": name, "bone": "Root",
                                  "a": bone_point(bones, indices, "Root", definition["a"]),
                                  "b": bone_point(bones, indices, "Root", definition["b"]),
                                  "r": definition["r"]}, name))
    for bone, agg_geom in load_physics_bodies(str(PHYSICS)) if PHYSICS else ():
        body = bones[indices[bone]] if bone in indices else None
        if body is None:
            continue
        body_t, body_q, body_scale = body
        for shape_type, elems in agg_geom.items():
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
                    # "collision" is Unreal's physics flag, kept for reference
                    # only; it is not the gameplay hurtbox state.
                    shapes.append(tagged({"shape": "capsule", "name": elem.get("Name", bone),
                                          "bone": bone, "a": a, "b": b,
                                          "r": float(elem.get("Radius", 0)),
                                          "collision": elem.get("CollisionEnabled", "unknown")}, bone))
                elif shape_type == "SphereElems":
                    shapes.append(tagged({"shape": "circle", "name": elem.get("Name", bone),
                                          "bone": bone, "c": c,
                                          "r": float(elem.get("Radius", 0)),
                                          "collision": elem.get("CollisionEnabled", "unknown")}, bone))
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


# AdvancedOnHitProperties values that almost every hit uses (from a survey of
# all 1,057 hit properties). Only values that differ are listed per hitbox,
# so unusual behavior stands out. Labels are what info.md prints.
HIT_EXTRA_DEFAULTS = {
    "HitstunMultiplier": (1.0, "hitstun x{}"),
    "HitfallHitstunMultiplier": (1.0, "hitfall hitstun x{}"),
    "HitpauseMultiplier": (1.0, "hitpause x{}"),
    "ExtraHitpauseForOpponent": (0, "{} hitpause for opponent"),
    "HitpauseMovementType": ("None", "hitpause movement {}"),
    "SDIMultiplier": (1.0, "SDI x{}"),
    "ASDIMultiplier": (-1.0, "ASDI x{}"),
    "bCanReverse": (True, "reverse hit {}"),
    "bReverseBasedOnHorizontalSpeed": (False, "reverses by horizontal speed {}"),
    "bForceFlinch": (False, "forces flinch {}"),
    "GroundTechable": (True, "techable {}"),
    "bIgnoresWeight": (False, "ignores weight {}"),
    "bPreventGroundBounce": (False, "prevents ground bounce {}"),
    "bAutoFloorhuggable": (False, "auto floorhuggable {}"),
    "ProjectileInteraction": ("Default", "projectile interaction {}"),
    "bForceKnockbackInKnockdown": (False, "knockback in knockdown {}"),
    "bPreserveFacing": (False, "preserves facing {}"),
    "KnockbackAngleMode": ("SpecifiedAngle", "angle mode {}"),
    "ParryReaction": ("Stun", "parry reaction {}"),
    "GrabPartnerInteraction": ("None", "grab partner {}"),
    "ExtraShieldStun": (0, "{} shieldstun"),
    "ShieldDamageMultiplier": (1.0, "shield damage x{}"),
    "ShieldPushbackMultiplier": (1.0, "shield pushback x{}"),
    "ShieldHitpauseMultiplier": (1.0, "shield hitpause x{}"),
    "FullChargeKnockbackMultiplier": (1.3, "full-charge knockback x{}"),
    "FullChargeDamageMultiplier": (1.6, "full-charge damage x{}"),
    "DriftDiMultiplier": (1.0, "drift DI x{}"),
    "FinalBaseKnockback": (0.0, "final base knockback {}"),
    "ForceTumble": (False, "forces tumble {}"),
    "IgnoreKnockbackArmor": (False, "ignores knockback armor {}"),
    "IgnoreSuperArmor": (False, "ignores super armor {}"),
    "IgnoreInvincibility": (False, "ignores invincibility {}"),
    "PreventChaingrabsOnHit": (False, "prevents chaingrabs {}"),
    "bIsWindbox": (False, "windbox {}"),
    "bReflectProjectiles": (False, "reflects projectiles {}"),
    "HitstunAnimationStateOverride": ("None", "hitstun state {}"),
}
# render_settings.json "info_notes_defaults" overrides the usual values.
for _key, _value in (setting("info_notes_defaults", {}) or {}).items():
    if _key in HIT_EXTRA_DEFAULTS:
        HIT_EXTRA_DEFAULTS[_key] = (_value, HIT_EXTRA_DEFAULTS[_key][1])


def number(value):
    return f"{value:g}" if isinstance(value, float) else value


def hitbox_stats(attr, on_hit):
    """One hitbox's data for info.md/info.json: its shape and response plus
    its damage properties (HitboxOnHitProperties[OnHitPropertiesName])."""
    props = on_hit.get(attr.get("OnHitPropertiesName")) or {}
    advanced = props.get("AdvancedOnHitProperties") or {}
    extras = {}
    for key, (default, label) in HIT_EXTRA_DEFAULTS.items():
        if key not in advanced:
            continue
        value = advanced[key]
        shown = short_enum(value) if isinstance(value, str) else value
        if shown != default:
            extras[key] = shown
    effect = short_enum(advanced.get("SpecialEffect"), "None")
    return {
        "name": attr.get("Name", "Hitbox"),
        "response": short_enum(attr.get("HitResponse")),
        # Same name-based sweet/sour inference as the frame colors.
        "spot": hitbox_spot(attr.get("Name", ""), attr.get("OnHitPropertiesName", "")),
        "radius": attr.get("Radius"),
        "bone": attr.get("BoneName"),
        "damage_property": attr.get("OnHitPropertiesName"),
        "damage": props.get("Damage"),
        "base_knockback": props.get("BaseKnockback"),
        "knockback_scaling": props.get("KnockbackScaling"),
        "angle": props.get("KnockbackAngle"),
        "effect": None if effect == "None" else effect,
        "extras": extras,
    }


SIGNED_EXTRAS = {"ExtraHitpauseForOpponent", "ExtraShieldStun"}  # shown as +1 / -1


def hitbox_notes(stats):
    notes = []
    for key, (_, label) in HIT_EXTRA_DEFAULTS.items():
        if key not in stats["extras"]:
            continue
        value = stats["extras"][key]
        text = ("yes" if value is True else "no" if value is False
                else f"{value:+g}" if key in SIGNED_EXTRAS else number(value))
        notes.append(label.format(text))
    return "; ".join(notes)


def hitbox_table(rows):
    """Markdown table of hitbox stats (rows carry a "frames" entry)."""
    lines = ["| Hitbox | Frames | Type | Sweet/Sour | Damage | Base KB | KB scaling | Angle | Effect | Radius | Notes |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        spot = {"sweet": "Sweet", "sour": "Sour"}.get(row.get("spot"), "-")
        cells = [row["name"], row["frames"], row["response"], spot,
                 *(number(row[key]) if row.get(key) is not None else "-"
                   for key in ("damage", "base_knockback", "knockback_scaling", "angle")),
                 row["effect"] or "-", number(row["radius"]) if row.get("radius") is not None else "-",
                 hitbox_notes(row) or "-"]
        lines.append("| " + " | ".join(str(cell).replace("|", "/") for cell in cells) + " |")
    return lines


def move_info(attack, name, variant, animation):
    props = attack.get("Properties", {})
    windows = props.get("Windows", []) or []
    frame_cursor = 0
    active_frames = set()
    sets_by_start = {}
    iasa_values = []
    frames_by_hitbox = {}
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
            # A hitbox never outlives its window: 70 entries in the game data
            # list a LengthInFrames that runs 1-5 frames past the window's
            # end (Clairen Dtilt: 3 in a 2-frame Active window), and in game
            # those boxes are active only until the window ends.
            hit_length = min(hit_length, length - relative)
            if hit_length <= 0:
                continue
            first = start + relative + 1  # display as 1-based gameplay frames
            last = start + relative + hit_length
            hit_frames = list(range(first, last + 1))
            active_frames.update(hit_frames)
            group = sets_by_start.setdefault(first, {"hit_frame": first, "hitboxes": set(), "frames": set()})
            group["hitboxes"].add(hit_window.get("HitboxName", "Hitbox"))
            group["frames"].update(hit_frames)
            frames_by_hitbox.setdefault(hit_window.get("HitboxName", "Hitbox"), set()).update(hit_frames)
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
    attrs = hitbox_attributes(props)
    on_hit = {entry.get("Name"): entry for entry in props.get("HitboxOnHitProperties", []) or []}
    hitboxes = [dict(hitbox_stats(attrs[hitbox], on_hit), frames=format_frame_ranges(frames))
                for hitbox, frames in sorted(frames_by_hitbox.items(), key=lambda item: (min(item[1]), item[0]))
                if hitbox in attrs]
    return {
        "character": CHAR.name,
        "attack": name,
        "variant": variant,
        "animation": animation.stem if animation else None,
        "frame_numbering": "1-based gameplay frames",
        "stats": values,
        "hitbox_sets": hitbox_sets,
        "hitboxes": hitboxes,
    }


def hurtbox_state_summary(frames):
    """Frame ranges where hurtboxes differ from the character's defaults.

    Character-wide defaults (e.g. Loxodont's always-ungrabbable limbs) are
    left out so each move lists only what the move itself changes. Frame
    numbers match the PNG numbering.
    """
    defaults = hurtbox_defaults()
    permanent = {name for name, status in defaults.items() if not status["temporary"]}
    groups = {}
    for frame in frames:
        for box in frame["boxes"]:
            if box.get("kind") != "hurt":
                continue
            name = box["hurtbox"]
            default = defaults.get(name, {"state": "Default", "active": True})
            if box["active"] != default["active"]:
                label = "Enabled" if box["active"] else "Disabled"
            elif box["active"] and box["state"] != default["state"]:
                label = box["state"]
            else:
                continue
            group = groups.setdefault(label, {"frames": set(), "hurtboxes": set()})
            group["frames"].add(frame["index"])
            group["hurtboxes"].add(name)
    summary = []
    for label, group in sorted(groups.items(), key=lambda item: min(item[1]["frames"])):
        names = group["hurtboxes"]
        # Hurtboxes already in this state by default also count toward "all".
        covered = names | {name for name in permanent if defaults[name]["state"] == label}
        summary.append({"state": label, "frames": format_frame_ranges(group["frames"]),
                        "hurtboxes": "all" if permanent and permanent <= covered else sorted(names)})
    return summary


def write_active_frames(move_dir, frame_stem, frames):
    """List the PNGs that have any active hitbox (character or article).

    active_frames.txt: one line per such PNG, "<file>\t<hitbox names>", so the
    frames that matter in a move can be picked out without opening them.
    """
    rows = []
    for frame in frames:
        # Article Detect/DetectAll boxes are passive sensors that stay on for
        # the article's whole life (Absa's cloud "Static Detector"); they'd
        # mark every frame. The character's own Detect boxes (counters) count.
        hits = [box for box in frame["boxes"] if box.get("kind") == "hit"
                and not (box.get("article")
                         and short_enum(box.get("hit_response")) in ("Detect", "DetectAll"))]
        if not hits:
            continue
        names = sorted({(f"{box['article']}: " if box.get("article") else "") + box["name"] for box in hits})
        rows.append({"file": f"{frame_stem}{frame['game_frame_zero_based']:04d}.png",
                     "frame": frame["game_frame_zero_based"], "hitboxes": names})
    text = "".join(f"{row['file']}\t{', '.join(row['hitboxes'])}\n" for row in rows)
    (move_dir / "active_frames.txt").write_text(text, encoding="utf-8")
    return rows


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
    if info.get("hitboxes"):
        lines.extend(["", "## Hitboxes", "",
                      "Base KB, KB scaling and angle are the game's knockback values; Notes lists only "
                      "settings that differ from the usual ones.", ""])
        lines.extend(hitbox_table(info["hitboxes"]))
    if info.get("articles"):
        lines.extend(["", "## Articles", "",
                      "Hitboxes on objects and projectiles the move spawns, with the frames each is active "
                      "(including frames after the PNGs stop). Projectile paths follow the game data only "
                      "(no homing, bouncing or collisions).", ""])
        for spawn in info["articles"]:
            notes = [spawn["asset"], f"spawn {spawn['spawn']}"]
            if spawn.get("projectile"):
                notes.append("projectile")
            if spawn.get("pose"):
                notes.append(spawn["pose"])
            notes += [f"rule: {rule}" for rule in spawn.get("rules") or []]
            lines.extend([f"### {spawn['article']} ({', '.join(notes)})", ""])
            lines.extend(hitbox_table(spawn["hitboxes"]))
            lines.append("")
    if info.get("hurtbox_states"):
        lines.extend(["", "## Hurtbox states", ""])
        for group in info["hurtbox_states"]:
            who = ("all hurtboxes" if group["hurtboxes"] == "all"
                   else ", ".join(group["hurtboxes"]))
            lines.append(f"- **{group['state']}:** frames {group['frames']} ({who})")
    lines.append("")
    (move_dir / "info.md").write_text("\n".join(lines), encoding="utf-8")


def hitbox_box(attr, pose, previous_pose, indices, effects, to_world=None, missing_bone_at_origin=False):
    """One active hitbox as a drawable circle/capsule dict, or None.

    pose/previous_pose/indices describe the skeleton the hitbox rides on (the
    character, or an article's own skeleton). to_world maps that skeleton's
    space into the character's world space (articles); missing_bone_at_origin
    puts hitboxes on a bone the skeleton lacks at the origin plus offset
    (articles without a skeleton use "Root"/"None").
    """
    def point(bones, bone, offset):
        found = bone_point(bones, indices, bone, offset)
        if found is None and missing_bone_at_origin:
            found = tuple(float(v) for v in offset)
        if found is not None and to_world:
            found = to_world(found)
        return found

    attach = attr.get("OffsetBoneName")
    if not attach or attach == "None":
        attach = attr.get("BoneName", "")
    center = point(pose, attach, vec(attr.get("Offset", {})))
    if center is None:
        return None
    interpolation = str(attr.get("InitialInterpolationMode", "None")).split("::")[-1]
    if interpolation == "Auto":
        source_bone = attr.get("InitialInterpolationBoneName")
        if not source_bone or source_bone == "None":
            source_bone = attach
        source_offset = attr.get("InitialInterpolationOffset", attr.get("Offset", {}))
        start = point(previous_pose, source_bone, vec(source_offset))
    elif interpolation in ("Manual", "ManualPermanent"):
        # A capsule from InitialInterpolationBone + Offset to the hitbox.
        # ManualPermanent (100 hitboxes, e.g. the Clairen Dtilt sword and
        # tipper) is drawn this way in training mode.
        # UseCurrentFrameForInterpolation picks this frame's pose (a capsule
        # along the weapon) or the previous frame's (a sweep).
        source_bone = attr.get("InitialInterpolationBoneName") or attach
        if source_bone == "None":
            source_bone = attach
        source_offset = attr.get("InitialInterpolationOffset", {})
        source_pose = pose if attr.get("UseCurrentFrameForInterpolation") else previous_pose
        start = point(source_pose, source_bone, vec(source_offset))
    else:
        start = None
    shape = {"shape": "circle", "kind": "hit", "c": center}
    if start is not None and interpolation in ("Auto", "Manual", "ManualPermanent"):
        shape = {"shape": "capsule", "kind": "hit", "a": start, "b": center}
    return {**shape, "name": attr.get("Name", "Hitbox"), "bone": attach,
            "r": float(attr.get("Radius", 0)),
            "interpolation": interpolation,
            "hit_response": str(attr.get("HitResponse", "")),
            "damage_property": attr.get("OnHitPropertiesName", ""),
            "effect": effects.get(attr.get("OnHitPropertiesName", ""), "None"),
            "spot": hitbox_spot(attr.get("Name", ""), attr.get("OnHitPropertiesName", ""))}


def sweep_hitboxes(hits, frame_index, last_centers):
    """Stretch continuing hitboxes from last frame's position (in place).

    InitialInterpolationMode only shapes a hitbox's first active frame. On
    every later consecutive frame the game sweeps it from where it was on the
    previous frame, so a moving box is a capsule and a still one a circle.
    The user saw this in training on Galvan Dspecial's claws (mode None):
    a circle on active frame 0, capsules on frames 1-4 while they move,
    circles on 5-9 once they stop. ManualPermanent boxes keep their own
    capsule on every frame. last_centers maps (article spawn or None, hitbox
    name) -> (frame index, center) across calls.
    """
    for box in hits:
        key = (box.get("article_spawn"), box["name"])
        center = box["b"] if box["shape"] == "capsule" else box["c"]
        previous = last_centers.get(key)
        if previous and previous[0] == frame_index - 1 and box["interpolation"] != "ManualPermanent":
            moved = math.dist(previous[1], center) > 1e-3
            for key_name in ("a", "b", "c"):
                box.pop(key_name, None)
            if moved:
                box.update(shape="capsule", a=previous[1], b=center)
            else:
                box.update(shape="circle", c=center)
            box["swept"] = True
        last_centers[key] = (frame_index, center)


def on_hit_effects(props):
    """On-hit SpecialEffect per damage-properties name (TipperStun, Burn, ...)."""
    return {entry.get("Name"): short_enum((entry.get("AdvancedOnHitProperties") or {})
                                          .get("SpecialEffect"), "None")
            for entry in props.get("HitboxOnHitProperties", []) or [] if entry.get("Name")}


def asset_file(object_path, suffix):
    """Exported file for an asset reference: "Rivals2/Content/X/Y.1" or
    "/Game/X/Y.Y" -> ROOT/Rivals2/Content/X/Y<suffix>."""
    path = str(object_path or "").rsplit(".", 1)[0]
    if path.startswith("/Game/"):
        path = "Rivals2/Content/" + path[len("/Game/"):]
    return ROOT / f"{path}{suffix}" if path else None


@functools.lru_cache(maxsize=None)
def load_article(object_path):
    """An article (ART_*) the move spawns: its properties and, when it has
    one, its own skeleton (via HurtboxesSkeletalMesh -> mesh -> Skeleton)."""
    path = asset_file(object_path, ".json")
    if not path or not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    props = next((entry.get("Properties") for entry in data
                  if str(entry.get("Name", "")).startswith("Default__") and entry.get("Properties")), None)
    if props is None:
        return None
    skeleton = skeleton_path = None
    mesh_path = asset_file((props.get("HurtboxesSkeletalMesh") or {}).get("ObjectPath"), ".json")
    if mesh_path and mesh_path.is_file():
        mesh = next((entry for entry in json.loads(mesh_path.read_text(encoding="utf-8"))
                     if entry.get("Type") == "SkeletalMesh"), {})
        skeleton_path = asset_file(((mesh.get("Properties") or {}).get("Skeleton") or {}).get("ObjectPath"), ".json")
        if skeleton_path and skeleton_path.is_file():
            _, indices, ref_local, parents = read_skeleton(skeleton_path)
            skeleton = (indices, ref_local, parents)
            skeleton_path = str(skeleton_path)
    return {"name": props.get("ArticleName") or path.stem.removeprefix("ART_"), "asset": path.stem,
            "props": props, "skeleton": skeleton, "skeleton_path": skeleton_path,
            "effects": on_hit_effects(props),
            "attrs": hitbox_attributes(props)}


@functools.lru_cache(maxsize=None)
def article_mesh(object_path, character_dir, skin_textures):
    """The default skin's visible mesh for an article with a skeleton: the
    exported Skins/Default/**/SK_*.glb whose SkeletalMesh uses the same
    skeleton (Galvan's claw -> Skins/Default/ChainHand/SK_Gal_DefaultChainHand)."""
    article = load_article(object_path)
    if not article or not article["skeleton_path"]:
        return None
    skin_dir = Path(character_dir) / "Skins/Default"
    target = Path(article["skeleton_path"]).resolve()
    for mesh_json in sorted(skin_dir.rglob("SK_*.json")):
        glb = mesh_json.with_suffix(".glb")
        if not glb.is_file():
            continue
        try:
            mesh = next((entry for entry in json.loads(mesh_json.read_text(encoding="utf-8"))
                         if entry.get("Type") == "SkeletalMesh"), None)
        except (OSError, ValueError):
            continue
        skeleton = asset_file((((mesh or {}).get("Properties") or {}).get("Skeleton") or {}).get("ObjectPath"), ".json")
        if skeleton and skeleton.resolve() == target:
            try:
                return GLBCharacter(glb, article["skeleton"][0], skin_textures=skin_textures,
                                    palette_dir=skin_dir)
            except (OSError, ValueError, KeyError, IndexError) as exc:
                print(f"{article['asset']}: can't draw mesh {glb.name}: {exc}")
                return None
    return None


# Hand-written rules for article behavior that lives in game code, not data
# (e.g. Absa's cloud is placed when special is released). Added as needed;
# see the file's own notes.
ARTICLE_RULES = ROOT / "article_rules.json"
# At most this many frames are rendered past the move's end while an article
# still has an active (non-Detect) hitbox; the character holds its last pose.
ARTICLE_EXTRA_FRAMES = int(setting("articles.extra_frames", 10))


@functools.lru_cache(maxsize=1)
def load_article_rules():
    if not ARTICLE_RULES.is_file():
        return {}
    data = json.loads(ARTICLE_RULES.read_text(encoding="utf-8"))
    return {move.casefold(): rules for move, rules in data.items() if not move.startswith("_")}


def find_article_path(asset_name):
    """"ART_Abs_Cloud" -> its object path under the current character."""
    found = next(CHAR.rglob(f"{asset_name}.json"), None) or next(CHARACTERS_ROOT.rglob(f"{asset_name}.json"), None)
    return str(found.relative_to(ROOT).with_suffix("")) if found else None


def article_timeline(names, start, switches, limit):
    """(window, frame in window) per article frame, following
    NextWindowStringTableKey; switches maps article frame -> window (rules)."""
    timeline, current, frame, guard = [], start, 0, 0
    while len(timeline) < limit and guard < limit + 1000:
        guard += 1
        if len(timeline) in switches:
            target = switches.pop(len(timeline))
            current, frame = target, 0
        window = names.get(current)
        if window is None:
            break
        if frame >= int(window.get("WindowLengthFrames", 0)):
            current, frame = window.get("NextWindowStringTableKey") or None, 0
            continue
        timeline.append((window, frame))
        frame += 1
    return timeline


def article_spawns_for(move_name, props, windows, frame_count, starts_by_name):
    """Articles with hitboxes that this move spawns or drives.

    Each spawn is simulated from ArticleCreationData: it appears on
    CreationWindowFrame of CreationWindowStringTableKey, at
    InitialOffsetFromOwner (X along the character's facing, Y up), facing
    InitialFacingMultiplier, in ArticleInitialWindowStringTableKey. "First"
    (e.g. Galvan Dspecial's claws) names no window; it is taken as the
    article's first window. The article then follows its own windows
    (NextWindowStringTableKey), VelocityData and gravity. What the game's
    code decides instead (button release, popping an existing cloud) comes
    from article_rules.json:
      "switch" rules: when the move enters a window, the article changes window
      "existing" rules: the move drives an article that is already out,
                        at an assumed offset
    """
    by_name = {w.get("StringTableKey"): w for w in windows}
    rules = load_article_rules().get(move_name.casefold(), [])
    creations = [dict(c, _rule_notes=[]) for c in props.get("ArticleCreationData", []) or []]
    for rule in rules:
        if "existing" in rule:
            object_path = find_article_path(rule["article"])
            if object_path:
                creations.append({"ArticleData": {"ObjectPath": object_path},
                                  "CreationWindowStringTableKey": rule["existing"],
                                  "CreationWindowFrame": int(rule.get("frame", 0)),
                                  "InitialOffsetFromOwner": rule.get("offset", {}),
                                  "InitialFacingMultiplier": rule.get("facing", 1),
                                  "ArticleInitialWindowStringTableKey": rule.get("article_window"),
                                  "_rule_notes": [rule.get("note", "assumed already on stage")]})
    spawns = []
    for creation in creations:
        article = load_article((creation.get("ArticleData") or {}).get("ObjectPath"))
        owner_window = by_name.get(creation.get("CreationWindowStringTableKey"))
        if not article or owner_window is None or not article["attrs"]:
            continue
        start_frame = starts_by_name[creation.get("CreationWindowStringTableKey")] + int(creation.get("CreationWindowFrame", 0))
        article_windows = article["props"].get("Windows", []) or []
        names = {w.get("StringTableKey"): w for w in article_windows}
        current = creation.get("ArticleInitialWindowStringTableKey")
        if current not in names:
            current = article_windows[0].get("StringTableKey") if article_windows else None
        notes = list(creation["_rule_notes"])
        switches = {}
        for rule in rules:
            if "switch" in rule and rule.get("article") == article["asset"] and rule["switch"] in by_name:
                at = starts_by_name[rule["switch"]] - start_frame
                if at >= 0 and rule.get("article_window") in names:
                    switches[at] = rule["article_window"]
                    notes.append(rule.get("note", f"switches to {rule['article_window']} when the move enters {rule['switch']}"))
        limit = max(0, frame_count + ARTICLE_EXTRA_FRAMES - start_frame)
        timeline = article_timeline(names, current, switches, limit)
        # Displacement from VelocityData and gravity, in the article's facing space.
        facing = -1.0 if float(creation.get("InitialFacingMultiplier", 1) or 1) < 0 else 1.0
        vx = vy = x = y = 0.0
        displacement = []
        for window, f in timeline:
            for velocity in window.get("VelocityData", []) or []:
                if not (int(velocity.get("ApplyVelocityOnWindowFrame", 0)) <= f
                        <= int(velocity.get("ApplyVelocityUntilFrame", 9999))):
                    continue
                v = velocity.get("Velocity") or {}
                for axis, kind in (("X", velocity.get("HorizontalVelocityType")),
                                   ("Y", velocity.get("VerticalVelocityType"))):
                    kind = short_enum(kind)
                    value = float(v.get(axis, 0) or 0)
                    if kind in ("SetVelocity", "ConstVelocity"):
                        vx, vy = (value, vy) if axis == "X" else (vx, value)
                    elif kind == "ConstAddVelocity":
                        vx, vy = (vx + value, vy) if axis == "X" else (vx, vy + value)
            advanced = window.get("AdvancedProperties") or {}
            gravity = float(advanced.get("Gravity", 0) or 0)
            if gravity > 0:
                vy = max(vy - gravity, -float(advanced.get("MaxFallSpeed", 9999) or 9999))
            displacement.append((x, y))
            x, y = x + vx, y + vy
        offset = creation.get("InitialOffsetFromOwner") or {}
        spawns.append({
            "article": article, "facing": facing, "start": start_frame,
            "offset": (float(offset.get("X", 0) or 0), float(offset.get("Y", 0) or 0)),
            "attached": bool(article["props"].get("bIsAttachedToOwner")),
            "projectile": bool(article["props"].get("bIsProjectile")),
            "timeline": timeline, "displacement": displacement, "anchor": None,
            "truncated": len(timeline) >= limit,
            "previous_pose": None, "id": len(spawns), "mesh_pose": None, "notes": notes,
            "object_path": (creation.get("ArticleData") or {}).get("ObjectPath"),
        })
    return spawns


def article_active_hitboxes(window, frame, attrs):
    """HitboxAttributes active on one frame of an article window, clipped to
    the window like the character's."""
    length = max(0, int(window.get("WindowLengthFrames", 0)))
    active = []
    for hit_window in window.get("HitboxWindows", []) or []:
        begin = int(hit_window.get("StartFrame", 0))
        span = min(int(hit_window.get("LengthInFrames", 0)) or (length - begin), length - begin)
        attr = attrs.get(hit_window.get("HitboxName", ""))
        if attr and begin <= frame < begin + span:
            active.append(attr)
    return active


def is_sensor(attr):
    return short_enum(attr.get("HitResponse")) in ("Detect", "DetectAll")


def article_last_change_frame(spawn):
    """Last move frame where the article's (non-Detect) hitboxes appear or
    change. After that they only travel, so later frames add nothing."""
    last, previous = None, frozenset()
    for local, (window, frame) in enumerate(spawn["timeline"]):
        current = frozenset(attr.get("Name") for attr in article_active_hitboxes(window, frame, spawn["article"]["attrs"])
                            if not is_sensor(attr))
        if current and current != previous:
            last = spawn["start"] + local
        previous = current
    return last


def article_spawn_summary(spawn):
    """info.md/info.json entry for one spawn, from the simulation (not the
    rendered frames, which may stop before the article is done)."""
    article = spawn["article"]
    per_hitbox, windows, unposed = {}, [], False
    for local, (window, frame) in enumerate(spawn["timeline"]):
        active = article_active_hitboxes(window, frame, article["attrs"])
        for attr in active:
            per_hitbox.setdefault((attr.get("Name"), short_enum(attr.get("HitResponse"))), set()).add(
                spawn["start"] + local + 1)  # 1-based, like the rest of info.md
        if active and window.get("StringTableKey") not in windows:
            windows.append(window.get("StringTableKey"))
            anim = asset_file((window.get("WindowAnimation") or {}).get("AssetPathName"), ".ueanim")
            unposed |= bool(article["skeleton"]) and not (anim and anim.is_file())
    last_frame = spawn["start"] + len(spawn["timeline"])  # 1-based number of the last simulated frame
    hitboxes = []
    on_hit = {entry.get("Name"): entry for entry in article["props"].get("HitboxOnHitProperties", []) or []}
    for (name, response), frames in sorted(per_hitbox.items(), key=lambda item: min(item[1])):
        ranges = format_frame_ranges(frames)
        if spawn["truncated"] and max(frames) == last_frame:
            ranges += " (continues)"
        hitboxes.append(dict(hitbox_stats(article["attrs"][name], on_hit), frames=ranges))
    return {"spawn": spawn["id"], "article": article["name"], "asset": article["asset"],
            "projectile": spawn["projectile"], "rules": spawn["notes"], "windows": windows,
            "hitboxes": hitboxes,
            "pose": "reference pose (animation not exported)" if unposed else None}


def article_pose(article, window, frame):
    """The article skeleton's world pose in its own space for a window frame."""
    indices, ref_local, parents = article["skeleton"]
    anim_path = asset_file((window.get("WindowAnimation") or {}).get("AssetPathName"), ".ueanim")
    if not anim_path or not anim_path.is_file():
        return compose_world(ref_local, parents), False
    animation = read_ueanim(str(anim_path))
    window_length = int(window.get("WindowLengthFrames", 0)) or 1
    anim_length = int(window.get("AnimationLengthFrames", window_length))
    timeline_frame = int(window.get("AnimationStartFrame", 0)) + frame * anim_length / window_length
    anim_frame = min(float(animation["frames"] - 1), timeline_frame * float(animation["fps"]) / FPS)
    return compose_world(animation_local_pose(animation, ref_local, indices, anim_frame), parents), True


def article_hitboxes(spawn, i, owner_pose, owner_indices):
    """Hitboxes of one spawned article on move frame i, in world space."""
    local = i - spawn["start"]
    spawn["mesh_pose"] = None
    if local < 0 or local >= len(spawn["timeline"]):
        return []
    window, frame = spawn["timeline"][local]
    article = spawn["article"]
    # Origin: the character origin (Group) now if the article is attached,
    # else where it was when it spawned.
    owner_origin = bone_point(owner_pose, owner_indices, "Root") or (0.0, 0.0, 0.0)
    if spawn["anchor"] is None or spawn["attached"]:
        spawn["anchor"] = owner_origin
    dx, dy = spawn["displacement"][local]
    facing = spawn["facing"]
    origin = (spawn["anchor"][0] + spawn["offset"][0] + dx * facing,
              spawn["anchor"][1],
              spawn["anchor"][2] + spawn["offset"][1] + dy)

    def to_world(p):
        # Facing -1 turns the article around (180 degrees about Z), which
        # keeps its skeleton's handedness, like the character turning around.
        return (origin[0] + p[0] * facing, origin[1] + p[1] * facing, origin[2] + p[2])

    if article["skeleton"]:
        indices = article["skeleton"][0]
        pose, animated = article_pose(article, window, frame)
    else:
        indices, pose, animated = {}, [], None
    previous = spawn["previous_pose"] if spawn["previous_pose"] is not None else pose
    spawn["previous_pose"] = pose
    if pose:
        turn = (0.0, 0.0, 1.0, 0.0) if facing < 0 else (0.0, 0.0, 0.0, 1.0)  # 180 degrees about Z
        spawn["mesh_pose"] = [(to_world(t), qmul(turn, q), sc) for t, q, sc in pose]
    boxes = []
    for attr in article_active_hitboxes(window, frame, article["attrs"]):
        box = hitbox_box(attr, pose, previous, indices, article["effects"],
                         to_world=to_world, missing_bone_at_origin=True)
        if box:
            box.update(camera_fit=spawn.get("last_change") is None or i <= spawn["last_change"],
                       article=article["name"], article_asset=article["asset"],
                       article_spawn=spawn["id"], article_window=window.get("StringTableKey"),
                       article_animated=animated, article_projectile=spawn["projectile"],
                       article_notes=spawn["notes"])
            boxes.append(box)
    return boxes


def gameplay_windows(windows):
    """Gameplay start frame of each window and the move's total length.

    Frames follow gameplay, not the animation: windows run back to back in
    list order, each WindowLengthFrames long (as info.md counts them). The
    animation is stretched or squeezed to fit (AnimationLengthFrames over
    WindowLengthFrames): Zetterburn Fspecial's Startup is 15 gameplay frames
    on 12 animation frames, which training mode confirms (active on the 16th).
    """
    starts, cursor = [], 0
    for window in windows:
        starts.append(cursor)
        cursor += max(0, int(window.get("WindowLengthFrames", 0)))
    return starts, cursor


def animation_time(windows, starts, frame):
    """Animation timeline frame (in FPS units) shown on a gameplay frame."""
    for window, start in zip(reversed(windows), reversed(starts)):
        length = max(0, int(window.get("WindowLengthFrames", 0)))
        if length and start <= frame:
            anim_length = int(window.get("AnimationLengthFrames", length))
            local = min(frame - start, length)  # past the last window: hold its end
            return int(window.get("AnimationStartFrame", 0)) + local * anim_length / length
    return float(frame)


def move_frames(attack, animation_path, ref_local, parents, indices, character_mesh=None,
                articles_out=None):
    props = attack.get("Properties", {})
    windows = props.get("Windows", []) or []
    window_starts, frame_count = gameplay_windows(windows)
    animation = None
    if animation_path and animation_path.suffix.lower() == ".ueanim":
        animation = read_ueanim(str(animation_path.resolve()))
        if not frame_count:  # no windows (stub attack data): show the animation as is
            frame_count = int(animation["frames"])
    frame_count = max(frame_count, 1)
    starts_by_name = {w.get("StringTableKey"): start for w, start in zip(windows, window_starts)}

    attrs = hitbox_attributes(props)
    effects = on_hit_effects(props)
    move_name = str(attack.get("Name", "")).removeprefix("Default__").removesuffix("_C")
    article_spawns = article_spawns_for(move_name, props, windows, frame_count, starts_by_name)
    # Past the move's end, render only until each article's hitboxes have
    # appeared or last changed (the user's choice: a projectile that only
    # travels needs no more frames), at most ARTICLE_EXTRA_FRAMES; the
    # character holds its last pose. info.md has the full article timings.
    for spawn in article_spawns:
        spawn["last_change"] = article_last_change_frame(spawn)
    last_change = max((spawn["last_change"] for spawn in article_spawns
                       if spawn["last_change"] is not None), default=None)
    if last_change is not None:
        frame_count = max(frame_count, min(last_change + 1, frame_count + ARTICLE_EXTRA_FRAMES))
    if articles_out is not None:
        articles_out.extend(summary for summary in map(article_spawn_summary, article_spawns)
                            if summary["hitboxes"])
    active_hitboxes = [[] for _ in range(frame_count)]
    events = [[] for _ in range(frame_count)]
    hurtbox_changes = []  # (frame, order, raw HurtboxStateChanges entry)
    window_names = [None] * frame_count
    for window, start in zip(windows, window_starts):
        length = max(0, int(window.get("WindowLengthFrames", 0)))
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
            # The window's end caps every hitbox (see move_info).
            active_length = min(active_length, length - relative_start)
            end = begin + active_length
            for frame in range(max(0, begin), min(frame_count, end)):
                attr = attrs.get(name)
                if attr:
                    active_hitboxes[frame].append(attr)
        for event in window.get("HurtboxStateChanges", []) or []:
            frame = start + int(event.get("WindowFrame", 0))
            frame = min(frame_count - 1, max(0, frame))
            hurtbox_changes.append((frame, len(hurtbox_changes), event))
            events[frame].append({"shape": "event", "kind": "hurtbox_state",
                                  "name": event.get("HurtboxName", "None"),
                                  "state": event.get("HurtboxState", "unknown"),
                                  "active": event.get("HurtboxActive", True)})

    defaults = hurtbox_defaults()
    hurtbox_states = {name: dict(status) for name, status in defaults.items()}
    hurtbox_changes.sort(key=lambda item: item[:2])
    next_change = 0
    frames = []
    last_hit_centers = {}
    animation_fps = float(animation["fps"]) if animation else FPS
    for i in range(frame_count):
        if animation:
            animation_frame = min(float(animation["frames"] - 1),
                                  animation_time(windows, window_starts, i) * animation_fps / FPS)
            local_pose = animation_local_pose(animation, ref_local, indices, animation_frame)
            pose_source = "ueanim"
        else:
            animation_frame = None
            local_pose = ref_local
            pose_source = "reference_skeleton"
        world_pose = compose_world(local_pose, parents)
        if animation and i > 0:
            previous_frame = min(float(animation["frames"] - 1), max(
                0.0, animation_time(windows, window_starts, i - 1) * animation_fps / FPS))
            previous_local = animation_local_pose(animation, ref_local, indices, previous_frame)
            previous_world = compose_world(previous_local, parents)
        else:
            previous_world = world_pose
        while next_change < len(hurtbox_changes) and hurtbox_changes[next_change][0] <= i:
            apply_hurtbox_event(hurtbox_states, defaults, hurtbox_changes[next_change][2])
            next_change += 1
        hurt = hurt_shapes(world_pose, indices, hurtbox_states)
        hits = [box for box in (hitbox_box(attr, world_pose, previous_world, indices, effects)
                                for attr in active_hitboxes[i]) if box]
        for spawn in article_spawns:
            hits.extend(article_hitboxes(spawn, i, world_pose, indices))
        sweep_hitboxes(hits, i, last_hit_centers)
        skeleton_lines = [{"a": world_pose[parent][0], "b": world_pose[index][0]}
                          for index, parent in enumerate(parents) if parent >= 0]
        frame_data = {"index": i + 1, "game_frame_zero_based": i,
                       "animation_frame": animation_frame,
                       "pose_source": pose_source, "window": window_names[i],
                       "skeleton": skeleton_lines,
                       "boxes": hurt + hits + events[i]}
        if character_mesh:
            primitives = character_mesh.posed_primitives(world_pose)
            # Article meshes (Galvan's claw chains, ...) share the z-buffer.
            for spawn in article_spawns:
                if spawn["mesh_pose"] is None:
                    continue
                mesh = article_mesh(spawn["object_path"], str(CHAR), character_mesh.skin_textures)
                if mesh:
                    primitives = primitives + mesh.posed_primitives(spawn["mesh_pose"])
            frame_data["character_mesh"] = primitives
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
                # Convert GLB meters back to UE cm, then fit the rotated
                # camera's horizontal axis.
                ue_x, ue_y, ue_z = gltf_to_ue_cm(vertices)
                h = ue_x * right[0] + ue_y * right[1]
                min_h = min(min_h, float(h.min()) - 5)
                max_h = max(max_h, float(h.max()) + 5)
                min_z = min(min_z, float(ue_z.min()) - 5)
                max_z = max(max_z, float(ue_z.max()) + 5)
        for box in frame["boxes"]:
            if box.get("kind") not in ("hit", "hurt") or not box_drawn(box):
                continue
            # Article hitboxes that only travel (after their last change)
            # don't widen the view: a fireball's whole flight would shrink the
            # character to a few pixels. They fly out of frame instead.
            if box.get("camera_fit") is False:
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


def box_drawn(box):
    """Hurtboxes switched off by HurtboxActive=false are hidden by default."""
    return box.get("kind") != "hurt" or box.get("active", True) or HURTBOX_SHOW_DISABLED


def composite_box(image, box, camera, fill_rgb, fill_alpha, outline_rgb, outline_alpha,
                  outline_width):
    """Alpha-composite one circle/capsule with a separate fill and outline.

    The whole silhouette (capsule body plus end caps) goes into one mask and
    the outline comes from eroding it, so a capsule gets one outer outline
    instead of each end circle's ring showing through a translucent fill.

    Works on a padded crop around the shape instead of the whole image (the
    erosion filter dominated render time at full size). The result is the
    same: the padding keeps the crop's inner edges empty, and where the crop
    meets the image border Pillow's filter pads by repeating edge pixels,
    exactly as it does for the full image. Composites into `image` in place
    and returns it.
    """
    r = box["r"] * camera["scale"]
    if box["shape"] == "capsule":
        ends = (project(box["a"], camera), project(box["b"], camera))
    else:
        ends = (project(box["c"], camera),)
    pad = outline_width + 3
    left = max(0, int(math.floor(min(x for x, _ in ends) - r)) - pad)
    top = max(0, int(math.floor(min(y for _, y in ends) - r)) - pad)
    right = min(image.width, int(math.ceil(max(x for x, _ in ends) + r)) + pad)
    bottom = min(image.height, int(math.ceil(max(y for _, y in ends) + r)) + pad)
    if right <= left or bottom <= top:
        return image  # entirely off screen
    size = (right - left, bottom - top)
    ends = tuple((x - left, y - top) for x, y in ends)
    shape = Image.new("L", size, 0)
    draw = ImageDraw.Draw(shape)
    if len(ends) == 2:
        draw.line(ends, fill=255, width=max(1, round(2*r)))
    for cx, cy in ends:
        draw.ellipse((cx-r, cy-r, cx+r, cy+r), fill=255)
    interior = shape.filter(ImageFilter.MinFilter(2*outline_width + 1))
    outline = ImageChops.subtract(shape, interior)
    fill_layer = Image.new("RGBA", size, (*fill_rgb, 0))
    fill_layer.putalpha(interior.point(lambda v: v * fill_alpha // 255))
    outline_layer = Image.new("RGBA", size, (*outline_rgb, 0))
    outline_layer.putalpha(outline.point(lambda v: v * outline_alpha // 255))
    image.alpha_composite(fill_layer, dest=(left, top))
    image.alpha_composite(outline_layer, dest=(left, top))
    for temporary in (shape, interior, outline, fill_layer, outline_layer):
        temporary.close()
    return image


# Candidate pixels tested per rasterization batch. Bounds memory (each
# candidate needs a few dozen bytes of temporaries) without changing results.
RASTER_BATCH_PIXELS = 4_000_000


def draw_textured_mesh(layer, primitives, camera):
    """Rasterize palette and normal mapped skinned triangles with a z-buffer.

    Works on whole arrays instead of one triangle at a time:
      1. Visibility pass: every pixel inside every triangle's screen bounding
         box becomes a candidate; barycentric weights reject outside pixels,
         and the z-buffer keeps the nearest triangle per pixel (on equal depth
         the later triangle wins, as before).
      2. Shading pass: only the visible pixels are textured and lit, once
         each (deferred shading), grouped by primitive for its textures.
    """
    theta = math.radians(-camera["rotation_clockwise"])
    view_x, view_y = math.cos(theta), math.sin(theta)
    light = np.array((-0.35, 0.65, 0.68), dtype=np.float32)
    light /= np.linalg.norm(light)

    # Concatenate the textured primitives into one triangle list.
    textured = [(vertices, normals, indices, uvs, texture, normal_texture)
                for vertices, normals, indices, _, uvs, texture, normal_texture in primitives
                if texture is not None and uvs is not None]
    if not textured:
        return
    offsets = np.cumsum([0] + [len(item[0]) for item in textured])
    vertices = np.concatenate([item[0] for item in textured]).astype(np.float64)
    normals = np.concatenate([item[1] for item in textured]).astype(np.float64)
    uvs = np.concatenate([item[3] for item in textured]).astype(np.float64)
    triangles = np.concatenate([item[2].reshape(-1, 3).astype(np.int64) + offsets[i]
                                for i, item in enumerate(textured)])
    triangle_primitive = np.concatenate([np.full(len(item[2]) // 3, i, dtype=np.int32)
                                         for i, item in enumerate(textured)])

    # Per-vertex screen position and depth. (view_x, view_y) is the camera's
    # forward axis (Unreal Right x Up), pointing away from the viewer; the
    # distance along it is negated so the max-depth test keeps the nearest
    # surface.
    ue_x, ue_y, ue_z = gltf_to_ue_cm(vertices)
    horizontal = ue_x * camera["right"][0] + ue_y * camera["right"][1]
    screen_x = WIDTH / 2 + (horizontal - camera["center_horizontal"]) * camera["scale"]
    screen_y = HEIGHT / 2 - (ue_z - camera["center_z"]) * camera["scale"]
    vertex_depth = -(ue_x * view_x + ue_y * view_y)

    # Per-triangle bounding boxes and barycentric (edge function) setup.
    xa, xb, xc = (screen_x[triangles[:, k]] for k in range(3))
    ya, yb, yc = (screen_y[triangles[:, k]] for k in range(3))
    x0 = np.maximum(0, np.floor(np.minimum(np.minimum(xa, xb), xc))).astype(np.int64)
    x1 = np.minimum(WIDTH, np.ceil(np.maximum(np.maximum(xa, xb), xc)).astype(np.int64) + 1)
    y0 = np.maximum(0, np.floor(np.minimum(np.minimum(ya, yb), yc))).astype(np.int64)
    y1 = np.minimum(HEIGHT, np.ceil(np.maximum(np.maximum(ya, yb), yc)).astype(np.int64) + 1)
    denom = (yb - yc) * (xa - xc) + (xc - xb) * (ya - yc)
    keep = np.flatnonzero((x1 > x0) & (y1 > y0) & (np.abs(denom) >= 1e-8))
    box_w, box_h = (x1 - x0)[keep], (y1 - y0)[keep]
    areas = box_w * box_h

    depth_buffer = np.full(HEIGHT * WIDTH, -np.inf, dtype=np.float64)
    triangle_buffer = np.full(HEIGHT * WIDTH, -1, dtype=np.int64)
    weight_a = np.zeros(HEIGHT * WIDTH, dtype=np.float64)
    weight_b = np.zeros(HEIGHT * WIDTH, dtype=np.float64)

    # Visibility pass, in batches of whole triangles.
    ends = np.cumsum(areas)
    batch_start = 0
    while batch_start < len(keep):
        limit = (ends[batch_start - 1] if batch_start else 0) + RASTER_BATCH_PIXELS
        batch_end = max(batch_start + 1, int(np.searchsorted(ends, limit, side="right")))
        rows = slice(batch_start, batch_end)
        batch_start = batch_end
        tri = keep[rows]
        counts = areas[rows]
        local = np.repeat(np.arange(len(tri)), counts)
        first = np.cumsum(counts) - counts
        offset = np.arange(int(counts.sum())) - first[local]
        width = box_w[rows][local]
        px = x0[tri][local] + offset % width
        py = y0[tri][local] + offset // width
        t = tri[local]
        gx, gy = px + .5, py + .5
        d = denom[t]
        wa = ((yb[t] - yc[t]) * (gx - xc[t]) + (xc[t] - xb[t]) * (gy - yc[t])) / d
        wb = ((yc[t] - ya[t]) * (gx - xc[t]) + (xa[t] - xc[t]) * (gy - yc[t])) / d
        wc = 1.0 - wa - wb
        inside = (wa >= -1e-5) & (wb >= -1e-5) & (wc >= -1e-5)
        if not inside.any():
            continue
        t, wa, wb, wc = t[inside], wa[inside], wb[inside], wc[inside]
        pixel = py[inside] * WIDTH + px[inside]
        corners = triangles[t]
        depth = (wa * vertex_depth[corners[:, 0]] + wb * vertex_depth[corners[:, 1]]
                 + wc * vertex_depth[corners[:, 2]])
        np.maximum.at(depth_buffer, pixel, depth)
        # Candidates at the pixel's max depth win. Fancy assignment applies in
        # order, so among equal depths the later triangle wins, like the old
        # per-triangle loop. Later batches overwrite earlier winners they beat.
        win = depth >= depth_buffer[pixel]
        pixel, t = pixel[win], t[win]
        triangle_buffer[pixel] = t
        weight_a[pixel] = wa[win]
        weight_b[pixel] = wb[win]

    # Shading pass: only visible pixels, grouped by primitive.
    visible = np.flatnonzero(triangle_buffer >= 0)
    color = np.zeros((HEIGHT * WIDTH, 3), dtype=np.uint8)
    if len(visible):
        t_all = triangle_buffer[visible]
        prim_all = triangle_primitive[t_all]
        for index, (_, _, _, _, texture, normal_texture) in enumerate(textured):
            select = prim_all == index
            if not select.any():
                continue
            pixels = visible[select]
            t = t_all[select]
            wa = weight_a[pixels]
            wb = weight_b[pixels]
            wc = 1.0 - wa - wb
            corners = triangles[t]
            uv0, uv1, uv2 = uvs[corners[:, 0]], uvs[corners[:, 1]], uvs[corners[:, 2]]
            u = wa * uv0[:, 0] + wb * uv1[:, 0] + wc * uv2[:, 0]
            v = wa * uv0[:, 1] + wb * uv1[:, 1] + wc * uv2[:, 1]
            tex_h, tex_w = texture.shape[:2]
            tx = np.clip((u * tex_w).astype(np.int32), 0, tex_w - 1)
            ty = np.clip((v * tex_h).astype(np.int32), 0, tex_h - 1)
            base_rgb = texture[ty, tx].astype(np.float32)
            if normal_texture is not None:
                # Per-triangle tangent frame from the UV layout.
                p0, p1, p2 = (vertices[corners[:, k]] for k in range(3))
                du1, dv1 = (uv1 - uv0)[:, 0:1], (uv1 - uv0)[:, 1:2]
                du2, dv2 = (uv2 - uv0)[:, 0:1], (uv2 - uv0)[:, 1:2]
                determinant = du1 * dv2 - dv1 * du2
                mapped_ok = np.abs(determinant[:, 0]) > 1e-8
                safe = np.where(mapped_ok[:, None], determinant, 1.0)
                edge1, edge2 = p1 - p0, p2 - p0
                tangent = (edge1 * dv2 - edge2 * dv1) / safe
                bitangent = (edge2 * du1 - edge1 * du2) / safe
                for vector in (tangent, bitangent):
                    length = np.linalg.norm(vector, axis=1, keepdims=True)
                    np.divide(vector, length, out=vector, where=length > 1e-8)
                handedness = np.where(np.sum(np.cross(edge1, edge2) * np.cross(tangent, bitangent),
                                             axis=1) >= 0, 1.0, -1.0)[:, None]
                n0, n1, n2 = (normals[corners[:, k]] for k in range(3))
                geom_n = wa[:, None] * n0 + wb[:, None] * n1 + wc[:, None] * n2
                geom_n /= np.maximum(np.linalg.norm(geom_n, axis=1, keepdims=True), 1e-8)
                tangent_grid = tangent - geom_n * np.sum(geom_n * tangent, axis=1, keepdims=True)
                tangent_grid /= np.maximum(np.linalg.norm(tangent_grid, axis=1, keepdims=True), 1e-8)
                bitangent_grid = np.cross(geom_n, tangent_grid) * handedness
                norm_h, norm_w = normal_texture.shape[:2]
                ntex = normal_texture[np.clip((v * norm_h).astype(np.int32), 0, norm_h - 1),
                                      np.clip((u * norm_w).astype(np.int32), 0, norm_w - 1)
                                      ].astype(np.float64) / 127.5 - 1
                # Rivals normal maps are Unreal/DirectX convention: invert green for this renderer.
                ntex[:, 1] *= -1
                mapped_n = (tangent_grid * ntex[:, 0:1] + bitangent_grid * ntex[:, 1:2]
                            + geom_n * ntex[:, 2:3])
                mapped_n /= np.maximum(np.linalg.norm(mapped_n, axis=1, keepdims=True), 1e-8)
                diffuse = np.maximum(0, mapped_n @ light)
                shade = np.where(diffuse < .30, .42, np.where(diffuse < .62, .70, 1.0)).astype(np.float32)
                # Triangles with a degenerate UV layout stay unlit, as before.
                base_rgb *= np.where(mapped_ok, shade, 1.0).astype(np.float32)[:, None]
            color[pixels] = np.clip(base_rgb, 0, 255).astype(np.uint8)
    rgba = np.zeros((HEIGHT * WIDTH, 4), dtype=np.uint8)
    rgba[:, :3] = color
    rgba[visible, 3] = 255
    layer.alpha_composite(Image.fromarray(rgba.reshape(HEIGHT, WIDTH, 4), "RGBA"))


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
                    ue_x, ue_y, ue_z = gltf_to_ue_cm(points)
                    # Negated distance along the camera's forward axis:
                    # ascending order draws farthest first, nearest last.
                    depth = -float((ue_x*math.cos(theta) + ue_y*math.sin(theta)).mean())
                    screen = [project((float(x), float(y), float(z)), camera)
                              for x, y, z in zip(ue_x, ue_y, ue_z)]
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
            draw.line((a, b), fill=(*SKELETON_COLOR, SKELETON_ALPHA), width=2)
        image = Image.alpha_composite(image, skeleton_layer)
        skeleton_layer.close()

    # Each box is composited separately so overlaps build opacity and the
    # skeleton stays visible beneath translucent fills.
    for box in frame["boxes"]:
        if box.get("kind") == "hurt" and box_drawn(box):
            fill = hurtbox_color(box)
            image = composite_box(image, box, camera, fill, HURTBOX_FILL_ALPHA,
                                  hurtbox_outline_color(fill), HURTBOX_OUTLINE_ALPHA,
                                  HURTBOX_OUTLINE_WIDTH)
    for box in frame["boxes"]:
        if box.get("kind") == "hit":
            color = hitbox_color(box)
            image = composite_box(image, box, camera, color, HITBOX_FILL_ALPHA,
                                  color, HITBOX_OUTLINE_ALPHA, HITBOX_OUTLINE_WIDTH)
    image.save(image_path)
    image.close()


def export_variant(attack, name, variant, animation, ref_local, parents, indices, out_dir,
                   show_skeleton=True, show_character=False, character_mesh=None,
                   camera_rotation=90):
    move_dir = out_dir / f"{name}_{variant}"
    articles = []
    frames = move_frames(attack, animation, ref_local, parents, indices, character_mesh, articles)
    camera = camera_for_frames(frames, camera_rotation)
    move_dir.mkdir(parents=True, exist_ok=True)
    frame_stem = f"RoA2_{CHAR.name}_{name}_{variant}_HB_"
    for stale in (*move_dir.glob("frame_*.png"), *move_dir.glob(f"{frame_stem}*.png")):
        stale.unlink()
    for frame in frames:
        # Files are numbered from 0000 (the move's first frame), as the user
        # chose on 2026-11-26; boxes.json keeps both "index" (1-based) and
        # "game_frame_zero_based".
        filename = f"{frame_stem}{frame['game_frame_zero_based']:04d}.png"
        render_frame(move_dir / filename, f"{name} ({variant})",
                     frame, camera, show_skeleton, show_character)
    info = move_info(attack, name, variant, animation)
    info["hurtbox_states"] = hurtbox_state_summary(frames)
    info["articles"] = articles
    info["active_frames"] = write_active_frames(move_dir, frame_stem, frames)
    write_move_info(move_dir, info)
    pose_source = "ueanim" if animation and animation.suffix.lower() == ".ueanim" else "reference_skeleton"
    data = {"move": name, "variant": variant, "animation": animation.stem if animation else None,
            "fps": FPS, "animation_source_fps": read_ueanim(str(animation.resolve()))["fps"]
            if pose_source == "ueanim" else None,
            "pose_source": pose_source,
            "pose_warning": None if pose_source == "ueanim" else REFERENCE_WARNING,
            "camera": camera,
            "hurtbox_asset": str(PHYSICS.relative_to(ROOT)) if PHYSICS else None,
            "character_data_asset": str(CHARACTER_DATA.relative_to(ROOT)) if CHARACTER_DATA else None,
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
    parser.add_argument("--skip", action="append", default=[], metavar="REGEX",
                        help="skip attack assets whose name matches this case-insensitive regular "
                             "expression, e.g. --skip Intro --skip Taunt (repeatable)")
    skeleton_group = parser.add_mutually_exclusive_group()
    skeleton_group.add_argument("--show-skeleton", action="store_true", dest="show_skeleton",
                                help="draw animated bone connections (default)")
    skeleton_group.add_argument("--hide-skeleton", action="store_false", dest="show_skeleton",
                                help="omit animated bone connections from PNGs")
    parser.set_defaults(show_skeleton=False)
    parser.add_argument("--show-character", action="store_true",
                        help="render the character mesh from Skins/Default/*.glb beneath boxes")
    parser.add_argument("--skin-textures", "--skin-texture", action="store_true",
                        help="bake the exported color-region masks and material palette onto the mesh (implies --show-character)")
    parser.add_argument("--camera-rotation", type=float, default=90,
                        help="rotate view clockwise around the character in degrees (default: 90, the "
                             "gameplay plane with Unreal X across the screen; 0 is the old view along "
                             "the depth axis)")
    parser.add_argument("--out", type=Path,
                        help="output folder (default: Rivals2/HitboxFrames/<character>)")
    args = parser.parse_args()
    show_character = args.show_character or args.skin_textures
    global CHAR, ATTACKS, PHYSICS, SKELETON, CHARACTER_DATA

    def discover(character):
        char_dir = CHARACTERS_ROOT / character
        attacks_dir = char_dir / "Attacks"
        attack_paths = sorted(attacks_dir.glob("ATT_*.json"))
        animation_dir = char_dir / "Animation"
        skeletons = sorted(animation_dir.glob("*Skeleton.json"))
        if not attack_paths or not skeletons:
            return None
        # Prefer CD_<Folder>.json (Gouie also has CD_BigGouie.json).
        character_data = char_dir / f"CD_{character}.json"
        if not character_data.is_file():
            character_data = next(iter(sorted(char_dir.glob("CD_*.json"))), None)
        # The character data asset names the hurtbox physics asset directly.
        physics_ref = (load_character_data(str(character_data)) if character_data else {}
                       ).get("HurtboxesPhysicsAsset", {}) or {}
        named = physics_ref.get("ObjectPath", "").rsplit(".", 1)[0]
        physics_path = ROOT / f"{named}.json" if named else None
        if not physics_path or not physics_path.is_file():
            # Fallback: the HB_*.json whose name matches the attack prefix
            # (e.g. ATT_Lox_* -> HB_Lox); accessory physics files are ignored.
            attack_prefix = attack_paths[0].stem.removeprefix("ATT_").split("_")[0].casefold()
            physics = list(char_dir.glob("HB_*.json"))
            physics.sort(key=lambda p: (not p.stem.removeprefix("HB_").casefold().startswith(attack_prefix),
                                       len(p.stem), p.name))
            physics_path = physics[0] if physics else None
        return (char_dir, attacks_dir, physics_path, skeletons[0], attack_paths,
                character_data)

    characters = sorted(p.name for p in CHARACTERS_ROOT.iterdir() if p.is_dir()) if args.all_characters else [args.character]
    overall_moves = overall_frames = 0
    for character in characters:
        found = discover(character)
        if not found:
            if args.all_characters:
                print(f"Skipping {character}: no attack JSONs or primary Animation/*Skeleton.json")
                continue
            raise SystemExit(f"Could not find attack JSONs and a skeleton under {CHARACTERS_ROOT / character}")
        CHAR, ATTACKS, PHYSICS, SKELETON, paths, CHARACTER_DATA = found
        default_out = ROOT / "Rivals2/HitboxFrames"
        out_dir = (args.out / character if args.all_characters else args.out) if args.out else default_out / character
        if args.move:
            paths = [p for p in paths if p.stem.casefold() == args.move.casefold()]
            if not paths:
                if args.all_characters:
                    continue
                raise SystemExit(f"No attack JSON named {args.move!r} under {ATTACKS}")
        if args.skip:
            kept = [p for p in paths if not any(re.search(pattern, p.stem, re.IGNORECASE)
                                                for pattern in args.skip)]
            if len(kept) < len(paths):
                print(f"{character}: skipping {len(paths) - len(kept)} attack assets matching "
                      f"{', '.join(args.skip)}: {', '.join(p.stem for p in paths if p not in kept)}")
            paths = kept
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
                                                 character_mesh, args.camera_rotation)
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

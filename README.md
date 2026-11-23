# Rivals 2 hitbox and hurtbox frames

This project reads FModel exports for Rivals 2, builds a per-frame representation of attack and hurt boxes, renders transparent PNGs, and then stitches each move into a GIF (or webm/apng).

The expected inputs are:

- FModel property JSON for the attack assets (`ATT_*.json`), the character's body physics asset (`HB_*.json`), and the main skeleton (`Animation/*Skeleton.json`).
- A matching UEFormat animation file (`.ueanim`) for each move animation. Without it, the renderer uses the skeleton reference pose and marks that output as a fallback.

## 1. Configure FModel and the `.usmap`

1. In FModel, select the Rivals 2 archive directory (the directory containing its `.pak` archive, `Rivals2/Content/Paks/`) and the game's Unreal version. The scripts use `GAME_UE5_8` for CUE4Parse exports and this should be auto-selected by FModel.
2. You can drag the `.usmap` file once the `.pak` is loaded. If that doesn't work open **Settings → General**, enable **Local Mapping File**, and set **Mapping File Path** to the Rivals 2 `.usmap` file. In this workspace the file is named `Rivals2-5.8.0-0+UE5-5418fa4e.usmap`.
3. As of this commit, no AES encryption was present. If that does get implemented, a solution for that needs to be created
4. Reload the archives. Browse to `Rivals2/Content/Characters/` and confirm that packages open and their properties are readable. The mapping file lets CUE4Parse/FModel interpret unversioned package properties; it does not itself extract assets.

FModel settings and package export behavior are described in the [official FModel settings guide](https://github.com/4sval/FModel/wiki/Settings-Explanation) and [Getting Started guide](https://github.com/4sval/FModel/wiki/Getting-Started).

## 2. Export the required data

Export the `Content/Characters` packages while preserving their directory structure. The scripts expect the files to end up beneath this project's `Rivals2/Content/Characters/` directory.

### Properties as JSON

Use FModel's **Save/Export Properties** action (or its **Export Session** in builds that provide it) to export package properties as JSON. At minimum, export:

- Right click on the `Characters` folder and under `Export Folder` select the following:
  - **Properties (.json)**

The renderer reads attack window and hitbox data from the `ATT_*.json`, capsule/sphere hurtbox data from `HB_*.json`, and bone reference transforms from the skeleton JSON. Keep the original package names and folder structure so animation references can be matched.

### Asset/Animations as UEFormat

Export the character's move animations in **UEFormat** format, which creates `.uasset` and `.ueanim` filesKeep them under the same character folder; the exact subfolder can follow FModel's exported path. 

- Right click on the `Characters` folder and under `Export Folder` select the following:
  - **Raw Data (.uasset)**
  - **Animations**

For example:

```text
Rivals2/Content/Characters/Loxodont/
├── Attacks/ATT_Lox_Bair.json
├── HB_Lox.json
└── Animation/
    ├── SK_Lox_Skeleton.json
    └── Attacks/AN_Lox_BAir.ueanim
```

The UEFormat model exporter supports `.uemodel` and `.ueanim`; the frame renderer specifically needs the `.ueanim` files, not `.uasset`/`.uexp` copies. See the [UEFormat format and Unreal plugin repository](https://github.com/h4lfheart/UEFormat).

Example:

![FModel Selections](images/FModel_Selection.png)

## 3. Python setup

Create a virtual environment

```bash
python -m venv .venv
```

Activate the virtual environment

### Windows

```powershell
.venv/bin/activate.ps1
```

### Linux (bash/fish)

```bash
source .venv/bin/activate
```

```bash
source .venv/bin/activate.fish
```

Run commands from the repository directory. Pillow is needed for PNG/GIF rendering:

```bash
python -m pip install -r requirements.txt
```

The renderer can read compressed `.ueanim` files using the `zstandard` Python package or the system `libzstd`. If neither is available, install the Python package:

```bash
python -m pip install zstandard
```

## 4. Render frames

Render all supported characters:

```bash
python rivals2_move_boxes.py --all
```

Render just one character or move:

```bash
python rivals2_move_boxes.py --character Loxodont
python rivals2_move_boxes.py --character Loxodont --move ATT_Lox_Bair
python rivals2_move_boxes.py --character Loxodont --move ATT_Lox_Bair --hide-skeleton
```

By default, outputs go to `Rivals2/HitboxFrames/<Character>/`. Each attack animation variant gets a folder, such as `ATT_Lox_Bair_Grounded/`. PNGs use a descriptive filename that includes game, character, attack asset, variant, and 1-based frame number:

```text
RoA2_Loxodont_ATT_Lox_Bair_Grounded_HB_0001.png
RoA2_Loxodont_ATT_Lox_Bair_Grounded_HB_0002.png
```

Each move folder also contains:

- `boxes.json` — frame-by-frame box positions and metadata.
- `info.json` and `info.md` — available move stats and active hitbox windows to mirror the [Brawl style output](https://rukaidata.com/P+/Ice%20Climbers/subactions/AttackAirF.html)

To change the output root, pass `--out`. With `--all`, the character name is added below that root:

```bash
python rivals2_move_boxes.py --all --out Rivals2/HitboxFrames
```

## 5. Create GIFs

Create a GIF for every move folder for every character. In fish or bash, run:

```bash
./make_character_gifs.sh --all Rivals2/HitboxFrames Rivals2/GIFs --no-dither
```

**TODO**
  - Create a windows batch script to handle this as well

This creates one subfolder per character and names each GIF after its move folder, for example `Rivals2/GIFs/Loxodont/ATT_Lox_Bair_Grounded.gif`.

To use a matte color instead of `--no-dither`:

```bash
./make_character_gifs.sh --all Rivals2/HitboxFrames Rivals2/GIFs --matte-color 313338
```

`--no-dither` and `--matte-color` are mutually exclusive. To render one move manually:

```bash
python make_attack_gif.py Rivals2/HitboxFrames/Loxodont/ATT_Lox_Bair_Grounded \
  --out Rivals2/GIFs/Loxodont/ATT_Lox_Bair_Grounded.gif --no-dither
```

`make_attack_gif.py` accepts both the newer descriptive `*_HB_####.png` names and legacy `frame_####.png` names. GIF transparency supports only fully transparent or fully opaque pixels; APNG and WebM output can preserve partial alpha:

```bash
python make_attack_gif.py Rivals2/HitboxFrames/Loxodont/ATT_Lox_Bair_Grounded --format apng
python make_attack_gif.py Rivals2/HitboxFrames/Loxodont/ATT_Lox_Bair_Grounded --format webm
```

## Optional: export package JSON from the pak with CUE4Parse. 

Note:
> This is not fully tested as getting it configured properly was giving me issues and decided to move on with hitbox extraction.

`export_characters_from_pak.py` can automate package JSON (or raw archive) extraction if `cue4parse` is installed and available on `PATH`. It uses the project-default pak and `.usmap` filenames, or accepts explicit paths:

```bash
python export_characters_from_pak.py --format list
python export_characters_from_pak.py --format json --out Rivals2/Exports/Characters
python export_characters_from_pak.py --pak /path/to/Rivals2-Windows.pak \
  --usmap /path/to/Rivals2.usmap --format json --out Rivals2/Exports/Characters
```

Use `--format raw` to extract raw `.uasset`/`.uexp` archive entries. Raw extraction does not create UEFormat `.ueanim` files and is not, by itself, enough to render animated frames. FModel remains the route used here for exporting UEFormat animations.


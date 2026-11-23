#!/usr/bin/env bash
# Create one named GIF per direct move folder under a character's HitboxFrames folder.
# Usage: ./make_character_gifs.sh INPUT_CHARACTER_FOLDER OUTPUT_FOLDER [GIF OPTIONS...]
#        ./make_character_gifs.sh --all INPUT_ROOT OUTPUT_ROOT [GIF OPTIONS...]
# Example: ./make_character_gifs.sh Rivals2/HitboxFrames/Loxodont Rivals2/GIFs/Loxodont --matte-color 313338
# Example: ./make_character_gifs.sh --all Rivals2/HitboxFrames Rivals2/GIFs --matte-color 313338
set -u

usage() {
    printf 'Usage: %s INPUT_CHARACTER_FOLDER OUTPUT_FOLDER [GIF OPTIONS...]\n' "$0" >&2
    printf '       %s --all INPUT_ROOT OUTPUT_ROOT [GIF OPTIONS...]\n' "$0" >&2
    printf 'Example: %s Rivals2/HitboxFrames/Loxodont Rivals2/GIFs/Loxodont --matte-color 313338\n' "$0" >&2
    printf 'Example: %s --all Rivals2/HitboxFrames Rivals2/GIFs --matte-color 313338\n' "$0" >&2
}

all_characters=0
if [[ ${1-} == --all ]]; then
    all_characters=1
    shift
fi

if (( $# < 2 )); then
    usage
    exit 2
fi

input_dir=$1
output_dir=$2
shift 2

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ ! -d "$input_dir" ]]; then
    printf 'Input folder does not exist: %s\n' "$input_dir" >&2
    exit 2
fi

shopt -s nullglob
character_dirs=()
if (( all_characters )); then
    character_dirs=("$input_dir"/*/)
else
    character_dirs=("$input_dir"/)
fi

created=0
failed=0
for character_dir in "${character_dirs[@]}"; do
    character_name=$(basename -- "${character_dir%/}")
    if (( all_characters )); then
        character_output="$output_dir/$character_name"
    else
        character_output=$output_dir
    fi
    mkdir -p -- "$character_output"
    move_dirs=("$character_dir"/*/)
    if (( ${#move_dirs[@]} == 0 )); then
        printf 'No move folders found in %s; skipping.\n' "$character_dir" >&2
        continue
    fi
    for move_dir in "${move_dirs[@]}"; do
        move_name=$(basename -- "${move_dir%/}")
        destination="$character_output/$move_name.gif"
        if python "$script_dir/make_attack_gif.py" "$move_dir" --out "$destination" "$@"; then
            ((created += 1))
        else
            printf 'Skipping failed move: %s\n' "$move_dir" >&2
            ((failed += 1))
        fi
    done
done
shopt -u nullglob

printf 'Finished: %d GIF(s) created, %d move folder(s) skipped.\n' "$created" "$failed"
(( failed == 0 ))

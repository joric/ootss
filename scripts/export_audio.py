"""Extract subtitle references per level into audio.json.

Output: { "<level>": ["<subtitle_id>", ...], ... }

python3 scripts/extract_audio_refs.py --package /path/to/levels.package --out audio.json
"""
import argparse
import json
import struct
from pathlib import Path

from simp_tool import parse_entities, decode_diff, build_tables, load_schema, extract_schema

AUDIO_FIELDS = {
    'Guy': {'initial_bark', 'win_bark'},
    'Marker': {'string_parameter', 'script_name'},
    'Voice_Log': {'name'}, 'Checkpoint': {'story'},
    'Sign': {'turn_away_bark', 'look_at_bark'}, 'Wardrobe': {'usage_bark'},
    'Boat': {'initial_talk_name', 'repeat_talk_name'},
    'Overworld_Unlock': {'bark_name'}, 'Judge': {'code'},
    'Settings': {'story_tag', 'intro_cutscene', 'outro_cutscene', 'story_proc_arg_string'},
    'Artifact': {'cutscene_name'},
}


def package_entries(handle):
    if handle.read(4) != b"simp":
        raise ValueError("Invalid Simple_Package header")
    handle.seek(16)
    toc = struct.unpack("<Q", handle.read(8))[0]
    handle.seek(toc + 8)
    count = struct.unpack("<Q", handle.read(8))[0]
    handle.seek(toc + 64)
    entries = {}
    for _ in range(count):
        length = struct.unpack("<I", handle.read(4))[0]
        name = handle.read(length).decode("utf-8")
        handle.read(1)
        entries[name.replace("\\", "/")] = struct.unpack("<QQ", handle.read(16))
    return entries


def initialize_schema(package, schema=None):
    source = schema or package.parent.parent / "sinking_star.exe"
    build_tables(extract_schema(str(source)) if source.suffix.lower() == ".exe"
                 else load_schema(str(source)))


def scan(package):
    levels = {}
    with package.open('rb') as handle:
        entries = package_entries(handle)
        for source, (size, offset) in sorted(entries.items()):
            if not source.endswith('.entities'):
                continue
            handle.seek(offset)
            raw = handle.read(size)
            values = set()
            for _, entity_type, diff, version in parse_entities(raw):
                # Filter by type BEFORE decoding — decode_diff is the expensive call.
                if entity_type not in AUDIO_FIELDS:
                    continue
                fields = {m['name']: m['value']
                          for m in decode_diff(entity_type, diff, version) if 'name' in m}
                for field in AUDIO_FIELDS[entity_type]:
                    value = fields.get(field)
                    if isinstance(value, str) and value:
                        values.add(value)
            if values:
                levels[Path(source).stem] = sorted(values)
    return levels


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--schema', type=Path,
                        help='Matching schema JSON or game executable; '
                             'defaults to ../sinking_star.exe next to the package')
    parser.add_argument('--out', type=Path, default=Path('audio.json'))
    args = parser.parse_args()

    initialize_schema(args.package, args.schema)
    levels = scan(args.package)
    args.out.write_text(json.dumps(levels, indent=2), encoding='utf-8')
    print(f'{len(levels)} levels, '
          f'{sum(len(v) for v in levels.values())} subtitle refs -> {args.out}')


if __name__ == '__main__':
    main()

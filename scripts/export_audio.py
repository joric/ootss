"""Extract subtitle references per level into audio.json.

Output: { "<level>": ["<subtitle_id>", ...], ... }

python3 scripts/extract_audio_refs.py --package /path/to/levels.package --out audio.json
python3 scripts/extract_audio_refs.py --package /path/to/levels.package \
    --subtitles en.subtitles.txt --out audio.json
"""
import argparse
import json
import struct
from collections import defaultdict
from pathlib import Path

from simp_tool import parse_entities, decode_diff, build_tables, load_schema, extract_schema

AUDIO_REF_FIELDS = {
    'Guy': {'initial_bark', 'win_bark'},
    'Marker': {'string_parameter'},
    'Voice_Log': {'name'},
    'Checkpoint': {'story'},
    'Sign': {'turn_away_bark', 'look_at_bark'},
    'Wardrobe': {'usage_bark'},
    'Boat': {'initial_talk_name', 'repeat_talk_name'},
    'Overworld_Unlock': {'bark_name'},
    'Judge': {'code'},
    'Artifact': {'cutscene_name'},
}

SETTINGS_NAMESPACE_FIELDS = {'story_tag', 'story_proc_arg_string',
                             'intro_cutscene', 'outro_cutscene'}

# Only these types can contribute anything; everything else is skipped
# before decode_diff runs.
WANTED_TYPES = set(AUDIO_REF_FIELDS) | {'Settings'}

MARKER_SCRIPT = {'bark', 'bark_conditional', 'bark_on_id'}


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


def load_namespace_index(path):
    if not path:
        return {}
    ids = set()
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        if line.startswith(':'):
            token = line.lstrip(':').strip().split()
            if token:
                ids.add(token[0])
    index = defaultdict(list)
    for block in ids:
        parts = block.split('_')
        for i in range(1, len(parts)):
            index['_'.join(parts[:i]) + '_'].append(block)
    return {k: tuple(v) for k, v in index.items()}


def scan(package, namespace_index):
    levels = {}
    with package.open('rb') as handle:
        entries = package_entries(handle)
        for source, (size, offset) in sorted(entries.items()):
            if not source.endswith('.entities'):
                continue
            handle.seek(offset)
            raw = handle.read(size)

            # First pass: collect ids of wanted entities only, without decoding
            # the diff. parse_entities is cheap; decode_diff is not.
            wanted = []
            for entity_id, entity_type, diff, version in parse_entities(raw):
                if entity_type not in WANTED_TYPES:
                    continue
                wanted.append((entity_type, diff, version))
            if not wanted:
                continue

            values = set()
            for entity_type, diff, version in wanted:
                fields = {m['name']: m['value']
                          for m in decode_diff(entity_type, diff, version) if 'name' in m}
                for field in AUDIO_REF_FIELDS.get(entity_type, ()):
                    value = fields.get(field)
                    if not isinstance(value, str) or not value:
                        continue
                    if entity_type == 'Marker' and field == 'string_parameter':
                        if fields.get('script_name') not in MARKER_SCRIPT:
                            continue
                    values.add(value)

                if entity_type == 'Settings' and namespace_index:
                    for field in SETTINGS_NAMESPACE_FIELDS:
                        value = fields.get(field)
                        if not isinstance(value, str) or not value:
                            continue
                        implied = namespace_index.get(value + '_')
                        if implied:
                            values.update(implied)
            if values:
                levels[Path(source).stem] = sorted(values)
    return levels


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--schema', type=Path,
                        help='Matching schema JSON or game executable; '
                             'defaults to ../sinking_star.exe next to the package')
    parser.add_argument('--subtitles', type=Path,
                        help='Optional English subtitles.txt. When given, expands '
                             'Settings dialogue namespaces into concrete block ids.')
    parser.add_argument('--out', type=Path, default=Path('audio.json'))
    args = parser.parse_args()

    initialize_schema(args.package, args.schema)
    namespace_index = load_namespace_index(args.subtitles)
    levels = scan(args.package, namespace_index)
    args.out.write_text(json.dumps(levels, indent=2), encoding='utf-8')
    print(f'{len(levels)} levels, '
          f'{sum(len(v) for v in levels.values())} subtitle refs -> {args.out}')


if __name__ == '__main__':
    main()

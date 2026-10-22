import json
import re
from pathlib import Path
from collections import defaultdict
import argparse
from pathlib import Path

def strip_inline_comment(line):
    if '#' in line:
        line = line.split('#')[0]
    return line.strip()

def is_valid_level_name(name):
    return bool(re.match(r'^[a-zA-Z0-9_.\-]+$', name))

def parse_level_set_file(filepath):
    groups = []
    current_group = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith('#'):
                continue
            if line.startswith('['):
                continue
            if line.startswith('*'):
                if line == '*choice' and current_group:
                    groups.append(current_group)
                    current_group = []
                continue
            line = strip_inline_comment(line)
            if line and is_valid_level_name(line):
                current_group.append(line)
    if current_group:
        groups.append(current_group)
    return groups

def build_data(level_sets_dir):
    all_levels = {}
    with_parents = {}
    all_by_area = defaultdict(dict)

    level_set_files = sorted(Path(level_sets_dir).glob('*.level_set'))
    if not level_set_files:
        print(f"No .level_set files found in {level_sets_dir}")
        return {}, {}

    filtered, skipped = [], []
    for fp in level_set_files:
        stem_lower = fp.stem.lower()
        if stem_lower.startswith('drafts') or stem_lower == 'design_illustrations':
            skipped.append(fp.stem)
        else:
            filtered.append(fp)

    print(f"Found {len(level_set_files)} level_set files "
          f"({len(filtered)} kept, {len(skipped)} skipped)\n")
    if skipped:
        print(f"Skipped: {', '.join(skipped)}\n")

    for filepath in filtered:
        area = filepath.stem
        print(f"Processing [{area}]: {filepath.name}")
        try:
            groups = parse_level_set_file(filepath)
            area_levels = {}
            prev_group = []
            for group in groups:
                for level in group:
                    area_levels[level] = sorted(set(prev_group))
                prev_group = group
            all_by_area[area] = area_levels
            print(f"  {len(area_levels)} levels across {len(groups)} groups")
        except Exception as e:
            print(f"  ERROR: {e}")

    for area, area_levels in all_by_area.items():
        for level, parents in area_levels.items():
            if level not in all_levels:
                all_levels[level] = {"parents": parents, "area": area}
                if parents:
                    with_parents[level] = {"parents": parents, "area": area}

    return all_levels, with_parents

def enrich_geojson(geojson_path, level_data):
    with open(geojson_path, 'r', encoding='utf-8') as f:
        geojson = json.load(f)

    features = geojson.get('features', [])
    matched = 0
    unmatched_names = []

    for feature in features:
        props = feature.get('properties') or {}
        name = props.get('name')
        if name and name in level_data:
            props['area'] = level_data[name]['area']
            parents = level_data[name]['parents']
            if parents:  # only write parents when non-empty
                props['parents'] = parents
            matched += 1
        elif name:
            unmatched_names.append(name)

    return geojson, matched, unmatched_names

import argparse
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build level prerequisites and enrich a markers GeoJSON file."
    )
    parser.add_argument(
        "level_sets_dir",
        type=Path,
        help="Directory containing unpacked level set data.",
    )
    parser.add_argument(
        "-m", "--markers",
        type=Path,
        default=None,
        help="Path to input markers GeoJSON (default: ./markers.geojson).",
    )
    parser.add_argument(
        "-p", "--prereqs",
        type=Path,
        default=None,
        help="Path to output level prerequisites JSON (default: ./level_prerequisites.json).",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Path to output enriched GeoJSON (default: ./markers_enriched.geojson).",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    level_sets_dir = args.level_sets_dir
    cwd = Path.cwd()
    markers_path = args.markers or (cwd / "markers.geojson")
    prereqs_path = args.prereqs or (cwd / "level_prerequisites.json")
    output_path = args.output or (cwd / "markers_enriched.geojson")

    print(f"Scanning: {level_sets_dir}\n")
    all_levels, with_parents = build_data(level_sets_dir)

    sorted_prereqs = {k: with_parents[k] for k in sorted(with_parents)}
    with open(prereqs_path, 'w', encoding='utf-8') as f:
        json.dump(sorted_prereqs, f, indent=2, sort_keys=True)

    print(f"\nSaved {len(sorted_prereqs)} levels (with parents) to:")
    print(f"  {prereqs_path}")
    print(f"(Total levels known incl. parentless: {len(all_levels)})")

    if not markers_path.exists():
        print(f"\nERROR: {markers_path} not found. Skipping enrichment.")
        return

    print(f"\nLoading markers: {markers_path}")
    enriched_geojson, matched, unmatched = enrich_geojson(markers_path, all_levels)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(enriched_geojson, f, indent=2, ensure_ascii=False)

    print(f"Enriched {matched} feature(s).")
    if unmatched:
        print(f"  {len(unmatched)} marker(s) had no matching level entry:")
        for name in unmatched[:20]:
            print(f"    - {name}")
        if len(unmatched) > 20:
            print(f"    ... and {len(unmatched) - 20} more")

    print(f"\nSaved enriched GeoJSON to:")
    print(f"  {output_path}")


if __name__ == "__main__":
    main()

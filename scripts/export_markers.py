#!/usr/bin/env python3
"""
Dump selected entity types from an .entities file as GeoJSON points.

Usage:
    # whitelist mode: only export the listed types
    python entities_geojson.py overworld.entities out.geojson Inanimate Fence Monster

    # blacklist mode: export everything except the listed types
    python entities_geojson.py overworld.entities out.geojson --exclude Inanimate

    # mix: start from a whitelist, then remove some
    python entities_geojson.py overworld.entities out.geojson Monster Fence --exclude Fence
"""

import argparse
import json
import struct
import sys

POSITION_MEMBERS = {0: "x", 1: "y", 2: "z"}
FLOAT_MEMBERS = {0, 1, 2, 6, 7, 8, 9, 0x16, 0x17, 0x18, 0x21, 0x22, 0x23}
STRING_MEMBERS_BASE = {0x1F}
# Human-readable "second name" members per entity type.  0x1000 is the slot
# used by Level_Entry / Level / Area_Pin / Checkpoint / Spawn_Point.
SECOND_NAME_MEMBERS = {
    "Level_Entry": {0x1000},
    "Level":       {0x1000},
    "Area_Pin":    {0x1000},
    "Checkpoint":  {0x1000},
    "Spawn_Point": {0x1000},
}
STRING_MEMBERS_BY_TYPE = {
    "Level_Entry": {0x1000},
    "Guy": {0x100E, 0x1011},
    "Settings": {0x1054},
    "Level": {0x1000},
    "Area_Pin": {0x1000},
    "Checkpoint": {0x1000},
    "Spawn_Point": {0x1000},
}


def read_u16(d, o): return struct.unpack_from("<H", d, o)[0]
def read_u32(d, o): return struct.unpack_from("<I", d, o)[0]
def read_u64(d, o): return struct.unpack_from("<Q", d, o)[0]
def read_f32(d, o): return struct.unpack_from("<f", d, o)[0]


def is_string_member(t, m):
    return m in STRING_MEMBERS_BASE or m in STRING_MEMBERS_BY_TYPE.get(t, ())


def looks_like_string(diff, cur):
    """Heuristic: after an 8-byte length prefix, does the payload look like UTF-8 text?"""
    if cur + 16 > len(diff):
        return False
    sl = read_u64(diff, cur + 8)
    if sl == 0 or sl > 4096:
        return False
    end = cur + 16 + sl
    if end > len(diff):
        return False
    payload = diff[cur + 16:end]
    try:
        s = payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(c.isprintable() or c in "\t\n\r" for c in s)


def skip_unknown(diff, start):
    p = start
    while True:
        p = diff.find(b"\xfd", p + 1)
        if p < 0:
            return len(diff)
        if p + 3 <= len(diff) and read_u16(diff, p + 1) < 0x1100:
            return p


def decode_diff(type_name, diff):
    out, cur, n = [], 0, len(diff)
    while cur < n:
        if cur == 0:
            if cur + 2 > n: break
            m = read_u16(diff, 0); cur = 2
        elif diff[cur] == 0xFD:
            if cur + 3 > n: break
            m = read_u16(diff, cur + 1); cur += 3
        else:
            break

        if m in FLOAT_MEMBERS:
            if cur + 8 > n: break
            out.append({"member": m, "float": read_f32(diff, cur + 4)})
            cur += 8
        elif is_string_member(type_name, m) or looks_like_string(diff, cur):
            if cur + 16 > n: break
            sl = read_u64(diff, cur + 8)
            end = cur + 16 + sl
            if end > n: break
            out.append({"member": m, "string": diff[cur + 16:end].decode("utf-8", "replace")})
            cur = end
        else:
            cur = skip_unknown(diff, cur)
    return out


def base_name(type_name, diff):
    """The base Entity name (member 0x1F), or None."""
    for m in decode_diff(type_name, diff):
        if m.get("member") == 0x1F and "string" in m:
            return m["string"]
    return None


def second_name(type_name, diff):
    """The type-specific human-readable name.

    Prefers a member listed in SECOND_NAME_MEMBERS for this type, otherwise
    falls back to the first string member that is not the base name (0x1F).
    """
    preferred = SECOND_NAME_MEMBERS.get(type_name, set())
    strings = [m for m in decode_diff(type_name, diff) if "string" in m]
    for m in strings:
        if m["member"] in preferred and m["member"] != 0x1F:
            return m["string"]
    for m in strings:
        if m["member"] != 0x1F:
            return m["string"]
    return None


def parse_entities(data):
    if data[:4] != b"enty":
        sys.exit("not an .entities file")
    c = 4 + 2 + 8
    tc = read_u64(data, c); c += 8
    names = []
    for _ in range(tc):
        l = read_u64(data, c)
        names.append(data[c + 8:c + 8 + l].decode("utf-8"))
        c += 8 + l + 8
    rc = read_u32(data, c); c += 4
    recs = []
    for _ in range(rc):
        eid, ti, sz = struct.unpack_from("<IHI", data, c); c += 10
        recs.append((eid, names[ti], data[c:c + sz])); c += sz
    return recs


def main():
    p = argparse.ArgumentParser(
        description="Dump selected entity types from an .entities file as GeoJSON points.",
        usage="%(prog)s <file.entities> <out.geojson> [Type ...] [-x Type ...]",
    )
    p.add_argument("src", help="input .entities file")
    p.add_argument("dst", help="output .geojson file")
    p.add_argument(
        "types", nargs="*", metavar="Type",
        help="entity types to include (whitelist). If none given, all types are considered "
             "unless --exclude is used.",
    )
    p.add_argument(
        "-x", "--exclude", action="append", default=[], metavar="Type",
        help="entity type to exclude (repeatable). Applied after the whitelist.",
    )
    args = p.parse_args()

    include = set(args.types)
    exclude = set(args.exclude)

    data = open(args.src, "rb").read()
    features = []
    for eid, tname, diff in parse_entities(data):
        if include and tname not in include:
            continue
        if tname in exclude:
            continue

        pos = {"x": 0.0, "y": 0.0, "z": 0.0}
        for m in decode_diff(tname, diff):
            num = m.get("member")
            if num in POSITION_MEMBERS:
                pos[POSITION_MEMBERS[num]] = m["float"]

        bn = base_name(tname, diff)
        sn = second_name(tname, diff)
        name = sn or bn

        props = {
            "type": tname,
            "id": eid,
        }
        if name is not None:
            props["name"] = name
        if bn is not None and bn != name:
            props["base"] = bn

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [pos["x"], pos["y"], pos["z"]],
            },
            "properties": props,
        })

    with open(args.dst, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f, indent=2, ensure_ascii=False)
    print(f"{len(features)} features -> {args.dst}")


if __name__ == "__main__":
    main()

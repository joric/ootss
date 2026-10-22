#!/usr/bin/env python3
"""Tools for Simple_Package (.package) files and their .entities data.

The field layout comes from the game exe: the Jai Type_Info embedded in the PE32+ file is
read directly (no IDA) and cached as a JSON "schema" (python simp_tool.py schema GAME.exe).
Entities are decoded with it, and it can also be written out as a C header.

Diff format: a list of changed fields. Each one is a u16 header (low 12 bits = field index,
0x1000 = field of the entity type rather than the base Entity), the old value, the new value
and a 0xFD terminator. The field index counts the leaf fields (nested structs flattened)
that exist in the type version stored in the .entities file; a field's first version is
read from its member notes.

Type_Info layout used (64-bit Jai):
  all      +0 u32 tag, +8 s64 runtime_size
  integer  +16 u8 signed          float   (size only)        bool  (size only)
  pointer  +16 pointee            variant +32 variant_of
  array    +16 element, +24 u16 kind (0 fixed, 1 view, 2 resizable), +32 count
  struct   +16 name(string), +64 members(count), +72 members(ptr), +136 constant storage
  member   (64 bytes) +0 name, +16 type, +24 offset, +32 u32 flags (1 constant, 0x10 using),
           +40 notes(count), +48 notes(ptr), +56 offset into constant storage
  enum     +16 name, +32 internal type, +40 names(count), +48 names(ptr), +64 values(ptr),
           +72 u32 flags (4 = flags)
"""
import argparse
import json
import os
import re
import struct
import sys

# ---------------------------------------------------------------- constants

BASE_TYPE = "Entity"    # the struct every entity type starts with
TRANSPARENT_MEMBERS = {"undoable_data"}   # wrapper members that add no name part, with or without "using"

# Field names (flattened member paths) the markers command reads.
POSITION_FIELDS = {"position.x": "x", "position.y": "y", "position.z": "z"}
LEVEL_NAME_FIELD = "level_name"

# Mixed level sets: never used as "area" if a regular set lists the level.
GENERIC_LEVEL_SETS = {"overworld", "intro", "intro_hard_puzzles", "design_illustrations" }

EPILOG = """\
examples:
  List everything inside the package:
    python simp_tool.py list levels.package

  Read the field layout out of the game exe once and cache it as JSON
  (a schema.json in the current folder is picked up automatically afterwards):
    python simp_tool.py schema sinking_star.exe -o schema.json

  Unpack all entries (keeps the folders from the entry names):
    python simp_tool.py extract levels.package --output-dir unpacked

  Unpack only some entries, and also write decoded .json and .txt next to each .entities:
    python simp_tool.py extract levels.package --name mirror_1.entities heroes1.level_set --decode-entities --text-entities

  List the entities of one level:
    python simp_tool.py entities levels.package --entry mirror_1.entities --list

  Dump all Mirror entities of one level (.bin + .json per entity):
    python simp_tool.py entities levels.package --entry mirror_1.entities --type Mirror

  Dump the overworld Level_Entry that points at heroes1_1, also as a text file:
    python simp_tool.py entities levels.package --entry overworld.entities --contains heroes1_1 --text

  Export the level entries of the overworld as GeoJSON markers:
    python simp_tool.py markers levels.package --entities overworld.entities --output level_entries.geojson

  Export only Level_Entry and Mirror entities (whitelist):
    python simp_tool.py markers levels.package --entities overworld.entities Level_Entry Mirror

  Export everything except Inanimate and Fence (blacklist):
    python simp_tool.py markers levels.package --entities overworld.entities --exclude Inanimate Fence

  Whitelist then remove some:
    python simp_tool.py markers levels.package --entities overworld.entities Monster Fence --exclude Fence

  Export with all entity properties nested under properties.entity_properties:
    python simp_tool.py markers levels.package --entities overworld.entities --entity-properties

  Collect markers from several sources (repeat the option; entry name or file path):
    python simp_tool.py markers levels.package --entities overworld.entities --entities D:\\maps\\custom.entities

  Print the field tables built from the schema (name|code|first_version per field):
    python simp_tool.py fields schema.json --type Level_Entry --notes

  Dump C headers (from the cached JSON or straight from the exe):
    python simp_tool.py headers schema.json -n "^(Guy|Mirror|Level_Entry)$" -o entities.h
    python simp_tool.py headers sinking_star.exe --list

Run 'python simp_tool.py COMMAND -h' for the options of one command.
"""


# ------------------------------------------------------------ binary helpers

def read_u16(data, offset):
    return struct.unpack_from("<H", data, offset)[0]


def read_u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def read_u64(data, offset):
    return struct.unpack_from("<Q", data, offset)[0]


def read_f32(data, offset):
    return struct.unpack_from("<f", data, offset)[0]


# ------------------------------------------------------------------ package

def read_package(data):
    """Return {entry_name: bytes} for a Simple_Package v1 file."""
    if data[:4] != b"simp":
        sys.exit("Not a Simple_Package file (missing 'simp' header).")

    toc_offset = read_u64(data, 16)
    entry_count = read_u64(data, toc_offset + 8)
    cursor = toc_offset + 64

    entries = {}
    for _ in range(entry_count):
        name_length = read_u32(data, cursor)
        cursor += 4
        name = data[cursor:cursor + name_length].decode("utf-8")
        cursor += name_length + 1  # skip the NUL terminator

        size = read_u64(data, cursor)
        offset = read_u64(data, cursor + 8)
        cursor += 16

        entries[name] = data[offset:offset + size]
    return entries


def normalize(name):
    return name.replace("\\", "/")


def file_name_of(entry_name):
    return normalize(entry_name).rsplit("/", 1)[-1]


def entry_matches(entry_name, wanted):
    """True if 'wanted' is the entry's full path or just its file name (case-insensitive)."""
    entry = normalize(entry_name).lower()
    wanted = normalize(wanted).lower()
    return entry == wanted or file_name_of(entry) == file_name_of(wanted)


def find_entry(entries, wanted):
    for name in entries:
        if entry_matches(name, wanted):
            return name
    return None


def load_source(path):
    """Return {entry_name: bytes} for a .package, or a single-entry dict for a plain file."""
    if not os.path.isfile(path):
        sys.exit(f"File not found: {path}")
    data = open(path, "rb").read()
    if data[:4] == b"simp":
        return read_package(data)
    return {os.path.basename(path): data}


def load_entities_source(entries, wanted):
    """Return the bytes of an .entities source: a file on disk, or an entry in the package."""
    if os.path.isfile(wanted):
        return open(wanted, "rb").read()
    name = find_entry(entries, wanted)
    if name is None:
        sys.exit(f"Entities source not found (not a file, not in the package): {wanted}")
    return entries[name]


# ------------------------------------------------- schema -> field tables
# The decoder works on tables of "name|code|first_version" fields per type. Type codes:
#   f32 f64 u8 s8 u16 s16 u32 s32 u64 s64 bool | s = string or reference
#   E:<enum> = enum | F<n> = n x float32 | X<n> = n raw bytes | A:<code> = array

SCHEMA = None       # schema dict (see extract_schema)
VERSION_RE = re.compile(r"\d+")
_TABLES = {}        # "base" / type name -> (version or None, [(name, code, first_version, notes)])


def member_fields(members, path=(), skip=None, version=-1, notes=""):
    """Leaf fields of a member list (nested structs flattened into a.b.c names), in order."""
    out, end = [], 0
    for m in members:
        t = m["type"]
        # skip constants, zero-sized members and members overlapping an earlier one (unions, Vector3.xy ...)
        if m is skip or m["flags"] & 1 or not t or not t["size"] or m["off"] < end:
            continue
        end = m["off"] + t["size"]
        own = " ".join(m["notes"])
        found = VERSION_RE.search(own)
        first = int(found.group(found.lastindex or 0)) if found else version
        # a 'using' member adds no name part (using undoable_data: ... -> position.x, not undoable_data.position.x)
        out += type_fields(t, path if m["flags"] & 0x10 or m["name"] in TRANSPARENT_MEMBERS else path + (m["name"],), first, own or notes)
    return out


def type_fields(t, path, version, notes):
    if t["k"] == "struct" and t["name"] in SCHEMA["structs"]:
        return member_fields(SCHEMA["structs"][t["name"]]["members"], path, None, version, notes)
    return [(".".join(path), type_code(t), version, notes)]


def type_code(t):
    k, size = t["k"], t["size"]
    if k == "int":
        code = ("s" if t["signed"] else "u") + str(size * 8)
        return code if code in NUMERIC_FORMATS else f"X{size}"
    if k == "float":
        return f"f{size * 8}" if size in (4, 8) else f"X{size}"
    if k == "bool":
        return "bool" if size == 1 else f"X{size}"
    if k in ("string", "ptr"):          # a pointer is assumed to be saved as a name ("string or reference")
        return "s"
    if k == "enum":
        return "E:" + t["name"] if t["name"] in _ENUMS else type_code({"k": "int", "signed": False, "size": size})
    if k == "array" and t["kind"] in (1, 2):
        return "A:" + element_code(t["of"])
    return f"X{size}"                   # fixed arrays and anything else


def element_code(t):
    """Array element: a struct of only floats is F<n>, any other struct raw bytes."""
    if t["k"] == "struct" and t["name"] in SCHEMA["structs"]:
        codes = [f[1] for f in type_fields(t, ("e",), -1, "")]
        return f"F{len(codes)}" if codes and set(codes) == {"f32"} else f"X{t['size']}"
    return type_code(t)


def build_tables(schema, version_pattern=r"\d+"):
    """Build the field tables for Entity ("base") and every type that starts with an Entity."""
    global SCHEMA, VERSION_RE
    SCHEMA, VERSION_RE = schema, re.compile(version_pattern)
    for table in (_TABLES, _SCHEMA, _ENUMS, _FIELD_CACHE):
        table.clear()
    for cname, e in schema["enums"].items():
        if e["int"] in NUMERIC_FORMATS:
            _ENUMS[cname] = {"format": NUMERIC_FORMATS[e["int"]], "flags": e["flags"],
                             "values": [tuple(v) for v in e["values"]]}
    structs = schema["structs"]
    if BASE_TYPE not in structs:
        sys.exit(f"Struct '{BASE_TYPE}' not found in the schema.")
    _TABLES["base"] = (structs[BASE_TYPE]["version"], member_fields(structs[BASE_TYPE]["members"]))
    for cname, s in sorted(structs.items()):
        base = next((m for m in s["members"] if m["off"] == 0 and m["type"] and m["type"]["k"] == "struct"
                     and m["type"]["name"] == BASE_TYPE), None)
        if base and cname != BASE_TYPE and s["name"] not in _TABLES:
            _TABLES[s["name"]] = (s["version"], member_fields(s["members"], skip=base))
    for name, (version, fields) in _TABLES.items():
        _SCHEMA[name] = {"version": 1 << 30 if version is None else version,
                         "fields": [(n, parse_code(c), v) for n, c, v, _ in fields]}


def use_schema(path, version_pattern=r"\d+"):
    """Load the schema (JSON, or the game exe) the decoder needs; default: ./schema.json."""
    if path is None and os.path.isfile("schema.json"):
        path = "schema.json"
    if path is None:
        sys.exit("No schema. Run 'python simp_tool.py schema GAME.exe -o schema.json' first, "
                 "or pass --schema GAME.exe|schema.json")
    build_tables(load_schema(path), version_pattern)


# ----------------------------------------------------------------- entities

NUMERIC_FORMATS = {"f32": "f", "f64": "d", "u8": "B", "s8": "b", "u16": "H", "s16": "h",
                   "u32": "I", "s32": "i", "u64": "Q", "s64": "q"}

_SCHEMA = {}        # "base" / type name -> {"version": int, "fields": [(name, spec, first_version)]}
_ENUMS = {}         # enum name -> {"format": struct code, "flags": bool, "values": [(name, value)]}
_FIELD_CACHE = {}


def parse_code(code):
    """Turn a schema type code into a spec tuple (see the schema tables at the end)."""
    if code in NUMERIC_FORMATS:
        return ("num", NUMERIC_FORMATS[code], code)
    if code == "bool":
        return ("bool",)
    if code == "s":
        return ("str",)
    if code.startswith("E:"):
        return ("enum", code[2:])
    if code.startswith("A:"):
        return ("array", parse_code(code[2:]))
    if code[0] == "F":
        return ("floats", int(code[1:]))
    return ("raw", int(code[1:]))      # X<n>


def describe_spec(spec):
    kind = spec[0]
    if kind == "num":
        return spec[2]
    if kind == "bool":
        return "bool"
    if kind == "str":
        return "string"
    if kind == "enum":
        return "enum " + spec[1]
    if kind == "floats":
        return f"f32[{spec[1]}]"
    if kind == "raw":
        return f"bytes[{spec[1]}]"
    return "[]" + describe_spec(spec[1])


def field_list(type_name, version):
    """Fields of a type (None = the base Entity fields) as stored at a given type version.

    A field with first_version N exists from version N on; the member number in a
    diff is the index into this filtered list (bit 0x1000 = type-specific)."""
    entry = _SCHEMA.get(type_name or "base")
    if entry is None:
        return []
    if version is None:
        version = entry["version"]
    key = (type_name, version)
    if key not in _FIELD_CACHE:
        _FIELD_CACHE[key] = [f for f in entry["fields"] if f[2] == -1 or f[2] <= version]
    return _FIELD_CACHE[key]


def enum_label(enum_name, value):
    """'NAME' or 'A|B' (flags; unknown bits as hex); None if nothing is known."""
    info = _ENUMS.get(enum_name)
    if info is None or not info["values"]:
        return None
    if not info["flags"]:
        return next((n for n, v in info["values"] if v == value), None)
    if value == 0:
        return next((n for n, v in info["values"] if v == 0), "0")
    names, rest = [], value
    for n, v in info["values"]:
        if v and rest & v == v:
            names.append(n)
            rest &= ~v
    if rest:
        names.append(hex(rest))
    return "|".join(names)


def _need(diff, cursor, size):
    if size < 0 or cursor + size > len(diff):
        raise ValueError("truncated value")


def read_value(diff, cursor, spec):
    """Decode one value at cursor; return (value, new_cursor). Raises ValueError if truncated."""
    kind = spec[0]
    if kind == "num" or kind == "enum":
        code = spec[1] if kind == "num" else _ENUMS[spec[1]]["format"]
        size = struct.calcsize(code)
        _need(diff, cursor, size)
        return struct.unpack_from("<" + code, diff, cursor)[0], cursor + size
    if kind == "bool":
        _need(diff, cursor, 1)
        return bool(diff[cursor]), cursor + 1
    if kind == "str":
        _need(diff, cursor, 8)
        size = read_u64(diff, cursor)
        _need(diff, cursor + 8, size)
        end = cursor + 8 + size
        return diff[cursor + 8:end].decode("utf-8", "replace"), end
    if kind == "floats":
        _need(diff, cursor, 4 * spec[1])
        return list(struct.unpack_from(f"<{spec[1]}f", diff, cursor)), cursor + 4 * spec[1]
    if kind == "raw":
        _need(diff, cursor, spec[1])
        return diff[cursor:cursor + spec[1]].hex(), cursor + spec[1]
    _need(diff, cursor, 8)                       # array: u64 count, then the elements
    count = read_u64(diff, cursor)
    cursor += 8
    if count > len(diff) - cursor:
        raise ValueError("bad array length")
    items = []
    for _ in range(count):
        item, cursor = read_value(diff, cursor, spec[1])
        items.append(item)
    return items, cursor


def is_default(value):
    if isinstance(value, list):
        return all(is_default(v) for v in value)
    return not value


def skip_unknown_value(diff, start):
    """Member not in the schema: its size isn't known, so guess that it ends where the
    next 0xFD <member> marker begins. This can be wrong inside float data."""
    position = start
    while True:
        position = diff.find(b"\xfd", position + 1)
        if position < 0:
            return len(diff)
        looks_like_marker = position + 3 <= len(diff) and read_u16(diff, position + 1) < 0x1100
        if looks_like_marker:
            return position


def parse_entities(data):
    """Return a list of (entity_id, type_name, diff_bytes, type_version)."""
    if data[:4] != b"enty":
        sys.exit("Not an .entities file (missing 'enty' header).")

    cursor = 4 + 2          # magic + u16 version
    cursor += 8             # header_value
    type_count = read_u64(data, cursor)
    cursor += 8

    type_names = []
    type_versions = []
    for _ in range(type_count):
        name_length = read_u64(data, cursor)
        type_names.append(data[cursor + 8:cursor + 8 + name_length].decode("utf-8"))
        type_versions.append(read_u64(data, cursor + 8 + name_length))
        cursor += 8 + name_length + 8  # length + name + type version

    record_count = read_u32(data, cursor)
    cursor += 4

    records = []
    for _ in range(record_count):
        entity_id, type_index, diff_size = struct.unpack_from("<IHI", data, cursor)
        cursor += 10
        records.append((entity_id, type_names[type_index], data[cursor:cursor + diff_size],
                        type_versions[type_index]))
        cursor += diff_size
    return records


def _append_tail(members, diff, cursor, member=None):
    """Record the remaining bytes as a raw blob so nothing is lost on truncation."""
    entry = {"raw": diff[cursor:].hex()}
    if member is not None:
        entry["member"] = member
    members.append(entry)


def decode_diff(type_name, diff, version=None):
    """Decode a record's diff into a list of member dicts.

    A diff is a list of changed fields: u16 header (low 12 bits = field index, 0x1000 =
    field of the entity type rather than the base Entity), the old value, the new value
    and a 0xFD terminator. Member dicts hold "member" (the header), "name", "type",
    "value" (new), "old" (only when not the default), "label" (enums).

    Every read is bounds-checked; if a member is truncated or doesn't fit the schema,
    the remaining bytes are dumped as a single {"raw": ...} entry instead of raising.
    """
    members = []
    cursor = 0
    length = len(diff)

    while cursor < length:
        if cursor + 2 > length:
            _append_tail(members, diff, cursor)
            break
        header = read_u16(diff, cursor)
        cursor += 2
        derived = bool(header & 0x1000)
        fields = field_list(type_name if derived else None, version if derived else None)
        index = header & 0xFFF

        if index >= len(fields):
            end = skip_unknown_value(diff, cursor)
            members.append({"member": header, "raw": diff[cursor:end].hex()})
            cursor = end
        else:
            name, spec, _ = fields[index]
            try:
                old, cursor_new = read_value(diff, cursor, spec)
                value, cursor = read_value(diff, cursor_new, spec)
            except ValueError:
                _append_tail(members, diff, cursor, header)
                break
            member = {"member": header, "name": name, "type": describe_spec(spec), "value": value}
            if not is_default(old):
                member["old"] = old
            if spec[0] == "enum":
                label = enum_label(spec[1], value)
                if label is not None:
                    member["label"] = label
            members.append(member)

        if cursor < length:
            if diff[cursor] != 0xFD:
                _append_tail(members, diff, cursor)
                break
            cursor += 1

    return members


def entity_to_dict(entity_id, type_name, diff, version=None):
    return {
        "id": entity_id,
        "type": type_name,
        "type_version": version,
        "diff_size": len(diff),
        "members": decode_diff(type_name, diff, version),
    }


def format_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.7g}"
    if isinstance(value, list):
        return "[" + ", ".join(format_value(v) for v in value) + "]"
    if isinstance(value, str):
        return value.replace("\r", "\\r").replace("\n", "\\n")
    return str(value)


def entity_to_text(entity_id, type_name, diff, version=None):
    """One header line, then one 'name = value' line per named member."""
    lines = [f"{type_name} {entity_id}"]
    unknown = 0
    for member in decode_diff(type_name, diff, version):
        if "name" not in member:
            unknown += 1
            continue
        line = f"{member['name']} = {member.get('label') or format_value(member['value'])}"
        if "old" in member:
            line += f"  # was {format_value(member['old'])}"
        lines.append(line)
    if unknown:
        lines.append(f"# {unknown} unknown members not shown")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ markers

def parse_level_set(text):
    """Return the level names listed in a .level_set file, in order."""
    levels = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()   # drop comments
        if not line or line.startswith("[") or line.startswith("*"):
            continue                           # version, glyphs, *choice, ...
        levels.append(line.split()[0])
    return levels


def read_level_areas(entries):
    """Return {level_name_lowercase: [level set names]}."""
    areas = {}
    for entry_name, data in sorted(entries.items()):
        filename = file_name_of(entry_name)
        if not filename.lower().endswith(".level_set"):
            continue
        set_name = filename[:-len(".level_set")]
        for level in parse_level_set(data.decode("utf-8-sig", "replace")):
            sets = areas.setdefault(level.lower(), [])
            if set_name not in sets:
                sets.append(set_name)
    return areas


def pick_area(set_names):
    """Prefer a regular level set over a generic mixed one."""
    if not set_names:
        return None
    return next((s for s in set_names if s not in GENERIC_LEVEL_SETS and 'drafts' not in s), "UNDEFINED")


def _json_safe(value):
    """Make a decoded value safe for JSON serialization."""
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)) or value is None:
        return value
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return str(value)


def _all_empty_strings(value):
    """True if value is a list whose elements are all empty strings (recursively for nested lists)."""
    if not isinstance(value, list) or not value:
        return False
    for v in value:
        if isinstance(v, list):
            if not _all_empty_strings(v):
                return False
        elif v != "":
            return False
    return True


def _nest_dotted(flat):
    """Turn {'position.x': 1, 'position.y': 2, 'a.b.c': 3} into nested dicts.

    Order is preserved: the first time a prefix appears, a dict is created; later
    dotted keys extend it. A collision between a scalar and a dict at the same key
    keeps the scalar (drops the later dict).
    """
    root = {}
    for key, value in flat.items():
        parts = key.split(".")
        node = root
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                # overwrite non-dict (rare); keeps later nested fields working
                nxt = {}
                node[part] = nxt
            node = nxt
        leaf = parts[-1]
        if leaf not in node or not isinstance(node[leaf], dict):
            node[leaf] = value
    return root


def read_level_entries(entries, sources, include_types=None, exclude_types=None,
                       entity_properties=False):
    """Return a list of dicts with decoded properties from every given .entities source.

    include_types: if non-empty, only entities whose type is in this set are kept.
    exclude_types: entities whose type is in this set are dropped (applied after include).
    entity_properties: if True, decode every named member into a "properties" dict
        (with arrays of all-empty-strings dropped, and dotted names nested).
    """
    include_types = {t.lower() for t in include_types} if include_types else None
    exclude_types = {t.lower() for t in exclude_types} if exclude_types else None

    result = []
    for source in sources:
        data = load_entities_source(entries, source)
        for entity_id, type_name, diff, version in parse_entities(data):
            tname = type_name.lower()
            if include_types and tname not in include_types:
                continue
            if exclude_types and tname in exclude_types:
                continue

            # Diffs only store non-default values, so a missing coordinate is 0.
            position = {"x": 0.0, "y": 0.0, "z": 0.0}
            level_name = None
            properties = {}

            for member in decode_diff(type_name, diff, version):
                name = member.get("name")
                if name is None:
                    continue

                # Track position and level_name for geometry / area lookup
                if name in POSITION_FIELDS:
                    position[POSITION_FIELDS[name]] = member["value"]
                elif name == LEVEL_NAME_FIELD:
                    level_name = member["value"]

                if entity_properties:
                    value = member.get("label") or member["value"]
                    value = _json_safe(value)
                    # Drop arrays whose elements are all empty strings (e.g. materials: ["", "", ""])
                    if _all_empty_strings(value):
                        continue
                    properties[name] = value

            entry = {"id": entity_id, "type": type_name, "level": level_name, **position}
            if entity_properties:
                entry["properties"] = properties
            result.append(entry)
    return result


def build_geojson(level_entries, areas, entity_properties=False):
    features = []
    for entry in sorted(level_entries, key=lambda e: (e["type"], e["level"] or "")):
        set_names = areas.get((entry["level"] or "").lower(), [])

        # id is always present
        props = {
            "type": entry["type"],
            "id": entry["id"],
        }

        # name only when the entity actually has one
        if entry["level"]:
            props["name"] = entry["level"]

        # area only when known
        area = pick_area(set_names)
        if area is not None:
            props["area"] = area

        # All entity fields go under entity_properties (nested by dotted name)
        if entity_properties:
            props["entity_properties"] = _nest_dotted(entry.get("properties", {}))

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [entry["x"], entry["y"], entry["z"]],
            },
            "properties": props,
        })
    return {"type": "FeatureCollection", "features": features}


# --------------------------------------------------- PE image / Type_Info reader

TAG_INT, TAG_FLOAT, TAG_BOOL, TAG_STRING, TAG_POINTER, TAG_PROC, TAG_VOID = 0, 1, 2, 3, 4, 5, 6
TAG_STRUCT, TAG_ARRAY, TAG_ANY, TAG_ENUM, TAG_TYPE, TAG_VARIANT = 7, 8, 10, 11, 13, 18


class Unmapped(Exception):
    pass


class Image:
    def __init__(self, data):
        self.data = data
        if data[:2] != b"MZ":
            sys.exit("Not a PE file.")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            sys.exit("Not a PE file (no PE signature).")
        count, = struct.unpack_from("<H", data, pe + 6)
        opt_size, = struct.unpack_from("<H", data, pe + 20)
        opt = pe + 24
        if struct.unpack_from("<H", data, opt)[0] != 0x20B:
            sys.exit("Only 64-bit (PE32+) executables are supported.")
        self.base = struct.unpack_from("<Q", data, opt + 24)[0]
        self.sections = []
        table = opt + opt_size
        for i in range(count):
            at = table + 40 * i
            name = data[at:at + 8].rstrip(b"\0").decode("latin-1")
            vsize, rva, raw_size, raw_ptr = struct.unpack_from("<IIII", data, at + 8)
            self.sections.append((name, self.base + rva, min(vsize, raw_size) if vsize else raw_size, raw_ptr))

    def section(self, name):
        return next((s for s in self.sections if s[0] == name), None)

    def off(self, va, size=1):
        for _, start, length, raw in self.sections:
            if start <= va and va + size <= start + length:
                return raw + va - start
        raise Unmapped(hex(va))

    def mapped(self, va, size=1):
        try:
            self.off(va, size)
            return True
        except Unmapped:
            return False

    def q(self, va):
        return struct.unpack_from("<q", self.data, self.off(va, 8))[0]

    def u64(self, va):
        return struct.unpack_from("<Q", self.data, self.off(va, 8))[0]

    def u32(self, va):
        return struct.unpack_from("<I", self.data, self.off(va, 4))[0]

    def u16(self, va):
        return struct.unpack_from("<H", self.data, self.off(va, 2))[0]

    def u8(self, va):
        return self.data[self.off(va, 1)]

    def string(self, va, limit=4096):
        """Jai string at va: s64 count, pointer to data."""
        n, p = self.q(va), self.u64(va + 8)
        if n <= 0 or n > limit or not p:
            return ""
        o = self.off(p, n)
        return self.data[o:o + n].decode("utf-8", "replace")


class Types:
    def __init__(self, img, sections):
        self.img = img
        self.scan_sections = sections
        self.structs = {}       # ea -> dict
        self.enums = {}         # ea -> dict
        self.cname = {}         # ea -> C name
        self.queue = []         # ("struct" | "enum", ea) still to be written into the schema

    # -- scanning
    def scan(self, tag):
        found = {}
        img = self.img
        pattern = re.compile(struct.pack("<I", tag), re.S)
        for sec in self.scan_sections:
            _, lo, length, raw = sec
            blob = img.data[raw:raw + length]
            for m in pattern.finditer(blob):
                off = m.start()
                if off % 8 or off + 96 > len(blob):
                    continue
                size, n, p = struct.unpack_from("<qqQ", blob, off + 8)
                if not (0 < n < 64 and 0 <= size < 1 << 24 and img.mapped(p, n)):
                    continue
                try:
                    name = img.data[img.off(p, n):img.off(p, n) + n].decode("ascii")
                except UnicodeDecodeError:
                    continue
                if not re.fullmatch(r"[A-Za-z_]\w*", name):
                    continue
                ea = lo + off
                if tag == TAG_STRUCT:
                    cnt, ptr = struct.unpack_from("<qQ", blob, off + 64)
                    if not (0 <= cnt < 20000 and (cnt == 0 or img.mapped(ptr, cnt * 64))):
                        continue
                found.setdefault(name, []).append(ea)
        return found

    # -- primitives
    def resolve(self, t):
        for _ in range(32):
            if self.img.u32(t) != TAG_VARIANT:
                break
            t = self.img.q(t + 32)
        return t

    def tag(self, t):
        return self.img.u32(t)

    def size(self, t):
        return self.img.q(t + 8)

    def int_name(self, t):
        t = self.resolve(t)
        return ("s" if self.img.u8(t + 16) else "u") + str(self.size(t) * 8)

    # -- structs / enums
    def get_struct(self, ea):
        if ea in self.structs:
            return self.structs[ea]
        img = self.img
        n, p = img.q(ea + 64), img.q(ea + 72)
        members = []
        for i in range(n):
            m = p + i * 64
            cnt = img.q(m + 40)
            members.append(dict(
                name=img.string(m), type=img.q(m + 16), off=img.q(m + 24), flags=img.u32(m + 32),
                notes=[img.string(img.q(m + 48) + j * 16) for j in range(cnt)] if 0 < cnt < 64 else [],
                cs_off=img.q(m + 56)))
        s = dict(ea=ea, name=img.string(ea + 16), size=self.size(ea), members=members, version=None)
        try:
            storage = img.q(ea + 136)
            for m in members:
                if m["name"] == "VERSION" and m["flags"] & 1 and storage:
                    s["version"] = img.u64(storage + m["cs_off"])
        except Unmapped:
            pass
        self.structs[ea] = s
        return s

    def get_enum(self, ea):
        if ea in self.enums:
            return self.enums[ea]
        img = self.img
        n, names, values = img.q(ea + 40), img.q(ea + 48), img.q(ea + 64)
        name = img.string(ea + 16)
        flags = img.u32(ea + 72)
        e = dict(ea=ea, name=name, int_type=self.int_name(img.q(ea + 32)),
                 flags=bool(flags & 4) or name.endswith("_Flags"),
                 values=[(img.string(names + i * 16), img.q(values + i * 8)) for i in range(n)])
        self.enums[ea] = e
        return e

    # -- C names
    @staticmethod
    def sanitize(name, ea):
        name = re.sub(r"\W", "_", name or "")
        if not name or name[0].isdigit():
            name = "anon_" + name
        if name == "anon_" or name in C_KEYWORDS:
            name = f"{name}_{ea:x}" if name == "anon_" else name + "_"
        return name

    def assign_names(self, kind, groups):
        """Same-named types with identical layout share a name; different ones get __2, __3 ..."""
        for name, eas in groups.items():
            variants = {}
            for ea in eas:
                obj = self.get_struct(ea) if kind == "struct" else self.get_enum(ea)
                variants.setdefault(self.signature(obj, kind), []).append(ea)

            def rank(item):
                objs = [self.structs[e] if kind == "struct" else self.enums[e] for e in item[1]]
                return (-any(o.get("version") is not None for o in objs), -len(objs[0].get("members", objs[0].get("values"))), item[1][0])
            for i, (_, group) in enumerate(sorted(variants.items(), key=rank)):
                cname = self.sanitize(name, group[0]) + ("" if i == 0 else f"__{i + 1}")
                for ea in group:
                    self.cname[ea] = cname

    def signature(self, obj, kind):
        if kind == "enum":
            return (obj["int_type"], tuple(obj["values"]))
        sig = [obj["size"]]
        for m in obj["members"]:
            try:
                t = self.resolve(m["type"])
                sig.append((m["name"], m["off"], m["flags"], self.tag(t), self.size(t)))
            except Unmapped:
                sig.append((m["name"], m["off"], m["flags"]))
        return tuple(sig)

    def struct_cname(self, ea):
        if ea not in self.cname:
            s = self.get_struct(ea)
            self.cname[ea] = self.sanitize(s["name"], ea) if s["name"] else f"anon_{ea:x}"
        return self.cname[ea]

    def enum_cname(self, ea):
        return self.cname.get(ea) or self.sanitize(self.get_enum(ea)["name"], ea)

    # -- serialising types into plain dicts
    def ser(self, t):
        """Type_Info -> self-contained dict. Structs and enums are references by C name
        (and queued so that their definitions end up in the schema too)."""
        t = self.resolve(t)
        img, tag = self.img, self.tag(t)
        d = {"k": "unknown", "tag": tag, "size": self.size(t)}
        if tag == TAG_INT:
            d.update(k="int", signed=bool(img.u8(t + 16)))
        elif tag in (TAG_FLOAT, TAG_BOOL, TAG_STRING, TAG_ANY, TAG_PROC, TAG_TYPE):
            d["k"] = {TAG_FLOAT: "float", TAG_BOOL: "bool", TAG_STRING: "string", TAG_ANY: "any",
                      TAG_PROC: "proc", TAG_TYPE: "type"}[tag]
        elif tag == TAG_ENUM:
            d.update(k="enum", name=self.enum_cname(t), jai=img.string(t + 16), int=self.int_name(img.q(t + 32)))
            self.queue.append(("enum", t))
        elif tag == TAG_STRUCT:
            d.update(k="struct", name=self.struct_cname(t), jai=img.string(t + 16))
            self.queue.append(("struct", t))
        elif tag == TAG_POINTER:
            target = img.q(t + 16)
            d.update(k="ptr", to=self.ser(target) if target else None)
        elif tag == TAG_ARRAY:
            d.update(k="array", kind=img.u16(t + 24), count=img.q(t + 32), of=self.ser(img.q(t + 16)))
        return d


C_KEYWORDS = set("""auto break case char const continue default do double else enum extern float for goto if
inline int long register restrict return short signed sizeof static struct switch typedef union unsigned void
volatile while class new delete this template typename namespace private public protected operator bool true false
and or not xor""".split())


# ------------------------------------------------------------ schema (JSON cache)
#
# {"format": 1, "source": "game.exe",
#  "structs": {cname: {"name", "size", "version", "members": [{"name", "type", "off", "flags", "notes"}]}},
#  "enums":   {cname: {"name", "int", "flags", "values": [[name, value], ...]}}}
# A type is {"k": int|float|bool|string|any|proc|type|enum|struct|ptr|array|unknown, "size", "tag", ...}.

def extract_schema(exe, section_names=None):
    img = Image(open(exe, "rb").read())
    wanted = section_names or [".rdata"]
    secs = [s for s in map(img.section, wanted) if s]
    if not secs:
        sys.exit("Section not found: " + ", ".join(wanted) + "  (have: " + ", ".join(s[0] for s in img.sections) + ")")

    types = Types(img, secs)
    struct_groups, enum_groups = types.scan(TAG_STRUCT), types.scan(TAG_ENUM)
    types.assign_names("struct", struct_groups)
    types.assign_names("enum", enum_groups)

    schema = {"format": 1, "source": os.path.basename(exe), "structs": {}, "enums": {}}
    types.queue = ([("struct", ea) for eas in struct_groups.values() for ea in eas]
                   + [("enum", ea) for eas in enum_groups.values() for ea in eas])
    while types.queue:
        kind, ea = types.queue.pop()
        try:
            if kind == "struct":
                cname = types.struct_cname(ea)
                if cname in schema["structs"]:
                    continue
                s, members = types.get_struct(ea), []
                for m in s["members"]:
                    try:
                        ty = types.ser(m["type"])
                    except Unmapped:
                        ty = None
                    members.append({"name": m["name"], "type": ty, "off": m["off"], "flags": m["flags"], "notes": m["notes"]})
                schema["structs"][cname] = {"name": s["name"], "size": s["size"], "version": s["version"], "members": members}
            else:
                cname = types.enum_cname(ea)
                if cname not in schema["enums"]:
                    e = types.get_enum(ea)
                    schema["enums"][cname] = {"name": e["name"], "int": e["int_type"], "flags": e["flags"],
                                              "values": [list(v) for v in e["values"]]}
        except Unmapped:
            pass
    return schema


def save_schema(schema, path):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(schema, f, indent=1, ensure_ascii=False)
        f.write("\n")


def load_schema(path):
    """Schema from a .json file, or straight from a PE exe (cached next to it as <exe>.schema.json)."""
    if not os.path.isfile(path):
        sys.exit(f"File not found: {path}")
    if open(path, "rb").read(2) != b"MZ":
        schema = json.load(open(path, encoding="utf-8"))
        if schema.get("format") != 1:
            sys.exit(f"Unsupported schema format in {path}")
        return schema
    cache = path + ".schema.json"
    if os.path.isfile(cache) and os.path.getmtime(cache) >= os.path.getmtime(path):
        return load_schema(cache)
    schema = extract_schema(path)
    try:
        save_schema(schema, cache)
        sys.stderr.write(f"cached schema -> {cache}\n")
    except OSError:
        pass
    return schema


# ------------------------------------------------------------- C header output

PRELUDE = """\
typedef signed char s8;
typedef unsigned char u8;
typedef short s16;
typedef unsigned short u16;
typedef int s32;
typedef unsigned int u32;
typedef long long s64;
typedef unsigned long long u64;
typedef float f32;
typedef double f64;
typedef u8 jbool;
typedef struct { s64 count; u8 *data; } Jai_string;
typedef struct { s64 count; void *data; } Jai_Array_View;      /* []T */
typedef struct { s64 count; void *data; s64 allocated; void *allocator_proc; void *allocator_data; } Jai_Array;  /* [..]T */
typedef struct { void *type; void *value; } Jai_Any;
"""


def c_type(t, refs, by_value=True):
    """C type name (no declarator) for a schema type, or None if it needs a byte array."""
    k, size = t["k"], t["size"]
    if k == "int" and size in (1, 2, 4, 8):
        return ("s" if t["signed"] else "u") + str(size * 8)
    if k == "float" and size in (4, 8):
        return f"f{size * 8}"
    if k == "bool" and size == 1:
        return "jbool"
    if k == "string":
        return "Jai_string"
    if k == "any":
        return "Jai_Any"
    if k == "enum":
        refs["enums"].add(t["name"])
        return t["int"]
    if k == "struct":
        if not by_value:
            refs["structs"].add(t["name"])
        return t["name"]
    if k == "ptr":
        inner = c_type(t["to"], refs, False) if t["to"] else None
        return (inner or "void") + " *"
    if k in ("proc", "type") and size == 8:
        return "void *"
    if k == "array" and t["kind"] in (1, 2):
        return ("Jai_Array_View", "Jai_Array")[t["kind"] - 1]
    return None


def jai_name(t):
    """Jai-style type name for comments."""
    k = t["k"]
    if k == "int":
        return ("s" if t["signed"] else "u") + str(t["size"] * 8)
    if k == "float":
        return f"float{t['size'] * 8}"
    if k in ("bool", "string"):
        return k
    if k == "ptr":
        return "*" + (jai_name(t["to"]) if t["to"] else "void")
    if k in ("struct", "enum"):
        return t["jai"] or f"tag{t['tag']}"
    if k == "array":
        return {0: f"[{t['count']}]", 1: "[]", 2: "[..]"}.get(t["kind"], "[?]") + jai_name(t["of"])
    return f"tag{t['tag']}"


def c_decl(t, name, refs):
    """(declaration, by-value struct dependency or None). Falls back to a byte array for unknown types."""
    total, dims = t["size"], []
    while t["k"] == "array" and t["kind"] == 0:
        esz = t["of"]["size"]
        if esz <= 0 or t["size"] % esz:
            break
        dims.append(t["size"] // esz)
        t = t["of"]
    base = c_type(t, refs)
    if base is None:
        return f"u8 {name}[{max(total, 1)}]", None
    sep = "" if base.endswith("*") else " "
    return f"{base}{sep}{name}" + "".join(f"[{d}]" for d in dims), (t["name"] if t["k"] == "struct" else None)


def c_ident(name, used):
    name = re.sub(r"\W", "_", name) or "_"
    if name[0].isdigit():
        name = "_" + name
    elif name in C_KEYWORDS:
        name += "_"
    base, i = name, 2
    while name in used:
        name = f"{base}_{i}"
        i += 1
    used.add(name)
    return name


def emit_struct(cname, s, refs):
    """Return (lines, by_value_dependency_names)."""
    lines, deps = [], []
    lines.append(f"/* size 0x{s['size']:X}" + (f", VERSION {s['version']}" if s["version"] is not None else "") + " */")
    lines.append(f"struct {cname} {{")
    cur, used = 0, set()
    for m in sorted((m for m in s["members"] if not m["flags"] & 1), key=lambda m: m["off"]):
        t = m["type"]
        if t is None:
            lines.append(f"    /* {m['name']}: unreadable type at +0x{m['off']:X} */")
            continue
        size, jname, note = t["size"], jai_name(t), " ".join(m["notes"])
        if size == 0 or m["off"] < cur:
            why = "zero-sized" if size == 0 else "overlaps previous"
            lines.append(f"    /* +0x{m['off']:X} {m['name']}: {jname} ({why}) {note} */")
            continue
        if m["off"] > cur:
            lines.append(f"    u8 _pad_{cur:X}[{m['off'] - cur}];")
        decl, dep = c_decl(t, c_ident(m["name"], used), refs)
        if dep:
            deps.append(dep)
        extra = " ".join(x for x in ("using" if m["flags"] & 0x10 else "", note) if x)
        lines.append(f"    {decl};  /* +0x{m['off']:X} {jname}{' ' + extra if extra else ''} */")
        cur = m["off"] + size
    if s["size"] > cur:
        lines.append(f"    u8 _pad_{cur:X}[{s['size'] - cur}];")
    lines.append("};")
    return lines, deps


def emit_enum(cname, e):
    lines = [f"/* enum {e['name'] or '?'} : {e['int']}{' (flags)' if e['flags'] else ''} */",
             f"enum {cname} {{"]
    used = set()
    for n, v in e["values"]:
        ident = c_ident(f"{cname}_{n}", used)
        val = f"0x{v & 0xFFFFFFFFFFFFFFFF:X}ULL" if e["flags"] and v > 0x7FFFFFFF else (f"{v}LL" if abs(v) > 0x7FFFFFFF else str(v))
        lines.append(f"    {ident} = {val},")
    lines.append("};")
    return lines


def make_header(schema, patterns=None, no_enums=False, no_prelude=False):
    """Return (header text, struct count, enum count). By-value dependencies are always included."""
    structs, enums = schema["structs"], schema["enums"]
    match = lambda name: not patterns or any(re.search(p, name) for p in patterns)
    refs = {"structs": set(), "enums": set()}
    out, done = [], set()

    def visit(cname, stack=()):     # dependency order: by-value members first
        if cname in done or cname in stack or cname not in structs:
            return
        lines, deps = emit_struct(cname, structs[cname], refs)
        for dep in deps:
            visit(dep, stack + (cname,))
        if cname not in done:
            done.add(cname)
            out.append((cname, lines))

    for cname in sorted(structs, key=lambda c: (structs[c]["name"], c)):
        if structs[cname]["name"] and match(structs[cname]["name"]):
            visit(cname)

    enum_lines, enum_count = [], 0
    for cname in sorted(enums, key=lambda c: (enums[c]["name"], c)):
        if cname in refs["enums"] or (not no_enums and match(enums[cname]["name"])):
            enum_lines += emit_enum(cname, enums[cname]) + [""]
            enum_count += 1

    text = [f"/* Generated by simp_tool.py from the Jai Type_Info in {schema['source']} */",
            "/* Offsets are exact (#pragma pack(1) with explicit padding). Enum members use their underlying integer type. */", ""]
    if not no_prelude:
        text.append(PRELUDE)
    text += ["#pragma pack(push, 1)", ""]
    text += [f"typedef struct {c} {c};" for c in sorted({c for c, _ in out} | refs["structs"])] + [""]
    text += enum_lines
    for _, lines in out:
        text += lines + [""]
    text += ["#pragma pack(pop)", ""]
    return "\n".join(text), len(out), enum_count


# ----------------------------------------------------------------- commands

def command_list(args):
    entries = load_source(args.source)
    for name in sorted(entries):
        print(f"{len(entries[name]):>10}  {name}")
    print(f"{len(entries)} entries")


def safe_output_path(root, entry_name):
    """Join an entry name onto root, refusing names that escape it."""
    target = os.path.abspath(os.path.join(root, normalize(entry_name).lstrip("/")))
    if os.path.commonpath([root, target]) != root:
        return None
    return target


def write_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def command_extract(args):
    entries = load_source(args.source)
    root = os.path.abspath(args.output_dir)

    if args.name:
        selected = [n for n in entries if any(entry_matches(n, w) for w in args.name)]
        for wanted in args.name:
            if not any(entry_matches(n, wanted) for n in entries):
                print(f"not found in package: {wanted}")
    else:
        selected = list(entries)

    if not selected:
        sys.exit("Nothing to extract.")
    if args.decode_entities or args.text_entities:
        use_schema(args.schema, args.version_note)

    for name in sorted(selected):
        target = safe_output_path(root, name)
        if target is None:
            print(f"skipped unsafe entry name: {name}")
            continue

        write_file(target, entries[name])
        print(f"{name} ({len(entries[name])} bytes)")

        is_entities = name.lower().endswith(".entities")
        if args.decode_entities and is_entities:
            records = parse_entities(entries[name])
            decoded = [entity_to_dict(*record) for record in records]
            write_file(target + ".json",
                       json.dumps(decoded, indent=1, ensure_ascii=False).encode("utf-8"))
            print(f"  decoded {len(decoded)} entities -> {target}.json")

        if args.text_entities and is_entities:
            blocks = [entity_to_text(*record) for record in parse_entities(entries[name])]
            write_file(target + ".txt", "\n".join(blocks).encode("utf-8"))
            print(f"  wrote {len(blocks)} entities as text -> {target}.txt")


def record_matches(args, entity_id, type_name, diff, version=None):
    if args.id is not None and entity_id != args.id:
        return False
    if args.type and type_name.lower() != args.type.lower():
        return False
    if args.contains and args.contains.encode() not in diff:
        return False
    return True


def command_entities(args):
    entries = load_source(args.source)

    if len(entries) == 1:
        data = next(iter(entries.values()))
    else:
        if not args.entry:
            print("Entities files in this package (pass one with --entry):")
            for name in sorted(entries):
                if name.lower().endswith(".entities"):
                    print("  " + file_name_of(name))
            return
        name = find_entry(entries, args.entry)
        if name is None:
            sys.exit(f"Entry not found in package: {args.entry}")
        data = entries[name]

    records = parse_entities(data)
    has_filter = args.contains or args.type or args.id is not None
    selected = [r for r in records if record_matches(args, *r)]

    if args.list:
        for entity_id, type_name, diff, version in selected:
            print(entity_id, type_name, len(diff))
        return

    if not (has_filter or args.all):
        args.parser.error("choose what to dump: --contains, --type, --id or --all "
                          "(or use --list to just see the entities)")
    if not selected:
        sys.exit("No entities matched.")

    use_schema(args.schema, args.version_note)
    os.makedirs(args.output_dir, exist_ok=True)
    for entity_id, type_name, diff, version in selected:
        base = os.path.join(args.output_dir, f"{type_name}_{entity_id}")
        with open(base + ".bin", "wb") as f:
            f.write(diff)
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(entity_to_dict(entity_id, type_name, diff, version), f, indent=1, ensure_ascii=False)
        if args.text:
            with open(base + ".txt", "w", encoding="utf-8") as f:
                f.write(entity_to_text(entity_id, type_name, diff, version))
        print(f"{type_name} id={entity_id} ({len(diff)} bytes) -> {base}.json / .bin")


def command_markers(args):
    use_schema(args.schema, args.version_note)
    entries = load_source(args.source)
    sources = args.entities

    level_entries = read_level_entries(entries, sources,
                                       include_types=args.types,
                                       exclude_types=args.exclude,
                                       entity_properties=args.entity_properties)
    areas = read_level_areas(entries)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(build_geojson(level_entries, areas,
                                entity_properties=args.entity_properties),
                  f, indent=2, ensure_ascii=False)

    without_area = [e["level"] for e in level_entries
                    if e["level"] and not areas.get(e["level"].lower())]
    print(f"Wrote {len(level_entries)} level entries from {len(sources)} source(s) to {args.output}")
    if args.types:
        print(f"Included types: {', '.join(args.types)}")
    if args.exclude:
        print(f"Excluded types: {', '.join(args.exclude)}")
    if args.entity_properties:
        print("Included entity properties under 'entity_properties'")
    if without_area:
        print(f"{len(without_area)} without an area (not in any level set): "
              + ", ".join(str(n) for n in without_area))


def command_schema(args):
    schema = extract_schema(args.exe, args.section)
    save_schema(schema, args.output)
    print(f"{len(schema['structs'])} structs, {len(schema['enums'])} enums -> {args.output}")


def command_headers(args):
    schema = load_schema(args.source)
    if args.list:
        for kind in ("structs", "enums"):
            for cname, obj in sorted(schema[kind].items()):
                print(f"{kind[:-1]:6} {cname:40}" + (f" size 0x{obj['size']:X}" if kind == "structs" else ""))
        return
    text, n_structs, n_enums = make_header(schema, args.name, args.no_enums, args.no_prelude)
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"{n_structs} structs, {n_enums} enums -> {args.output}")
    else:
        sys.stdout.write(text)


def command_fields(args):
    """Print the field tables in 'name|code|first_version' form, one '@Type version' block per type."""
    build_tables(load_schema(args.source), args.version_note)
    for name in args.type or list(_TABLES):
        if name not in _TABLES:
            print(f"@{name} (not an entity type in the schema)")
            continue
        version, fields = _TABLES[name]
        print(f"@{name} {0 if version is None else version}")
        for field, code, first, notes in fields:
            print(f"{field}|{code}|{first}" + (f"  # {notes}" if args.notes and notes else ""))
    versioned = sum(1 for _, fields in _TABLES.values() for f in fields if f[2] != -1)
    sys.stderr.write(f"{len(_TABLES)} types, {versioned} fields with a version note\n")
    if not versioned:
        sys.stderr.write("warning: no field has a version note, so older .entities files may be misnumbered "
                         "(see --notes / --version-note)\n")


# --------------------------------------------------------------------- main

class HelpParser(argparse.ArgumentParser):
    """Prints the full help (options + examples) on any usage error."""

    def error(self, message):
        sys.stderr.write(f"error: {message}\n\n")
        self.print_help(sys.stderr)
        sys.exit(2)


def add_command(commands, name, help_text, example, run):
    sub = commands.add_parser(
        name,
        help=help_text,
        description=help_text,
        epilog="example:\n  " + example,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub.set_defaults(run=run, parser=sub)
    return sub


def add_schema_options(sub):
    sub.add_argument("--schema", metavar="FILE",
                     help="schema JSON or game exe (default: schema.json in the current folder)")
    sub.add_argument("--version-note", metavar="REGEX", default=r"\d+",
                     help="regex that finds a field's first version in its member notes "
                          "(default: the first number)")


def build_parser():
    parser = HelpParser(
        description="Tools for Simple_Package (.package) files and their .entities data.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=HelpParser)

    sub = add_command(commands, "list", "list the entries of a package",
                      "python simp_tool.py list levels.package", command_list)
    sub.add_argument("source", metavar="PACKAGE", help="path to a .package file")

    sub = add_command(commands, "extract", "unpack package entries to files",
                      "python simp_tool.py extract levels.package --output-dir unpacked",
                      command_extract)
    sub.add_argument("source", metavar="PACKAGE", help="path to a .package file")
    sub.add_argument("--name", metavar="NAME", nargs="+",
                     help="only these entries, by file name or full path "
                          "(e.g. mirror_1.entities heroes1.level_set); default: all")
    sub.add_argument("--output-dir", metavar="DIR", default="unpacked",
                     help="folder to write into (default: %(default)s)")
    sub.add_argument("--decode-entities", action="store_true",
                     help="also write <file>.entities.json with all decoded entities")
    sub.add_argument("--text-entities", action="store_true",
                     help="also write <file>.entities.txt: every entity as a block of "
                          "'name = value' lines (named members only)")
    add_schema_options(sub)

    sub = add_command(commands, "entities", "list or dump entity records of one level",
                      "python simp_tool.py entities levels.package "
                      "--entry overworld.entities --contains heroes1_1",
                      command_entities)
    sub.add_argument("source", metavar="PATH",
                     help="a .package file, or an extracted .entities file")
    sub.add_argument("--entry", metavar="NAME",
                     help="which .entities file inside the package, e.g. mirror_1.entities "
                          "(omit it to list the available ones)")
    sub.add_argument("--contains", metavar="TEXT",
                     help="only entities whose data contains this text")
    sub.add_argument("--type", metavar="TYPE",
                     help="only entities of this type, e.g. Mirror, Guy, Level_Entry")
    sub.add_argument("--id", type=int, metavar="NUMBER", help="only the entity with this id")
    sub.add_argument("--all", action="store_true", help="select every entity in the file")
    sub.add_argument("--list", action="store_true",
                     help="print 'id type size' per entity instead of writing files")
    sub.add_argument("--text", action="store_true",
                     help="also write <Type>_<id>.txt with one 'name = value' line "
                          "per named member")
    sub.add_argument("--output-dir", metavar="DIR", default="entities_out",
                     help="folder for dumped files (default: %(default)s)")
    add_schema_options(sub)

    sub = add_command(commands, "markers", "export entities as GeoJSON points",
                      "python simp_tool.py markers levels.package "
                      "--entities overworld.entities --output level_entries.geojson",
                      command_markers)
    sub.add_argument("source", metavar="PACKAGE", help="path to levels.package")
    sub.add_argument("--entities", metavar="SOURCE", action="append", required=True,
                     help="an .entities source to collect markers from: an "
                          "entry name inside the package (e.g. overworld.entities) or a "
                          "file path on disk. Required; repeat the option for several "
                          "sources")
    sub.add_argument("--output", metavar="FILE", default="level_entries.geojson",
                     help="GeoJSON file to write (default: %(default)s)")
    sub.add_argument("types", nargs="*", metavar="Type",
                     help="entity types to include (whitelist). If none given, all types "
                          "are considered unless --exclude is used. Example: Level_Entry Mirror")
    sub.add_argument("-x", "--exclude", action="append", default=[], metavar="Type",
                     help="entity type to exclude (repeatable). Applied after the whitelist")
    sub.add_argument("--entity-properties", action="store_true",
                     help="include every decoded entity member under properties.entity_properties "
                          "(arrays whose elements are all empty strings are dropped, "
                          "dotted names are nested)")
    add_schema_options(sub)

    sub = add_command(commands, "schema", "read the Type_Info from the game exe and save it as a JSON schema",
                      "python simp_tool.py schema sinking_star.exe -o schema.json", command_schema)
    sub.add_argument("exe", metavar="EXE", help="path to the Jai executable (PE32+)")
    sub.add_argument("-o", "--output", metavar="FILE", default="schema.json",
                     help="schema file to write (default: %(default)s)")
    sub.add_argument("--section", action="append", metavar="NAME",
                     help="section(s) to scan for Type_Info (default: .rdata)")

    sub = add_command(commands, "headers", "write C headers for the structs / enums in the schema",
                      "python simp_tool.py headers schema.json -n \"^(Guy|Mirror)$\" -o entities.h",
                      command_headers)
    sub.add_argument("source", metavar="SCHEMA", help="schema JSON, or the game exe (cached as <exe>.schema.json)")
    sub.add_argument("-o", "--output", metavar="FILE", help="header file to write (default: stdout)")
    sub.add_argument("-n", "--name", action="append", metavar="REGEX",
                     help="only structs/enums whose name matches (re.search); repeatable. "
                          "By-value dependencies are always included")
    sub.add_argument("--list", action="store_true", help="list the types in the schema and exit")
    sub.add_argument("--no-enums", action="store_true", help="skip enums that nothing selected refers to")
    sub.add_argument("--no-prelude", action="store_true", help="omit the typedef block (s32, f32, Jai_string ...)")

    sub = add_command(commands, "fields", "print the field tables the decoder uses (name|code|first_version)",
                      "python simp_tool.py fields schema.json --type Level_Entry --notes", command_fields)
    sub.add_argument("source", metavar="SCHEMA", help="schema JSON, or the game exe")
    sub.add_argument("--type", metavar="TYPE", action="append",
                     help="only this type (repeatable; 'base' = the Entity fields); default: all")
    sub.add_argument("--notes", action="store_true", help="append each field's raw member notes as a comment")
    sub.add_argument("--version-note", metavar="REGEX", default=r"\d+",
                     help="regex that finds a field's first version in its member notes "
                          "(default: the first number)")

    return parser


def main():
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help()
        return
    args = parser.parse_args()
    if not hasattr(args, "run"):
        parser.print_help()
        return
    args.run(args)


if __name__ == "__main__":
    main()

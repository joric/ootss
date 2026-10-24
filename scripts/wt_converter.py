#!/usr/bin/env python3
"""
WikiTactics cache -> solutions.json converter.

Reads the crawl cache produced by ``wt_crawler.py`` and emits one record
per puzzle, keyed by level name from the mapping table (``wt.txt``).

For each slug, a single source is chosen:

    * /s/        — the primary solution file, if it exists in the cache
    * published  — a community-published solution, if there is no /s/ one
                   (or if --prefer-community is set, when it overrides /s/)

Sources are never combined, and no two slugs are ever joined.

Default record: {"name", "note": "", "solution"}.
With --info: adds "url", "json", "note" (raw moves), timing arrays,
and source metadata.

Usage:
    wt_converter.py [--info] [--prefer-community]
                    [--out PATH] [--wt PATH] [-v]

Mapping table (wt.txt): level_name,area_slug,puzzle_slug
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

BASE = "https://sinkingstar.wikitactics.com"
PUBLISHED_BASE = "https://id.wikitactics.com/api/community/published"

SOLUTION_URL_FMT = BASE + "/s/{slug}.json"

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"

log = logging.getLogger("converter")


# --------------------------------------------------------------------------- #
# Cache paths
# --------------------------------------------------------------------------- #

def cache_path_for_url(url: str) -> Path:
    """Map a URL to a path under cache/, preserving host and URL path.

    Must stay in sync with wt_crawler.cache_path_for_url.
    """
    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    if path.endswith("/"):
        path += "index.html"
    else:
        suffix = Path(path).suffix
        if not suffix:
            path = path.rstrip("/") + "/index.html"
    return CACHE_DIR / host / Path(path.lstrip("/"))


def solution_cache_path(slug: str) -> Path:
    return cache_path_for_url(SOLUTION_URL_FMT.format(slug=slug))


def published_page_cache_path(index: int) -> Path:
    base = cache_path_for_url(PUBLISHED_BASE).parent
    return base / f"page-{index:04d}.json"


def _is_usable_file(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Wait / move macros
# --------------------------------------------------------------------------- #

# Classes that map to a real playable character slot number (1..9).
CHAR_TO_NUM = {
    "warrior": "1",
    "thief": "2",
    "wizard": "3",
    "priestess": "4",
    "bard": "5",
    "druid": "6",
    "beamer": "8",
    "trader": "9",
}

NON_PLAYABLE_CLASSES = {"pusher"}

DIR_TO_CHAR = {
    "N": "U", "S": "D", "E": "R", "W": "L",
    "A": "X", "T": "V", "F": "F",
    "tick": "B", "wait": "BB", "pause": "BBBB",
    "A@1": "X", "A@2": "X", "A@3": "X", "A@4": "X",
    "A@5": "X", "A@6": "X", "A@7": "X", "A@8": "X",
}

WAIT_MIN_B = 4
WAIT_MAX_B = 8
WAIT_MAX_LEVEL = 2
ELEVATOR_PRESS_CHAR = "V"

QUANTUM_US = 250_000
LONG_RUN_QUANTUM_US = 423_000
MAX_LITERAL_WAIT_US = 1_000_000
_B_RUN_RE = re.compile(r"B+")


def collapse_long_waits(s: str) -> str:
    """Collapse runs of B whose real duration exceeds 1 second."""
    threshold_b = int(MAX_LITERAL_WAIT_US / QUANTUM_US)

    def _repl(m: "re.Match[str]") -> str:
        n = len(m.group(0))
        if n <= threshold_b:
            return m.group(0)
        secs = n * LONG_RUN_QUANTUM_US / 1_000_000
        return f"_{secs:.1f}_"

    return _B_RUN_RE.sub(_repl, s)


def _wait_bs(levels: int) -> int:
    if levels <= 0:
        return 0
    if levels >= WAIT_MAX_LEVEL:
        return WAIT_MAX_B
    span = WAIT_MAX_LEVEL - 1
    if span <= 0:
        return WAIT_MAX_B
    frac = (levels - 1) / span
    return int(round(WAIT_MIN_B + frac * (WAIT_MAX_B - WAIT_MIN_B)))


def fall_wait(dz: int) -> str:
    if dz >= 0:
        return ""
    return "B" * _wait_bs(abs(dz))


def elevator_wait(dz: int) -> str:
    if dz == 0:
        return ""
    total = _wait_bs(abs(dz))
    pre = total // 2
    post = total - pre
    return ("B" * pre) + ELEVATOR_PRESS_CHAR + ("B" * post)


MIN_EMIT_US = 150_000


def us_to_bs(us: int | None) -> int:
    if us is None or us < MIN_EMIT_US:
        return 0
    return max(1, int(round(us / QUANTUM_US)))


def _timing_for_move(timing: dict | None, i: int) -> tuple[int | None, int]:
    return [], 0
    if not timing:
        return None, 0
    action_ticks = timing.get("action_ticks") or []
    input_delay_ticks = timing.get("input_delay_ticks") or []
    duration = action_ticks[i] if i < len(action_ticks) else None
    extra = input_delay_ticks[i] if i < len(input_delay_ticks) else 0
    return duration, extra


def _move_class(char_name: str) -> str:
    return char_name.split("#")[0]


def _instance_index(char_name: str) -> int:
    base, sep, suffix = char_name.partition("#")
    if not sep:
        return 1
    try:
        return int(suffix)
    except ValueError:
        return 1


def _has_digit_key(char_name: str) -> bool:
    cls = _move_class(char_name)
    return cls in CHAR_TO_NUM and cls not in NON_PLAYABLE_CLASSES


def _cycle_order(groups: list[dict] | None) -> list[str]:
    """Names in the game's C-cycle order; ``groups[0]`` is default-selected."""
    if not groups:
        return []
    return [
        g["name"] for g in groups
        if isinstance(g, dict) and isinstance(g.get("name"), str)
    ]


def _cycle_position(order: list[str], name: str) -> int | None:
    try:
        return order.index(name)
    except ValueError:
        return None


def _pusher_z(frame: list) -> int | None:
    for obj in frame:
        if obj.get("k") == "pusher":
            p = obj.get("p") or []
            if len(p) >= 3:
                return p[2]
    return None


def move_deltas(moves: list[str], frames: list | None) -> dict[int, int]:
    if not frames or len(frames) < 2:
        return {}
    deltas: dict[int, int] = {}
    limit = min(len(moves), len(frames) - 1)
    for i in range(limit):
        z_before = _pusher_z(frames[i])
        z_after = _pusher_z(frames[i + 1])
        if z_before is None or z_after is None:
            continue
        dz = z_after - z_before
        if dz != 0:
            deltas[i] = dz
    return deltas


# --------------------------------------------------------------------------- #
# Spawn detection
# --------------------------------------------------------------------------- #

def _actor_ids_in_frame(frame: list) -> set[int]:
    out: set[int] = set()
    for obj in frame:
        if isinstance(obj, dict):
            oid = obj.get("id")
            if isinstance(oid, int):
                out.add(oid)
    return out


def _name_to_toid(groups: list[dict] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    if not groups:
        return out
    for entry in groups:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        toid = entry.get("toid")
        if isinstance(name, str) and isinstance(toid, int):
            out[name] = toid
    return out


def _initial_actor_names(
    groups: list[dict] | None,
    frames: list | None,
) -> set[str]:
    if not frames:
        return set()
    if not groups:
        return set()
    initial_ids = _actor_ids_in_frame(frames[0])
    if not initial_ids:
        return set()
    name_to_toid = _name_to_toid(groups)
    return {
        name for name, toid in name_to_toid.items()
        if toid in initial_ids
    }


def detect_spawned_chars(
    moves: list[str],
    groups: list[dict] | None,
    frames: list | None,
) -> set[str]:
    if not frames:
        return set()
    initial = _initial_actor_names(groups, frames)
    if not initial:
        return set()
    driven = {
        m.split(":", 1)[0] for m in moves if ":" in m
    }
    return driven - initial


# --------------------------------------------------------------------------- #
# Move conversion
# --------------------------------------------------------------------------- #

def convert_moves(
    moves: list[str],
    level: str,
    timing: dict | None = None,
    deltas: dict[int, int] | None = None,
    *,
    groups: list[dict] | None = None,
    default_char: str | None = None,
    frames: list | None = None,
) -> str:
    if not moves:
        return ""
    deltas = deltas or {}

    spawned = detect_spawned_chars(moves, groups, frames)
    use_cycle = not spawned

    all_playable = all(
        _has_digit_key(m.split(":", 1)[0]) for m in moves if ":" in m
    )
    if use_cycle and all_playable:
        result: list[str] = []
        current_char: str | None = None
        for i, move in enumerate(moves):
            if ":" not in move:
                continue
            char_name, action = move.split(":", 1)
            cls = _move_class(char_name)
            key = CHAR_TO_NUM[cls]
            target_idx = _instance_index(char_name)

            if char_name != current_char:
                is_first = not result
                drop_first = is_first and target_idx == 1
                if not drop_first:
                    if result:
                        result.append(" ")
                    result.append(key)
                current_char = char_name

            dz = deltas.get(i)
            if action == "T" and dz is not None:
                result.append(elevator_wait(dz))
            else:
                result.append(
                    DIR_TO_CHAR.get(
                        action, action if action.isdigit() else "?"
                    )
                )
                if dz is not None and dz < 0:
                    result.append(fall_wait(dz))

            if i < len(moves) - 1:
                duration, extra = _timing_for_move(timing, i)
                n_b = us_to_bs((duration or 0) + (extra or 0))
                if n_b:
                    result.append("B")
        return collapse_long_waits("".join(result))

    # ---- mixed level --------------------------------------------------
    #
    # When the map ships a ``groups`` roster, that roster is the
    # authoritative C-cycle order and ``groups[0]`` is the actor the
    # game selects at level start.  We track the current selection by
    # position in that roster and emit exactly the C presses needed to
    # move from the current position to the target pusher — regardless
    # of how many pushers the level has.  (The old ``multi_pusher``
    # gate assumed pusher #1 was default-selected, which is false when
    # the default is a playable class such as the beamer.)
    #
    # Without ``groups`` we fall back to the previous behaviour:
    # assume the first driven char is selected and cycle numbered
    # pushers with C.

    order = _cycle_order(groups)

    playable_chars = sorted({
        m.split(":", 1)[0] for m in moves
        if ":" in m and _has_digit_key(m.split(":", 1)[0])
    })
    pusher_instances = sorted(
        {
            m.split(":", 1)[0] for m in moves
            if ":" in m
            and _move_class(m.split(":", 1)[0]) in NON_PLAYABLE_CLASSES
        },
        key=_instance_index,
    )
    n_pushers = len(pusher_instances)
    multi_pusher = n_pushers > 1

    if order:
        current_cycle: int | None = 0
        current_char: str | None = order[0]
    elif moves and ":" in moves[0]:
        current_cycle = None
        current_char = moves[0].split(":", 1)[0]
    else:
        current_cycle = None
        current_char = None
    current_pusher_idx = 0

    result: list[str] = []

    for i, move in enumerate(moves):
        if ":" not in move:
            continue
        char_name, action = move.split(":", 1)
        cls = _move_class(char_name)

        if cls in NON_PLAYABLE_CLASSES:
            if use_cycle and current_cycle is not None and order:
                target = _cycle_position(order, char_name)
                if target is not None and target != current_cycle:
                    n = len(order)
                    result.append("C" * ((target - current_cycle) % n))
                    current_cycle = target
                    current_char = char_name
            elif use_cycle and multi_pusher and char_name in pusher_instances:
                # Fallback for payloads without ``groups``.
                target = pusher_instances.index(char_name)
                if target != current_pusher_idx:
                    presses = (target - current_pusher_idx) % n_pushers
                    if presses:
                        result.append("C" * presses)
                    current_pusher_idx = target
                    current_char = char_name
        elif _has_digit_key(char_name):
            if char_name != current_char:
                # Separate a digit key from any preceding movement so the
                # game parser treats the digit as a character selection,
                # not as a direction.  No leading space when the digit is
                # the very first emitted token.
                if result:
                    result.append(" ")
                result.append(CHAR_TO_NUM[cls])
                current_char = char_name
                # A digit press also moves the game's selection pointer;
                # keep the C-cycle tracker in sync when the roster is
                # known and the char is listed in it.
                if order:
                    idx = _cycle_position(order, char_name)
                    if idx is not None:
                        current_cycle = idx
        # else: queen / scripted NPC -> never selected.

        dz = deltas.get(i)
        if action == "T" and dz is not None:
            result.append(elevator_wait(dz))
        else:
            result.append(
                DIR_TO_CHAR.get(
                    action, action if action.isdigit() else "?"
                )
            )
            if dz is not None and dz < 0:
                result.append(fall_wait(dz))

    return collapse_long_waits("".join(result))


# --------------------------------------------------------------------------- #
# Cache readers
# --------------------------------------------------------------------------- #

def _load_published_pages(page_numbers: list[int]) -> list[dict]:
    entries: list[dict] = []
    for n in sorted(page_numbers):
        path = published_page_cache_path(n)
        if not _is_usable_file(path):
            continue
        try:
            payload = json.loads(path.read_bytes())
        except Exception:  # noqa: BLE001
            continue
        page_entries = payload.get("entries")
        if isinstance(page_entries, list):
            entries.extend(e for e in page_entries if isinstance(e, dict))
    return entries


def read_cached_published_entries() -> list[dict]:
    """Read every cached published page in the cache directory."""
    published_dir = published_page_cache_path(0).parent
    if not published_dir.is_dir():
        return []
    pages = []
    for path in published_dir.glob("page-*.json"):
        try:
            pages.append(int(path.stem.split("-", 1)[1]))
        except ValueError:
            continue
    return _load_published_pages(pages)


def _load_wt_table(path: Path) -> dict[str, tuple[str, str]]:
    table: dict[str, tuple[str, str]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) >= 3:
                table[row[0]] = (row[1], row[2])
    return table


# --------------------------------------------------------------------------- #
# Record building
# --------------------------------------------------------------------------- #

def _build_record(
    level: str,
    area: str,
    slug: str,
    data: dict,
    source_kind: str,
    *,
    info: bool = False,
) -> dict:
    moves = data.get("moves", [])
    timing = data.get("timing") or {}
    frames = data.get("frames") or []
    groups = data.get("groups") or []
    deltas = move_deltas(moves, frames)

    if source_kind == "published":
        json_url = PUBLISHED_BASE + "?site=sinkingstar&type=solution"
    else:
        json_url = SOLUTION_URL_FMT.format(slug=slug)

    record = {
        "name": level,
        "note": " ".join(moves) if info else "",
        "solution": convert_moves(
            moves, level, timing,
            deltas=deltas,
            groups=groups,
            frames=frames,
        ),
    }

    if not info:
        return record

    record["url"] = f"{BASE}/areas/{area}/#{slug}"
    record["json"] = json_url

    if source_kind in ("published", "s"):
        candidate = data.get("author")
        if isinstance(candidate, dict) and candidate.get("displayName"):
            record["author"] = candidate["displayName"]
            if candidate.get("profileUrl"):
                record["authorProfile"] = candidate["profileUrl"]

    if source_kind == "published":
        record["source"] = "published"
        if data.get("id"):
            record["post_id"] = data["id"]
        if data.get("publishedAt"):
            record["publishedAt"] = data["publishedAt"]
        if data.get("checkedBy"):
            record["checkedBy"] = data["checkedBy"]
        ingame = data.get("inGame") or {}
        if isinstance(ingame, dict) and ingame.get("checkedAt"):
            record["inGameCheckedAt"] = ingame["checkedAt"]

    for key in ("action_ticks", "frame_ticks", "input_delay_ticks"):
        value = timing.get(key)
        if value:
            record[key] = ",".join(map(str, value))

    return record


# --------------------------------------------------------------------------- #
# Source breakdown (for the summary line)
# --------------------------------------------------------------------------- #

@dataclass
class ConvertStats:
    total_levels: int = 0
    records_written: int = 0
    from_s: int = 0
    from_published: int = 0
    published_overlap_dropped: int = 0
    primary_missing: int = 0
    primary_found: int = 0
    pub_added: int = 0
    pub_skipped_unmapped: int = 0


def convert_cache_to_json(
    *,
    wt_path: Path,
    out_path: Path,
    prefer_community: bool = False,
    info: bool = False,
) -> ConvertStats:
    if not wt_path.is_file():
        raise FileNotFoundError(
            f"mapping table not found: {wt_path}\n"
            f"expected one row per puzzle: level_name,area_slug,puzzle_slug\n"
            f"pass --wt PATH to point at it."
        )

    levels = _load_wt_table(wt_path)
    levels_by_slug = {
        slug: (name, (area, slug))
        for name, (area, slug) in levels.items()
    }

    published = read_cached_published_entries()

    records: list[dict] = []
    by_name: dict[str, dict] = {}
    source_of: dict[str, str] = {}
    stats = ConvertStats(total_levels=len(levels))

    def _upsert(level: str, rec: dict, source: str) -> bool:
        existing = by_name.get(level)
        if existing is None:
            records.append(rec)
            by_name[level] = rec
            source_of[level] = source
            return True
        if prefer_community:
            records[records.index(existing)] = rec
            by_name[level] = rec
            source_of[level] = source
            return True
        return False

    for entry in published:
        pid = entry.get("puzzleId")
        if not pid or pid not in levels_by_slug:
            stats.pub_skipped_unmapped += 1
            continue
        level, (area, slug) = levels_by_slug[pid]
        rec = _build_record(level, area, slug, entry, "published", info=info)
        if source_of.get(level) is not None and not prefer_community:
            stats.published_overlap_dropped += 1
            continue
        if _upsert(level, rec, "published"):
            stats.pub_added += 1

    for level, (area, slug) in levels.items():
        path = solution_cache_path(slug)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            stats.primary_missing += 1
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("convert: unreadable %s (%s)", path, exc)
            stats.primary_missing += 1
            continue
        rec = _build_record(level, area, slug, data, "s", info=info)
        existing = by_name.get(level)
        if existing is None:
            records.append(rec)
            by_name[level] = rec
            source_of[level] = "s"
            stats.primary_found += 1
        elif not prefer_community:
            records[records.index(existing)] = rec
            by_name[level] = rec
            source_of[level] = "s"
            stats.primary_found += 1
        else:
            stats.primary_found += 1

    records.sort(key=lambda r: r["name"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)

    stats.records_written = len(records)
    stats.from_s = sum(1 for v in source_of.values() if v == "s")
    stats.from_published = sum(
        1 for v in source_of.values() if v == "published"
    )

    log.info(
        "converter: %d records written "
        "(from /s/: %d, from published: %d, published overlap dropped: %d)",
        stats.records_written, stats.from_s, stats.from_published,
        stats.published_overlap_dropped,
    )
    log.info(
        "converter: /s/ cache: %d found, %d missing "
        "(published added %d, unmapped %d)",
        stats.primary_found, stats.primary_missing,
        stats.pub_added, stats.pub_skipped_unmapped,
    )
    return stats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out", type=Path, default=SCRIPT_DIR / "solutions.json",
        help="output JSON path (default: solutions.json next to this script)",
    )
    parser.add_argument(
        "--wt", type=Path, default=SCRIPT_DIR / "wt.txt",
        help="mapping table (level_name,area_slug,puzzle_slug). "
             "Default: wt.txt next to this script.",
    )
    parser.add_argument(
        "--info", action="store_true",
        help="emit full records (url, json, note, timing arrays, "
             "source metadata); off by default",
    )
    parser.add_argument(
        "--prefer-community", action="store_true",
        help="prefer published solutions over /s/ primary solutions",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="show cache hits and per-slug warnings",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    wt_path = args.wt.expanduser().resolve()
    out_path = args.out.expanduser().resolve()

    try:
        stats = convert_cache_to_json(
            wt_path=wt_path,
            out_path=out_path,
            prefer_community=args.prefer_community,
            info=args.info,
        )
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130

    log.info("converted %d record(s) -> %s", stats.records_written, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

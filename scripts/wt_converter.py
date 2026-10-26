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

Default record: {"name", "solution"}.
With --info: adds "url", "json", "moves" (raw moves), timing arrays,
source metadata, and "order" (all actors in switching order).

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

# --------------------------------------------------------------------------- #
# Character-selection mode
# --------------------------------------------------------------------------- #
#
# SELECTION_MODE controls how switches between actors are encoded:
#
#   "digit"  — use the digit keys (1..9) to select playable characters.
#              This is the in-game default and what the official solutions
#              use.  Instanced classes (e.g. priestess#2) may require
#              repeated presses of the same digit.
#
#   "cycle"  — use only the C key to cycle through the roster.  Multiple
#              C presses are emitted as needed to reach the target actor.
#              Works for both playable characters and pushers, and never
#              uses digit keys.
#
# Change this constant to switch the default for the whole conversion.
#
SELECTION_MODE = "cycle"


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


def _find_in_order(order: list[str], char_name: str) -> int | None:
    """Locate ``char_name`` in the roster.

    Exact match first; then fall back to matching by base class and
    instance index so rosters that store bare class names (``"priestess"``)
    still resolve instanced move names (``"priestess#1"``).
    """
    try:
        return order.index(char_name)
    except ValueError:
        pass
    cls = _move_class(char_name)
    idx = _instance_index(char_name)
    seen = 0
    for i, name in enumerate(order):
        if _move_class(name) != cls:
            continue
        seen += 1
        if seen == idx:
            return i
    # Single-instance class stored bare: treat "priestess#1" == "priestess".
    if idx == 1:
        for i, name in enumerate(order):
            if name == cls:
                return i
    return None


def _class_instance_count(order: list[str], cls: str) -> int:
    if not order:
        return 1
    return sum(1 for name in order if _move_class(name) == cls) or 1


def _digit_presses(
    char_name: str,
    current_char: str | None,
    order: list[str],
) -> list[str]:
    """Digit-key presses needed to select ``char_name``.

    Returns a list of single-digit strings.  The game selects a class
    with its digit key.  Repeated presses of the same digit cycle
    between instances of that class (``priestess#1`` -> ``priestess#2``).
    When coming from a *different* class, the first press selects
    instance ``#1``, so selecting instance ``#k`` needs ``k`` presses.
    """
    cls = _move_class(char_name)
    key = CHAR_TO_NUM[cls]
    target_idx = _instance_index(char_name)

    if current_char is None:
        return [key] * target_idx

    cur_cls = _move_class(current_char)
    if cls != cur_cls:
        return [key] * target_idx

    cur_idx = _instance_index(current_char)
    if target_idx == cur_idx:
        return []

    n = _class_instance_count(order, cls)
    presses = (target_idx - cur_idx) % n
    if presses == 0:
        presses = n
    return [key] * presses


def _cycle_presses(
    char_name: str,
    current_cycle: int | None,
    order: list[str],
) -> int:
    """C-key presses needed to reach ``char_name`` from ``current_cycle``.

    The roster order is the C-cycle order; ``C`` advances one position
    at a time and wraps.  If the roster is unknown, return 0 (caller
    will fall back to digit selection).
    """
    if not order or current_cycle is None:
        return 0
    target = _find_in_order(order, char_name)
    if target is None or target == current_cycle:
        return 0
    n = len(order)
    return (target - current_cycle) % n


def _players_order(groups: list[dict] | None, moves: list[str]) -> str:
    """All actors in C-cycle order, space-separated.

    Uses the ``groups`` roster when present; otherwise falls back to
    the distinct actors seen in the move list, in first-appearance
    order.
    """
    order = _cycle_order(groups)
    if not order:
        seen: list[str] = []
        for m in moves:
            if ":" not in m:
                continue
            n = m.split(":", 1)[0]
            if n not in seen:
                seen.append(n)
        order = seen
    return " ".join(order)


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

def _emit_selection(
    char_name: str,
    current_char: str | None,
    current_cycle: int | None,
    order: list[str],
    mode: str,
) -> tuple[list[str], str | None, int | None]:
    """Return (tokens, new_current_char, new_current_cycle).

    ``tokens`` are space-separated fragments (without the trailing space).
    In ``cycle`` mode we press C as many times as needed to reach the
    target actor's roster position and never emit digit keys.  In
    ``digit`` mode we press the class digit once per required instance.
    """
    if mode == "cycle" and order and current_cycle is not None:
        presses = _cycle_presses(char_name, current_cycle, order)
        if presses:
            return (
                ["C" * presses],
                char_name,
                _find_in_order(order, char_name),
            )
        return [], current_char, current_cycle

    # digit mode (or no roster to cycle with)
    presses = _digit_presses(char_name, current_char, order)
    toks = [p for p in presses]
    new_cycle = current_cycle
    if order:
        pos = _find_in_order(order, char_name)
        if pos is not None:
            new_cycle = pos
    return toks, char_name, new_cycle


def convert_moves(
    moves: list[str],
    level: str,
    timing: dict | None = None,
    deltas: dict[int, int] | None = None,
    *,
    groups: list[dict] | None = None,
    default_char: str | None = None,
    frames: list | None = None,
    mode: str | None = None,
) -> str:
    if not moves:
        return ""
    deltas = deltas or {}
    mode = mode or SELECTION_MODE

    spawned = detect_spawned_chars(moves, groups, frames)
    use_cycle = not spawned

    all_playable = all(
        _has_digit_key(m.split(":", 1)[0]) for m in moves if ":" in m
    )
    if use_cycle and all_playable:
        order = _cycle_order(groups)
        result: list[str] = []

        if order:
            current_cycle: int | None = 0
            current_char: str | None = order[0]
        else:
            current_cycle = None
            current_char = None

        for i, move in enumerate(moves):
            if ":" not in move:
                continue
            char_name, action = move.split(":", 1)

            if char_name != current_char:
                toks, current_char, current_cycle = _emit_selection(
                    char_name, current_char, current_cycle, order, mode,
                )
                for t in toks:
                    if result:
                        result.append(" ")
                    result.append(t)

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
    order = _cycle_order(groups)

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
                target = _find_in_order(order, char_name)
                if target is not None and target != current_cycle:
                    n = len(order)
                    result.append("C" * ((target - current_cycle) % n))
                    current_cycle = target
                    current_char = char_name
            elif use_cycle and multi_pusher and char_name in pusher_instances:
                target = pusher_instances.index(char_name)
                if target != current_pusher_idx:
                    presses = (target - current_pusher_idx) % n_pushers
                    if presses:
                        result.append("C" * presses)
                    current_pusher_idx = target
                    current_char = char_name
        elif _has_digit_key(char_name):
            if char_name != current_char:
                toks, current_char, current_cycle = _emit_selection(
                    char_name, current_char, current_cycle, order, mode,
                )
                for t in toks:
                    if result:
                        result.append(" ")
                    result.append(t)
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
    mode: str | None = None,
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
        "solution": convert_moves(
            moves, level, timing,
            deltas=deltas,
            groups=groups,
            frames=frames,
            mode=mode,
        ),
    }

    if not info:
        return record

    record["url"] = f"{BASE}/areas/{area}/#{slug}"
    record["json"] = json_url
    record["moves"] = " ".join(moves)

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

    # All actors in switching order, space-separated.
    record["order"] = _players_order(groups, moves)

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
    mode: str | None = None,
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
        rec = _build_record(
            level, area, slug, entry, "published", info=info, mode=mode,
        )
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
        rec = _build_record(
            level, area, slug, data, "s", info=info, mode=mode,
        )
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
        help="emit full records (url, json, moves, timing arrays, "
             "source metadata, order); off by default",
    )
    parser.add_argument(
        "--prefer-community", action="store_true",
        help="prefer published solutions over /s/ primary solutions",
    )
    parser.add_argument(
        "--selection-mode", choices=["digit", "cycle"], default=None,
        help="character-switching encoding: 'digit' (default in-game) or "
             "'cycle' (C key only, multiple presses as needed). "
             "Defaults to the SELECTION_MODE constant at the top of the file.",
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
    mode = args.selection_mode or SELECTION_MODE

    try:
        stats = convert_cache_to_json(
            wt_path=wt_path,
            out_path=out_path,
            prefer_community=args.prefer_community,
            info=args.info,
            mode=mode,
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

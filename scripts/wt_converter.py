#!/usr/bin/env python3
"""
WikiTactics cache -> solutions.json converter.

Character-switch order:

    1. If the level's ``groups`` array is present and its **first** actor
       is at the maximum y among frame-0 actors (i.e. bottom of the
       screen), use the ``groups`` order as the C-cycle.
    2. Otherwise, derive the order from frame-0 coordinates: sort by
       y ascending (top to bottom), then x ascending (left to right),
       i.e. a row-major grid read ``(y, x)``.

All classes in CHAR_TO_NUM are selectable, including ``pusher``.

If a **druid** is present in the roster, the converter falls back to
**digit-key selection** for every switch.

Selection is suppressed entirely when the roster contains only a single
character (nothing to switch to).

Timing:
    USE_ACTION_TICKS emits per-move ``_x.y_`` sleeps derived from
    ``action_ticks`` / ``input_delay_ticks``.  When enabled and the level
    carries timestamp data, all other waits are suppressed.

Elevator:
    ``T`` always maps to the ``V`` keypress.  Elevator wait padding
    (``B..V..B``) is applied only when the ``T`` move corresponds to a
    known non-zero height change of the elevator carriage (``pusher`` ->
    ``Elevator_Platform`` -> ``Guy``).  Unclear or zero deltas emit a
    plain ``V``.

Output token vocabulary:

    * direction / action keys   (U D L R X V F, digits, C, ...)
    * ``B``                     — literal wait key (only when not using ticks)
    * ``_x.y_``                 — explicit time sleep (USE_ACTION_TICKS)

Usage:
    wt_converter.py [--info] [--prefer-community]
                    [--use-action-ticks | --no-use-action-ticks]
                    [--elevator-wait | --no-elevator-wait]
                    [--elevator-pre-b N] [--elevator-post-b N]
                    [--fall-wait | --no-fall-wait]
                    [--selection-mode {digit,cycle}]
                    [--out PATH] [--wt PATH] [-v]

Mapping table (wt.txt): level_name,area_slug,puzzle_slug
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

SELECTION_MODE = "cycle"

USE_ACTION_TICKS = False
EMIT_ELEVATOR_WAIT = True
EMIT_FALL_WAIT = False
FALL_WAIT_AS_SLEEP = False

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
# Move macros
# --------------------------------------------------------------------------- #

CHAR_TO_NUM = {
    "warrior": "1",
    "thief": "2",
    "wizard": "3",
    "priestess": "4",
    "bard": "5",
    "druid": "6",
    "pusher": "7",
    "beamer": "8",
    "trader": "9",
}

FORCE_DIGIT_CLASSES = {"druid"}

DIR_TO_CHAR = {
    "N": "U", "S": "D", "E": "R", "W": "L",
    "A": "X", "T": "V", "F": "F",
    "tick": "B", "wait": "B", "pause": "B",
    "A@1": "X", "A@2": "X", "A@3": "X", "A@4": "X",
    "A@5": "X", "A@6": "X", "A@7": "X", "A@8": "X",
}

ELEVATOR_PRESS_CHAR = "V"
ELEVATOR_PRE_B = 2
ELEVATOR_POST_B = 4

FALL_WAIT_MIN_B = 4
FALL_WAIT_MAX_B = 8
FALL_WAIT_MAX_LEVEL = 2

QUANTUM_US = 250_000
MIN_EMIT_US = 150_000


def elevator_wait(dz, *, pre_b=ELEVATOR_PRE_B, post_b=ELEVATOR_POST_B):
    # Only called when a known non-zero height change is occurring.
    return ("B" * max(pre_b, 0)) + ELEVATOR_PRESS_CHAR + ("B" * max(post_b, 0))


def _fall_bs(levels):
    if levels <= 0:
        return 0
    if levels >= FALL_WAIT_MAX_LEVEL:
        return FALL_WAIT_MAX_B
    span = FALL_WAIT_MAX_LEVEL - 1
    if span <= 0:
        return FALL_WAIT_MAX_B
    frac = (levels - 1) / span
    return int(round(FALL_WAIT_MIN_B + frac * (FALL_WAIT_MAX_B - FALL_WAIT_MIN_B)))


def fall_wait(dz):
    if dz >= 0:
        return ""
    levels = abs(dz)
    if FALL_WAIT_AS_SLEEP:
        us = _fall_bs(levels) * QUANTUM_US
        return us_to_sleep(us)
    return "B" * _fall_bs(levels)


def us_to_sleep(us):
    if us is None or us < MIN_EMIT_US:
        return ""
    secs = us / 1_000_000
    return f"_{secs:.1f}_"


def _has_action_ticks(timing):
    if not timing:
        return False
    return bool(timing.get("action_ticks")) or bool(timing.get("input_delay_ticks"))


def _timing_for_move(timing, i):
    if not timing:
        return None, 0
    at = timing.get("action_ticks") or []
    idt = timing.get("input_delay_ticks") or []
    duration = at[i] if i < len(at) else None
    extra = idt[i] if i < len(idt) else 0
    return duration, extra


def _move_class(char_name):
    return char_name.split("#")[0]


def _instance_index(char_name):
    base, sep, suffix = char_name.partition("#")
    if not sep:
        return 1
    try:
        return int(suffix)
    except ValueError:
        return 1


def _is_selectable_class(cls):
    return cls in CHAR_TO_NUM


def _is_selectable(char_name):
    return _is_selectable_class(_move_class(char_name))


# --------------------------------------------------------------------------- #
# Coordinate data from frame 0
# --------------------------------------------------------------------------- #

def _frame0_actors(frames):
    """[(x, y, class, id), ...] for selectable Guys at frame 0."""
    if not frames:
        return []
    frame0 = frames[0]
    if not isinstance(frame0, list):
        return []
    out = []
    for obj in frame0:
        if not isinstance(obj, dict):
            continue
        if obj.get("t") != "Guy":
            continue
        cls = obj.get("k")
        if not _is_selectable_class(cls):
            continue
        p = obj.get("p")
        if not isinstance(p, list) or len(p) < 2:
            continue
        x, y = p[0], p[1]
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        out.append((int(x), int(y), cls, obj.get("id")))
    return out


def _coordinate_order(frames):
    """Row-major grid read: y ascending, then x ascending."""
    actors = _frame0_actors(frames)
    if not actors:
        return []
    actors.sort(key=lambda a: (a[1], a[0]))
    counts = {}
    order = []
    for _, _, cls, _ in actors:
        counts[cls] = counts.get(cls, 0) + 1
        idx = counts[cls]
        order.append(cls if idx == 1 else f"{cls}#{idx}")
    return order


def _groups_order(groups):
    """Return the ``groups`` order, or [] if groups is absent/empty."""
    if not groups:
        return []
    names = [
        g["name"] for g in groups
        if isinstance(g, dict) and isinstance(g.get("name"), str)
        and _is_selectable(g["name"])
    ]
    return names


def _groups_first_is_bottom(groups, frames):
    """True if the first actor in ``groups`` is at the maximum y of frame 0."""
    if not groups or not isinstance(groups[0], dict):
        return False
    first_name = groups[0].get("name")
    if not isinstance(first_name, str) or not _is_selectable(first_name):
        return False
    actors = _frame0_actors(frames)
    if not actors:
        return False
    first_cls = _move_class(first_name)
    max_y = max(a[1] for a in actors)
    for x, y, cls, _ in actors:
        if cls == first_cls and y == max_y:
            return True
    return False


def _roster_order(groups, frames):
    """Cycle order.

    Uses ``groups`` when its first actor sits at the bottom row (max y);
    otherwise uses the row-major ``(y, x)`` coordinate order.
    """
    if _groups_first_is_bottom(groups, frames):
        order = _groups_order(groups)
        if order:
            return order
    return _coordinate_order(frames)


def _find_in_order(order, char_name):
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
    if idx == 1:
        for i, name in enumerate(order):
            if name == cls:
                return i
    return None


def _class_instance_count(order, cls):
    if not order:
        return 1
    return sum(1 for name in order if _move_class(name) == cls) or 1


def _digit_presses(char_name, current_char, order):
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


def _cycle_presses(char_name, current_cycle, order):
    if not order or current_cycle is None:
        return 0
    target = _find_in_order(order, char_name)
    if target is None or target == current_cycle:
        return 0
    n = len(order)
    return (target - current_cycle) % n


def _has_druid_in_roster(groups, frames, moves):
    for x, y, cls, _ in _frame0_actors(frames):
        if cls in FORCE_DIGIT_CLASSES:
            return True
    if groups:
        for entry in groups:
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str) and _move_class(name) in FORCE_DIGIT_CLASSES:
                    return True
    for m in moves:
        if ":" in m and _move_class(m.split(":", 1)[0]) in FORCE_DIGIT_CLASSES:
            return True
    return False


def _players_order(groups, frames, moves):
    order = _roster_order(groups, frames)
    seen = set(order)
    for m in moves:
        if ":" not in m:
            continue
        name = m.split(":", 1)[0]
        if not _is_selectable(name) or name in seen:
            continue
        order.append(name)
        seen.add(name)
    return " ".join(order)


# --------------------------------------------------------------------------- #
# Elevator carriage height
# --------------------------------------------------------------------------- #

def _frame_z(frame):
    """Return a representative elevator-carriage z for a frame.

    Prefers an explicit ``pusher``; falls back to ``Elevator_Platform``;
    then to any ``Guy``'s z.  Returns None if no carrier is present.
    """
    if not isinstance(frame, list):
        return None

    def _z_of(obj):
        p = obj.get("p") or []
        return p[2] if len(p) >= 3 else None

    for obj in frame:
        if isinstance(obj, dict) and obj.get("k") == "pusher":
            z = _z_of(obj)
            if z is not None:
                return z
    for obj in frame:
        if isinstance(obj, dict) and obj.get("t") == "Elevator_Platform":
            z = _z_of(obj)
            if z is not None:
                return z
    for obj in frame:
        if isinstance(obj, dict) and obj.get("t") == "Guy":
            z = _z_of(obj)
            if z is not None:
                return z
    return None


def move_deltas(moves, frames):
    """Map move index -> z delta (int) or None (unclear)."""
    if not frames or len(frames) < 2:
        return {}
    deltas = {}
    limit = min(len(moves), len(frames) - 1)
    for i in range(limit):
        zb = _frame_z(frames[i])
        za = _frame_z(frames[i + 1])
        if zb is None or za is None:
            deltas[i] = None
        else:
            deltas[i] = za - zb
    return deltas


# --------------------------------------------------------------------------- #
# Spawn detection
# --------------------------------------------------------------------------- #

def _actor_ids_in_frame(frame):
    out = set()
    for obj in frame:
        if isinstance(obj, dict):
            oid = obj.get("id")
            if isinstance(oid, int):
                out.add(oid)
    return out


def _name_to_toid(groups):
    out = {}
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


def _initial_actor_names(groups, frames):
    if not frames or not groups:
        return set()
    ids = _actor_ids_in_frame(frames[0])
    if not ids:
        return set()
    m = _name_to_toid(groups)
    return {name for name, toid in m.items() if toid in ids}


def detect_spawned_chars(moves, groups, frames):
    if not frames:
        return set()
    initial = _initial_actor_names(groups, frames)
    if not initial:
        return set()
    driven = {
        m.split(":", 1)[0] for m in moves
        if ":" in m and _is_selectable(m.split(":", 1)[0])
    }
    return driven - initial


# --------------------------------------------------------------------------- #
# Move conversion
# --------------------------------------------------------------------------- #

def _emit_selection(char_name, current_char, current_cycle, order, mode):
    if mode == "cycle" and order and current_cycle is not None:
        presses = _cycle_presses(char_name, current_cycle, order)
        if presses:
            return ("C" * presses, False, char_name, _find_in_order(order, char_name))
        return "", False, current_char, current_cycle
    presses = _digit_presses(char_name, current_char, order)
    fragment = " ".join(presses)
    new_cycle = current_cycle
    if order:
        pos = _find_in_order(order, char_name)
        if pos is not None:
            new_cycle = pos
    return fragment, True, char_name, new_cycle


def _emit_action(action, dz, *, emit_elevator_wait, emit_fall_wait,
                 elevator_pre_b, elevator_post_b):
    if action == "T":
        # T is the V keypress.  Elevator padding applies only when this
        # move corresponds to a known non-zero height change.
        if emit_elevator_wait and dz is not None and dz != 0:
            return elevator_wait(dz, pre_b=elevator_pre_b, post_b=elevator_post_b)
        return ELEVATOR_PRESS_CHAR
    out = DIR_TO_CHAR.get(action, action if action.isdigit() else "?")
    if emit_fall_wait and dz is not None and dz < 0:
        out += fall_wait(dz)
    return out


def convert_moves(moves, level, timing=None, deltas=None, *,
                  groups=None, default_char=None, frames=None, mode=None,
                  use_action_ticks=None, emit_elevator_wait=None,
                  emit_fall_wait=None, elevator_pre_b=None, elevator_post_b=None):
    if not moves:
        return ""
    deltas = deltas or {}
    mode = mode or SELECTION_MODE
    if use_action_ticks is None:
        use_action_ticks = USE_ACTION_TICKS
    if emit_elevator_wait is None:
        emit_elevator_wait = EMIT_ELEVATOR_WAIT
    if emit_fall_wait is None:
        emit_fall_wait = EMIT_FALL_WAIT
    if elevator_pre_b is None:
        elevator_pre_b = ELEVATOR_PRE_B
    if elevator_post_b is None:
        elevator_post_b = ELEVATOR_POST_B

    order = _roster_order(groups, frames)
    # A roster with a single character has nothing to switch to, so no
    # selection keystrokes (cycle ``C`` or digit keys) are needed.
    single_character = len(order) == 1
    if _has_druid_in_roster(groups, frames, moves):
        mode = "digit"

    ticks_available = _has_action_ticks(timing)
    use_ticks = bool(use_action_ticks and ticks_available)
    if use_ticks:
        emit_elevator_wait = False
        emit_fall_wait = False

    spawned = detect_spawned_chars(moves, groups, frames)
    use_cycle = not spawned
    all_playable = all(_is_selectable(m.split(":", 1)[0]) for m in moves if ":" in m)

    if use_cycle and all_playable:
        result = []
        if order and mode != "digit":
            current_cycle = 0
            current_char = order[0]
        elif order and mode == "digit":
            # Digit mode: do not assume any character is pre-selected;
            # force the first selectable move to emit its selection digits.
            current_cycle = None
            current_char = None
        else:
            current_cycle = None
            current_char = None
        for i, move in enumerate(moves):
            if ":" not in move:
                continue
            char_name, action = move.split(":", 1)
            if (not single_character
                    and _is_selectable(char_name)
                    and char_name != current_char):
                frag, needs_space, current_char, current_cycle = _emit_selection(
                    char_name, current_char, current_cycle, order, mode)
                if frag:
                    if needs_space and result:
                        result.append(" ")
                    result.append(frag)
            dz = deltas.get(i)
            result.append(_emit_action(
                action, dz, emit_elevator_wait=emit_elevator_wait,
                emit_fall_wait=emit_fall_wait,
                elevator_pre_b=elevator_pre_b, elevator_post_b=elevator_post_b))
            if use_ticks and i < len(moves) - 1:
                d, e = _timing_for_move(timing, i)
                sleep = us_to_sleep((d or 0) + (e or 0))
                if sleep:
                    result.append(sleep)
        return "".join(result)

    # Non-selectable move names present (e.g. stones, doors driven by the
    # level).  These are not characters, so they cannot be cycle-selected;
    # emit only their actions.
    if order and mode != "digit":
        current_cycle = 0
        current_char = order[0]
    elif moves and ":" in moves[0] and _is_selectable(moves[0].split(":", 1)[0]) and mode != "digit":
        current_cycle = None
        current_char = moves[0].split(":", 1)[0]
    else:
        current_cycle = None
        current_char = None
    result = []
    for i, move in enumerate(moves):
        if ":" not in move:
            continue
        char_name, action = move.split(":", 1)
        if (not single_character
                and _is_selectable(char_name)
                and char_name != current_char):
            frag, needs_space, current_char, current_cycle = _emit_selection(
                char_name, current_char, current_cycle, order, mode)
            if frag:
                if needs_space and result:
                    result.append(" ")
                result.append(frag)
        dz = deltas.get(i)
        result.append(_emit_action(
            action, dz, emit_elevator_wait=emit_elevator_wait,
            emit_fall_wait=emit_fall_wait,
            elevator_pre_b=elevator_pre_b, elevator_post_b=elevator_post_b))
        if use_ticks and i < len(moves) - 1:
            d, e = _timing_for_move(timing, i)
            sleep = us_to_sleep((d or 0) + (e or 0))
            if sleep:
                result.append(sleep)
    return "".join(result)


# --------------------------------------------------------------------------- #
# Cache readers
# --------------------------------------------------------------------------- #

def _load_published_pages(page_numbers):
    entries = []
    for n in sorted(page_numbers):
        path = published_page_cache_path(n)
        if not _is_usable_file(path):
            continue
        try:
            payload = json.loads(path.read_bytes())
        except Exception:
            continue
        page_entries = payload.get("entries")
        if isinstance(page_entries, list):
            entries.extend(e for e in page_entries if isinstance(e, dict))
    return entries


def read_cached_published_entries():
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


def _load_wt_table(path):
    table = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) >= 3:
                table[row[0]] = (row[1], row[2])
    return table


# --------------------------------------------------------------------------- #
# Record building
# --------------------------------------------------------------------------- #

def _build_record(level, area, slug, data, source_kind, *, info=False, mode=None,
                  use_action_ticks=None, emit_elevator_wait=None,
                  emit_fall_wait=None, elevator_pre_b=None, elevator_post_b=None):
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
            moves, level, timing, deltas=deltas, groups=groups, frames=frames,
            mode=mode, use_action_ticks=use_action_ticks,
            emit_elevator_wait=emit_elevator_wait,
            emit_fall_wait=emit_fall_wait,
            elevator_pre_b=elevator_pre_b, elevator_post_b=elevator_post_b),
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
    record["order"] = _players_order(groups, frames, moves)
    return record


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


def convert_cache_to_json(*, wt_path, out_path, prefer_community=False,
                          info=False, mode=None, use_action_ticks=None,
                          emit_elevator_wait=None, emit_fall_wait=None,
                          elevator_pre_b=None, elevator_post_b=None):
    if not wt_path.is_file():
        raise FileNotFoundError(
            f"mapping table not found: {wt_path}\n"
            f"expected one row per puzzle: level_name,area_slug,puzzle_slug\n"
            f"pass --wt PATH to point at it.")

    levels = _load_wt_table(wt_path)
    levels_by_slug = {slug: (name, (area, slug)) for name, (area, slug) in levels.items()}
    published = read_cached_published_entries()

    records = []
    by_name = {}
    source_of = {}
    stats = ConvertStats(total_levels=len(levels))

    def _upsert(level, rec, source):
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
        rec = _build_record(level, area, slug, entry, "published", info=info,
                            mode=mode, use_action_ticks=use_action_ticks,
                            emit_elevator_wait=emit_elevator_wait,
                            emit_fall_wait=emit_fall_wait,
                            elevator_pre_b=elevator_pre_b,
                            elevator_post_b=elevator_post_b)
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
        except Exception as exc:
            log.warning("convert: unreadable %s (%s)", path, exc)
            stats.primary_missing += 1
            continue
        rec = _build_record(level, area, slug, data, "s", info=info, mode=mode,
                            use_action_ticks=use_action_ticks,
                            emit_elevator_wait=emit_elevator_wait,
                            emit_fall_wait=emit_fall_wait,
                            elevator_pre_b=elevator_pre_b,
                            elevator_post_b=elevator_post_b)
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
    stats.from_published = sum(1 for v in source_of.values() if v == "published")
    log.info("converter: %d records written (from /s/: %d, from published: %d, "
             "published overlap dropped: %d)",
             stats.records_written, stats.from_s, stats.from_published,
             stats.published_overlap_dropped)
    log.info("converter: /s/ cache: %d found, %d missing (published added %d, "
             "unmapped %d)", stats.primary_found, stats.primary_missing,
             stats.pub_added, stats.pub_skipped_unmapped)
    return stats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=SCRIPT_DIR / "solutions.json")
    parser.add_argument("--wt", type=Path, default=SCRIPT_DIR / "wt.txt")
    parser.add_argument("--info", action="store_true")
    parser.add_argument("--prefer-community", action="store_true")
    parser.add_argument("--selection-mode", choices=["digit", "cycle"], default=None)
    ticks_group = parser.add_mutually_exclusive_group()
    ticks_group.add_argument("--use-action-ticks", dest="use_action_ticks",
                             action="store_true", default=None)
    ticks_group.add_argument("--no-use-action-ticks", dest="use_action_ticks",
                             action="store_false")
    elev_group = parser.add_mutually_exclusive_group()
    elev_group.add_argument("--elevator-wait", dest="emit_elevator_wait",
                            action="store_true", default=None)
    elev_group.add_argument("--no-elevator-wait", dest="emit_elevator_wait",
                            action="store_false")
    parser.add_argument("--elevator-pre-b", type=int, default=None)
    parser.add_argument("--elevator-post-b", type=int, default=None)
    fall_group = parser.add_mutually_exclusive_group()
    fall_group.add_argument("--fall-wait", dest="emit_fall_wait",
                            action="store_true", default=None)
    fall_group.add_argument("--no-fall-wait", dest="emit_fall_wait",
                            action="store_false")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S")

    try:
        stats = convert_cache_to_json(
            wt_path=args.wt.expanduser().resolve(),
            out_path=args.out.expanduser().resolve(),
            prefer_community=args.prefer_community, info=args.info,
            mode=args.selection_mode or SELECTION_MODE,
            use_action_ticks=args.use_action_ticks,
            emit_elevator_wait=args.emit_elevator_wait,
            emit_fall_wait=args.emit_fall_wait,
            elevator_pre_b=args.elevator_pre_b,
            elevator_post_b=args.elevator_post_b)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130
    log.info("converted %d record(s) -> %s", stats.records_written, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

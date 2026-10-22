#!/usr/bin/env python3
"""
WikiTactics crawl + convert for Order of the Sinking Star.

* https://gist.github.com/joric/03831c74a33e73a0cd4aac96705d0707 crawler.py
* https://gist.github.com/joric/0a95af72111a066ac44a801089c58c1d wt.txt

Stages: root -> puzzle slugs -> /s/ -> published -> convert.

Puzzle slugs come from any embedded JSON catalogue we can find:

* ``<script id="puzzle-index">`` on /community/ carries the full 920-puzzle
  catalogue as ``{"areas":[["slug","Name",[["puzzle-slug",n],...]],...]}``.
* ``<script id="map-data">`` on / carries the map subset as
  ``{"puzzles":[{"i":"puzzle-slug",...,"s":0|1},...]}``.  The ``s`` flag
  means "1 = /s/ solution exists on the site, 0 = none".

Cached /s/ files whose slug is not in any catalogue are reported as
orphans.  Orphans are matched to a catalogued slug by area prefix plus
name similarity, and adopted only when the match is unambiguous; the
orphan's bytes are copied to the parent slug's cache path so the
converter picks it up without changes.

Every puzzle is one record.  For each slug, a single source is chosen:

    * /s/      — the primary solution file, if it exists in the cache
    * published — a community-published solution, if there is no /s/ one
                  (or if --prefer-community is set, when it overrides /s/)

Sources are never combined, and no two slugs are ever joined.  The
"Sources" section of the summary breaks the final count down so it's
obvious where every record came from and how many puzzles remain
unsolved.

The cache mirrors each source host's URL path exactly under cache/:
    https://sinkingstar.wikitactics.com/                      -> cache/sinkingstar.wikitactics.com/index.html
    https://sinkingstar.wikitactics.com/community/            -> cache/sinkingstar.wikitactics.com/community/index.html
    https://sinkingstar.wikitactics.com/s/alter-beam-2.json   -> cache/sinkingstar.wikitactics.com/s/alter-beam-2.json
    https://id.wikitactics.com/api/community/published?...    -> cache/id.wikitactics.com/api/community/published/index.json
                                                               (query-string pages get an index suffix;
                                                                cursor pages go to page-NNNN.json next to it)

Default record: {"name", "note": "", "solution"}.
With --info: adds "url", "json", "note" (raw moves), timing arrays,
and source metadata.

Non-empty cache files are never re-fetched; zero-byte files are deleted.

Usage:
    crawler.py [--convert-only | --no-convert] [--no-published]
               [--refresh-published] [--info]
               [--out PATH] [--wt PATH]
               [--prefer-community] [-v]

Mapping table (wt.txt): level_name,area_slug,puzzle_slug
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import logging
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote, quote

import requests
from bs4 import BeautifulSoup

BASE = "https://sinkingstar.wikitactics.com"
PUBLISHED_BASE = "https://id.wikitactics.com/api/community/published"

ROOT_PAGE = f"{BASE}/"
COMMUNITY_PAGE = f"{BASE}/community/"

# Pages we try to pull puzzle slugs from.  Order matters: the first
# source that yields slugs wins, and later sources are only consulted
# if the earlier ones came up empty.
INDEX_PAGES = [COMMUNITY_PAGE, ROOT_PAGE]

SOLUTION_URL_FMT = BASE + "/s/{slug}.json"

PUBLISHED_PAGE_URL_FMT = (
    PUBLISHED_BASE
    + "?site=sinkingstar&type=solution&limit={limit}{cursor}"
)
PUBLISHED_PAGE_LIMIT = 200

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"

USER_AGENT = (
    "WikiTactics-Crawler/1.6 "
    "(+https://github.com/example/wikitactics-crawler)"
)
DEFAULT_WORKERS = 8
DEFAULT_DELAY = 0.15
MAX_RETRIES = 4
TIMEOUT = (10, 25)
ATTEMPT_WALL_CLOCK = 45
DEFAULT_HEARTBEAT = 2.0
DEFAULT_STALL_WARN = 15.0

ORPHAN_MIN_RATIO = 0.55
ORPHAN_MIN_MARGIN = 0.10

log = logging.getLogger("crawler")
_tls = threading.local()


def get_session() -> requests.Session:
    session = getattr(_tls, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        _tls.session = session
    return session


def sleep_jitter(base: float) -> None:
    time.sleep(base * (0.5 + random.random()))


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #

@dataclass
class StageProgress:
    name: str = ""
    total: int = 0
    done: int = 0
    new: int = 0
    cached: int = 0
    failed: int = 0
    in_flight: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    def rate(self) -> float:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return self.done / elapsed if elapsed > 0 and self.done else 0.0

    def eta(self) -> float | None:
        rate = self.rate()
        if rate <= 0 or self.total <= 0 or self.done >= self.total:
            return None
        return (self.total - self.done) / rate


class ProgressBoard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stages: dict[str, StageProgress] = {}
        self._last_activity = time.time()
        self._started_at = time.time()
        self._inflight: dict[int, tuple[str, str]] = {}
        self._global_total = 0
        self._global_done = 0

    def set_global(self, total: int) -> None:
        with self._lock:
            self._global_total = total

    def bump_global_done(self) -> None:
        with self._lock:
            self._global_done += 1

    def open_stage(self, key: str, name: str, total: int) -> None:
        with self._lock:
            self._stages[key] = StageProgress(
                name=name, total=total, started_at=time.time()
            )

    def close_stage(self, key: str) -> None:
        with self._lock:
            stage = self._stages.get(key)
            if stage:
                stage.finished_at = time.time()
                stage.in_flight = 0

    def stage_tick(self, key: str, *, status: str) -> None:
        with self._lock:
            stage = self._stages.get(key)
            if stage is None:
                return
            stage.done += 1
            if status == "new":
                stage.new += 1
            elif status in ("cached", "skip"):
                stage.cached += 1
            elif status == "failed":
                stage.failed += 1
            self._last_activity = time.time()

    def stage_inflight(self, key: str, delta: int) -> None:
        with self._lock:
            stage = self._stages.get(key)
            if stage:
                stage.in_flight += delta

    def track_start(self, stage: str, url: str) -> None:
        with self._lock:
            self._inflight[threading.get_ident()] = (stage, url)

    def track_end(self) -> None:
        with self._lock:
            self._inflight.pop(threading.get_ident(), None)

    def mark_activity(self) -> None:
        with self._lock:
            self._last_activity = time.time()

    def snapshot(self):
        with self._lock:
            stages = [
                StageProgress(**vars(stage))
                for stage in self._stages.values()
            ]
            return (
                stages,
                list(self._inflight.values()),
                time.time() - self._last_activity,
                time.time() - self._started_at,
                self._global_done,
                self._global_total,
            )


PROGRESS = ProgressBoard()


def _fmt_eta(eta: float | None) -> str:
    if eta is None:
        return "—"
    if eta < 60:
        return f"{eta:4.0f}s"
    if eta < 3600:
        return f"{eta / 60:4.1f}m"
    return f"{eta / 3600:4.1f}h"


def _heartbeat_loop(
    stop_evt: threading.Event, interval: float, stall_warn: float
) -> None:
    while not stop_evt.wait(interval):
        stages, inflight, since, elapsed, gdone, gtotal = PROGRESS.snapshot()
        parts = []
        for stage in stages:
            if stage.total > 0 and stage.done >= stage.total and stage.finished_at:
                continue
            parts.append(
                f"{stage.name}: {stage.done}/{stage.total}"
                f" (+{stage.new} new, {stage.cached} cached,"
                f" {stage.failed} fail, {stage.in_flight} inflight,"
                f" {stage.rate():.1f}/s, eta {_fmt_eta(stage.eta())})"
            )
        if gtotal:
            parts.append(f"TOTAL {gdone}/{gtotal}")
        log.info("[%6.1fs] %s", elapsed, " | ".join(parts) or "idle")
        if since >= stall_warn and inflight:
            log.warning(
                "no completed requests for %.0fs; in-flight URLs:", since
            )
            for stage_name, url in inflight[:20]:
                log.warning("   [%s] %s", stage_name, url)
            if len(inflight) > 20:
                log.warning("   ... and %d more", len(inflight) - 20)


# --------------------------------------------------------------------------- #
# Cache paths
# --------------------------------------------------------------------------- #

def cache_path_for_url(url: str) -> Path:
    """Map a URL to a path under cache/, preserving host and URL path.

    https://sinkingstar.wikitactics.com/                     -> cache/sinkingstar.wikitactics.com/index.html
    https://sinkingstar.wikitactics.com/community/           -> cache/sinkingstar.wikitactics.com/community/index.html
    https://sinkingstar.wikitactics.com/s/alter-beam-2.json  -> cache/sinkingstar.wikitactics.com/s/alter-beam-2.json
    https://id.wikitactics.com/api/community/published?x=y   -> cache/id.wikitactics.com/api/community/published/index.html
    """
    parsed = urlparse(url)
    host = parsed.netloc
    path = unquote(parsed.path or "/")
    if path.endswith("/"):
        path += "index.html"
    else:
        suffix = Path(path).suffix
        if not suffix:
            path = path.rstrip("/") + "/index.html"
    return CACHE_DIR / host / Path(path.lstrip("/"))


def solution_cache_path(slug: str) -> Path:
    """Solutions live at /s/<slug>.json on sinkingstar.wikitactics.com."""
    return cache_path_for_url(SOLUTION_URL_FMT.format(slug=slug))


def published_page_cache_path(index: int) -> Path:
    """Published catalogue pages sit next to the base endpoint as page-NNNN.json."""
    base = cache_path_for_url(PUBLISHED_BASE).parent
    return base / f"page-{index:04d}.json"


def published_index_cache_path() -> Path:
    return cache_path_for_url(PUBLISHED_BASE)


def solutions_cache_dir() -> Path:
    return cache_path_for_url(SOLUTION_URL_FMT.format(slug="_")).parent


def _is_usable_file(path: Path) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_size == 0:
        try:
            path.unlink()
            log.warning("removed zero-byte cache file: %s", path)
        except OSError:
            pass
        return False
    return True


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(
        path.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}"
    )
    tmp.write_bytes(data)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None = None
    from_cache: bool = False
    body: bytes | None = None
    error: str | None = None


_404_lock = threading.Lock()
_404_count = 0


def _bump_404() -> None:
    global _404_count
    with _404_lock:
        _404_count += 1


def get_404_count() -> int:
    with _404_lock:
        return _404_count


def _do_get(url: str) -> requests.Response:
    session = get_session()
    result: dict = {}
    done = threading.Event()

    def worker() -> None:
        try:
            result["resp"] = session.get(
                url, timeout=TIMEOUT, allow_redirects=True
            )
        except Exception as exc:  # noqa: BLE001
            result["exc"] = exc
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True).start()
    if not done.wait(timeout=ATTEMPT_WALL_CLOCK):
        raise requests.Timeout(
            f"wall-clock cap {ATTEMPT_WALL_CLOCK}s exceeded for {url}"
        )
    if "exc" in result:
        raise result["exc"]
    return result["resp"]


def fetch_once(
    url: str, *, delay: float, expect_json: bool, stage_key: str
) -> FetchResult:
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            sleep_jitter(delay)
            PROGRESS.track_start(stage_key, url)
            try:
                resp = _do_get(url)
            finally:
                PROGRESS.track_end()
                PROGRESS.mark_activity()
            if resp.status_code == 404:
                _bump_404()
                return FetchResult(
                    url, ok=False, status=404, error="not found"
                )
            if resp.status_code >= 500 or resp.status_code == 429:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            body = resp.content
            if expect_json:
                try:
                    json.loads(body)
                except ValueError as exc:
                    raise ValueError(
                        f"invalid JSON from {url}: {exc}"
                    ) from exc
            return FetchResult(
                url, ok=True, status=resp.status_code, body=body
            )
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            backoff = min(30.0, (2 ** attempt) + random.random())
            log.debug(
                "attempt %d/%d failed for %s: %s (retry in %.1fs)",
                attempt, MAX_RETRIES, url, exc, backoff,
            )
            time.sleep(backoff)
    return FetchResult(url, ok=False, error=str(last_err))


def fetch(
    url: str,
    dest: Path,
    *,
    delay: float = DEFAULT_DELAY,
    expect_json: bool = False,
    stage_key: str = "generic",
    force: bool = False,
) -> FetchResult:
    if not force and _is_usable_file(dest):
        body = None
        if expect_json:
            try:
                body = dest.read_bytes()
            except OSError:
                pass
        return FetchResult(url=url, ok=True, from_cache=True, body=body)
    res = fetch_once(
        url, delay=delay, expect_json=expect_json, stage_key=stage_key
    )
    if res.ok and res.body is not None:
        write_atomic(dest, res.body)
    return res


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

PUZZLE_INDEX_RE = re.compile(
    r'<script[^>]*id=["\']puzzle-index["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)
MAP_DATA_RE = re.compile(
    r'<script[^>]*id=["\']map-data["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)
PUZZLE_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
AREA_PATH_RE = re.compile(r"^/areas/([^/]+)/?$")
PLAY_ANCHOR_RE = re.compile(r"^([A-Za-z0-9_\-]+?)-play$")


def _slugs_from_puzzle_index(html: str) -> list[str]:
    """Extract slugs from the puzzle-index embedded JSON."""
    match = PUZZLE_INDEX_RE.search(html)
    if not match:
        return []
    try:
        payload = json.loads(match.group(1).strip())
    except ValueError as exc:
        log.error("failed to parse puzzle-index JSON: %s", exc)
        return []

    areas = payload.get("areas")
    if not isinstance(areas, list):
        log.error("puzzle-index has no 'areas' array")
        return []

    slugs: list[str] = []
    seen: set[str] = set()
    for area in areas:
        if not isinstance(area, list) or len(area) < 3:
            continue
        puzzles = area[2]
        if not isinstance(puzzles, list):
            continue
        for entry in puzzles:
            if not isinstance(entry, list) or not entry:
                continue
            slug = entry[0]
            if not isinstance(slug, str) or not slug:
                continue
            if not PUZZLE_ID_SAFE_RE.match(slug):
                log.warning("skipping suspicious puzzle id: %r", slug)
                continue
            if slug in seen:
                continue
            seen.add(slug)
            slugs.append(slug)
    return slugs


def _slugs_from_map_data(html: str) -> list[str]:
    """Extract slugs from the map-data embedded JSON."""
    match = MAP_DATA_RE.search(html)
    if not match:
        return []
    try:
        payload = json.loads(match.group(1).strip())
    except ValueError as exc:
        log.error("failed to parse map-data JSON: %s", exc)
        return []

    puzzles = payload.get("puzzles")
    if not isinstance(puzzles, list):
        log.error("map-data has no 'puzzles' array")
        return []

    slugs: list[str] = []
    seen: set[str] = set()
    for entry in puzzles:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("i")
        if not isinstance(slug, str) or not slug:
            continue
        if not PUZZLE_ID_SAFE_RE.match(slug):
            log.warning("skipping suspicious puzzle id: %r", slug)
            continue
        if slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


def extract_map_data_flags(html: str) -> dict[str, int]:
    """Return {slug: s} from map-data, where s=1 means /s/ exists."""
    match = MAP_DATA_RE.search(html)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1).strip())
    except ValueError:
        return {}
    puzzles = payload.get("puzzles")
    if not isinstance(puzzles, list):
        return {}
    flags: dict[str, int] = {}
    for entry in puzzles:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("i")
        s = entry.get("s")
        if isinstance(slug, str) and isinstance(s, int):
            flags[slug] = s
    return flags


def _slugs_from_play_anchors(html: str, page_url: str) -> list[str]:
    """Extract puzzle slugs from ``#<slug>-play`` anchors and /s/ links."""
    soup = BeautifulSoup(html, "html.parser")
    slugs: list[str] = []
    seen: set[str] = set()

    def _add(slug: str | None) -> None:
        if not slug or not isinstance(slug, str):
            return
        if not PUZZLE_ID_SAFE_RE.match(slug):
            return
        if slug in seen:
            return
        seen.add(slug)
        slugs.append(slug)

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        parsed = urlparse(urljoin(page_url, href))
        if parsed.fragment:
            match = PLAY_ANCHOR_RE.match(parsed.fragment)
            if match:
                _add(match.group(1))
        path = parsed.path
        if path.startswith("/s/") and path.endswith(".json"):
            _add(path[len("/s/"):-len(".json")])

    return slugs


def extract_puzzle_slugs(
    html: str, page_url: str
) -> tuple[str, list[str]]:
    """Return (source_name, slugs) for one index page."""
    for name, fn in (
        ("puzzle-index", _slugs_from_puzzle_index),
        ("map-data", _slugs_from_map_data),
    ):
        slugs = fn(html)
        if slugs:
            return name, slugs

    slugs = _slugs_from_play_anchors(html, page_url)
    if slugs:
        return "play-anchors", slugs

    return "none", []


def extract_area_links(html: str, page_url: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip().split("#", 1)[0].split("?", 1)[0]
        if not href:
            continue
        parsed = urlparse(urljoin(page_url, href))
        if parsed.netloc != urlparse(BASE).netloc:
            continue
        if AREA_PATH_RE.match(parsed.path):
            found.add(f"{BASE}{parsed.path.rstrip('/')}/")
    return found


# --------------------------------------------------------------------------- #
# Orphans
# --------------------------------------------------------------------------- #

def _area_of_slug(slug: str) -> str:
    """Return the slug without its trailing -<n> if it looks numbered."""
    m = re.match(r"^(.+?)-\d+$", slug)
    return m.group(1) if m else slug


def _list_cached_solution_slugs() -> list[str]:
    directory = solutions_cache_dir()
    if not directory.is_dir():
        return []
    out: list[str] = []
    for path in sorted(directory.glob("*.json")):
        stem = path.stem
        if PUZZLE_ID_SAFE_RE.match(stem) and _is_usable_file(path):
            out.append(stem)
    return out


def find_orphan_solutions(known_slugs: set[str]) -> list[str]:
    """Return cached /s/ slugs that are not in the discovered set."""
    cached = _list_cached_solution_slugs()
    return [s for s in cached if s not in known_slugs]


def attach_orphans(
    orphans: list[str],
    known_slugs: list[str],
    stats: "CrawlStats",
) -> dict[str, str]:
    """Try to adopt orphan slugs onto catalogued slugs by name similarity."""
    adopted: dict[str, str] = {}
    stats.orphans_found = len(orphans)
    stats.orphans_adopted = 0
    stats.orphans_unmatched = []
    if not orphans:
        return adopted

    by_area: dict[str, list[str]] = {}
    for slug in known_slugs:
        by_area.setdefault(_area_of_slug(slug), []).append(slug)

    adopted_targets: set[str] = set()

    for orphan in orphans:
        area = _area_of_slug(orphan)
        candidates = by_area.get(area, [])
        if not candidates:
            log.warning(
                "orphan %s: no catalogued slugs in area %r",
                orphan, area,
            )
            stats.orphans_unmatched.append(orphan)
            continue

        scored: list[tuple[float, str]] = []
        for cand in candidates:
            if cand in adopted_targets:
                continue
            if _is_usable_file(solution_cache_path(cand)):
                continue
            ratio = difflib.SequenceMatcher(None, orphan, cand).ratio()
            scored.append((ratio, cand))

        if not scored:
            log.warning(
                "orphan %s: every candidate in area %r already has a "
                "verified solution", orphan, area,
            )
            stats.orphans_unmatched.append(orphan)
            continue

        scored.sort(reverse=True)
        best_score, best_slug = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0

        if best_score < ORPHAN_MIN_RATIO:
            log.info(
                "orphan %s: best match %s (%.2f) below %.2f; not adopted",
                orphan, best_slug, best_score, ORPHAN_MIN_RATIO,
            )
            stats.orphans_unmatched.append(orphan)
            continue
        if best_score - runner_up < ORPHAN_MIN_MARGIN:
            log.info(
                "orphan %s: ambiguous (best %s %.2f vs runner-up %.2f); "
                "not adopted",
                orphan, best_slug, best_score, runner_up,
            )
            stats.orphans_unmatched.append(orphan)
            continue

        adopted[orphan] = best_slug
        adopted_targets.add(best_slug)
        log.info(
            "orphan %s -> adopted as %s (score %.2f, margin %.2f)",
            orphan, best_slug, best_score, best_score - runner_up,
        )

    stats.orphans_adopted = len(adopted)
    return adopted


def materialise_adopted_orphans(adopted: dict[str, str]) -> list[str]:
    """Copy each adopted orphan's cache file to its parent's cache path."""
    written: list[str] = []
    for orphan, parent in adopted.items():
        src = solution_cache_path(orphan)
        dst = solution_cache_path(parent)
        if _is_usable_file(dst):
            log.debug(
                "adopted %s -> %s: parent already has a file, skipping copy",
                orphan, parent,
            )
            continue
        try:
            data = src.read_bytes()
        except OSError as exc:
            log.warning(
                "adopted %s -> %s: cannot read %s (%s)",
                orphan, parent, src, exc,
            )
            continue
        write_atomic(dst, data)
        written.append(parent)
    return written


# --------------------------------------------------------------------------- #
# Crawl stats
# --------------------------------------------------------------------------- #

@dataclass
class CrawlStats:
    # crawling
    pages_fetched: int = 0
    pages_cached: int = 0
    pages_failed: int = 0
    pub_pages_fetched: int = 0
    pub_pages_cached: int = 0
    pub_pages_failed: int = 0
    pub_entries: int = 0

    # /s/
    sol_new: int = 0
    sol_cached: int = 0
    sol_failed: int = 0
    sol_skipped_s0: int = 0
    unresolved_solutions: list[str] = field(default_factory=list)

    # discovery
    puzzles: set[str] = field(default_factory=set)
    slug_sources: dict[str, str] = field(default_factory=dict)

    # orphans
    orphans_found: int = 0
    orphans_adopted: int = 0
    orphans_unmatched: list[str] = field(default_factory=list)
    adopted_map: dict[str, str] = field(default_factory=dict)

    # converter breakdown — filled by convert_cache_to_json
    out_total: int = 0
    out_from_s: int = 0
    out_from_published: int = 0
    out_overlap_published: int = 0
    out_missing: int = 0
    out_missing_s0: int = 0
    out_missing_404: int = 0

    def summary(self, *, prefer_community: bool) -> str:
        total = len(self.puzzles)
        s_resolved = (
            total
            - len(self.unresolved_solutions)
            - self.sol_skipped_s0
        )
        lines = [
            "",
            "=== Crawl summary ===",
            "",
            "Discovery",
            f"  puzzles in catalogue:   {total}",
        ]
        if self.slug_sources:
            for src, desc in self.slug_sources.items():
                lines.append(f"    {src}")
                lines.append(f"      -> {desc}")
        lines += [
            "",
            "Crawl",
            f"  index pages:            {self.pages_fetched} new, "
            f"{self.pages_cached} cached, {self.pages_failed} failed",
            f"  published pages:        {self.pub_pages_fetched} new, "
            f"{self.pub_pages_cached} cached, "
            f"{self.pub_pages_failed} failed",
            f"  published entries:      {self.pub_entries}",
            f"  404 responses:          {get_404_count()}",
            "",
            "Sources for /s/",
            f"  already cached:         {self.sol_cached}",
            f"  fetched this run:       {self.sol_new}",
            f"  skipped (s:0):          {self.sol_skipped_s0}",
            f"  failed (non-404):       {self.sol_failed}",
            f"  unavailable (404):      {len(self.unresolved_solutions)}",
            f"  /s/ resolved:           {s_resolved}/{total}",
        ]
        if self.orphans_found:
            lines += [
                "",
                "Orphans",
                f"  found on disk:          {self.orphans_found}",
                f"  adopted onto a parent:  {self.orphans_adopted}",
                f"  left alone:             {len(self.orphans_unmatched)}",
            ]
            if self.adopted_map:
                for orphan, parent in sorted(self.adopted_map.items()):
                    lines.append(f"    {orphan}  ->  {parent}")
            if self.orphans_unmatched:
                lines.append("  unmatched:")
                for slug in sorted(self.orphans_unmatched):
                    lines.append(f"    {slug}")
        if self.out_total:
            policy = (
                "published wins over /s/ (--prefer-community)"
                if prefer_community
                else "/s/ wins over published (default)"
            )
            lines += [
                "",
                "Output (solutions.json)",
                f"  policy:                 {policy}",
                f"  records written:        {self.out_total}/{total}",
                f"    from /s/:             {self.out_from_s}",
                f"    from published only:  {self.out_from_published}",
                f"    published overridden: {self.out_overlap_published} "
                f"(same slug as /s/)",
                f"  still unsolved:         {self.out_missing}/{total}",
            ]
            if self.out_missing_s0 or self.out_missing_404:
                lines.append(
                    f"    of which s:0 (no /s/ on site):    "
                    f"{self.out_missing_s0}"
                )
                lines.append(
                    f"    of which /s/ 404, no published:  "
                    f"{self.out_missing_404}"
                )
        lines += ["", f"Cache dir:               {CACHE_DIR}"]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Index pages
# --------------------------------------------------------------------------- #

def _load_or_fetch_page(
    url: str, *, delay: float, stage_key: str, stats: CrawlStats
) -> bytes | None:
    dest = cache_path_for_url(url)
    PROGRESS.stage_inflight(stage_key, +1)
    try:
        res = fetch(url, dest, delay=delay, stage_key=stage_key)
    finally:
        PROGRESS.stage_inflight(stage_key, -1)
    if not res.ok:
        stats.pages_failed += 1
        PROGRESS.stage_tick(stage_key, status="failed")
        return None
    if res.from_cache:
        stats.pages_cached += 1
        PROGRESS.stage_tick(stage_key, status="cached")
    else:
        stats.pages_fetched += 1
        PROGRESS.stage_tick(stage_key, status="new")
    try:
        return dest.read_bytes()
    except OSError as exc:
        log.error("failed to read cached %s: %s", dest, exc)
        return None


def stage_index_pages(
    workers: int, delay: float, stats: CrawlStats
) -> dict[str, bytes]:
    """Fetch all index pages.  Returns url -> html bytes."""
    PROGRESS.open_stage("index", "index", len(INDEX_PAGES))
    results: dict[str, bytes] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _load_or_fetch_page,
                url,
                delay=delay,
                stage_key="index",
                stats=stats,
            ): url
            for url in INDEX_PAGES
        }
        for fut in as_completed(futures):
            html = fut.result()
            if html is not None:
                results[futures[fut]] = html
    PROGRESS.close_stage("index")
    return results


def stage_area_pages(
    area_urls: list[str], workers: int, delay: float, stats: CrawlStats
) -> dict[str, bytes]:
    """Fetch area pages to recover puzzle slugs the embedded indexes missed."""
    PROGRESS.open_stage("areas", "areas", len(area_urls))
    results: dict[str, bytes] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _load_or_fetch_page,
                url,
                delay=delay,
                stage_key="areas",
                stats=stats,
            ): url
            for url in area_urls
        }
        for fut in as_completed(futures):
            html = fut.result()
            if html is not None:
                results[futures[fut]] = html
    PROGRESS.close_stage("areas")
    return results


# --------------------------------------------------------------------------- #
# Published catalogue
# --------------------------------------------------------------------------- #

@dataclass
class _PublishedIndex:
    pages: list[int] = field(default_factory=list)
    next_cursor: str | None = None
    fetched_at: str = ""

    @classmethod
    def from_file(cls, path: Path) -> "_PublishedIndex | None":
        try:
            raw = json.loads(path.read_bytes())
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(raw, dict):
            return None
        return cls(
            pages=[int(x) for x in raw.get("pages", [])],
            next_cursor=raw.get("nextCursor"),
            fetched_at=raw.get("fetchedAt", ""),
        )

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "pages": sorted(self.pages),
                "nextCursor": self.next_cursor,
                "fetchedAt": self.fetched_at,
            },
            indent=2,
        ).encode()


def _published_url(cursor: str | None) -> str:
    if cursor:
        return PUBLISHED_PAGE_URL_FMT.format(
            limit=PUBLISHED_PAGE_LIMIT,
            cursor=f"&cursor={quote(cursor, safe='')}",
        )
    return PUBLISHED_PAGE_URL_FMT.format(
        limit=PUBLISHED_PAGE_LIMIT, cursor=""
    )


def stage_published_catalogue(
    workers: int, delay: float, stats: CrawlStats, *, refresh: bool
) -> list[dict]:
    published_dir = published_page_cache_path(0).parent
    published_dir.mkdir(parents=True, exist_ok=True)
    index_path = published_index_cache_path()
    index = _PublishedIndex.from_file(index_path)

    if refresh:
        for path in sorted(published_dir.glob("page-*.json")):
            try:
                path.unlink()
            except OSError:
                pass
        index = None

    if index is not None and index.next_cursor is None and index.pages:
        entries = _load_published_pages(index.pages)
        if entries:
            stats.pub_pages_cached = len(index.pages)
            stats.pub_entries = len(entries)
            log.info(
                "published: complete in cache (%d pages, %d entries)",
                len(index.pages), len(entries),
            )
            PROGRESS.open_stage("published", "published", 0)
            PROGRESS.close_stage("published")
            return entries

    PROGRESS.open_stage("published", "published", 0)
    entries: list[dict] = []
    page_idx = 0
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while True:
        dest = published_page_cache_path(page_idx)
        url = _published_url(cursor)
        PROGRESS.stage_inflight("published", +1)
        try:
            res = fetch(
                url, dest, delay=delay, expect_json=True,
                stage_key="published",
            )
        finally:
            PROGRESS.stage_inflight("published", -1)

        if not res.ok:
            stats.pub_pages_failed += 1
            PROGRESS.stage_tick("published", status="failed")
            log.error(
                "published: page %d failed (%s)",
                page_idx, res.status or res.error,
            )
            break

        if res.from_cache:
            stats.pub_pages_cached += 1
            PROGRESS.stage_tick("published", status="cached")
        else:
            stats.pub_pages_fetched += 1
            PROGRESS.stage_tick("published", status="new")

        try:
            payload = json.loads(dest.read_bytes())
        except Exception as exc:  # noqa: BLE001
            log.error("published: page %d unreadable (%s)", page_idx, exc)
            stats.pub_pages_failed += 1
            break

        page_entries = payload.get("entries")
        if not isinstance(page_entries, list):
            log.error(
                "published: page %d missing 'entries' array", page_idx
            )
            break
        entries.extend(e for e in page_entries if isinstance(e, dict))

        next_cursor = payload.get("nextCursor")
        if not next_cursor:
            index = _PublishedIndex(
                pages=sorted(
                    {*([index.pages] if index else []),
                     *range(page_idx + 1)}
                ),
                next_cursor=None,
                fetched_at=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
            )
            write_atomic(index_path, index.to_bytes())
            break

        if next_cursor in seen_cursors:
            log.warning(
                "published: cursor loop detected at page %d; stopping",
                page_idx,
            )
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        page_idx += 1

    PROGRESS.close_stage("published")
    stats.pub_entries = len(entries)
    log.info(
        "published: %d pages (%d new, %d cached), %d entries",
        page_idx + 1, stats.pub_pages_fetched,
        stats.pub_pages_cached, len(entries),
    )
    return entries


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
    published_dir = published_page_cache_path(0).parent
    if not published_dir.is_dir():
        return []
    pages = sorted(
        int(path.stem.split("-", 1)[1])
        for path in published_dir.glob("page-*.json")
    )
    return _load_published_pages(pages)


# --------------------------------------------------------------------------- #
# Solutions
# --------------------------------------------------------------------------- #

def stage_solutions(
    slugs: list[str],
    flags: dict[str, int],
    workers: int,
    delay: float,
    stats: CrawlStats,
) -> None:
    slug_list = sorted(slugs)
    stats.puzzles = set(slug_list)

    needs: list[str] = []
    cached = 0
    skipped_s0 = 0
    for slug in slug_list:
        if flags.get(slug) == 0:
            skipped_s0 += 1
            continue
        if _is_usable_file(solution_cache_path(slug)):
            cached += 1
        else:
            needs.append(slug)

    stats.sol_skipped_s0 = skipped_s0

    PROGRESS.open_stage("solutions", "solutions /s/", len(slug_list))

    for _ in range(cached):
        stats.sol_cached += 1
        PROGRESS.stage_tick("solutions", status="skip")
        PROGRESS.bump_global_done()

    for _ in range(skipped_s0):
        PROGRESS.stage_tick("solutions", status="skip")
        PROGRESS.bump_global_done()

    log.info(
        "solutions /s/: %d total, %d already cached, %d skipped (s:0), "
        "%d to fetch",
        len(slug_list), cached, skipped_s0, len(needs),
    )

    if not needs:
        PROGRESS.close_stage("solutions")
        return

    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for slug in needs:
            url = SOLUTION_URL_FMT.format(slug=slug)
            dest = solution_cache_path(slug)
            PROGRESS.stage_inflight("solutions", +1)
            futures[
                pool.submit(
                    fetch,
                    url,
                    dest,
                    delay=delay,
                    expect_json=True,
                    stage_key="solutions",
                )
            ] = slug

        for fut in as_completed(futures):
            slug = futures[fut]
            PROGRESS.stage_inflight("solutions", -1)
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                stats.sol_failed += 1
                stats.unresolved_solutions.append(slug)
                PROGRESS.stage_tick("solutions", status="failed")
                PROGRESS.bump_global_done()
                log.debug("solutions worker crashed on %s: %s", slug, exc)
                continue
            if res.ok:
                if res.from_cache:
                    stats.sol_cached += 1
                    PROGRESS.stage_tick("solutions", status="cached")
                else:
                    stats.sol_new += 1
                    PROGRESS.stage_tick("solutions", status="new")
            else:
                if res.status == 404:
                    stats.unresolved_solutions.append(slug)
                    PROGRESS.stage_tick("solutions", status="skip")
                else:
                    stats.sol_failed += 1
                    stats.unresolved_solutions.append(slug)
                    PROGRESS.stage_tick("solutions", status="failed")
            PROGRESS.bump_global_done()

    PROGRESS.close_stage("solutions")
    log.info(
        "solutions /s/ complete in %.1fs (404s so far: %d)",
        time.time() - start, get_404_count(),
    )
    resolved = (
        len(slug_list)
        - len(stats.unresolved_solutions)
        - skipped_s0
    )
    log.info(
        "solutions resolved: %d/%d (%d skipped via s:0, %d 404)",
        resolved, len(slug_list), skipped_s0,
        len(stats.unresolved_solutions),
    )


# --------------------------------------------------------------------------- #
# Converter
# --------------------------------------------------------------------------- #

# Classes that map to a real playable character slot number (1..9/C).
CHAR_TO_NUM = {
    "warrior": "1",
    "thief": "2",
    "wizard": "3",
    "priestess": "4",
    "bard": "5",
    "druid": "6",
    "beamer": "8",
    "trader": "9",
    "queen": "C",
}

# "pusher" has no fixed character slot: the same pusher action can be
# performed by warrior / trader / etc., so it cannot be selected with a
# digit.  If any move uses it, the whole level falls back to C cycling.
NON_PLAYABLE_CLASSES = {"pusher"}


DIR_TO_CHAR = {
    "N": "U", "S": "D", "E": "R", "W": "L",
    "A": "X", "T": "V", "F": "F",
    "tick": "B", "wait": "BB", "pause": "BBBB",
    "A@1": "X", "A@2": "X", "A@3": "X", "A@4": "X",
    "A@5": "X", "A@6": "X", "A@7": "X", "A@8": "X",
}

# ---- Wait macros ----------------------------------------------------------
#
# Both waits are functions of the elevation change (dz = z_after -
# z_before) seen on the frame after the move.  Negative dz = falling
# (ledge drop, drop trap, elevator going down); positive dz = riding up.
#
# Rule of thumb: 4 B minimum, 8 B maximum, linear in between, clamped
# outside [1, WAIT_MAX_LEVEL].  The B-count is the total wait (the "V"
# press itself is not counted as a B).
#
# Elevator T moves are special: the macro also contains the "V" press
# itself, since the caller emits it as part of the wait rather than as
# DIR_TO_CHAR["T"].

WAIT_MIN_B = 4
WAIT_MAX_B = 8
WAIT_MAX_LEVEL = 2         # |dz| at which the wait saturates
ELEVATOR_PRESS_CHAR = "V"


def _wait_bs(levels: int) -> int:
    """Interpolate B-count: WAIT_MIN_B at 1, WAIT_MAX_B at WAIT_MAX_LEVEL."""
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
    """Return the wait string to append after a move that dropped by dz."""
    if dz >= 0:
        return ""
    return "B" * _wait_bs(abs(dz))


def elevator_wait(dz: int) -> str:
    """Return the elevator macro for a T move with elevation change dz.

    The V press sits in the middle of the wait; the pre/post split is
    half-and-half (pre gets the floor).  Returns "" when dz == 0.
    """
    if dz == 0:
        return ""
    total = _wait_bs(abs(dz))
    pre = total // 2
    post = total - pre
    return ("B" * pre) + ELEVATOR_PRESS_CHAR + ("B" * post)


QUANTUM_US = 150_000
MIN_EMIT_US = 150_000


def us_to_bs(us: int | None) -> int:
    if us is None or us < MIN_EMIT_US:
        return 0
    return max(1, int(round(us / QUANTUM_US)))


def _timing_for_move(timing: dict | None, i: int) -> tuple[int | None, int]:
    return [], 0  # early exit for now
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


def _collect_ordered_chars(moves: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for move in moves:
        if ":" not in move:
            continue
        char_name, _ = move.split(":", 1)
        if char_name not in seen:
            seen.add(char_name)
            ordered.append(char_name)
    return ordered


def _pusher_z(frame: list) -> int | None:
    """Return the z-coordinate of the pusher in a frame, or None."""
    for obj in frame:
        if obj.get("k") == "pusher":
            p = obj.get("p") or []
            if len(p) >= 3:
                return p[2]
    return None


def move_deltas(moves: list[str], frames: list | None) -> dict[int, int]:
    """Return {move_index: dz} for moves whose z changes on the next frame.

    dz = z_after - z_before.  Positive = ascended, negative = fell.
    Moves with no z change (or missing frames) are omitted.
    """
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


def convert_moves(
    moves: list[str],
    level: str,
    timing: dict | None = None,
    deltas: dict[int, int] | None = None,
) -> str:
    if not moves:
        return ""

    deltas = deltas or {}

    needs_cycle = not all(
        _has_digit_key(m.split(":", 1)[0])
        for m in moves
        if ":" in m
    )

    result: list[str] = []
    current_char: str | None = None

    if needs_cycle:
        # In cycle mode, each distinct character class gets its own
        # rotation slot, ordered by first appearance.  Multiple
        # instances of the same class share a slot (cycling cannot
        # distinguish them anyway).
        ordered: list[str] = []
        slot_of: dict[str, int] = {}
        for move in moves:
            if ":" not in move:
                continue
            cls = _move_class(move.split(":", 1)[0])
            if cls not in slot_of:
                slot_of[cls] = len(ordered)
                ordered.append(cls)
        n = len(ordered)

        for i, move in enumerate(moves):
            if ":" not in move:
                continue
            char_name, action = move.split(":", 1)
            cls = _move_class(char_name)

            if cls != current_char:
                is_first = not result
                target = slot_of[cls]
                current = 0 if current_char is None else slot_of[current_char]
                presses = (target - current) % n if n > 1 else 0

                if not (is_first and presses == 0):
                    result.append("C" * presses)
                current_char = cls

            dz = deltas.get(i)
            if action == "T" and dz is not None:
                # The V press lives inside the elevator macro.
                result.append(elevator_wait(dz))
            else:
                result.append(
                    DIR_TO_CHAR.get(
                        action, action if action.isdigit() else "?"
                    )
                )
                if dz is not None and dz < 0:
                    result.append(fall_wait(dz))

        return "".join(result)

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

    return "".join(result)


def _load_wt_table(path: Path) -> dict[str, tuple[str, str]]:
    table: dict[str, tuple[str, str]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) >= 3:
                table[row[0]] = (row[1], row[2])
    return table


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
    deltas = move_deltas(moves, frames)

    if source_kind == "published":
        json_url = PUBLISHED_BASE + "?site=sinkingstar&type=solution"
    else:
        json_url = SOLUTION_URL_FMT.format(slug=slug)

    record = {
        "name": level,
        "note": " ".join(moves) if info else "",
        "solution": convert_moves(
            moves, level, timing, deltas=deltas,
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


def convert_cache_to_json(
    *,
    wt_path: Path,
    out_path: Path,
    prefer_community: bool = False,
    info: bool = False,
    stats: "CrawlStats | None" = None,
) -> int:
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
    # Track where each output record came from, per slug, so we can
    # report the breakdown in the summary.
    source_of: dict[str, str] = {}  # level name -> "s" | "published"
    published_overlap = 0

    def _upsert(
        level: str, rec: dict, source: str
    ) -> bool:
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

    pub_added = pub_skipped_unmapped = 0
    for entry in published:
        pid = entry.get("puzzleId")
        if not pid or pid not in levels_by_slug:
            pub_skipped_unmapped += 1
            continue
        level, (area, slug) = levels_by_slug[pid]
        rec = _build_record(level, area, slug, entry, "published", info=info)
        current = source_of.get(level)
        if current is not None and not prefer_community:
            # published entry maps to a level that /s/ already filled;
            # default policy discards it.
            published_overlap += 1
            continue
        if _upsert(level, rec, "published"):
            pub_added += 1

    primary_found = primary_missing = 0
    primary_missing_slugs: list[str] = []
    for level, (area, slug) in levels.items():
        path = solution_cache_path(slug)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            primary_missing += 1
            primary_missing_slugs.append(slug)
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("convert: unreadable %s (%s)", path, exc)
            primary_missing += 1
            primary_missing_slugs.append(slug)
            continue
        rec = _build_record(level, area, slug, data, "s", info=info)
        existing = by_name.get(level)
        if existing is None:
            records.append(rec)
            by_name[level] = rec
            source_of[level] = "s"
            primary_found += 1
        elif not prefer_community:
            records[records.index(existing)] = rec
            by_name[level] = rec
            source_of[level] = "s"
            primary_found += 1
        else:
            primary_found += 1

    records.sort(key=lambda r: r["name"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)

    # ---- source breakdown -------------------------------------------------
    n_from_s = sum(1 for v in source_of.values() if v == "s")
    n_from_pub = sum(1 for v in source_of.values() if v == "published")

    log.info(
        "converter: %d records written "
        "(from /s/: %d, from published: %d, published overlap dropped: %d)",
        len(records), n_from_s, n_from_pub, published_overlap,
    )
    log.info(
        "converter: /s/ cache: %d found, %d missing "
        "(published added %d, unmapped %d)",
        primary_found, primary_missing, pub_added, pub_skipped_unmapped,
    )

    if stats is not None:
        stats.out_total = len(records)
        stats.out_from_s = n_from_s
        stats.out_from_published = n_from_pub
        stats.out_overlap_published = published_overlap
        stats.out_missing = (
            len(levels) - len(records)
        )
        # Split the missing ones by reason: map-data s:0, or /s/ 404.
        missing_slugs = set(primary_missing_slugs)
        missing_s0 = 0
        missing_404 = 0
        for level, (_area, slug) in levels.items():
            if level in by_name:
                continue
            # was this slug skipped as s:0 during the crawl?
            # We don't have flags here, so approximate: if the slug is in
            # unresolved_solutions it 404'd; otherwise assume s:0.
            if slug in set(stats.unresolved_solutions):
                missing_404 += 1
            else:
                missing_s0 += 1
        stats.out_missing_s0 = missing_s0
        stats.out_missing_404 = missing_404

    return len(records)


# --------------------------------------------------------------------------- #
# Crawl driver
# --------------------------------------------------------------------------- #

def discover_puzzle_slugs(
    workers: int, delay: float, stats: CrawlStats
) -> tuple[list[str], dict[str, int]]:
    """Return (all_slugs, known_flags)."""
    slugs: list[str] = []
    seen: set[str] = set()
    flags: dict[str, int] = {}

    def _merge(source: str, new_slugs: list[str]) -> None:
        added = 0
        for slug in new_slugs:
            if slug in seen:
                continue
            seen.add(slug)
            slugs.append(slug)
            added += 1
        if new_slugs:
            stats.slug_sources[source] = (
                f"{len(new_slugs)} slugs"
                + (f" ({added} new)" if added != len(new_slugs) else "")
            )
        log.info("%s -> %d slugs (%d new)", source, len(new_slugs), added)

    log.info("== Stage 1: index pages ==")
    index_html = stage_index_pages(workers, delay, stats)
    for url in INDEX_PAGES:
        html = index_html.get(url)
        if html is None:
            continue
        text = html.decode("utf-8", "replace")
        source, found = extract_puzzle_slugs(text, url)
        if found:
            _merge(f"{url} ({source})", found)
        for slug, s in extract_map_data_flags(text).items():
            flags[slug] = s

    if not slugs:
        log.info("== Stage 2: area pages (no embedded catalogue found) ==")
        area_urls: set[str] = set()
        for url, html in index_html.items():
            area_urls.update(
                extract_area_links(
                    html.decode("utf-8", "replace"), url
                )
            )
        if not area_urls:
            log.error("no area links found; cannot discover puzzles")
            return [], flags

        log.info("discovered %d area pages", len(area_urls))
        area_html = stage_area_pages(
            sorted(area_urls), workers, delay, stats
        )
        for url, html in area_html.items():
            text = html.decode("utf-8", "replace")
            _, found = extract_puzzle_slugs(text, url)
            if found:
                _merge(url, found)
            for slug, s in extract_map_data_flags(text).items():
                flags[slug] = s

    return slugs, flags


def crawl(
    workers: int = DEFAULT_WORKERS,
    delay: float = DEFAULT_DELAY,
    refresh_published: bool = False,
    no_published: bool = False,
) -> CrawlStats:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    log.info("cache root: %s", CACHE_DIR)
    stats = CrawlStats()

    puzzle_slugs, flags = discover_puzzle_slugs(workers, delay, stats)
    if not puzzle_slugs:
        log.error("no puzzle slugs discovered; aborting")
        return stats
    log.info("discovered %d unique puzzle slugs", len(puzzle_slugs))

    known_set = set(puzzle_slugs)
    orphans = find_orphan_solutions(known_set)
    if orphans:
        log.info(
            "== Orphan /s/ files: %d cached but not in catalogue ==",
            len(orphans),
        )
        for slug in orphans:
            log.info("  orphan: %s", slug)
        adopted = attach_orphans(orphans, puzzle_slugs, stats)
        stats.adopted_map = dict(adopted)
        if adopted:
            written = materialise_adopted_orphans(adopted)
            log.info(
                "adopted %d orphan(s); materialised %d into parent slugs",
                len(adopted), len(written),
            )
        else:
            log.info("no orphan could be adopted unambiguously")

    no_solution = sum(1 for s in puzzle_slugs if flags.get(s) == 0)
    log.info("%d slugs have no /s/ solution (s:0)", no_solution)

    PROGRESS.set_global(len(puzzle_slugs))

    if no_published:
        log.info(
            "== Stage 3: published catalogue "
            "(disabled by --no-published) =="
        )
        PROGRESS.open_stage("published", "published", 0)
        PROGRESS.close_stage("published")
    else:
        log.info("== Stage 3: published catalogue ==")
        stage_published_catalogue(
            workers, delay, stats, refresh=refresh_published
        )

    log.info("== Stage 4: solutions /s/ ==")
    stage_solutions(puzzle_slugs, flags, workers, delay, stats)

    manifest = {
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
        "index_pages": INDEX_PAGES,
        "endpoints": {
            "published": PUBLISHED_PAGE_URL_FMT.format(
                limit=PUBLISHED_PAGE_LIMIT, cursor="{cursor}"
            ),
            "solutions": SOLUTION_URL_FMT.format(slug="{slug}"),
        },
        "slug_sources": dict(stats.slug_sources),
        "orphans": {
            "found": stats.orphans_found,
            "adopted": stats.orphans_adopted,
            "unmatched": sorted(stats.orphans_unmatched),
            "adoptions": dict(stats.adopted_map),
        },
        "puzzles": sorted(stats.puzzles),
        "missing_solutions": sorted(stats.unresolved_solutions),
        "counts": {
            "puzzles": len(stats.puzzles),
            "published_pages_fetched": stats.pub_pages_fetched,
            "published_pages_cached": stats.pub_pages_cached,
            "published_pages_failed": stats.pub_pages_failed,
            "published_entries": stats.pub_entries,
            "solutions_new": stats.sol_new,
            "solutions_cached": stats.sol_cached,
            "solutions_failed": stats.sol_failed,
            "solutions_skipped_s0": stats.sol_skipped_s0,
            "pages_fetched": stats.pages_fetched,
            "pages_cached": stats.pages_cached,
            "http_404": get_404_count(),
        },
    }
    write_atomic(
        CACHE_DIR / "manifest.json",
        json.dumps(manifest, indent=2).encode(),
    )
    log.info("wrote manifest -> %s", CACHE_DIR / "manifest.json")
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
        "-w", "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"parallel threads (default {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "-d", "--delay", type=float, default=DEFAULT_DELAY,
        help=f"per-thread delay between requests, seconds "
             f"(default {DEFAULT_DELAY})",
    )
    parser.add_argument(
        "--no-published", action="store_true",
        help="skip the published-solutions catalogue crawl",
    )
    parser.add_argument(
        "--refresh-published", action="store_true",
        help="re-fetch the published catalogue from page 1",
    )
    parser.add_argument(
        "--heartbeat", type=float, default=DEFAULT_HEARTBEAT,
        help=f"seconds between progress lines "
             f"(default {DEFAULT_HEARTBEAT}; 0 disables)",
    )
    parser.add_argument(
        "--stall-warn", type=float, default=DEFAULT_STALL_WARN,
        help=f"warn if no request completes for this many seconds "
             f"(default {DEFAULT_STALL_WARN})",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="show cache hits, retries, and 404 warnings",
    )
    parser.add_argument(
        "--convert-only", action="store_true",
        help="skip the crawl; only rebuild the output file from cache",
    )
    parser.add_argument(
        "--no-convert", action="store_true",
        help="run the crawl but do not write the converted JSON",
    )
    parser.add_argument(
        "--out", type=Path, default=SCRIPT_DIR / "solutions.json",
        help="output JSON path for the converter "
             "(default: solutions.json next to this script)",
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

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("urllib3", "requests", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.convert_only and args.no_convert:
        parser.error(
            "--convert-only and --no-convert are mutually exclusive"
        )

    wt_path = args.wt.expanduser().resolve()
    out_path = args.out.expanduser().resolve()
    if not args.no_convert and not wt_path.is_file():
        log.warning(
            "mapping table not found yet: %s "
            "(converter will fail unless --no-convert is used)",
            wt_path,
        )

    t0 = time.time()
    stats: CrawlStats | None = None

    if not args.convert_only:
        stop_evt = threading.Event()
        hb_thread = None
        if args.heartbeat > 0:
            hb_thread = threading.Thread(
                target=_heartbeat_loop,
                args=(stop_evt, args.heartbeat, args.stall_warn),
                daemon=True,
                name="heartbeat",
            )
            hb_thread.start()
        try:
            stats = crawl(
                workers=args.workers,
                delay=args.delay,
                refresh_published=args.refresh_published,
                no_published=args.no_published,
            )
        except KeyboardInterrupt:
            log.warning("interrupted by user")
            return 130
        finally:
            stop_evt.set()
            if hb_thread:
                hb_thread.join(timeout=1.0)

    rc = 0
    if stats and (
        stats.sol_failed > 0
        or stats.pub_pages_failed > 0
    ):
        rc = 1

    if not args.no_convert:
        try:
            count = convert_cache_to_json(
                wt_path=wt_path,
                out_path=out_path,
                prefer_community=args.prefer_community,
                info=args.info,
                stats=stats,
            )
            log.info("converted %d record(s) -> %s", count, out_path)
        except FileNotFoundError as exc:
            log.error("%s", exc)
            rc = rc or 2
        except KeyboardInterrupt:
            return 130

    if stats is not None:
        log.info(
            stats.summary(prefer_community=args.prefer_community)
        )

    log.info("done in %.1fs", time.time() - t0)
    return rc


if __name__ == "__main__":
    sys.exit(main())

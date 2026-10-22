"""Capture a top-down map of the Order of the Sinking Star overworld (ground layer).

Drives the running game from outside with ReadProcessMemory/WriteProcessMemory: no DLL injection
and no change to the exe. The game renders every tile itself through its own screenshot code;
this script only points the camera, takes the shots and stitches them.

What it does to the running game (restart the game afterwards to undo it all):
  - clears the overworld fog of war and hides the hero
  - freezes simulation time, forces the highest LOD, turns off planar reflections
  - draws every wrap copy of the overworld so the map edges are complete
  - freezes the camera straight down from far above with a narrow field of view (near-orthographic)
  - renders at the tile resolution, without HUD, and saves one game screenshot per tile

Usage (game running on the overworld, any save you don't care about):
  python sinking_star_map_capture.py [--px 128] [--tiles 8 8] [--out map_tiles] [--stitch-only]
  python sinking_star_map_capture.py --make-signatures [--exe PATH]
  python sinking_star_map_capture.py --find-addresses [--exe PATH]

  --px      pixels per grid square in the final map
  --tiles   tile columns and rows; each tile must be a whole number of pixels
  --out     folder for the tiles and the stitched map
  --exe     exe for the two options below (default: the running game's)
  --make-signatures  from an exe whose addresses are known (BUILDS or a saved resolution), save
            code patterns that find the globals again in later builds
            (sinking_star_signatures.json, next to this script)
  --find-addresses   resolve the globals of an exe from those patterns and print them; the game
            is not touched

Game builds: the static globals move with every build. A build listed in BUILDS is used as is. For a
new one the script first tries the saved patterns (sinking_star_signatures.json); without them it
searches the running game for the globals, using the spacing they had in the newest known build.
Either way every address is checked against the live process before anything is written to it, the
result is remembered in the json, and patterns for the next update are made from the new exe. If
all of that fails, find the addresses in IDA and add the build to BUILDS.

Needs: Windows, Python 3, `pip install pefile pillow` (and `numpy` for a build that has to be resolved).

Pixel position of square (x, y)'s centre in the stitched map, with x0/y0 = the grid corner
minus half a square and H = grid rows:
  pixel_x = (x - x0) * px
  pixel_y = (y0 + H - y) * px          # north up

Known limits: pickup particles keep animating (they don't use the simulation clock); the game's
wrap copies leave out static terrain, so terrain overhanging a wrap join can miss a sliver there.
"""
import argparse
import collections
import ctypes
import ctypes.wintypes as wt
import itertools
import json
import math
import os
import re
import shutil
import struct
import sys
import time
from types import SimpleNamespace

import pefile

EXE_NAME = 'sinking_star.exe'

# The game's static globals, as addresses at the exe's preferred image base, per build (key: the
# exe's PE TimeDateStamp). A build that is not listed is resolved from code signatures instead (see
# "Build resolver" below); if that fails, add it here by hand. Struct member offsets are read from
# the game's Type_Info.
IMAGE_BASE = 0x140000000
BUILDS = {
    0x6AD32EAE: {                           # 2026-10-17 evening update
        'SCREENSHOT_REQUEST': 0x140B693A8,      # u8: 1 = take a screenshot at the end of the frame
        'RENDER_OVERRIDE': 0x140802B27,         # u8 Render_Resolution; 7 = Custom_Screenshot
        'CUSTOM_WIDTH': 0x140802B30,            # s64
        'CUSTOM_HEIGHT': 0x140802B38,           # s64
        'SWAPCHAIN': 0x140B69EC8,               # pointer: [0] = hWnd, [+8] = cached width
        'FINAL_TARGET': 0x140802CD0,            # pointer to the render target the screenshot reads
        'CAMERA_MODE': 0x140B6A808,             # s64: 0 follow, 1 debug free camera, 2 pose left alone
        'TIME_MULTIPLIER': 0x140802028,         # f64 base simulation time multiplier
        'LOD_LEVEL2_THRESHOLD': 0x140802064,    # f32
        'LOD_LEVEL1_THRESHOLD': 0x140802068,    # f32
        'SHADOW_FADE_THRESHOLD': 0x14080205C,   # f32
        'FORCE_MAP_LEVEL0': 0x140802B55,        # u8
        'REFLECTION_COUNT': 0x14108D338,        # s64 live Graphics_Settings.reflection_count
    },
}
# What each global holds in the live process, to sanity-check an address (see global_is_plausible).
GLOBAL_KINDS = {
    'SCREENSHOT_REQUEST': 'bool',
    'RENDER_OVERRIDE': 'enum',
    'CUSTOM_WIDTH': 'size',
    'CUSTOM_HEIGHT': 'size',
    'SWAPCHAIN': 'window',
    'FINAL_TARGET': 'target',
    'CAMERA_MODE': 'mode',
    'TIME_MULTIPLIER': 'f64',
    'LOD_LEVEL2_THRESHOLD': 'f32',
    'LOD_LEVEL1_THRESHOLD': 'f32',
    'SHADOW_FADE_THRESHOLD': 'f32',
    'FORCE_MAP_LEVEL0': 'bool',
    'REFLECTION_COUNT': 'count',
}
# Without these the capture cannot work. The rest only make the tiles consistent, so a build where
# one of them cannot be found is captured without it (with a warning).
REQUIRED = ('SCREENSHOT_REQUEST', 'RENDER_OVERRIDE', 'CUSTOM_WIDTH', 'CUSTOM_HEIGHT', 'SWAPCHAIN', 'FINAL_TARGET', 'CAMERA_MODE')

# Camera pose inside the Entity_Manager.
MANAGER_CAMERA_POSITION = 0x3D98      # Vector3
MANAGER_CAMERA_ORIENTATION = 0x3DB0   # quaternion x, y, z, w
MANAGER_CAMERA_PROJECTION = 0x3DC0    # fov (radians), z_near, z_far

FINAL_TARGET_WIDTH = 0x38
FINAL_TARGET_HEIGHT = 0x3C
TOPDOWN_NORTH_UP = (-0.5, 0.5, 0.5, 0.5)
CAMERA_HEIGHT = 50000.0
FOG_CLEARED = 2
WM_SIZE = 0x0005

# ---------------------------------------------------------------------------------------------
# Process memory

PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS = 0x2
LIST_MODULES_ALL = 0x3
MEM_COMMIT = 0x1000
PAGE_READWRITE = 0x04

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
psapi = ctypes.WinDLL('psapi', use_last_error=True)
user32 = ctypes.WinDLL('user32', use_last_error=True)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
        ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', wt.DWORD), ('cntThreads', wt.DWORD),
        ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', ctypes.c_long), ('dwFlags', wt.DWORD),
        ('szExeFile', ctypes.c_wchar * 260),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p), ('AllocationProtect', wt.DWORD),
        ('PartitionId', wt.WORD), ('RegionSize', ctypes.c_size_t), ('State', wt.DWORD), ('Protect', wt.DWORD),
        ('Type', wt.DWORD),
    ]


kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD)]
psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p), wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD]
psapi.GetModuleBaseNameW.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_wchar_p, wt.DWORD]
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.IsWindow.argtypes = [ctypes.c_void_p]      # may be asked about garbage: never an ArgumentError


def find_pid(name):
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    found = []
    ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
    while ok:
        if entry.szExeFile.lower() == name.lower():
            found.append(entry.th32ProcessID)
        ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    kernel32.CloseHandle(snapshot)
    if len(found) != 1:
        raise RuntimeError('expected one %s process, found %d' % (name, len(found)))
    return found[0]


class Game:
    def __init__(self):
        self.pid = find_pid(EXE_NAME)
        access = PROCESS_VM_READ | PROCESS_QUERY_INFORMATION | PROCESS_VM_WRITE | PROCESS_VM_OPERATION
        self.handle = kernel32.OpenProcess(access, False, self.pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.base = self.main_module_base()
        self.exe_path = self.image_path()
        pe_offset = self.u32(self.base + 0x3C)
        self.timestamp = self.u32(self.base + pe_offset + 8)
        addresses, source = find_globals(self)
        self.strict = source not in ('built-in table', 'saved resolution')    # inferred addresses must pass every check
        print('build %08x: globals from %s' % (self.timestamp, source))
        bad = [name for name, address in addresses.items() if address and not global_is_plausible(self, name, self.va(address))]
        if bad:
            message = 'these globals do not hold what they should: ' + ', '.join(bad)
            if not self.strict:
                print('warning:', message)
            elif any(name in REQUIRED for name in bad):
                raise RuntimeError(message + '. Nothing was written to the game.')
            else:
                print('warning:', message, '(left alone)')
                for name in bad:
                    addresses[name] = None
        self.addr = SimpleNamespace(**addresses)
        if self.strict:
            remember_build(self.timestamp, addresses)
            refresh_signatures(self.exe_path, addresses)

    def main_module_base(self):
        modules = (ctypes.c_void_p * 1024)()
        needed = wt.DWORD()
        if not psapi.EnumProcessModulesEx(self.handle, modules, ctypes.sizeof(modules), ctypes.byref(needed), LIST_MODULES_ALL):
            raise ctypes.WinError(ctypes.get_last_error())
        name = ctypes.create_unicode_buffer(260)
        for module in modules[:needed.value // ctypes.sizeof(ctypes.c_void_p)]:
            psapi.GetModuleBaseNameW(self.handle, module, name, 260)
            if name.value.lower() == EXE_NAME:
                return module
        raise RuntimeError('main module not found')

    def image_path(self):
        buffer = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(self.handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value

    def va(self, address):
        """Runtime address of an address at the exe's preferred image base."""
        return self.base + (address - IMAGE_BASE)

    def read(self, address, size):
        buffer = ctypes.create_string_buffer(size)
        done = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(done)) or done.value != size:
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw

    def write(self, address, data):
        done = ctypes.c_size_t()
        if not kernel32.WriteProcessMemory(self.handle, address, data, len(data), ctypes.byref(done)) or done.value != len(data):
            raise ctypes.WinError(ctypes.get_last_error())

    def u8(self, address): return self.read(address, 1)[0]
    def u32(self, address): return struct.unpack('<I', self.read(address, 4))[0]
    def s32(self, address): return struct.unpack('<i', self.read(address, 4))[0]
    def s64(self, address): return struct.unpack('<q', self.read(address, 8))[0]
    def u64(self, address): return struct.unpack('<Q', self.read(address, 8))[0]
    def f32(self, address): return struct.unpack('<f', self.read(address, 4))[0]

    def put_u8(self, address, value): self.write(address, bytes([value]))
    def put_s64(self, address, value): self.write(address, struct.pack('<q', value))
    def put_f32(self, address, value): self.write(address, struct.pack('<f', value))
    def put_f64(self, address, value): self.write(address, struct.pack('<d', value))

    def writable_regions(self):
        address = 0
        info = MEMORY_BASIC_INFORMATION()
        while kernel32.VirtualQueryEx(self.handle, address, ctypes.byref(info), ctypes.sizeof(info)):
            base = info.BaseAddress or 0
            if info.State == MEM_COMMIT and info.Protect == PAGE_READWRITE and info.RegionSize < 1 << 31:
                yield base, info.RegionSize
            address = base + info.RegionSize


# ---------------------------------------------------------------------------------------------
# Jai Type_Info, read from the exe file

TYPE_STRUCT = 7
TYPE_VARIANT = 18


class TypeInfo:
    def __init__(self, exe_path):
        self.pe = pefile.PE(exe_path, fast_load=True)
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.image = self.pe.get_memory_mapped_image()
        self.structs = self.scan_structs()

    def q(self, address): return struct.unpack_from('<Q', self.image, address - self.base)[0]
    def d(self, address): return struct.unpack_from('<I', self.image, address - self.base)[0]

    def string(self, address):
        length = self.q(address)
        data = self.q(address + 8)
        if length <= 0 or length > 4096 or not data:
            return ''
        return self.image[data - self.base:data - self.base + length].decode('utf-8', 'replace')

    def scan_structs(self):
        """Type_Info_Struct records by name: tag 7, then size, then the name string."""
        found = {}
        for section in self.pe.sections:
            if section.Name.rstrip(b'\0') not in (b'.rdata', b'.data'):
                continue
            start = self.base + section.VirtualAddress
            data = self.image[section.VirtualAddress:section.VirtualAddress + section.Misc_VirtualSize]
            for offset in range(0, len(data) - 96, 8):
                if struct.unpack_from('<I', data, offset)[0] != TYPE_STRUCT:
                    continue
                size, name_length, name_data = struct.unpack_from('<qqQ', data, offset + 8)
                if not (0 < name_length < 64 and self.base <= name_data < self.base + len(self.image) and 0 <= size < 1 << 24):
                    continue
                name_bytes = self.image[name_data - self.base:name_data - self.base + name_length]
                if not re.fullmatch(rb'[A-Za-z_]\w*', name_bytes):
                    continue
                found.setdefault(name_bytes.decode(), []).append(start + offset)
        return found

    def struct_info(self, name):
        records = self.structs.get(name)
        if not records:
            raise RuntimeError('no Type_Info for struct %s' % name)
        return records[0]

    def offset(self, struct_name, member_name):
        record = self.struct_info(struct_name)
        count = self.q(record + 64)
        members = self.q(record + 72)
        for index in range(count):
            member = members + index * 64
            if self.string(member) == member_name:
                return self.q(member + 24)
        raise RuntimeError('%s has no member %s' % (struct_name, member_name))


# ---------------------------------------------------------------------------------------------
# Build resolver
#
# A new game build moves the static globals. The code that uses them moves too, but it stays
# byte-for-byte the same apart from its displacements. So each global is found again from patterns
# of the code around its uses (displacements masked), made once from a build whose addresses are
# known and kept in sinking_star_signatures.json. Several patterns vote for every global.

SIGNATURE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sinking_star_signatures.json')
WINDOWS = ((4, 4), (6, 6), (8, 8), (12, 12), (16, 16), (24, 24), (32, 32))   # bytes before / after an operand
SIGNATURES_PER_GLOBAL = 6
EXECUTABLE = 0x20000000
OPCODES_WITH_IMM8 = (0x80, 0x83, 0xC0, 0xC1, 0xC6, 0xF6)
OPCODES_WITH_IMM32 = (0x81, 0xC7, 0xF7)


def load_state():
    try:
        with open(SIGNATURE_FILE) as file:
            return json.load(file)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(SIGNATURE_FILE, 'w') as file:
        json.dump(state, file, indent=1)


def known_build(timestamp):
    """(addresses, source) of a build from the built-in table or an earlier resolution, else (None, None)."""
    saved = load_state().get('builds', {}).get('%08x' % timestamp)
    for source, addresses in (('built-in table', BUILDS.get(timestamp)), ('saved resolution', saved)):
        if addresses and all(name in addresses for name in GLOBAL_KINDS):
            return dict(addresses), source
    return None, None


def remember_build(timestamp, addresses):
    state = load_state()
    state.setdefault('builds', {})['%08x' % timestamp] = addresses
    save_state(state)


def format_pattern(pattern):
    return ' '.join('??' if byte is None else '%02X' % byte for byte in pattern)


def parse_pattern(text):
    return [None if token == '??' else int(token, 16) for token in text.split()]


def compile_pattern(pattern):
    return re.compile(b''.join(b'.' if byte is None else re.escape(bytes([byte])) for byte in pattern), re.DOTALL)


def trim(pattern, at):
    """Drop masked bytes from both ends of a pattern; `at` is the index of the operand in it."""
    while pattern[0] is None:
        pattern.pop(0)
        at -= 1
    while pattern[-1] is None:
        pattern.pop()
    return pattern, at


class Image:
    """The exe file laid out as the loader maps it, so an index into `data` is an RVA."""

    def __init__(self, path):
        self.pe = pefile.PE(path, fast_load=True)
        if self.pe.OPTIONAL_HEADER.ImageBase != IMAGE_BASE:
            raise RuntimeError('unexpected image base %#x' % self.pe.OPTIONAL_HEADER.ImageBase)
        self.timestamp = self.pe.FILE_HEADER.TimeDateStamp
        self.data = bytes(self.pe.get_memory_mapped_image())
        self.code = [(s.VirtualAddress, s.VirtualAddress + s.Misc_VirtualSize)
                     for s in self.pe.sections if s.Characteristics & EXECUTABLE]
        self.index = None

    def displacement_at(self, position):
        return int.from_bytes(self.data[position:position + 4], 'little', signed=True)

    def operands(self):
        """Every rip-relative operand in the code, sorted by target: (targets, positions, tails).
        The operand is the 4-byte displacement at `position`; `tail` is the size of an immediate
        after it, so the target is position + 4 + tail + displacement."""
        if self.index is None:
            import numpy as np
            image = np.frombuffer(self.data, dtype=np.uint8)
            size = len(image)
            inside = np.zeros(size, dtype=bool)
            for start, end in self.code:
                inside[start:end] = True
            # A ModRM byte with mod=00 and rm=101 (rip-relative) sits just before the displacement.
            positions = np.nonzero((image[3:size - 4] & 0xC7) == 0x05)[0] + 4
            positions = positions[inside[positions]]
            opcode = image[positions - 2]
            tails = np.zeros(len(positions), dtype=np.int64)
            tails[np.isin(opcode, OPCODES_WITH_IMM8)] = 1
            tails[np.isin(opcode, OPCODES_WITH_IMM32)] = 4
            tails[(opcode == 0xBA) & (image[positions - 3] == 0x0F)] = 1
            displacements = np.ndarray((size - 3,), dtype='<i4', buffer=self.data, strides=(1,))[positions]
            targets = positions + 4 + tails + displacements
            keep = (targets >= 0) & (targets < size)
            targets, positions, tails = targets[keep], positions[keep], tails[keep]
            order = np.argsort(targets, kind='stable')
            self.index = (targets[order], positions[order], tails[order])
        return self.index

    def refs_to(self, rva, span=0):
        """[(position, tail, delta)] for operands targeting within `span` bytes of rva; delta = rva - target."""
        import numpy as np
        targets, positions, tails = self.operands()
        low = np.searchsorted(targets, rva - span, 'left')
        high = np.searchsorted(targets, rva + span, 'right')
        return [(int(positions[i]), int(tails[i]), rva - int(targets[i])) for i in range(low, high)]

    def looks_relative(self, position):
        """Whether the 4 bytes here look like a call, jump or rip-relative displacement into the image."""
        data = self.data
        before = data[position - 1]
        if before in (0xE8, 0xE9) or (0x80 <= before <= 0x8F and data[position - 2] == 0x0F) or (before & 0xC7) == 0x05:
            return 0 <= position + 4 + self.displacement_at(position) < len(data)
        return False

    def pattern(self, position, tail, left, right):
        """The bytes around an operand with every displacement masked (None), and the operand's index in them."""
        start = max(position - left, 2)
        end = position + 4 + tail + right
        masked = [False] * (end - start)
        # Candidates start up to 3 bytes before the window and run to its end, so a displacement the
        # window cuts in half still has its visible bytes masked (they change with every build).
        for q in range(max(start - 3, 2), end):
            # Never mask the instruction's own opcode or immediate bytes, only the operand itself.
            if q == position or (not position - 3 <= q < position + 4 + tail and q + 4 <= len(self.data) and self.looks_relative(q)):
                for k in range(max(q, start), min(q + 4, end)):
                    masked[k - start] = True
        pattern = [None if is_masked else byte for is_masked, byte in zip(masked, self.data[start:end])]
        return pattern, position - start

    def unique_patterns(self, rva, wanted):
        """Signatures for the global at rva: patterns that match exactly one place in this image."""
        found = []
        for span in (0, 0x40, 0x200):      # no direct use: fall back to uses of nearby addresses
            refs = self.refs_to(rva, span)
            for position, tail, delta in refs[::max(1, len(refs) // 40)]:
                for left, right in WINDOWS:     # the smallest window that is unique
                    pattern, at = trim(*self.pattern(position, tail, left, right))
                    matches = itertools.islice(compile_pattern(pattern).finditer(self.data), 2)
                    if sum(1 for _ in matches) == 1:
                        found.append({'pat': format_pattern(pattern), 'at': at, 'tail': tail, 'delta': delta})
                        break
                if len(found) >= wanted:
                    return found
            if len(found) >= 2:
                break
        return found

    def make_signatures(self, addresses):
        signatures = {}
        for name, address in addresses.items():
            signatures[name] = self.unique_patterns(address - IMAGE_BASE, SIGNATURES_PER_GLOBAL)
            print('  %-22s %d signatures' % (name, len(signatures[name])), flush=True)
        return signatures

    def resolve(self, signatures, reference=None):
        """{name: (address, votes, signatures)} for every global whose signatures agree in this image.
        With the addresses of the build the signatures were made from, a global with a single vote
        is accepted too when it moved like the confident ones did."""
        found = {}
        counted = {}
        for name, entries in signatures.items():
            votes = collections.Counter()
            for entry in entries:
                matches = list(itertools.islice(compile_pattern(parse_pattern(entry['pat'])).finditer(self.data), 4))
                if len(matches) > 3:
                    continue        # too common in this build to mean anything
                for match in matches:
                    position = match.start() + entry['at']
                    votes[IMAGE_BASE + position + 4 + entry['tail'] + self.displacement_at(position) + entry['delta']] += 1
            counted[name] = votes
            ranked = votes.most_common(2)
            if ranked and ranked[0][1] >= min(2, len(entries)) and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
                found[name] = (ranked[0][0], ranked[0][1], len(entries))
        if reference:
            shifts = collections.Counter(address - reference[name] for name, (address, _, _) in found.items() if reference.get(name))
            common = {shift for shift, count in shifts.items() if count >= 2}
            for name, votes in counted.items():
                if name in found or not reference.get(name):
                    continue
                fits = sorted(((count, address) for address, count in votes.items() if address - reference[name] in common), reverse=True)
                if fits and (len(fits) == 1 or fits[0][0] > fits[1][0]):
                    found[name] = (fits[0][1], fits[0][0], len(signatures[name]))
        return found


def infer_missing(found, reference):
    """Globals the signatures could not settle but that sat right beside one they did (the same
    block of the same source file) move with it: place them at the earlier spacing, with 0 votes."""
    placed = dict(found)
    for name in GLOBAL_KINDS:
        if found.get(name) or not reference.get(name):
            continue
        near = sorted((abs(reference[other] - reference[name]), other) for other in found
                      if reference.get(other) and abs(reference[other] - reference[name]) <= 0x40)
        if near:
            other = near[0][1]
            placed[name] = (reference[name] + found[other][0] - reference[other], 0, 0)
    return placed


def report(found, reference, show_votes=True):
    """Print {name: (address, votes, signatures)} with each shift from a reference build's address."""
    for name in GLOBAL_KINDS:
        if found.get(name):
            address, votes, total = found[name]
            shift = '%+#x' % (address - reference[name]) if reference and reference.get(name) else '?'
            tail = ('   votes %d/%d' % (votes, total) if total else '   inferred from its neighbour') if show_votes else ''
            print('  %-22s %#x   shift %s%s' % (name, address, shift, tail))
        else:
            print('  %-22s NOT FOUND' % name)


def resolve_from_signatures(image):
    state = load_state()
    signatures = state.get('signatures')
    if not signatures:
        raise RuntimeError(
            'build %08x is not known and there are no signatures (%s). With the game running, a normal '
            'run searches the live process instead and makes signatures for the next build.' % (image.timestamp, SIGNATURE_FILE))
    reference, _ = known_build(int(state['from_timestamp'], 16))
    print('resolving the globals of build %08x from the signatures of build %s (scans the exe)' % (
        image.timestamp, state['from_timestamp']), flush=True)
    found = image.resolve(signatures, reference)
    if reference:
        found = infer_missing(found, reference)
    report(found, reference)
    missing = [name for name in REQUIRED if name not in found]
    if missing:
        raise RuntimeError('could not resolve %s in build %08x from the signatures' % (', '.join(missing), image.timestamp))
    return {name: found[name][0] if name in found else None for name in GLOBAL_KINDS}


def find_globals(game):
    """(addresses, source) for the running build: the built-in table, an earlier resolution, the
    signatures of an earlier build, or (needing neither) a search of the live process."""
    addresses, source = known_build(game.timestamp)
    if addresses:
        return addresses, source
    image = Image(game.exe_path)
    if image.timestamp != game.timestamp:
        raise RuntimeError('the exe on disk (%08x) is not the running build (%08x): restart the game' % (image.timestamp, game.timestamp))
    if load_state().get('signatures'):
        try:
            return resolve_from_signatures(image), 'signatures'
        except RuntimeError as error:
            print(error)
    else:
        print('no signatures yet')
    print('searching the live process instead', flush=True)
    return resolve_from_layout(game, image), 'live layout'


def refresh_signatures(exe_path, addresses):
    """Make signatures from the running build, so the next update is resolved from the nearest code
    and needs no old exe. The saved set is kept unless the new one is as good (as many signatures,
    up to 3, for every global, and 2 when there was no set before)."""
    try:
        image = Image(exe_path)
        print('making signatures from this build for the next update (takes a while)', flush=True)
        state = load_state()
        old = state.get('signatures', {})
        fresh = image.make_signatures({name: address for name, address in addresses.items() if address})
        if any(len(entries) < max(2 if not old else 1, min(3, len(old.get(name, ())))) for name, entries in fresh.items()):
            print('keeping the saved signatures: this build gave fewer for some globals')
            return
        state['signatures'] = fresh
        state['from_timestamp'] = '%08x' % image.timestamp
        save_state(state)
    except Exception as error:
        print('warning: no signatures made (%s); run --make-signatures later' % error)


def make_signatures_command(exe_path):
    image = Image(exe_path)
    addresses, source = known_build(image.timestamp)
    if not addresses:
        raise RuntimeError('build %08x has no known addresses: find them in IDA, add them to BUILDS and run this again' % image.timestamp)
    print('making signatures from build %08x (%s)' % (image.timestamp, source))
    addresses = {name: address for name, address in addresses.items() if address}
    signatures = image.make_signatures(addresses)
    found = image.resolve(signatures)
    wrong = [name for name in addresses if name not in found or found[name][0] != addresses[name]]
    if wrong:
        print('warning: these are not found again in the same exe: ' + ', '.join(wrong))
    state = load_state()
    state['signatures'] = signatures
    state['from_timestamp'] = '%08x' % image.timestamp
    save_state(state)
    print('saved', SIGNATURE_FILE)


def find_addresses_command(exe_path):
    image = Image(exe_path)
    addresses, source = known_build(image.timestamp)
    if addresses:
        print('build %08x is known (%s)' % (image.timestamp, source))
        report({name: (address, 1, 1) for name, address in addresses.items() if address}, None, False)
    else:
        resolve_from_signatures(image)


def running_exe_path():
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, find_pid(EXE_NAME))
    buffer = ctypes.create_unicode_buffer(1024)
    size = wt.DWORD(1024)
    ok = kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
    kernel32.CloseHandle(handle)
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


def global_is_plausible(game, name, address, memory=None):
    """Whether the value at this runtime address looks like what the global should hold. The global
    itself is read from `memory` (default: the game, or a Dump of it); pointers are followed live."""
    memory = memory or game
    kind = GLOBAL_KINDS[name]
    try:
        if kind == 'bool':
            return memory.u8(address) <= 1
        if kind == 'enum':
            return memory.u8(address) <= 0x20 or memory.u8(address) == 0xFF
        if kind == 'size':
            return 0 <= memory.s64(address) <= 65536
        if kind == 'count':
            return 0 <= memory.s64(address) <= 64
        if kind == 'mode':
            return 0 <= memory.s64(address) <= 2
        if kind == 'f64':
            return 0.0 <= struct.unpack('<d', memory.read(address, 8))[0] <= 1000.0
        if kind == 'f32':
            return abs(memory.f32(address)) < 1e7
        if kind == 'window':
            pointer = memory.u64(address)
            return bool(user32.IsWindow(game.u64(pointer))) and 0 <= game.s64(pointer + 8) <= 65536
        if kind == 'target':
            pointer = memory.u64(address)
            return pointer == 0 or (0 < game.s32(pointer + FINAL_TARGET_WIDTH) <= 32768
                                    and 0 < game.s32(pointer + FINAL_TARGET_HEIGHT) <= 32768)
    except OSError:
        return False
    return False


def camera_layout_is_plausible(game, manager):
    """Whether the Entity_Manager holds a sensible camera pose at the offsets this script uses."""
    try:
        values = struct.unpack('<3f', game.read(manager + MANAGER_CAMERA_POSITION, 12))
        qx, qy, qz, qw = struct.unpack('<4f', game.read(manager + MANAGER_CAMERA_ORIENTATION, 16))
        fov, near, far = struct.unpack('<3f', game.read(manager + MANAGER_CAMERA_PROJECTION, 12))
    except OSError:
        return False
    length = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    return all(math.isfinite(v) for v in values + (fov, near, far)) and abs(length - 1) < 0.05 and 0 < fov < math.pi and 0 < near < far


# ---------------------------------------------------------------------------------------------
# Live layout search
#
# With no signatures (and no exe of an earlier build) the globals are found in the running game.
# They keep their spacing from build to build unless something between them changed, so two
# anchors are enough: the data block around TIME_MULTIPLIER is found by its values (1.0 as an f64,
# 0xFF at the resolution override, sane floats, flags and sizes at the earlier spacing), and
# SWAPCHAIN by its pointer chain: a global pointing at a struct whose first qword is the game's
# window handle. The rest follow at the earlier spacing and must pass the same value checks.

PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100


class Dump:
    """A copy of the running module's data. Reads take runtime addresses, like Game."""

    def __init__(self, data, base):
        self.data = data
        self.base = base

    def read(self, address, size):
        start = address - self.base
        if start < 0 or start + size > len(self.data):
            raise OSError('outside the module')
        return self.data[start:start + size]

    def u8(self, address): return self.read(address, 1)[0]
    def s32(self, address): return struct.unpack('<i', self.read(address, 4))[0]
    def s64(self, address): return struct.unpack('<q', self.read(address, 8))[0]
    def u64(self, address): return struct.unpack('<Q', self.read(address, 8))[0]
    def f32(self, address): return struct.unpack('<f', self.read(address, 4))[0]


def read_module_data(game):
    """The running module's committed pages as one Dump (unreadable pages are zeros)."""
    pe_offset = game.u32(game.base + 0x3C)
    size = game.u32(game.base + pe_offset + 0x50)       # OptionalHeader.SizeOfImage
    data = bytearray(size)
    address = game.base
    info = MEMORY_BASIC_INFORMATION()
    while address < game.base + size and kernel32.VirtualQueryEx(game.handle, address, ctypes.byref(info), ctypes.sizeof(info)):
        start = info.BaseAddress or 0
        end = start + info.RegionSize
        if info.State == MEM_COMMIT and info.Protect and not info.Protect & (PAGE_NOACCESS | PAGE_GUARD):
            low, high = max(start, game.base), min(end, game.base + size)
            try:
                data[low - game.base:high - game.base] = game.read(low, high - low)
            except OSError:
                pass
        address = end
    return Dump(bytes(data), game.base)


def game_windows(pid):
    """Handles of the visible top-level windows of a process."""
    found = []
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]

    def visit(hwnd, lparam):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, ctypes.c_void_p, wt.LPARAM)(visit), 0)
    return found


def reference_build():
    """(timestamp, addresses) of the newest build whose required globals are all known."""
    builds = dict(BUILDS)
    for key, addresses in load_state().get('builds', {}).items():
        builds.setdefault(int(key, 16), addresses)
    for timestamp in sorted(builds, reverse=True):
        if all(builds[timestamp].get(name) for name in REQUIRED):
            return timestamp, builds[timestamp]
    raise RuntimeError('no known build to take the layout from: add the build to BUILDS by hand')


def spacing(reference, anchor):
    """{name: offset from the anchor global} for the globals that sat close to it."""
    return {name: address - reference[anchor] for name, address in reference.items()
            if address and abs(address - reference[anchor]) < 0x10000}


def fits(game, dump, rva, offsets):
    """Whether every global at this spacing from rva holds a plausible value."""
    return all(global_is_plausible(game, name, game.base + rva + offset, dump) for name, offset in offsets.items())


def search_data_block(game, dump, reference):
    """RVAs where the block around TIME_MULTIPLIER could be, and the spacing used."""
    import numpy as np
    offsets = spacing(reference, 'TIME_MULTIPLIER')
    words = np.frombuffer(dump.data, dtype=np.uint64, count=len(dump.data) // 8)
    raw = np.frombuffer(dump.data, dtype=np.uint8)
    thresholds = [name for name in offsets if GLOBAL_KINDS[name] == 'f32']
    for time_value in (1.0, 0.0):       # a fresh game, or one this script already froze
        bits = struct.unpack('<Q', struct.pack('<d', time_value))[0]
        positions = np.nonzero(words == bits)[0] * 8
        positions = positions[positions + offsets['RENDER_OVERRIDE'] < len(dump.data)]
        positions = positions[np.isin(raw[positions + offsets['RENDER_OVERRIDE']], (0xFF, 7))]
        found = [rva for rva in map(int, positions[:20000])
                 if fits(game, dump, rva, offsets) and all(dump.f32(game.base + rva + offsets[name]) != 0 for name in thresholds)]
        if found:
            return found, offsets
    return [], offsets


def search_swapchain(game, dump, reference):
    """RVAs where SWAPCHAIN could be (with its neighbours at the earlier spacing), and the spacing used."""
    import numpy as np
    offsets = spacing(reference, 'SWAPCHAIN')
    needles = [struct.pack('<Q', hwnd) for hwnd in game_windows(game.pid)]
    structs = set()         # where a heap qword holds the window handle
    for start, size in game.writable_regions():
        try:
            region = game.read(start, size)
        except OSError:
            continue
        for needle in needles:
            position = region.find(needle)
            while position >= 0:
                if position % 8 == 0:
                    structs.add(start + position)
                position = region.find(needle, position + 1)
    if not structs:
        return [], offsets
    words = np.frombuffer(dump.data, dtype=np.uint64, count=len(dump.data) // 8)
    hits = np.nonzero(np.isin(words, np.array(sorted(structs), dtype=np.uint64)))[0] * 8
    return [rva for rva in map(int, hits) if fits(game, dump, rva, offsets)], offsets


def choose_used(image, candidates, offsets, what, expected):
    """The candidate to take among places that all hold plausible values. Best is the one whose
    globals the exe's code really uses (a copy or a leftover has no code touching it). If the code
    analysis shows no use anywhere, the candidate clearly nearest the earlier build's address
    (`expected`, an RVA) wins: layouts rarely move far."""
    if not candidates:
        raise RuntimeError('%s was not found at the earlier spacing' % what)
    scores = {rva: sum(bool(image.refs_to(rva + offset, 8)) for offset in offsets.values()) for rva in candidates}
    best = max(scores.values())
    top = [rva for rva, score in scores.items() if score == best]
    if len(candidates) > 1:
        print('%s: %d candidates, globals used by code: %s' % (
            what, len(candidates), ', '.join('%#x=%d/%d' % (rva, score, len(offsets)) for rva, score in list(scores.items())[:8])))
    if best * 2 >= len(offsets):
        if len(top) != 1:
            raise RuntimeError('%s is ambiguous: several places fit equally well' % what)
        return top[0]
    nearest = sorted(candidates, key=lambda rva: abs(rva - expected))
    gap = abs(nearest[0] - expected)
    if gap > 0x20000 or (len(nearest) > 1 and abs(nearest[1] - expected) < 4 * gap + 0x10000):
        raise RuntimeError('%s: the code analysis shows no use of any candidate and none is clearly nearest the earlier address' % what)
    print('warning: no code use seen for %s; taking %#x, nearest the earlier build (%+#x)' % (what, nearest[0], nearest[0] - expected))
    return nearest[0]


def resolve_from_layout(game, image):
    timestamp, reference = reference_build()
    print('taking the layout from build %08x; reading the module and scanning memory' % timestamp, flush=True)
    dump = read_module_data(game)
    found = {name: None for name in GLOBAL_KINDS}

    block = reference['TIME_MULTIPLIER'] - IMAGE_BASE
    print('exe code: %d rip-relative operands, %d of them into the %#x block area' % (
        len(image.operands()[0]), len(image.refs_to(block, 0x10000)), block), flush=True)

    blocks, offsets = search_data_block(game, dump, reference)
    in_use = [rva for rva in blocks if dump.u64(game.base + rva + offsets['FINAL_TARGET'])]     # a render target exists
    rva = choose_used(image, in_use or blocks, offsets, 'the data block around TIME_MULTIPLIER', block)
    for name, offset in offsets.items():
        found[name] = IMAGE_BASE + rva + offset

    chains, offsets = search_swapchain(game, dump, reference)
    rva = choose_used(image, chains, offsets, 'SWAPCHAIN', reference['SWAPCHAIN'] - IMAGE_BASE)
    for name, offset in offsets.items():
        found[name] = IMAGE_BASE + rva + offset

    report({name: (address, 0, 0) for name, address in found.items() if address}, reference, False)
    missing = [name for name in REQUIRED if not found[name]]
    if missing:
        raise RuntimeError('not found: ' + ', '.join(missing))
    unused = [name for name, address in found.items() if address and not image.refs_to(address - IMAGE_BASE, 0x40)]
    if unused:
        print('warning: no code in the exe uses anything near', ', '.join(unused))
    return found


# ---------------------------------------------------------------------------------------------
# Entities

def find_entities(game, types, type_names):
    """Live entities whose Entity.entity_type points at the named Type_Info: [(entity, manager)]."""
    entity_type = types.offset('Entity', 'entity_type')
    entity_manager = types.offset('Entity', 'entity_manager')
    wanted = {}
    for name in type_names:
        for record in types.structs.get(name, []):
            wanted[struct.pack('<Q', game.va(record))] = name
    found = {name: [] for name in type_names}
    for base, size in game.writable_regions():
        try:
            data = game.read(base, size)
        except OSError:
            continue
        for needle, name in wanted.items():
            position = data.find(needle)
            while position >= 0:
                if position % 8 == 0 and position >= entity_type:
                    entity_start = position - entity_type
                    manager = struct.unpack_from('<Q', data, entity_start + entity_manager)[0]
                    if manager:
                        found[name].append((base + entity_start, manager))
                position = data.find(needle, position + 1)
    return found


class Overworld:
    def __init__(self, game, types):
        self.game = game
        self.types = types
        entities = find_entities(game, types, ['Settings', 'Fog_Of_War', 'Guy'])

        grid = types.offset('Settings', 'grid')
        squares_x = types.offset('Gridlike', 'squares_x')
        squares_y = types.offset('Gridlike', 'squares_y')
        corner = types.offset('Gridlike', 'corner')
        # The overworld is the level with the largest grid.
        best_area = 0
        for settings, manager in entities['Settings']:
            columns = game.s64(settings + grid + squares_x)
            rows = game.s64(settings + grid + squares_y)
            if 0 < columns < 10000 and 0 < rows < 10000 and columns * rows > best_area:
                best_area = columns * rows
                self.settings = settings
                self.manager = manager
                self.squares_x = columns
                self.squares_y = rows
        if not best_area:
            raise RuntimeError('overworld Settings not found')
        if not camera_layout_is_plausible(game, self.manager):
            message = 'no sensible camera pose at the Entity_Manager offsets %#x, %#x, %#x: moved in this build?' % (
                MANAGER_CAMERA_POSITION, MANAGER_CAMERA_ORIENTATION, MANAGER_CAMERA_PROJECTION)
            if game.strict:
                raise RuntimeError(message)
            print('warning:', message)
        corner_x, corner_y, corner_z = struct.unpack('<3i', game.read(self.settings + grid + corner, 12))
        # Whole-number coordinates are square centres.
        self.world_x0 = corner_x - 0.5
        self.world_y0 = corner_y - 0.5

        self.fogs = [entity for entity, manager in entities['Fog_Of_War'] if manager == self.manager]
        self.guys = [entity for entity, manager in entities['Guy'] if manager == self.manager]
        print('overworld grid %d x %d, corner (%d, %d), %d fog regions, %d heroes' % (
            self.squares_x, self.squares_y, corner_x, corner_y, len(self.fogs), len(self.guys)))

    def clear_fog(self):
        region_states = self.types.offset('Fog_Of_War', 'region_states')
        for fog in self.fogs:
            count = self.game.s64(fog + region_states)
            data = self.game.u64(fog + region_states + 8)
            if count > 0 and data:
                self.game.write(data, bytes([FOG_CLEARED]) * count)

    def hide_hero(self):
        undoable_data = self.types.offset('Entity', 'undoable_data')
        scale = self.types.offset('Undoable_Entity_Data', 'scale')
        for guy in self.guys:
            self.game.write(guy + undoable_data + scale, struct.pack('<3f', 0.0, 0.0, 0.0))

    def draw_all_wrap_copies(self):
        force = self.types.offset('Settings', 'force_wrapping_override_despite_per_screen')
        override = self.types.offset('Settings', 'wrapping_override')
        self.game.put_u8(self.settings + override, 0)
        self.game.put_u8(self.settings + force, 1)

    def set_shadow_distance(self, distance):
        offset = self.types.offset('Settings', 'shadow_max_visible_distance')
        self.game.put_f32(self.settings + offset, distance)

    def place_camera(self, center_x, center_y, height_squares):
        game = self.game
        fov = 2 * math.atan(height_squares / 2 / CAMERA_HEIGHT)
        game.put_s64(game.va(game.addr.CAMERA_MODE), 2)
        game.write(self.manager + MANAGER_CAMERA_POSITION, struct.pack('<3f', center_x, center_y, CAMERA_HEIGHT))
        game.write(self.manager + MANAGER_CAMERA_ORIENTATION, struct.pack('<4f', *TOPDOWN_NORTH_UP))
        game.write(self.manager + MANAGER_CAMERA_PROJECTION, struct.pack('<3f', fov, CAMERA_HEIGHT - 100, CAMERA_HEIGHT + 100))


# ---------------------------------------------------------------------------------------------
# Rendering and screenshots

def freeze_and_force_detail(game):
    skipped = []
    for name, put, value in (('TIME_MULTIPLIER', game.put_f64, 0.0), ('LOD_LEVEL2_THRESHOLD', game.put_f32, -1.0),
                             ('LOD_LEVEL1_THRESHOLD', game.put_f32, -1.0), ('SHADOW_FADE_THRESHOLD', game.put_f32, -1.0),
                             ('FORCE_MAP_LEVEL0', game.put_u8, 1), ('REFLECTION_COUNT', game.put_s64, 0)):
        address = getattr(game.addr, name)
        if address:
            put(game.va(address), value)
        else:
            skipped.append(name)
    if skipped:
        print('warning: not found in this build, so not set: ' + ', '.join(skipped) + ' (tiles may differ at the seams)')


def render_size(game):
    target = game.u64(game.va(game.addr.FINAL_TARGET))
    if not target:
        return None
    return game.s32(target + FINAL_TARGET_WIDTH), game.s32(target + FINAL_TARGET_HEIGHT)


def set_resolution(game, width, height):
    if render_size(game) == (width, height):
        return
    a = game.addr
    game.put_s64(game.va(a.CUSTOM_WIDTH), width)
    game.put_s64(game.va(a.CUSTOM_HEIGHT), height)
    game.put_u8(game.va(a.RENDER_OVERRIDE), 7)
    # The resolution is only recomputed on a swapchain resize: forget the cached width and resize.
    swapchain = game.u64(game.va(a.SWAPCHAIN))
    game.put_s64(swapchain + 8, 0)
    user32.PostMessageW(game.u64(swapchain), WM_SIZE, 0, 0)
    time.sleep(2.0)
    actual = render_size(game)
    if actual != (width, height):
        raise RuntimeError('render target is %s, wanted %dx%d' % (actual, width, height))


def shoot(game, screenshot_dir, out_path):
    os.makedirs(screenshot_dir, exist_ok=True)
    existing = set(os.listdir(screenshot_dir))
    request = game.va(game.addr.SCREENSHOT_REQUEST)
    game.put_u8(request, 1)
    start = time.time()
    while game.u8(request) != 0:
        if time.time() - start > 60:
            raise RuntimeError('screenshot request was not taken')
        time.sleep(0.05)
    new_files = set(os.listdir(screenshot_dir)) - existing
    if len(new_files) != 1:
        raise RuntimeError('expected one new screenshot, got %s' % sorted(new_files))
    shutil.move(os.path.join(screenshot_dir, new_files.pop()), out_path)
    # Screenshot names have one-second resolution; wait so the next name differs.
    time.sleep(1.0)


# ---------------------------------------------------------------------------------------------
# Tiles

def tile_path(out_dir, row, column):
    return os.path.join(out_dir, 'tile_r%02d_c%02d.png' % (row, column))


def tile_size(squares_x, squares_y, tiles_x, tiles_y, px_per_square):
    width_squares = squares_x / tiles_x
    height_squares = squares_y / tiles_y
    width_px = width_squares * px_per_square
    height_px = height_squares * px_per_square
    if width_px != int(width_px) or height_px != int(height_px):
        raise ValueError('tiles must be a whole number of pixels: %g x %g' % (width_px, height_px))
    return width_squares, height_squares, int(width_px), int(height_px)


def shoot_tiles(game, world, px_per_square, tiles_x, tiles_y, out_dir):
    width_squares, height_squares, width_px, height_px = tile_size(world.squares_x, world.squares_y, tiles_x, tiles_y, px_per_square)
    screenshot_dir = os.path.join(os.path.dirname(game.exe_path), 'screenshots')
    os.makedirs(out_dir, exist_ok=True)
    set_resolution(game, width_px, height_px)
    for row in range(tiles_y):
        for column in range(tiles_x):
            out_path = tile_path(out_dir, row, column)
            if os.path.exists(out_path):
                continue
            # Row 0 is the north (top) row.
            center_x = world.world_x0 + (column + 0.5) * width_squares
            center_y = world.world_y0 + world.squares_y - (row + 0.5) * height_squares
            world.place_camera(center_x, center_y, height_squares)
            time.sleep(0.5)
            shoot(game, screenshot_dir, out_path)
            print('tile', row, column, 'at', round(center_x, 3), round(center_y, 3), flush=True)


def stitch(squares_x, squares_y, px_per_square, tiles_x, tiles_y, out_dir):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    width_squares, height_squares, width_px, height_px = tile_size(squares_x, squares_y, tiles_x, tiles_y, px_per_square)
    canvas = Image.new('RGB', (squares_x * px_per_square, squares_y * px_per_square))
    for row in range(tiles_y):
        for column in range(tiles_x):
            tile = Image.open(tile_path(out_dir, row, column)).convert('RGB')
            if tile.size != (width_px, height_px):
                raise RuntimeError('tile %d,%d is %s, wanted %dx%d' % (row, column, tile.size, width_px, height_px))
            canvas.paste(tile, (column * width_px, row * height_px))
    out_path = os.path.join(out_dir, 'overworld_ground_%dpx.png' % px_per_square)
    canvas.save(out_path)
    print('saved', out_path, canvas.size)


def main():
    parser = argparse.ArgumentParser(description='Capture the overworld map from the running game.')
    parser.add_argument('--px', type=int, default=128, help='pixels per grid square')
    parser.add_argument('--tiles', type=int, nargs=2, default=(8, 8), metavar=('COLUMNS', 'ROWS'))
    parser.add_argument('--out', default='map_tiles')
    parser.add_argument('--stitch-only', action='store_true')
    parser.add_argument('--exe', help="exe to analyse for --make-signatures / --find-addresses (default: the running game's)")
    parser.add_argument('--make-signatures', action='store_true', help='save patterns that find the globals in later builds')
    parser.add_argument('--find-addresses', action='store_true', help='resolve and print the globals of an exe; the game is not touched')
    args = parser.parse_args()
    tiles_x, tiles_y = args.tiles

    if args.make_signatures or args.find_addresses:
        exe_path = args.exe or running_exe_path()
        if args.make_signatures:
            make_signatures_command(exe_path)
        else:
            find_addresses_command(exe_path)
        return

    game = Game()
    print('reading Type_Info from', game.exe_path)
    types = TypeInfo(game.exe_path)
    print('scanning memory for entities')
    world = Overworld(game, types)
    tile_size(world.squares_x, world.squares_y, tiles_x, tiles_y, args.px)

    if not args.stitch_only:
        world.clear_fog()
        world.hide_hero()
        world.draw_all_wrap_copies()
        world.set_shadow_distance(CAMERA_HEIGHT + 200)
        freeze_and_force_detail(game)
        shoot_tiles(game, world, args.px, tiles_x, tiles_y, args.out)
    stitch(world.squares_x, world.squares_y, args.px, tiles_x, tiles_y, args.out)


if __name__ == '__main__':
    main()

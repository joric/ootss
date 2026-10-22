#!/usr/bin/env python3
"""
Convert a .texture file (40-byte header + LZ4 block of raw GPU texture data) to PNG.

Simplest use:  python jai_texture_to_png.py file.texture  

Requires: pip install numpy pillow

Decodes: R8 RG8 RGB8 RGBA8 RGBX8 BGRA8 B5G6R5 R16 R16F RG16F RGBA16F R32F RG32F
         RGBA32F BC1 BC3 BC4 BC5   (BC6/BC7/ETC are reported as unsupported)

Other:
  python jai_texture_to_png.py tex.texture --probe
  python jai_texture_to_png.py tex.texture -W 2048 -H 1152 -f BC1 --decompress none
"""
import argparse, bz2, lzma, struct, sys, zlib
import numpy as np
from PIL import Image

FORMATS = {
    'R8': 1, 'RG8': 2, 'RGB8': 3, 'RGBA8': 4, 'RGBX8': 5, 'BGRA8': 6, 'B5G6R5': 7,
    'RGB10A2': 8, 'R16': 9, 'R16F': 0x10, 'RG16F': 0x11, 'RGBA16F': 0x12, 'R32F': 0x13,
    'RG32F': 0x14, 'RGBA32F': 0x15, 'RG11B10F': 0x16, 'RGB9E5F': 0x17,
    'BC1': 0x100, 'BC3': 0x101, 'BC4': 0x102, 'BC5': 0x103, 'BC6': 0x104, 'BC7': 0x105,
}
ID2NAME = {v: k for k, v in FORMATS.items()}
BPT = {'R8': 1, 'RG8': 2, 'RGB8': 3, 'RGBA8': 4, 'RGBX8': 4, 'BGRA8': 4, 'B5G6R5': 2,
       'RGB10A2': 4, 'R16': 2, 'R16F': 2, 'RG16F': 4, 'RGBA16F': 8, 'R32F': 4,
       'RG32F': 8, 'RGBA32F': 16, 'RG11B10F': 4, 'RGB9E5F': 4}
BPB = {'BC1': 8, 'BC3': 16, 'BC4': 8, 'BC5': 16, 'BC6': 16, 'BC7': 16}


def level_size(fmt, w, h):
    if fmt in BPT:
        return w * h * BPT[fmt]
    return ((w + 3) // 4) * ((h + 3) // 4) * BPB[fmt]


def blocks_to_image(px, w, h):
    c = px.shape[-1]
    bw, bh = (w + 3) // 4, (h + 3) // 4
    a = px.reshape(bh, bw, 4, 4, c).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, c)
    return a[:h, :w]


def bc4_values(b):
    n = len(b)
    a0 = b[:, 0].astype(np.int32)
    a1 = b[:, 1].astype(np.int32)
    pal = np.zeros((n, 8), np.int32)
    pal[:, 0], pal[:, 1] = a0, a1
    six = a0 > a1
    for i in range(1, 7):
        pal[:, 1 + i] = np.where(six, ((7 - i) * a0 + i * a1) // 7,
                                 np.where(i <= 4, ((5 - i) * a0 + i * a1) // 5, 0))
    pal[:, 7] = np.where(six, pal[:, 7], 255)
    bits = np.zeros(n, np.uint64)
    for k in range(6):
        bits |= b[:, 2 + k].astype(np.uint64) << np.uint64(8 * k)
    idx = ((bits[:, None] >> (np.uint64(3) * np.arange(16, dtype=np.uint64))) & np.uint64(7)).astype(np.int64)
    return np.take_along_axis(pal, idx, 1).astype(np.uint8)


def bc1_colors(b, force4=False):
    n = len(b)
    c0 = b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8)
    c1 = b[:, 2].astype(np.int32) | (b[:, 3].astype(np.int32) << 8)

    def rgb(c):
        return np.stack([(((c >> 11) & 31) * 255 + 15) // 31,
                         (((c >> 5) & 63) * 255 + 31) // 63,
                         ((c & 31) * 255 + 15) // 31], 1)
    k0, k1 = rgb(c0), rgb(c1)
    four = (c0 > c1) | force4
    f = four[:, None]
    pal = np.zeros((n, 4, 4), np.int32)
    pal[:, 0, :3], pal[:, 1, :3] = k0, k1
    pal[:, 2, :3] = np.where(f, (2 * k0 + k1) // 3, (k0 + k1) // 2)
    pal[:, 3, :3] = np.where(f, (k0 + 2 * k1) // 3, 0)
    pal[:, :, 3] = 255
    pal[:, 3, 3] = np.where(four, 255, 0)
    bits = np.zeros(n, np.uint32)
    for k in range(4):
        bits |= b[:, 4 + k].astype(np.uint32) << np.uint32(8 * k)
    idx = ((bits[:, None] >> (np.uint32(2) * np.arange(16, dtype=np.uint32))) & np.uint32(3)).astype(np.int64)
    return np.take_along_axis(pal, idx[:, :, None], 1).astype(np.uint8)


def decode(fmt, data, w, h):
    if fmt in BPB:
        bw, bh = (w + 3) // 4, (h + 3) // 4
        b = np.frombuffer(data, np.uint8, bw * bh * BPB[fmt]).reshape(-1, BPB[fmt])
        if fmt == 'BC1':
            px = bc1_colors(b)
        elif fmt == 'BC3':
            px = bc1_colors(b[:, 8:], force4=True)
            px[:, :, 3] = bc4_values(b[:, :8])
        elif fmt == 'BC4':
            v = bc4_values(b)
            px = np.stack([v, v, v], -1)
        elif fmt == 'BC5':
            px = np.stack([bc4_values(b[:, :8]), bc4_values(b[:, 8:]),
                           np.zeros((len(b), 16), np.uint8)], -1)
        else:
            sys.exit(f'{fmt} decoding is not implemented')
        return blocks_to_image(px, w, h)

    raw = np.frombuffer(data, np.uint8, w * h * BPT[fmt])
    if fmt in ('R8', 'RG8', 'RGB8', 'RGBA8'):
        c = BPT[fmt]
        a = raw.reshape(h, w, c)
        if c == 1:
            a = np.repeat(a, 3, 2)
        elif c == 2:
            a = np.concatenate([a, np.zeros((h, w, 1), np.uint8)], 2)
        return a
    if fmt == 'RGBX8':
        return raw.reshape(h, w, 4)[:, :, :3]
    if fmt == 'BGRA8':
        return raw.reshape(h, w, 4)[:, :, [2, 1, 0, 3]]
    if fmt == 'B5G6R5':
        v = raw.view('<u2').reshape(h, w).astype(np.int32)
        return np.stack([(((v >> 11) & 31) * 255 + 15) // 31, (((v >> 5) & 63) * 255 + 31) // 63,
                         ((v & 31) * 255 + 15) // 31], -1).astype(np.uint8)
    if fmt == 'R16':
        v = (raw.view('<u2').reshape(h, w) >> 8).astype(np.uint8)
        return np.stack([v] * 3, -1)
    if fmt in ('R16F', 'RG16F', 'RGBA16F', 'R32F', 'RG32F', 'RGBA32F'):
        dt = '<f2' if '16' in fmt else '<f4'
        c = BPT[fmt] // (2 if '16' in fmt else 4)
        a = np.clip(raw.view(dt).reshape(h, w, c).astype(np.float32), 0, 1)
        a = (a * 255 + 0.5).astype(np.uint8)
        if c == 1:
            a = np.repeat(a, 3, 2)
        elif c == 2:
            a = np.concatenate([a, np.zeros((h, w, 1), np.uint8)], 2)
        return a
    sys.exit(f'{fmt} decoding is not implemented')


def lz4_block(src):
    out = bytearray(); i = 0; n = len(src)
    while i < n:
        t = src[i]; i += 1
        l = t >> 4
        if l == 15:
            while True:
                b = src[i]; i += 1; l += b
                if b != 255: break
        out += src[i:i + l]; i += l
        if i >= n: break
        off = src[i] | (src[i + 1] << 8); i += 2
        m = t & 15
        if m == 15:
            while True:
                b = src[i]; i += 1; m += b
                if b != 255: break
        m += 4
        if off == 0 or off > len(out): raise ValueError('bad LZ4 offset')
        s = len(out) - off
        if off >= m: out += out[s:s + m]
        else:
            for k in range(m): out.append(out[s + k])
    return bytes(out)


DECOMP = {
    'lz4': lambda d: lz4_block(d),
    'none': lambda d: d,
    'zlib': zlib.decompress,
    'deflate': lambda d: zlib.decompress(d, -15),
    'lzma': lzma.decompress,
    'bz2': bz2.decompress,
}


def probe(data):
    print(f'file size: {len(data)} bytes')
    print('header u16 words:', ' '.join(f'{x:04X}' for x in struct.unpack_from('<16H', data, 0)))
    print('trying standard decompressors at every offset < 128...')
    found = False
    for off in range(128):
        for name in ('zlib', 'deflate', 'lzma', 'bz2'):
            try:
                out = DECOMP[name](data[off:])
            except Exception:
                continue
            if len(out) > 1024:
                print(f'  offset {off:#x}: {name} -> {len(out)} bytes')
                found = True
    if not found:
        print('  nothing matched: the payload uses a custom/non-standard compression.')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input')
    ap.add_argument('-o', '--output')
    ap.add_argument('-W', '--width', type=int)
    ap.add_argument('-H', '--height', type=int)
    ap.add_argument('-f', '--format', choices=sorted(FORMATS))
    ap.add_argument('--offset', type=lambda s: int(s, 0), default=0x28, help='payload start in file (default 0x28 = header size)')
    ap.add_argument('--decompress', choices=sorted(DECOMP), default='lz4', help='default lz4 (what .texture files use)')
    ap.add_argument('--mip', type=int, default=0, help='mip level to export')
    ap.add_argument('--probe', action='store_true', help='inspect header / look for standard compression')
    ap.add_argument('--guess-header', action='store_true',
                    help='guess width/height/format from the u16 fields seen at 0x0C/0x0E/0x14')
    a = ap.parse_args()

    data = open(a.input, 'rb').read()
    if a.probe:
        return probe(data)

    if a.guess_header or not (a.width and a.height and a.format):
        gw, gh = struct.unpack_from('<HH', data, 0x0C)
        gf = struct.unpack_from('<H', data, 0x14)[0]
        a.width = a.width or gw
        a.height = a.height or gh
        a.format = a.format or ID2NAME.get(gf)
        print(f'header guess -> {a.width}x{a.height} {a.format}')
    if not a.format:
        sys.exit('unknown format, pass -f')

    payload = DECOMP[a.decompress](data[a.offset:])
    w, h, off = a.width, a.height, 0
    for _ in range(a.mip):
        off += level_size(a.format, w, h)
        w, h = max(1, w >> 1), max(1, h >> 1)
    need = level_size(a.format, w, h)
    if len(payload) < off + need:
        sys.exit(f'payload too short: have {len(payload)}, need {off + need} '
                 f'(data is probably compressed or the offset/size is wrong)')

    img = decode(a.format, payload[off:off + need], w, h)
    if img.shape[2] == 4 and (img[:, :, 3] == 255).all():
        img = img[:, :, :3]
    out = a.output or a.input.rsplit('.', 1)[0] + '.png'
    Image.fromarray(np.ascontiguousarray(img)).save(out)
    print(f'saved {out} ({w}x{h})')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""将 NapCat 的 1-bit 二维码 PNG 无损放大（纯标准库，不依赖 PIL）。"""
import struct
import sys
import zlib


def read_png(path):
    src = open(path, "rb").read()
    assert src[:8] == b"\x89PNG\r\n\x1a\n", "not a png"
    pos, idat = 8, b""
    w = h = bitdepth = ctype = None
    while pos < len(src):
        ln, typ = struct.unpack(">I4s", src[pos:pos + 8])
        pos += 8
        data = src[pos:pos + ln]
        pos += ln + 4
        if typ == b"IHDR":
            w, h, bitdepth, ctype = struct.unpack(">IIBB", data[:10])
        elif typ == b"IDAT":
            idat += data
    return w, h, bitdepth, ctype, zlib.decompress(idat)


def unfilter(w, h, bitdepth, raw):
    stride = (w * bitdepth + 7) // 8
    out, prev, p = bytearray(), bytearray(stride), 0
    for _ in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:
            for i in range(stride):
                line[i] = (line[i] + (line[i - 1] if i else 0)) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                a = line[i - 1] if i else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = line[i - 1] if i else 0
                b, c = prev[i], (prev[i - 1] if i else 0)
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out += line
        prev = line
    return out, stride


def upscale(src, dst, scale=6):
    w, h, bitdepth, _ctype, raw = read_png(src)
    data, stride = unfilter(w, h, bitdepth, raw)
    pix = bytearray()
    for y in range(h):
        row = data[y * stride:(y + 1) * stride]
        for x in range(w):
            bit = (row[x >> 3] >> (7 - (x & 7))) & 1
            pix.append(0 if bit else 255)
    nw, nh = w * scale, h * scale
    big = bytearray()
    for y in range(nh):
        row = pix[(y // scale) * w:(y // scale + 1) * w]
        line = bytearray()
        for v in row:
            line += bytes([v]) * scale
        big += b"\x00" + line

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", nw, nh, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(big), 9))
           + chunk(b"IEND", b""))
    open(dst, "wb").write(png)


if __name__ == "__main__":
    upscale(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 6)

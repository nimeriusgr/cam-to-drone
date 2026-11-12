#!/usr/bin/env python3
"""Draws the app icons (PNG) with the standard library only: a camera lens with four drone rotors around it.
Run once after changing the design:  python server/make_icons.py"""
import math
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
BG, FG, ACCENT = (10, 15, 21), (230, 237, 243), (59, 130, 246)


def png(path, w, h, px):
    raw = b"".join(b"\x00" + bytes(c for p in px[y * w:(y + 1) * w] for c in p) for y in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)  # noqa: E731
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ring(d, r, t):
    return max(0.0, min(1.0, t / 2 + 0.5 - abs(d - r)))


def disc(d, r):
    return max(0.0, min(1.0, r + 0.5 - d))


def mix(a, b, k):
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def icon(size, mono=False, pad=0.0):
    s = size / 512
    px = []
    rot = [(math.cos(a) * 150 * s, math.sin(a) * 150 * s) for a in (math.pi / 4, 3 * math.pi / 4, 5 * math.pi / 4, 7 * math.pi / 4)]
    for y in range(size):
        for x in range(size):
            cx, cy = x + 0.5 - size / 2, y + 0.5 - size / 2
            d = math.hypot(cx, cy) / (1 - pad)
            k_fg = max(ring(d, 92 * s, 22 * s), disc(d, 34 * s))
            for rx, ry in rot:
                dr = math.hypot(cx - rx / (1 + pad), cy - ry / (1 + pad))
                k_fg = max(k_fg, ring(dr, 46 * s, 14 * s))
                # arms from lens to rotors
                t = max(0.0, min(1.0, (cx * rx + cy * ry) / (rx * rx + ry * ry)))
                if 0.62 < t < 0.72:
                    k_fg = max(k_fg, disc(math.hypot(cx - t * rx, cy - t * ry), 7 * s))
            k_acc = disc(d, 34 * s)
            if mono:
                px.append((255, 255, 255, int(255 * k_fg)))
            else:
                c = mix(BG, FG, k_fg)
                c = mix(c, ACCENT, k_acc)
                px.append(c + (255,))
    return px


if __name__ == "__main__":
    for n in (192, 512):
        png(os.path.join(OUT, f"icon-{n}.png"), n, n, icon(n))
    png(os.path.join(OUT, "icon-maskable-512.png"), 512, 512, icon(512, pad=0.22))
    png(os.path.join(OUT, "badge-96.png"), 96, 96, icon(96, mono=True))
    print("icons written to", OUT)

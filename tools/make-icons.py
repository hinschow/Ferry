#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Ferry 的应用图标 —— 只用标准库，不依赖 Pillow。

    python3 tools/make-icons.py [输出目录]

产出 assets/ferry.png / ferry.ico / ferry.icns。
图案：蓝色圆角方块上一座白色的桥（两个桥墩 + 拱 + 桥面），
16px 下也还认得出是桥。4 倍超采样做抗锯齿。
"""
import math
import os
import struct
import sys
import zlib

SS = 4                     # 超采样倍数
BG1 = (0x17, 0x5C, 0xD3)   # 主色，与控制台的 C_ACCENT 一致
BG2 = (0x0B, 0x36, 0x8C)   # 渐变到深一点的蓝
FG = (0xFF, 0xFF, 0xFF)


def render(size):
    """返回 size×size 的 RGBA 字节串"""
    ss = SS if size <= 128 else 2      # 大图本身就够密，超采样降一档省时间
    n = size * ss
    # 先在放大画布上取样，再按 ss×ss 求平均 —— 边缘就有了抗锯齿
    rows = []
    for y in range(n):
        row = []
        for x in range(n):
            row.append(sample(x + 0.5, y + 0.5, n))
        rows.append(row)

    out = bytearray()
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for dy in range(ss):
                for dx in range(ss):
                    pr, pg, pb, pa = rows[y * ss + dy][x * ss + dx]
                    r += pr * pa; g += pg * pa; b += pb * pa; a += pa
            if a:
                out += bytes((r // a, g // a, b // a, a // (ss * ss)))
            else:
                out += b"\0\0\0\0"
    return bytes(out)


def sample(x, y, n):
    """画布坐标 -> 像素颜色。单位化到 0..1 好写形状。"""
    u, v = x / n, y / n

    # ---- 底：圆角方块
    if not _in_round_rect(u, v, 0.0, 0.0, 1.0, 1.0, 0.22):
        return (0, 0, 0, 0)
    t = (u + v) / 2.0
    bg = tuple(int(BG1[i] + (BG2[i] - BG1[i]) * t) for i in range(3))

    DECK_T, DECK_B, BASE = 0.335, 0.415, 0.665

    # ---- 桥面：横贯的一道白杠
    if DECK_T <= v <= DECK_B and 0.07 <= u <= 0.93:
        return FG + (255,)

    # ---- 拱：圆心落在桥面下方 R 处，取上半个圆环 —— 拱顶正好顶住桥面底
    # 各处留白至少要有 16px 图标下的 1.5 个像素，否则会糊成一团
    R, W = 0.245, 0.085
    cy = DECK_B + R
    if v <= cy and abs(math.hypot(u - 0.5, v - cy) - R) <= W / 2 and v <= BASE:
        return FG + (255,)

    # ---- 两侧桥墩：从桥面垂到基线，把拱的两条腿夹住
    for px in (0.115, 0.885):
        if abs(u - px) <= 0.045 and DECK_B <= v <= BASE:
            return FG + (255,)

    # ---- 水面：桥下两道波纹（长短不一，比对称更像水）
    for wy, x0, x1 in ((0.76, 0.13, 0.62), (0.87, 0.38, 0.87)):
        if abs(v - wy) <= 0.030 and x0 <= u <= x1:
            return FG + (255,)

    return bg + (255,)


def _in_round_rect(x, y, x0, y0, x1, y1, r):
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return math.hypot(x - cx, y - cy) <= r + 1e-9


# ---------------------------------------------------------------- PNG

def png(size, rgba):
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)                              # 每行的 filter type
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


# ---------------------------------------------------------------- ICO

def dib(size, rgba):
    """32 位 BGRA 的 DIB。小尺寸用它比 PNG 兼容性好。"""
    hdr = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    px = bytearray()
    for y in range(size - 1, -1, -1):              # DIB 自下而上
        for x in range(size):
            r, g, b, a = rgba[(y * size + x) * 4:(y * size + x) * 4 + 4]
            px += bytes((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4          # AND 掩码按 4 字节对齐
    return hdr + bytes(px) + b"\0" * (mask_stride * size)


def ico(images):
    n = len(images)
    out = struct.pack("<HHH", 0, 1, n)
    off = 6 + 16 * n
    body = b""
    for size, data in images:
        out += struct.pack("<BBBBHHII", size if size < 256 else 0,
                           size if size < 256 else 0, 0, 0, 1, 32, len(data), off)
        off += len(data)
        body += data
    return out + body


# ---------------------------------------------------------------- ICNS

def icns(entries):
    body = b""
    for tag, data in entries:
        body += tag + struct.pack(">I", len(data) + 8) + data
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
    os.makedirs(out_dir, exist_ok=True)

    cache = {}

    def px(s):
        if s not in cache:
            cache[s] = render(s)
        return cache[s]

    with open(os.path.join(out_dir, "ferry.png"), "wb") as fh:
        fh.write(png(256, px(256)))

    with open(os.path.join(out_dir, "ferry.ico"), "wb") as fh:
        fh.write(ico([(s, dib(s, px(s))) for s in (16, 32, 48, 64, 128)]
                     + [(256, png(256, px(256)))]))

    with open(os.path.join(out_dir, "ferry.icns"), "wb") as fh:
        fh.write(icns([(b"ic11", png(32, px(32))),      # 16pt @2x
                       (b"ic12", png(64, px(64))),      # 32pt @2x
                       (b"ic07", png(128, px(128))),
                       (b"ic08", png(256, px(256))),
                       (b"ic09", png(512, px(512)))]))

    for f in ("ferry.png", "ferry.ico", "ferry.icns"):
        p = os.path.join(out_dir, f)
        print(f"  {f:12} {os.path.getsize(p):>7} 字节")


if __name__ == "__main__":
    main()

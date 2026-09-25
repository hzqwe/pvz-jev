"""把 out/ 里的大截图降采样成 docs/ 用的小图（不依赖 Pillow）。

只支持**本项目自己写的 PNG**：`win32.Screen.save_png` 固定用
`filter type 0`（无过滤）+ 8bit RGB，所以解码就是 `zlib.decompress` 之后
每行丢掉第一个 filter 字节。外部工具生成的 PNG 不一定满足这个前提。

    python tools/make_doc_screenshot.py out/final_board2.png docs/screenshot.png --scale 3
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pvz.win32 import Screen  # noqa: E402


def read_rgb_png(path: str) -> tuple[int, int, bytearray]:
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    pos, w, h, idat = 8, 0, 0, bytearray()
    while pos < len(data):
        ln = struct.unpack(">I", data[pos : pos + 4])[0]
        tag = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + ln]
        pos += 12 + ln
        if tag == b"IHDR":
            w, h, bd, ct = struct.unpack(">IIBB", chunk[:10])
            if bd != 8 or ct != 2:
                raise ValueError(f"只支持 8bit RGB，实际 bit={bd} colortype={ct}")
        elif tag == b"IDAT":
            idat += chunk
        elif tag == b"IEND":
            break
    raw = zlib.decompress(bytes(idat))
    stride = w * 3 + 1
    px = bytearray(w * h * 3)
    for y in range(h):
        if raw[y * stride] != 0:
            raise ValueError(f"第 {y} 行 filter={raw[y * stride]}，本工具只处理 filter 0")
        px[y * w * 3 : (y + 1) * w * 3] = raw[y * stride + 1 : (y + 1) * stride]
    return w, h, px


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--scale", type=int, default=3)
    args = ap.parse_args()

    w, h, px = read_rgb_png(args.src)
    f = max(1, args.scale)
    W, H = w // f, h // f
    out = bytearray()
    for y in range(H):
        base = (y * f) * w * 3
        for x in range(W):
            i = base + (x * f) * 3
            r, g, b = px[i], px[i + 1], px[i + 2]
            out += bytes((b, g, r, 255))          # Screen 要 BGRA
    Screen(0, 0, W, H, bytes(out)).save_png(args.dst)
    print(f"{args.src} {w}x{h} -> {args.dst} {W}x{H} "
          f"({os.path.getsize(args.dst) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

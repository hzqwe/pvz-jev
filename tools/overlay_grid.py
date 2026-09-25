"""把候选网格/卡槽画到游戏截图上，用来肉眼验证几何对不对。

比"猜一个偏移然后祈祷"可靠得多：网格线落在格子边界上就是对的，
偏了就一眼看出来，还能直接读出差多少像素。

    python tools/overlay_grid.py                # 用 data/layout.json 的当前值
    python tools/overlay_grid.py --left 404 --top 240 --cw 197 --ch 259
    python tools/overlay_grid.py --raw          # 用屏幕像素（不换算逻辑坐标）
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvz import win32 as W                      # noqa: E402
from pvz.ui import Layout                       # noqa: E402

PROCESS_NAMES = ["PlantsVsZombies.exe", "pvzHE-Launcher.exe"]

RED = (255, 0, 0)
BLUE = (0, 128, 255)
YELLOW = (255, 230, 0)
WHITE = (255, 255, 255)
GREEN = (0, 255, 0)


def put(px: bytearray, w: int, x: int, y: int, rgb) -> None:
    if 0 <= x < w and 0 <= y < len(px) // (w * 4):
        i = (y * w + x) * 4
        px[i], px[i + 1], px[i + 2] = rgb[2], rgb[1], rgb[0]


def hline(px, w, x0, x1, y, rgb) -> None:
    for x in range(min(x0, x1), max(x0, x1) + 1):
        put(px, w, x, y, rgb)
        put(px, w, x, y + 1, rgb)


def vline(px, w, x, y0, y1, rgb) -> None:
    for y in range(min(y0, y1), max(y0, y1) + 1):
        put(px, w, x, y, rgb)
        put(px, w, x + 1, y, rgb)


def dot(px, w, x, y, rgb, r=5) -> None:
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                put(px, w, x + dx, y + dy, rgb)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--left", type=int)
    ap.add_argument("--top", type=int)
    ap.add_argument("--cw", type=float)
    ap.add_argument("--ch", type=float)
    ap.add_argument("--raw", action="store_true", help="参数按屏幕像素解释")
    ap.add_argument("--rows", type=int, default=5)
    ap.add_argument("--cols", type=int, default=9)
    ap.add_argument("--out", default="out/overlay.png")
    args = ap.parse_args()

    pid = W.find_pid(PROCESS_NAMES)
    win = W.find_game_window(pid) if pid else None
    if not win:
        print("没找到游戏窗口")
        return 1
    if win.client_size[0] <= 0:
        W.restore_window(win.hwnd)
        win = W.find_game_window(pid) or win
    scr = W.capture_window(win)
    if not scr:
        print("抓屏失败")
        return 1

    lay = Layout.load(client_size=(scr.w, scr.h))
    sx, sy = lay.scale()

    if args.left is not None:
        left = args.left
        top = args.top or 0
        cw = args.cw or 0
        ch = args.ch or 0
    else:
        left = lay.grid_left * sx
        top = lay.grid_top * sy
        cw = lay.cell_w * sx
        ch = lay.cell_h * sy

    print(f"截图 {scr.w}x{scr.h}  缩放 ({sx:.3f},{sy:.3f})")
    print(f"网格 left={left:.0f} top={top:.0f} cell={cw:.1f}x{ch:.1f} "
          f"({args.cols}列{args.rows}行)")
    print(f"草坪右下角 = ({left + cw * args.cols:.0f}, {top + ch * args.rows:.0f})")

    px = bytearray(scr.pixels)
    w = scr.w

    # 网格线
    for c in range(args.cols + 1):
        vline(px, w, int(left + c * cw), int(top), int(top + args.rows * ch), RED)
    for r in range(args.rows + 1):
        hline(px, w, int(left), int(left + args.cols * cw), int(top + r * ch), RED)
    # 格子中心
    for r in range(args.rows):
        for c in range(args.cols):
            cx = int(left + c * cw + cw / 2)
            cy = int(top + r * ch + ch / 2)
            dot(px, w, cx, cy, YELLOW, 4)

    # 卡槽
    print("\n卡槽:")
    for i in range(16):
        x, y = lay.card_center(i)
        x, y = int(x), int(y)
        x0, y0, cwd, chd = lay.card_rect(i)
        vline(px, w, x0, y0, y0 + chd, BLUE)
        vline(px, w, x0 + cwd, y0, y0 + chd, BLUE)
        hline(px, w, x0, x0 + cwd, y0, BLUE)
        hline(px, w, x0, x0 + cwd, y0 + chd, BLUE)
        dot(px, w, x, y, WHITE, 3)
        if i < 4 or i == 15:
            print(f"  #{i:2d} 中心 ({x:4d},{y:4d})  矩形 {x0},{y0} {cwd}x{chd}")

    out = W.Screen(scr.x, scr.y, scr.w, scr.h, bytes(px))
    out.save_png(args.out)
    print(f"\n已写出 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

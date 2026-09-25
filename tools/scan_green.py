"""在截图里找"绿色文字按钮"，并把行投影打出来。

为什么需要它：暂停菜单的按钮是**竖排**的（查看图鉴 / 重新开始 / 主菜单 / 返回游戏），
按估算坐标盲点，算错一点就会点到"主菜单"或"重新开始"——会毁掉用户的进度。
所以按钮位置必须**从像素里量出来**，不能猜。

绿色文字判定：g 明显高于 r 和 b。草坪也是绿的，所以默认把搜索区域限制在
石碑范围内（石碑是灰色，绿色文字在上面很突出）。

用法：
    python tools/scan_green.py                    # 用默认区域
    python tools/scan_green.py --x0 800 --x1 1800 --y0 150 --y1 1500
    python tools/scan_green.py --dump             # 额外打印若干采样点的颜色
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvz import win32 as W                    # noqa: E402

PROCESS_NAMES = ["PlantsVsZombies.exe", "pvzHE-Launcher.exe"]


def is_green(r: int, g: int, b: int) -> bool:
    return g > 110 and g > r + 35 and g > b + 35


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--x0", type=int, default=800)
    ap.add_argument("--x1", type=int, default=1800)
    ap.add_argument("--y0", type=int, default=150)
    ap.add_argument("--y1", type=int, default=1520)
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--profile", action="store_true", help="打印逐行剖面")
    ap.add_argument("--min-px", type=int, default=0, help="覆盖自动阈值")
    ap.add_argument("--save", default="out/scan.png")
    args = ap.parse_args()

    pid = W.find_pid(PROCESS_NAMES)
    if not pid:
        print("没找到游戏进程")
        return 1
    win = W.find_game_window(pid)
    if not win:
        print("没找到主窗口")
        return 1
    if win.client_size[0] <= 0:
        W.restore_window(win.hwnd)
        win = W.find_game_window(pid) or win

    scr = W.capture_window(win)
    if not scr:
        print("抓屏失败")
        return 1
    scr.save_png(args.save)
    print(f"抓屏 {scr.w}x{scr.h} @ ({scr.x},{scr.y}) -> {args.save}")

    x0 = max(0, args.x0 - scr.x)
    x1 = min(scr.w, args.x1 - scr.x)
    y0 = max(0, args.y0 - scr.y)
    y1 = min(scr.h, args.y1 - scr.y)
    print(f"搜索区域（截图局部坐标） x[{x0},{x1}) y[{y0},{y1})")

    rows: list[int] = []
    xhits: list[tuple[int, int]] = []          # 每行绿色像素的 (min_x, max_x)
    for y in range(y0, y1):
        n = 0
        lo, hi = -1, -1
        base = y * scr.w * 4
        for x in range(x0, x1):
            i = base + x * 4
            b, g, r = scr.pixels[i], scr.pixels[i + 1], scr.pixels[i + 2]
            if is_green(r, g, b):
                n += 1
                if lo < 0:
                    lo = x
                hi = x
        rows.append(n)
        xhits.append((lo, hi))

    # 找出连续的非空行段
    thresh = args.min_px or max(3, (x1 - x0) // 200)

    if args.profile:
        print("\n逐行剖面（每 8 行）:")
        for idx in range(0, len(rows), 8):
            n = rows[idx]
            bar = "#" * min(60, n // max(1, (x1 - x0) // 200))
            print(f"  y={y0 + idx:5d} {n:5d} {bar}")

    segs: list[tuple[int, int, int]] = []
    cur = None
    for idx, n in enumerate(rows):
        y = y0 + idx
        if n >= thresh:
            if cur is None:
                cur = [y, y, 0]
            cur[1] = y
            cur[2] = max(cur[2], n)
        else:
            if cur is not None:
                if cur[1] - cur[0] >= 4:      # 太薄的是噪点
                    segs.append((cur[0], cur[1], cur[2]))
                cur = None
    if cur is not None and cur[1] - cur[0] >= 4:
        segs.append((cur[0], cur[1], cur[2]))

    print(f"\n绿色行段（阈值 {thresh} 像素/行）:")
    for i, (a, b_, peak) in enumerate(segs):
        xs = [(lo, hi) for lo, hi in xhits[a - y0 : b_ - y0 + 1] if lo >= 0]
        if xs:
            blo = min(t[0] for t in xs)
            bhi = max(t[1] for t in xs)
            xinfo = f"  x {blo}..{bhi}  中心x={(blo + bhi) // 2}"
        else:
            xinfo = ""
        print(f"  #{i}  y {a}..{b_}   高{b_ - a + 1:4d}  峰值{peak:5d}  中心y={(a + b_) // 2}{xinfo}")

    if args.dump:
        print("\n采样点颜色（截图局部坐标）:")
        for y in range(y0, y1, max(1, (y1 - y0) // 30)):
            i = (y * scr.w + (x0 + x1) // 2) * 4
            b, g, r = scr.pixels[i], scr.pixels[i + 1], scr.pixels[i + 2]
            print(f"  y={y:5d}  rgb=({r:3d},{g:3d},{b:3d})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

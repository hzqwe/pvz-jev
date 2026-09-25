"""用竖直边缘检测找出草坪的格线 x 位置。

为什么不用"列平均亮度"：白天草坪本身有深浅斑块，亮度均值被斑块淹没，
看不出周期（试过了，确实看不出来）。

改用**边缘**：格线是相邻格之间的细暗线，会产生一条竖直的边缘。
做法：对一小段竖直条带，逐列计算 |dG/dx| 并沿 y 平均，峰值就是格线。

为了避开暂停菜单石碑，默认只取草坪左右两侧露出来的部分。

    python tools/find_gridlines.py
    python tools/find_gridlines.py --y0 245 --y1 330
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvz import win32 as W                      # noqa: E402

PROCESS_NAMES = ["PlantsVsZombies.exe", "pvzHE-Launcher.exe"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--y0", type=int, default=245)
    ap.add_argument("--y1", type=int, default=335)
    ap.add_argument("--x0", type=int, default=400)
    ap.add_argument("--x1", type=int, default=2180)
    ap.add_argument("--gap0", type=int, default=840, help="石碑左边界（跳过）")
    ap.add_argument("--gap1", type=int, default=1735, help="石碑右边界（跳过）")
    ap.add_argument("--thresh", type=float, default=1.6)
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
    px, st, w = scr.pixels, scr.w * 4, scr.w

    # 逐列的竖直边缘强度
    edge = []
    for x in range(args.x0, args.x1 + 1):
        s = 0.0
        n = 0
        for y in range(args.y0, args.y1):
            i = y * st + x * 4
            s += abs(px[i + 5] - px[i - 3])      # G(x+1) - G(x-1)
            n += 1
        edge.append(s / n)

    # 跳过石碑覆盖区
    def covered(x: int) -> bool:
        return args.gap0 <= x <= args.gap1

    peaks = []
    for i in range(2, len(edge) - 2):
        x = args.x0 + i
        if covered(x):
            continue
        if edge[i] >= args.thresh and edge[i] == max(edge[max(0, i - 5):i + 6]):
            if not peaks or x - peaks[-1][0] > 25:
                peaks.append((x, edge[i]))

    print(f"条带 y {args.y0}..{args.y1}   搜索 x {args.x0}..{args.x1}")
    print(f"边缘强度 min={min(edge):.2f} max={max(edge):.2f}")
    print(f"\n检出格线候选 {len(peaks)} 条:")
    for x, v in peaks:
        print(f"   x={x:5d}  强度 {v:.2f}")
    if len(peaks) >= 2:
        d = [peaks[i + 1][0] - peaks[i][0] for i in range(len(peaks) - 1)]
        print(f"\n相邻间距: {d}")
        inside = [t for t in d if not covered(peaks[d.index(t)][0])]
        print(f"平均间距 {sum(d) / len(d):.1f}px")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""诊断：暂停菜单里的绿色文字行段到底被检测成了什么。

背景：杂交版 v3.9.9 的暂停菜单是**自定义覆盖层**，只有一个按钮「返回游戏」，
不是我最初假设的 4 个（查看图鉴/重新开始/主菜单/返回游戏）。而
`find_pause_resume()` 里的安全阀是"恰好 4 个才返回"，于是它凑到 4 个
（标题「游戏暂停」、提示「点击返回游戏」、按钮「返回游戏」，再加上别的东西），
点到了错误的位置。

这个工具把中间窄带里所有绿色行段打印出来，并把框画回截图，用来重新定标。

    python tools/probe_pause.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvz import ui as U                        # noqa: E402
from pvz import win32 as W                     # noqa: E402

PROCESS_NAMES = ["PlantsVsZombies.exe", "pvzHE-Launcher.exe"]


def main() -> int:
    pid = W.find_pid(PROCESS_NAMES)
    win = W.find_game_window(pid) if pid else None
    if not win:
        print("没找到游戏窗口")
        return 1
    if win.client_size[0] <= 0:
        W.restore_window(win.hwnd)
        win = W.find_game_window(pid) or win

    shot = W.capture_window(win)
    if shot is None:
        print("抓屏失败")
        return 1
    print(f"截图 {shot.w}x{shot.h} @ ({shot.x},{shot.y})")

    x0 = shot.x + int(shot.w * 0.40)
    x1 = shot.x + int(shot.w * 0.60)
    y0 = shot.y + int(shot.h * 0.14)
    y1 = shot.y + int(shot.h * 0.96)
    print(f"窄带 x {x0}..{x1}   y {y0}..{y1}")

    for min_px in (2, 4, 8):
        segs = U.find_green_buttons(shot, x0, x1, y0, y1, min_px=min_px, min_height=10)
        print(f"\n--- min_px={min_px} 找到 {len(segs)} 段 ---")
        for i, s in enumerate(segs):
            h = s["y1"] - s["y0"]
            w = (s["x1"] - s["x0"]) if s["x1"] is not None else None
            print(f"  [{i}] y {s['y0']}..{s['y1']} (h={h})  x {s['x0']}..{s['x1']} (w={w})  "
                  f"cx={s['cx']} cy={s['cy']} peak={s['peak']}")

    # 画回截图：用绿色框标出检测到的段，用红点标出当前 find_pause_resume 的返回点
    segs = U.find_green_buttons(shot, x0, x1, y0, y1, min_px=4, min_height=10)
    px = bytearray(shot.pixels)
    stride = shot.w * 4

    def hline(y: int, xa: int, xb: int, rgb=(255, 0, 0)):
        if not (0 <= y < shot.h):
            return
        r, g, b = rgb
        for x in range(max(0, xa), min(shot.w, xb)):
            i = y * stride + x * 4
            px[i], px[i + 1], px[i + 2] = b, g, r

    def vline(x: int, ya: int, yb: int, rgb=(255, 0, 0)):
        if not (0 <= x < shot.w):
            return
        r, g, b = rgb
        for y in range(max(0, ya), min(shot.h, yb)):
            i = y * stride + x * 4
            px[i], px[i + 1], px[i + 2] = b, g, r

    for s in segs:
        # 画回时转成截图局部坐标
        yy0, yy1 = s["y0"] - shot.y, s["y1"] - shot.y
        hline(yy0, s["x0"] - shot.x, s["x1"] - shot.x, (0, 255, 255))
        hline(yy1, s["x0"] - shot.x, s["x1"] - shot.x, (0, 255, 255))
        vline(s["x0"] - shot.x, yy0, yy1, (0, 255, 255))
        vline(s["x1"] - shot.x, yy0, yy1, (0, 255, 255))

    xy = U.find_pause_resume(shot, fallback=None)
    print(f"\nfind_pause_resume -> {xy}")
    if xy:
        cx, cy = xy[0] - shot.x, xy[1] - shot.y
        for d in range(-25, 26):
            hline(cy + d, cx - 2, cx + 3, (255, 0, 255))
            vline(cx + d, cy - 2, cy + 3, (255, 0, 255))

    out = W.Screen(shot.x, shot.y, shot.w, shot.h, bytes(px))
    path = out.save_png("out/pause_probe.png")
    print(f"标注图 -> {path}")

    # 顺便把窄带裁出来放大，方便肉眼看
    xa = max(0, x0 - shot.x)
    xb = min(shot.w, x1 - shot.x)
    ya = max(0, y0 - shot.y)
    yb = min(shot.h, y1 - shot.y)
    cw, ch = xb - xa, yb - ya
    crop = bytearray()
    for y in range(ya, yb):
        base = y * stride
        crop += shot.pixels[base + xa * 4: base + xb * 4]
    W.Screen(xa, ya, cw, ch, bytes(crop)).save_png("out/pause_band.png")
    print(f"窄带裁图 -> out/pause_band.png ({cw}x{ch})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

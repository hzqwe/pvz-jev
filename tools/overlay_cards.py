"""把算出来的卡槽框画回截图，肉眼核对卡槽几何。

为什么必须做：卡槽点击"没反应"有两个完全不同的原因 ——
  (a) 消息没被游戏接收（焦点/注入方式问题）
  (b) 坐标算错了，点在两张卡的缝隙上
只看"阳光没扣"分不出是哪一种。把框画出来一看就知道。

    python tools/overlay_cards.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvz import win32 as W                     # noqa: E402
from pvz.ui import Layout                      # noqa: E402

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
    lay = Layout.load(client_size=win.client_size)
    print(f"客户区 {win.client_size}  缩放 {lay.scale()}")
    print(f"卡槽: first_x={lay.card_first_x} y={lay.card_y} w={lay.card_w} h={lay.card_h} "
          f"stride={lay.card_stride} per_row={lay.card_per_row} rows={lay.card_rows}")

    px = bytearray(shot.pixels)
    stride = shot.w * 4

    def put(x, y, rgb):
        if 0 <= x < shot.w and 0 <= y < shot.h:
            i = y * stride + x * 4
            px[i], px[i + 1], px[i + 2] = rgb[2], rgb[1], rgb[0]

    def hline(y, xa, xb, rgb=(255, 0, 255)):
        for x in range(max(0, xa), min(shot.w, xb)):
            put(x, y, rgb)

    def vline(x, ya, yb, rgb=(255, 0, 255)):
        for y in range(max(0, ya), min(shot.h, yb)):
            put(x, y, rgb)

    n = lay.card_rows * lay.card_per_row
    for i in range(n):
        x, y, w, h = lay.card_rect(i)
        # 转成截图局部坐标
        x -= shot.x
        y -= shot.y
        col = (0, 255, 0) if i % 2 == 0 else (0, 255, 255)
        hline(y, x, x + w, col)
        hline(y + h, x, x + w, col)
        vline(x, y, y + h, col)
        vline(x + w, y, y + h, col)
        cx, cy = lay.card_center(i)
        cx -= shot.x
        cy -= shot.y
        # 中心画一个小十字
        hline(cy, cx - 6, cx + 7, (255, 0, 0))
        vline(cx, cy - 6, cy + 7, (255, 0, 0))
        print(f"  卡{i:>2}: rect=({x},{y},{w},{h}) 中心=({cx},{cy})")

    # 草坪网格也画一下，顺便核对
    for r in range(5):
        for c in range(9):
            gx, gy = lay.cell_center(r, c)
            gx -= shot.x
            gy -= shot.y
            hline(gy, gx - 5, gx + 6, (255, 128, 0))
            vline(gx, gy - 5, gy + 6, (255, 128, 0))

    out = W.Screen(shot.x, shot.y, shot.w, shot.h, bytes(px))
    print(f"\n标注图 -> {out.save_png('out/cards_overlay.png')}")

    # 卡槽条单独裁出来（放大看）
    xa = max(0, 0)
    xb = min(shot.w, 2400)
    ya, yb = 0, min(shot.h, 200)
    cw, ch = xb - xa, yb - ya
    crop = bytearray()
    for y in range(ya, yb):
        base = y * stride
        crop += px[base + xa * 4: base + xb * 4]
    print(f"卡槽条裁图 -> {W.Screen(xa, ya, cw, ch, bytes(crop)).save_png('out/cards_bar.png')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""裁剪游戏截图的一块并存成 PNG，方便放大肉眼核对。"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pvz import win32 as W

ap = argparse.ArgumentParser()
ap.add_argument("--x0", type=int, required=True)
ap.add_argument("--y0", type=int, required=True)
ap.add_argument("--x1", type=int, required=True)
ap.add_argument("--y1", type=int, required=True)
ap.add_argument("--scale-y", type=float, default=1.0, help="竖直拉伸倍数（放大格线）")
ap.add_argument("--out", default="out/crop.png")
args = ap.parse_args()

pid = W.find_pid(["PlantsVsZombies.exe", "pvzHE-Launcher.exe"])
win = W.find_game_window(pid)
if win.client_size[0] <= 0:
    W.restore_window(win.hwnd)
    win = W.find_game_window(pid) or win
scr = W.capture_window(win)

cw = args.x1 - args.x0
ch = args.y1 - args.y0
sy = args.scale_y
oh = int(ch * sy)
out = bytearray(cw * oh * 4)
for oy in range(oh):
    sy_src = args.y0 + int(oy / sy)
    if sy_src >= scr.h:
        break
    row = sy_src * scr.w * 4
    for x in range(cw):
        si = row + (args.x0 + x) * 4
        di = (oy * cw + x) * 4
        out[di:di + 4] = scr.pixels[si:si + 4]

W.Screen(0, 0, cw, oh, bytes(out)).save_png(args.out)
print(f"{args.x0},{args.y0}..{args.x1},{args.y1} -> {args.out}  ({cw}x{oh})")

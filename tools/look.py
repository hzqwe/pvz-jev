"""看一眼游戏现在是什么状态。

比 probe.py 轻，专门解决三个反复踩到的坑：
  1. 窗口被最小化 -> 客户区 0x0、坐标 -32000，什么都读不到 -> 先 restore_window
  2. 分不清「暂停」还是「对局刚开局」-> 连读两次 game_clock，不动就是暂停
  3. 抓屏拿不到 -> 打印实际窗口几何，方便判断是全屏 DDraw 还是普通窗口

用法：
    python tools/look.py                 # 只看 + 抓屏到 out/look.png
    python tools/look.py --no-shot       # 不抓屏
    python tools/look.py --wait 2.0      # 两次读时钟的间隔秒数
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvz import offsets as O                     # noqa: E402
from pvz import win32 as W                       # noqa: E402
from pvz.board import BoardReader                # noqa: E402

PROCESS_NAMES = ["PlantsVsZombies.exe", "pvzHE-Launcher.exe"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-shot", action="store_true")
    ap.add_argument("--wait", type=float, default=1.5)
    ap.add_argument("--out", default="out/look.png")
    args = ap.parse_args()

    pid = W.find_pid(PROCESS_NAMES)
    if not pid:
        print("没找到游戏进程。")
        return 1
    print(f"pid = {pid}")

    win = W.find_game_window(pid)
    if not win:
        print("没找到主窗口。")
        return 1
    print("恢复前:", win.describe())

    if win.client_size[0] <= 0 or win.client_rect[0] < -10000:
        print("-> 窗口被最小化，尝试恢复…")
        W.restore_window(win.hwnd)
        time.sleep(0.4)
        win = W.find_game_window(pid) or win
    print("恢复后:", win.describe())

    with W.ProcessMemory(pid, "PlantsVsZombies.exe") as mem:
        lawn = mem.u32(mem.va(O.LAWN_PTR))
        board = mem.u32(lawn + O.OFF_BOARD) if lawn else None
        if not board:
            print(f"指针链断了: LawnApp={lawn!r} Board={board!r}")
            return 1
        print(f"LawnApp=0x{lawn:X}  Board=0x{board:X}")

        ui = mem.i32(lawn + O.OFF_GAME_UI)
        mode = mem.i32(lawn + O.OFF_GAME_MODE)
        scene = mem.i32(board + O.OFF_SCENE)
        sun = mem.i32(board + O.OFF_SUN)
        paused = mem.u8(board + O.OFF_GAME_PAUSED)
        clk1 = mem.i32(board + O.OFF_GAME_CLOCK)
        print(f"game_ui={ui}  game_mode={mode}  scene={scene}  sun={sun}")
        print(f"game_paused={paused}   game_clock={clk1}")

        time.sleep(args.wait)
        clk2 = mem.i32(board + O.OFF_GAME_CLOCK)
        d = (clk2 - clk1) if (clk1 is not None and clk2 is not None) else None
        print(f"game_clock={clk2}  (Δ={d} / {args.wait}s)")
        if d == 0:
            print("=> 时钟没走：游戏**暂停中**（菜单开着，或窗口没拿到焦点）")
        elif d is not None:
            print(f"=> 时钟在走，约 {d / args.wait:.0f} tick/s -> 对局进行中")

        reader = BoardReader(pid)
        st = reader.read()
        if st:
            from pvz.plants import PlantBook
            from pvz.serialize import render_text

            book = PlantBook()
            print()
            print(render_text(st, book))
            print(f"ok={st.ok}  {st.reason}")
            for n in st.notes:
                print("  ·", n)

    if not args.no_shot:
        scr = W.capture_window(win)
        if scr:
            path = scr.save_png(args.out)
            print(f"\n抓屏 OK -> {path}  ({scr.w}x{scr.h} @ {scr.x},{scr.y})")
        else:
            print("\n抓屏失败（客户区可能是 0x0）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

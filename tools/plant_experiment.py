"""受控实验：种一株植物，观察植物数组槽位怎么变。

要回答三个问题：
  Q1 空槽（未使用）和真实植物槽位怎么区分？—— 空槽全零，而 type=0 恰好是
     豌豆射手、row=col=0 恰好是 A1，所以"全零槽"会伪装成"R1A 的豌豆射手"，
     这正是"植物数 3 但只画出 2 株"和"点击后新植物出现在 r0c0"的真凶。
  Q2 Board+0xB0 / Board+0xB8 哪个是"容量"、哪个是"已用数量"？
  Q3 我们算出来的格子中心坐标到底对不对？（点 r4c0，看读回来的 row/col）

做法：关掉暂停菜单 -> dump 一次 -> 后台点击种一株 -> 等 1.5s -> 再 dump 一次
-> 打印差异。全程只读内存 + 注入鼠标点击，不写游戏内存。

    python tools/plant_experiment.py --slot 14 --row 4 --col 0
    python tools/plant_experiment.py --no-plant          # 只看当前状态
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvz import offsets as O                    # noqa: E402
from pvz import ui as U                         # noqa: E402
from pvz import win32 as W                      # noqa: E402
from pvz.plants import PlantBook                # noqa: E402
from pvz.ui import Clicker, Layout              # noqa: E402

PROCESS_NAMES = ["PlantsVsZombies.exe", "pvzHE-Launcher.exe"]

# 一个槽位里用来判断"到底有没有东西"的字段
SLOT_FIELDS = [0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C, 0x20, 0x24, 0x28]


def snapshot(pm: W.ProcessMemory, book: PlantBook, slots: int = 12) -> dict:
    lawn = pm.u32(pm.va(O.LAWN_PTR))
    board = pm.u32(lawn + O.OFF_BOARD) if lawn else None
    out: dict = {"board": board}
    if not board:
        return out
    out["sun"] = pm.i32(board + O.OFF_SUN)
    out["clock"] = pm.i32(board + O.OFF_GAME_CLOCK)
    out["ui"] = pm.i32(lawn + O.OFF_GAME_UI)
    out["B0"] = pm.i32(board + O.OFF_PLANT_COUNT_MAX)
    out["B8"] = pm.i32(board + O.OFF_PLANT_NEXT_POS)
    base = pm.u32(board + O.OFF_PLANT)
    out["base"] = base
    rows = []
    if base:
        for i in range(slots):
            a = base + i * O.PLANT_STRUCT
            raw = pm.read(a, 0x40) or b""
            nz = sum(1 for byte in raw if byte)
            rows.append({
                "i": i,
                "addr": a,
                "nonzero_bytes": nz,
                "f00": pm.u32(a + 0x00),
                "f04": pm.u32(a + 0x04),
                "f08": pm.u32(a + 0x08),
                "f0C": pm.u32(a + 0x0C),
                "row": pm.i32(a + O.P_ROW),
                "type": pm.i32(a + O.P_TYPE),
                "col": pm.i32(a + O.P_COL),
                "x": pm.f32(a + 0x2C),
                "y": pm.f32(a + 0x30),
                "dead": pm.u8(a + O.P_DEAD),
                "squish": pm.u8(a + O.P_SQUISHED),
                "asleep": pm.u8(a + O.P_ASLEEP),
            })
    out["slots"] = rows
    return out


def show(tag: str, s: dict, book: PlantBook) -> None:
    print(f"\n===== {tag} =====")
    if not s.get("board"):
        print("  读不到 Board")
        return
    print(f"  Board=0x{s['board']:X}  base=0x{s.get('base') or 0:X}  sun={s['sun']}  clock={s['clock']}  ui={s['ui']}")
    print(f"  Board+0x{O.OFF_PLANT_COUNT_MAX:03X}={s['B0']}   "
          f"Board+0x{O.OFF_PLANT_NEXT_POS:03X}={s['B8']}")
    print(f"  {'i':>2} {'非零字节':>6} {'f00':>10} {'row':>4} {'col':>4} {'type':>5} {'名称':<20} "
          f"{'x':>9} {'y':>8} {'dead':>4} {'sq':>3} {'asl':>3}")
    for r in s["slots"]:
        name = book.name(r["type"]) if isinstance(r["type"], int) and 0 <= r["type"] < 4096 else "?"
        print(f"  {r['i']:>2} {r['nonzero_bytes']:>6} {(r['f00'] or 0):>10} "
              f"{r['row']!s:>4} {r['col']!s:>4} {r['type']!s:>5} {name:<20} "
              f"{r['x']!s:>9} {r['y']!s:>8} {r['dead']!s:>4} {r['squish']!s:>3} {r['asleep']!s:>3}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=14)
    ap.add_argument("--row", type=int, default=4)
    ap.add_argument("--col", type=int, default=0)
    ap.add_argument("--slots", type=int, default=12)
    ap.add_argument("--no-plant", action="store_true")
    ap.add_argument("--foreground", action="store_true")
    args = ap.parse_args()

    pid = W.find_pid(PROCESS_NAMES)
    win = W.find_game_window(pid) if pid else None
    if not win:
        print("没找到游戏窗口")
        return 1
    if win.client_size[0] <= 0:
        W.restore_window(win.hwnd)
        win = W.find_game_window(pid) or win
    book = PlantBook()
    lay = Layout.load(client_size=win.client_size)

    with W.ProcessMemory(pid, "PlantsVsZombies.exe") as pm:
        # --- 关掉暂停菜单 ---
        W.focus_window(win.hwnd)
        time.sleep(0.4)
        for _ in range(3):
            c0 = pm.i32((pm.u32(pm.va(O.LAWN_PTR)) + O.OFF_BOARD) + O.OFF_GAME_CLOCK)
            time.sleep(1.0)
            c1 = pm.i32((pm.u32(pm.va(O.LAWN_PTR)) + O.OFF_BOARD) + O.OFF_GAME_CLOCK)
            if c0 != c1:
                print(f"时钟在走（{c0}->{c1}），对局进行中")
                break
            shot = W.capture_window(win)
            xy = U.find_pause_resume(shot, fallback=None) if shot else None
            src = "像素量测"
            if xy is None and lay.pause_resume():
                xy, src = lay.pause_resume(), "layout 兜底"
            if xy is None:
                print("时钟冻住但找不到「返回游戏」，退出")
                return 3
            print(f"时钟冻住 -> 点「返回游戏」@ {xy}（{src}，后台 PostMessage）")
            # ⚠️ 必须用后台 PostMessage。实测前台 SetCursorPos+mouse_event
            #    连点 3 次都关不掉菜单，而后台点击一次就生效（clock 立刻递增）。
            W.post_click(win.hwnd, xy[0] - win.client_rect[0], xy[1] - win.client_rect[1], 0.08)
            time.sleep(0.8)

        before = snapshot(pm, book, args.slots)
        show("点击前", before, book)

        if args.no_plant:
            return 0

        cx, cy = lay.card_center(args.slot)
        gx, gy = lay.cell_center(args.row, args.col)
        print(f"\n将点击：卡槽 {args.slot} @ ({cx},{cy})  格子 r{args.row}c{args.col} @ ({gx},{gy})")

        ck = Clicker(win, foreground=args.foreground)
        if args.foreground:
            W.focus_window(win.hwnd)
            time.sleep(0.3)
        ck.click_card(args.slot, lay, "pick")
        time.sleep(0.4)
        ck.click_grid(args.row, args.col, lay, "place")
        time.sleep(1.6)

        after = snapshot(pm, book, args.slots)
        show("点击后", after, book)

        # --- 差异 ---
        print("\n===== 差异 =====")
        print(f"  sun  {before.get('sun')} -> {after.get('sun')}"
              f"   (Δ={None if before.get('sun') is None or after.get('sun') is None else after['sun'] - before['sun']})")
        print(f"  B0   {before.get('B0')} -> {after.get('B0')}")
        print(f"  B8   {before.get('B8')} -> {after.get('B8')}")
        for b, a in zip(before.get("slots", []), after.get("slots", [])):
            keys = ("nonzero_bytes", "f00", "row", "col", "type", "x", "y", "dead")
            if any(b.get(k) != a.get(k) for k in keys):
                print(f"  槽 {b['i']}: " + "  ".join(f"{k} {b.get(k)}->{a.get(k)}" for k in keys
                                                    if b.get(k) != a.get(k)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

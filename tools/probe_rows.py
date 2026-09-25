"""逐格落点探针：把"点哪个坐标会种在哪一行"变成 ground truth。

为什么要单独做：草坪几何里 **只有行心是推断值**（量小推车 y 间距 + 假设"推车在行心
下方 68px"）。列是量出来的、行是推的 —— 所以"某一行的点击落空"这种故障必须
用实验定位，不能靠调参猜。

判据用 `cursor_grab`（手持种子）+ 植物数组回读，两者都不受阳光涨落影响：
  * 点卡后 `holding=0`        -> 卡没拿起来（冷却/阳光不够），本次不计
  * 落点后 `holding=1`        -> 落点被游戏拒绝，种子还在手上
  * 落点后多了植物            -> 成功，回读它的 (row,col) 与点击行对照

    python tools/probe_rows.py --slot 14 --col 3
    python tools/probe_rows.py --slot 14 --col 3 --rows 0,1,2,3,4
"""

from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pvz.board import PROCESS_NAMES, BoardReader  # noqa: E402
from pvz.ui import Clicker, Layout, find_pause_resume  # noqa: E402
from pvz.win32 import capture_window, find_game_window, find_pid, post_click, restore_window  # noqa: E402


def ensure_running(r: BoardReader, win) -> bool:
    restore_window(win.hwnd)
    time.sleep(0.4)
    for _ in range(3):
        a = r.read().game_clock
        time.sleep(0.7)
        if r.read().game_clock != a:
            return True
        s = capture_window(win, use_printwindow=True)
        xy = find_pause_resume(s) if s else None
        if xy is None:
            return False
        post_click(win.hwnd, xy[0] - win.client_rect[0], xy[1] - win.client_rect[1], 0.08)
        time.sleep(1.0)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=14)
    ap.add_argument("--col", type=int, default=3)
    ap.add_argument("--rows", default="0,1,2,3,4")
    ap.add_argument("--gap", type=float, default=0.45)
    args = ap.parse_args()

    pid = find_pid(PROCESS_NAMES)
    win = find_game_window(pid)
    r = BoardReader(pid)
    lay = Layout.load(client_size=win.client_size)
    ck = Clicker(win)

    if not ensure_running(r, win):
        print("❌ 无法让时钟走起来，先关掉暂停菜单再试")
        r.close()
        return 3
    print(f"窗口 {win.client_size}  卡槽{args.slot} 中心={lay.card_center(args.slot)}  "
          f"列{args.col} x={lay.cell_center(0, args.col)[0]}")

    for row in [int(x) for x in args.rows.split(",") if x.strip()]:
        x, y = lay.cell_center(row, args.col)
        ck.cancel_seed("reset")
        time.sleep(0.25)
        b0 = r.read()
        p0 = sorted((p.type_id, p.row, p.col) for p in b0.plants)
        slot = next((s for s in b0.slots if s.index == args.slot), None)
        if slot is None or not slot.ready:
            print(f"r{row + 1}c{args.col}: 卡槽{args.slot} 还在冷却 "
                  f"({slot.cd_left if slot else '?'}/{slot.cd_total if slot else '?'}) —— 等 8s")
            time.sleep(8.0)
            b0 = r.read()
            p0 = sorted((p.type_id, p.row, p.col) for p in b0.plants)
        ck.click_card(args.slot, lay)
        time.sleep(args.gap)
        mid = r.read()
        if not mid.holding:
            print(f"r{row + 1}c{args.col}: ❌ 卡没拿起来（跳过，不计入落点结论）")
            continue
        ck.click_grid(row, args.col, lay)
        time.sleep(0.9)
        a1 = r.read()
        new = [p for p in sorted((p.type_id, p.row, p.col) for p in a1.plants) if p not in p0]
        held = "还举着" if a1.holding else "已放下"
        verdict = "✅ 成功" if new else "❌ 落空"
        print(f"r{row + 1}c{args.col}: xy=({x},{y}) {verdict}  {held}  新增={new}  "
              f"sun {b0.sun}->{a1.sun}")
        if a1.holding:
            ck.cancel_seed("cleanup")
            time.sleep(0.3)
    r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

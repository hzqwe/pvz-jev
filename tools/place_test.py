"""落点测试：把"种一株植物"这件事的每一步都打出来，直到能稳定成功。

为什么单独做：agent 跑一整局时只能看到"种成 0"，看不出是哪一步断的。
这个工具按顺序验证并打印每一环：
  1. 窗口是否被最小化 / 客户区尺寸（=0x0 会让所有点击坐标变成 (0,0)）
  2. 布局缩放系数与目标坐标（缩放=0 是"点击完全没反应"的头号原因）
  3. 对局是否在跑（时钟冻住 = 暂停菜单挡着，点卡只会扣阳光不落点）
  4. 目标格子是否空着
  5. 点卡 -> 间隔 -> 点格子 -> 回读内存确认植物真的多了一株
  6. 失败就换另一种点击方式再试一次，并明确说出哪种能用

    python tools/place_test.py --slot 14 --row 3 --col 0
    python tools/place_test.py --slot 14 --row 3 --col 0 --no-focus
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
from pvz.board import BoardReader               # noqa: E402
from pvz.plants import PlantBook                # noqa: E402
from pvz.serialize import COL_LABEL             # noqa: E402
from pvz.ui import Clicker, Layout              # noqa: E402

PROCESS_NAMES = ["PlantsVsZombies.exe", "pvzHE-Launcher.exe"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=14)
    ap.add_argument("--row", type=int, default=3)
    ap.add_argument("--col", type=int, default=0)
    ap.add_argument("--no-focus", action="store_true")
    ap.add_argument("--no-dismiss", action="store_true")
    args = ap.parse_args()

    pid = W.find_pid(PROCESS_NAMES)
    if not pid:
        print("没找到游戏进程")
        return 1
    book = PlantBook()
    rd = BoardReader(pid)

    # --- 1. 窗口 ---
    win = W.find_game_window(pid)
    if not win:
        print("没找到游戏窗口")
        return 1
    print(f"[1] 恢复前: {win.describe()}")
    if win.client_size[0] <= 0:
        W.restore_window(win.hwnd)
        time.sleep(0.5)
        win = W.find_game_window(pid) or win
    print(f"    恢复后: {win.describe()}")
    if win.client_size[0] <= 0:
        print("    ❌ 客户区还是 0x0，无法继续")
        return 1

    # --- 2. 布局 ---
    lay = Layout.load(client_size=win.client_size)
    sx, sy = lay.scale()
    cx, cy = lay.card_center(args.slot)
    gx, gy = lay.cell_center(args.row, args.col)
    print(f"[2] 布局: 基准 {lay.base_w}x{lay.base_h} 客户区 {lay.client_w}x{lay.client_h} "
          f"缩放=({sx:.3f},{sy:.3f})")
    print(f"    草坪 rect={lay.lawn_rect()}   卡槽 {args.slot} 中心=({cx},{cy})   "
          f"格子 r{args.row}c{args.col} 中心=({gx},{gy})")
    if sx <= 0 or sy <= 0:
        print("    ❌ 缩放为 0，所有点击都会落到 (0,0)")
        return 1

    # --- 3. 对局是否在跑 ---
    if not args.no_dismiss:
        for attempt in range(3):
            c0 = rd.read().game_clock
            time.sleep(1.0)
            c1 = rd.read().game_clock
            print(f"[3] 尝试{attempt + 1}: game_clock {c0} -> {c1}")
            if c0 is not None and c1 is not None and c0 != c1:
                print(f"    ✅ 对局进行中（Δ={c1 - c0}/1.0s）")
                break
            shot = W.capture_window(win)
            xy = U.find_pause_resume(shot, fallback=None) if shot else None
            src = "像素量测"
            if xy is None and lay.pause_resume():
                xy, src = lay.pause_resume(), "layout 兜底"
            if xy is None:
                print("    ❌ 时钟冻住但找不到「返回游戏」按钮")
                return 3
            print(f"    时钟冻住 -> 后台点击「返回游戏」@ {xy}（{src}）")
            W.post_click(win.hwnd, xy[0] - win.client_rect[0], xy[1] - win.client_rect[1], 0.08)
            time.sleep(0.9)
        else:
            print("    ❌ 3 次都没能让时钟走起来")
            return 3

    # --- 4. 目标格子 ---
    st = rd.read()
    occ = st.occupancy()
    print(f"[4] 当前 阳光={st.sun} 植物={len(st.plants)} 僵尸={len(st.zombies)}  "
          f"ui={st.ui} clock={st.game_clock}")
    print(f"    植物: " + (", ".join(
        f"{book.name(p.type_id)}@r{p.row + 1}{COL_LABEL[p.col]}"
        for p in st.plants) or "（无）"))
    if (args.row, args.col) in occ:
        print(f"    ❌ 目标格子 r{args.row}c{args.col} 已被占用")
        return 2
    slot = next((s for s in st.slots if s.index == args.slot), None)
    if slot is None:
        print(f"    ❌ 没有下标为 {args.slot} 的卡槽")
        return 2
    cost = book.cost(slot.type_id)
    print(f"    卡槽 {args.slot} = {book.name(slot.type_id)} 就绪={slot.ready} 费用={cost}")
    if not slot.ready:
        print("    ❌ 该卡还在冷却")
        return 2
    if cost is not None and (st.sun or 0) < cost:
        print(f"    ❌ 阳光不够（{st.sun} < {cost}）")
        return 2

    # --- 5/6. 点击 ---
    def attempt(foreground: bool, label: str) -> bool:
        ck = Clicker(win, foreground=foreground)
        if not args.no_focus:
            W.focus_window(win.hwnd)
            time.sleep(0.4)
        before = {(p.row, p.col) for p in rd.read().plants}
        print(f"\n[5] {label}: 点卡({cx},{cy}) -> 等 0.35s -> 点格({gx},{gy})")
        ck.pick_and_place(args.slot, args.row, args.col, lay)
        time.sleep(1.3)
        after_st = rd.read()
        after = {(p.row, p.col) for p in after_st.plants}
        ok = (args.row, args.col) in after
        print(f"    阳光 {st.sun} -> {after_st.sun}   植物 {len(before)} -> {len(after)}")
        print(f"    {'✅ 种上了' if ok else '❌ 没种上'}")
        if not ok:
            ck.cancel_seed()
        return ok

    if attempt(False, "后台 PostMessage"):
        print("\n结论：后台点击可用 —— 直接跑 run_agent.py --live")
        rd.close()
        return 0
    print("\n后台不行，改前台真实光标…")
    if attempt(True, "前台 SetCursorPos+mouse_event"):
        print("\n结论：**必须加 --foreground**")
        rd.close()
        return 0
    print("\n结论：两种都不行。检查：目标格是否被墓碑/弹坑占住、该植物是否要求特殊地形。")
    rd.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

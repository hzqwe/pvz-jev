"""判定"点击注入到底有没有生效"—— 这是整条链路最后一个未知量。

做法（对照实验，不猜）：
  0. **前置**：让游戏窗口拿到焦点，并把暂停菜单关掉（PvZ 一旦失去焦点就自动弹
     暂停菜单，时钟停走）。用"连续两次读 game_clock 是否变化"确认真的在跑。
     —— 少了这一步，两种点击方式都会"没变化"，得出的结论是错的（踩过）。
  1. 读一次植物列表（含每株的 index/row/col/type）
  2. 用**后台 PostMessage** 种一株，等 1.2s，再读
  3. 没变化就换**前台 SetCursorPos+mouse_event** 再种一株，等 1.2s，再读
  4. 打印每一步的植物差异，明确说出是哪种方式生效

为什么要单独做：agent 跑一轮看不出"点击有没有被游戏接收"—— 阳光在涨、
决策在出、日志在写，全都正常，只有"植物没多"这一个信号能说明问题。
把它单独拎出来，结论才是确定的。

    python tools/click_test.py --slot 14 --row 4 --col 0
    python tools/click_test.py --slot 14 --row 4 --col 0 --skip-background
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvz import win32 as W                     # noqa: E402
from pvz import ui as U                        # noqa: E402
from pvz.board import BoardReader              # noqa: E402
from pvz.plants import PlantBook               # noqa: E402
from pvz.ui import Clicker, Layout             # noqa: E402

PROCESS_NAMES = ["PlantsVsZombies.exe", "pvzHE-Launcher.exe"]


def plant_set(reader: BoardReader) -> list[tuple[int, int, int]]:
    """草坪上到底有哪些植物 —— **判据只认这个**。

    ⚠️ 不要拿"整张快照字符串"当判据：阳光会自己涨（天降 + 自动收集），
    于是 `after != before` 永远为真，**一株没种上也会报"✅ 生效了"**。
    实测踩过：`sun=1050 -> 1100`、植物 0 株，脚本却说后台点击可用，
    差点把"点击注入失效"这个真问题放过去。
    """
    st = reader.read()
    return sorted((p.type_id, p.row, p.col) for p in st.plants)


def snapshot(reader: BoardReader, book: PlantBook) -> str:
    st = reader.read()
    items = sorted(
        (f"{book.name(p.type_id)}@r{p.row}c{p.col}(idx{p.index})" for p in st.plants)
    )
    return f"sun={st.sun} 植物{len(st.plants)}株: " + (", ".join(items) if items else "（空）")


def read_clock(reader: BoardReader) -> int | None:
    return reader.read().game_clock


def ensure_running(win, reader, lay, tries: int = 2) -> bool:
    """让游戏进入"时钟在走"的状态（拿到焦点 + 关掉暂停菜单）。

    返回 True 表示时钟确实在推进。这是**所有点击实验的前提**：
    暂停菜单挡在前面时，任何点击都只会落在菜单上，实验结论无意义。
    """
    print("\n=== 前置：确保对局在进行 ===")
    W.focus_window(win.hwnd)
    time.sleep(0.45)

    for attempt in range(1, tries + 1):
        c0 = read_clock(reader)
        time.sleep(1.2)
        c1 = read_clock(reader)
        print(f"  [尝试{attempt}] game_clock {c0} -> {c1}")
        if c0 is not None and c1 is not None and c1 != c0:
            print(f"  ✅ 时钟在走（Δ={c1 - c0}/1.2s），对局进行中")
            return True

        # 冻住了 —— 抓屏找「返回游戏」按钮
        shot = W.capture_window(win)
        if shot is None:
            print("  ❌ 抓屏失败")
            return False
        xy = U.find_pause_resume(shot, fallback=None)
        src = "像素量测"
        if xy is None:
            xy, src = lay.pause_resume(), "layout 兜底"
        if xy is None:
            print("  ❌ 时钟冻住，但找不到「返回游戏」按钮（不乱点）")
            return False
        print(f"  时钟冻住 -> 点「返回游戏」@ {xy}（{src}，后台 PostMessage）")
        # ⚠️ 必须用后台 PostMessage。实测前台 SetCursorPos+mouse_event 连点 3 次
        #    都关不掉菜单（clock 一直冻着），后台点击一次就生效。
        W.post_click(win.hwnd, xy[0] - win.client_rect[0], xy[1] - win.client_rect[1], 0.08)
        time.sleep(0.8)

    c0 = read_clock(reader)
    time.sleep(1.2)
    c1 = read_clock(reader)
    print(f"  [最终] game_clock {c0} -> {c1}")
    if c0 is not None and c1 is not None and c1 != c0:
        return True
    print("  ❌ 无法让时钟走起来 —— 请手动点一下「返回游戏」后重试")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=14, help="卡槽下标（默认 14=豌豆射手）")
    ap.add_argument("--row", type=int, default=4)
    ap.add_argument("--col", type=int, default=0)
    ap.add_argument("--skip-background", action="store_true")
    ap.add_argument("--no-ensure", action="store_true", help="跳过暂停菜单处理（调试用）")
    args = ap.parse_args()

    pid = W.find_pid(PROCESS_NAMES)
    win = W.find_game_window(pid) if pid else None
    if not win:
        print("没找到游戏窗口")
        return 1
    if win.client_size[0] <= 0:
        W.restore_window(win.hwnd)
        win = W.find_game_window(pid) or win
    print(f"窗口 {win.describe()}")

    reader = BoardReader(pid)
    book = PlantBook()
    lay = Layout.load(client_size=win.client_size)

    print(f"缩放 {lay.scale()}  （量测基准 {lay.base_w}x{lay.base_h}）")
    print(f"卡槽 {args.slot} 中心 = {lay.card_center(args.slot)}  rect={lay.card_rect(args.slot)}")
    print(f"格子 r{args.row}c{args.col} 中心 = {lay.cell_center(args.row, args.col)}")
    print(f"草坪 rect = {lay.lawn_rect()}")

    if not args.no_ensure:
        if not ensure_running(win, reader, lay):
            reader.close()
            return 3

    print("\n--- 点击前 ---")
    before = snapshot(reader, book)
    print(" ", before)

    def try_method(foreground: bool, label: str) -> bool:
        ck = Clicker(win, foreground=foreground)
        if foreground:
            W.focus_window(win.hwnd)
            time.sleep(0.3)
        base_plants = plant_set(reader)        # 每次都重新取基准，避免上一次的残留
        # 先点卡（选种），隔一点时间再点格子（放置）——
        # 两次点击之间必须有间隔，否则游戏可能还没把"手持种子"状态建立起来
        ck.click_card(args.slot, lay, f"{label}: pick card")
        time.sleep(0.35)
        ck.click_grid(args.row, args.col, lay, f"{label}: place")
        time.sleep(1.2)
        after = snapshot(reader, book)
        print(f"\n--- {label} 点击后 ---")
        print(" ", after)
        # ⚠️ 判据只认"草坪上多了植物"。阳光涨落不算数（见 plant_set 的说明）。
        new_plants = [p for p in plant_set(reader) if p not in base_plants]
        changed = bool(new_plants)
        print(f"  => {'✅ 生效了！新种下 ' + str(new_plants) if changed else '❌ 没变化（草坪上没多出植物）'}")
        return changed

    if not args.skip_background:
        if try_method(False, "后台 PostMessage"):
            print("\n结论：后台点击可用。")
            reader.close()
            return 0

    print("\n后台无效，改用前台真实光标…")
    if try_method(True, "前台 SetCursorPos+mouse_event"):
        print("\n结论：**必须用前台模式**（--foreground）。")
        reader.close()
        return 0

    print("\n结论：两种方式都没生效 —— 需要查：卡槽坐标对不对 / 该卡是否就绪 / 阳光够不够。")
    reader.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

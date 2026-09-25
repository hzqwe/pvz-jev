"""验证 Board+0x138 -> CursorObject+0x30 (cursor_grab) 能不能当"手持种子"信号。

为什么要这个：现在 `execute()` 用「点卡后阳光有没有掉」判断"种子有没有拿起来"。
但实测阳光会**自己涨**（400 -> 740 无人收集），所以这个判据会被污染：
真花了 100 阳光、同时又涨了 100，差值就是 0 -> 被误判成"点卡被拒" -> 记成本下界
-> 几轮后每株植物都"要 900 阳光" -> 表现就是「植物种不下去」。

`cursor_grab` 是游戏自己维护的状态，不受阳光波动影响，是这条链路唯一干净的判据。

    python tools/probe_cursor.py
"""

from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pvz.board import PROCESS_NAMES, BoardReader  # noqa: E402
from pvz.ui import Clicker, Layout, find_pause_resume, grab  # noqa: E402
from pvz.win32 import find_game_window, find_pid, post_click  # noqa: E402

OFF_CURSOR = 0x138
OFF_CURSOR_GRAB = 0x30


def dump(r: BoardReader, tag: str) -> dict:
    pm = r.pm
    board = r.board_ptr()
    cur = pm.u32(board + OFF_CURSOR) if board else None
    grab_v = pm.i32(cur + OFF_CURSOR_GRAB) if cur else None
    # 顺手扫一下 cursor 对象的前 0x60 字节，看有没有别的能当判据的字段
    raw = {}
    if cur:
        for off in range(0, 0x60, 4):
            raw[f"0x{off:02X}"] = pm.i32(cur + off)
    b = r.read()
    print(f"[{tag}] board=0x{board:X} cursor=0x{cur:X} grab={grab_v} "
          f"sun={b.sun} clock={b.game_clock} paused={b.paused}")
    return {"cur": cur, "grab": grab_v, "raw": raw, "sun": b.sun}


def main() -> int:
    r = BoardReader()
    pid = r.pid
    win = find_game_window(pid)
    lay = Layout.load(client_size=win.client_size)
    ck = Clicker(win)

    # 确保在跑
    for _ in range(3):
        a = r.read().game_clock
        time.sleep(0.6)
        if r.read().game_clock != a:
            break
        shot = grab(win)
        xy = find_pause_resume(shot) if shot else None
        if xy:
            print(f"时钟冻住 -> 点返回游戏 @ {xy}")
            post_click(win.hwnd, xy[0] - win.client_rect[0], xy[1] - win.client_rect[1], 0.08)
            time.sleep(1.0)
        else:
            print("时钟冻住且找不到按钮")
            break

    print("\n=== 1) 空手状态 ===")
    a = dump(r, "idle")

    print("\n=== 2) 点卡槽 14（阳光向日葵）后 ===")
    ck.click_card(14, lay, "pick card 14")
    time.sleep(0.5)
    b = dump(r, "holding")

    print("\n=== 3) 右键取消后 ===")
    ck.cancel_seed("cancel")
    time.sleep(0.5)
    c = dump(r, "cancelled")

    print("\n=== 结论 ===")
    print(f"  grab: 空手={a['grab']}  手持={b['grab']}  取消后={c['grab']}")
    print(f"  sun : 空手={a['sun']}  手持={b['sun']}  取消后={c['sun']}")
    if b["grab"] not in (0, None) and b["grab"] != a["grab"]:
        print("  ✅ cursor_grab 可以当'手持种子'判据")
        print("     cursor 对象原始字段：")
        for k, v in b["raw"].items():
            if v:
                print(f"       {k} = {v}")
    else:
        print("  ❌ cursor_grab 没变化，不能直接用（需要另找判据）")
    r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

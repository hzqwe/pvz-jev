"""现场诊断：一屏打印"游戏现在到底在什么状态"。

用途：`种不下植物` 这类故障需要区分「游戏没在跑」「窗口不对」「卡在冷却」
「阳光不够」「坐标错」五种情况。这个脚本一次把全部证据打出来。

    python tools/diag_live.py
"""

from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pvz import offsets as O  # noqa: E402
from pvz.board import PROCESS_NAMES, BoardReader  # noqa: E402
from pvz.win32 import (  # noqa: E402
    find_game_window,
    find_pid,
    is_foreground,
    list_windows,
    module_base,
)


def main() -> int:
    pid = find_pid(PROCESS_NAMES)
    print(f"pid={pid}  base=0x{(module_base(pid) or 0):X}" if pid else "pid=None")
    if pid:
        for w in list_windows(pid):
            print(f"  win {w.describe()}")
        g = find_game_window(pid)
        print(f"  -> find_game_window: {g.describe() if g else None}")
        if g:
            import ctypes

            u = ctypes.WinDLL("user32")
            fg = u.GetForegroundWindow()
            print(f"  foreground=0x{fg:X}  is_game_foreground={fg == g.hwnd}")
            print(f"  IsIconic={bool(u.IsIconic(g.hwnd))}")

    r = BoardReader()
    if not r.attached:
        print("!! 未附着:", r.notes)
        return 1

    samples = []
    for _ in range(7):
        b = r.read()
        samples.append((b.ui, b.scene, b.game_clock, b.sun, b.paused, len(b.plants), len(b.zombies)))
        time.sleep(0.5)

    print("\nui  scene  clock   sun   paused  plants  zombies")
    for s in samples:
        print("  ".join(str(x) for x in s))
    clocks = [s[2] for s in samples]
    print(f"\n时钟推进: {clocks[0]} -> {clocks[-1]}  "
          f"{'✅ 在走' if len(set(clocks)) > 1 else '❌ 冻住'}")

    b = r.read()
    print(f"\nok={b.ok} reason={b.reason!r} ui={b.ui} scene={b.scene} "
          f"level={b.level} sun={b.sun} clock={b.game_clock} paused={b.paused}")
    print(f"plants={[(p.row, p.col, p.type_id) for p in b.plants]}")
    print(f"zombies={[(z.row, z.type_id, round(z.x, 1) if z.x else None) for z in b.zombies]}")
    print("\nslots:")
    for s in b.slots:
        print(f"  [{s.index:>2}] type={s.type_id:>4} cd_left={s.cd_left:>6} "
              f"cd_total={s.cd_total:>6} ready={s.ready}")
    if b.notes:
        print("notes:", b.notes)
    print(f"\noffsets: UI_PLAYING={O.UI_PLAYING} OFF_GAME_CLOCK=0x{O.OFF_GAME_CLOCK:X}")
    r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

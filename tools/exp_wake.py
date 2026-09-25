"""决定性实验：最小化/恢复窗口 与 时钟是否推进 的关系。

    python tools/exp_wake.py restore     # 只做 SW_RESTORE，看时钟
    python tools/exp_wake.py cycle       # minimize -> restore
    python tools/exp_wake.py post        # 后台 PostMessage 点草坪中心
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes as wt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pvz.board import PROCESS_NAMES, BoardReader  # noqa: E402
from pvz.win32 import find_game_window, find_pid, is_foreground, post_click  # noqa: E402

u = ctypes.WinDLL("user32", use_last_error=True)
SW_MINIMIZE, SW_RESTORE = 6, 9


def snap(tag: str) -> int | None:
    r = BoardReader()
    b = r.read()
    w = find_game_window(r.pid) if r.pid else None
    fg = u.GetForegroundWindow()
    print(f"[{tag}] iconic={bool(u.IsIconic(w.hwnd)) if w else '?'} "
          f"client={w.client_size if w else '?'} fg=0x{fg:X} "
          f"is_fg={w and fg == w.hwnd} clock={b.game_clock} ui={b.ui} paused={b.paused}")
    r.close()
    return b.game_clock


def watch(tag: str, secs: float = 3.0) -> None:
    vals = []
    t0 = time.time()
    r = BoardReader()
    while time.time() - t0 < secs:
        vals.append(r.read().game_clock)
        time.sleep(0.4)
    r.close()
    print(f"  {tag}: {vals}  {'✅在走' if len(set(vals)) > 1 else '❌冻住'}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "restore"
    pid = find_pid(PROCESS_NAMES)
    w = find_game_window(pid)
    if not w:
        print("找不到窗口")
        return 1
    print(f"hwnd=0x{w.hwnd:X} iconic={bool(u.IsIconic(w.hwnd))}")
    snap("before")
    watch("before")

    if mode == "restore":
        u.ShowWindow(w.hwnd, SW_RESTORE)
        print("-> ShowWindow(SW_RESTORE)")
    elif mode == "cycle":
        u.ShowWindow(w.hwnd, SW_MINIMIZE)
        print("-> SW_MINIMIZE")
        time.sleep(0.4)
        u.ShowWindow(w.hwnd, SW_RESTORE)
        print("-> SW_RESTORE")
    elif mode == "post":
        post_click(w.hwnd, w.client_size[0] // 2 or 1280, w.client_size[1] // 2 or 800)
        print("-> post_click 客户区中心")
    elif mode == "fg":
        u.SetForegroundWindow(w.hwnd)
        print("-> SetForegroundWindow")

    time.sleep(1.2)
    snap("after")
    watch("after", 4.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

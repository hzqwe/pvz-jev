"""卡住的时候跑这个 —— 一屏看清"现在到底卡在哪"。

    python tools/diag_hang.py

只读，不碰任何窗口（唯一例外是可选的一张屏幕截图，而且走屏幕 DC）。
把输出整段发给我就行。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(line_buffering=True, errors="replace")
    except (AttributeError, ValueError):
        pass

from pvz.board import GAME_EXE_NAMES, BoardReader          # noqa: E402
from pvz.win32 import (                                     # noqa: E402
    capture_window,
    find_game_window,
    is_hung_window,
    is_process_alive,
    list_processes,
    list_windows,
    process_name,
)

LINE = "=" * 68
INTERESTING = ("zombie", "launcher", "plantsvszombies")


def section(t: str) -> None:
    print()
    print(f"--- {t} " + "-" * max(0, 62 - len(t)))


def parent_map() -> dict[int, tuple[str, int]]:
    """pid -> (exe名, 父pid)。用来解释"僵尸为什么清不掉"。"""
    TH32CS_SNAPPROCESS = 0x2

    class PE32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    out: dict[int, tuple[str, int]] = {}
    e = PE32()
    e.dwSize = ctypes.sizeof(PE32)
    if k.Process32First(snap, ctypes.byref(e)):
        while True:
            out[e.th32ProcessID] = (
                e.szExeFile.decode("gbk", "replace"), e.th32ParentProcessID
            )
            if not k.Process32Next(snap, ctypes.byref(e)):
                break
    k.CloseHandle(snap)
    return out


def main() -> int:
    print(LINE)
    print(f"  PvZ × Jev 卡死诊断   {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(LINE)

    procs = list_processes()
    parents = parent_map()

    # -- 1. 相关进程 -------------------------------------------------
    section("1. 相关进程（zombie / launcher / 游戏本体）")
    rel = [(p, n) for p, n in procs
           if any(k in n.lower() for k in INTERESTING)]
    if not rel:
        print("  （一个都没有 —— 游戏和启动器都没在跑）")
    for pid, name in rel:
        alive = is_process_alive(pid)
        ppid = parents.get(pid, ("?", 0))[1]
        pname = parents.get(ppid, ("(已退出)", 0))[0]
        kind = ""
        if not alive:
            kind = "  ← **僵尸**：已终止，进程对象还没被回收"
        elif name.lower() in GAME_EXE_NAMES:
            kind = "  ← 游戏本体"
        else:
            kind = "  ← 启动器/加载器"
        print(f"  {name:24} pid={pid:6} 存活={str(alive):5} 父={pname}(pid={ppid}){kind}")

    # -- 2. 窗口 ------------------------------------------------------
    section("2. 顶层窗口状态")
    for pid, name in rel:
        ws = list_windows(pid)
        if not ws:
            print(f"  {name} pid={pid}: 没有顶层窗口")
            continue
        for w in ws:
            hung = is_hung_window(w.hwnd)
            print(f"  {name} pid={pid}: hwnd=0x{w.hwnd:X} cls={w.cls!r}")
            print(f"      标题={w.title!r}")
            print(f"      客户区={w.client_size} 可见={w.visible} "
                  f"**卡住(IsHungAppWindow)={hung}**")
            if w.client_size[0] <= 0:
                print("      ⚠ 客户区是 0x0 → 窗口被**最小化**了，抓屏/点击全是废的")

    # -- 3. agent 会怎么判断 ------------------------------------------
    section("3. agent 侧会怎么判断（这就是它下一秒要做的事）")
    zombie = [(p, n) for p, n in rel if not is_process_alive(p)]
    if zombie:
        for pid, name in zombie:
            print(f"  ✗ {name} pid={pid} 是僵尸 → agent 会**跳过**它（不会去读它的内存）")
    game = [(p, n) for p, n in rel
            if is_process_alive(p) and n.lower() in GAME_EXE_NAMES]
    if not game:
        print("  ✗ 没有活着的游戏本体 → agent 会打印"
              "「未找到游戏进程」并安静等待（一个窗口操作都不做）")
    else:
        pid, name = game[0]
        w = find_game_window(pid)
        if w is None:
            print(f"  ✗ 游戏 pid={pid} 活着，但**找不到可用窗口**"
                  f"（进程死了 / 窗口卡住 / 客户区为 0）")
        else:
            print(f"  ✓ 游戏 pid={pid} 活着，窗口 0x{w.hwnd:X} 客户区={w.client_size}")
            r = BoardReader()
            try:
                if r.attach():
                    b = r.read()
                    print(f"      内存：ui={b.ui} clock={b.game_clock} "
                          f"sun={b.sun} paused={b.paused} 植物={len(b.plants)} "
                          f"僵尸={len(b.zombies)}")
                    print(f"      时钟={b.game_clock} → 5 秒后再读一次可判断游戏是否在更新")
                else:
                    print(f"      attach 失败：{r.notes}")
            finally:
                r.close()

    # -- 4. 解释"为什么清不掉" ----------------------------------------
    section("4. 为什么任务管理器结束不了")
    if zombie:
        for pid, name in zombie:
            ppid = parents.get(pid, ("?", 0))[1]
            pname = parents.get(ppid, ("(已退出)", 0))[0]
            holder = parents.get(ppid)
            if holder and is_process_alive(ppid):
                print(f"  {name} pid={pid} 已经**终止**了 —— 没有东西可杀。")
                print(f"  但 **{pname} (pid={ppid}) 还活着**，它没有 CloseHandle")
                print(f"  子进程句柄，所以内核不回收 pid={pid} 的进程对象。")
                print(f"  → 想让列表干净：**关掉 {pname}**（不是关游戏）。")
            else:
                print(f"  {name} pid={pid} 已终止，父进程也不在了；"
                      f"句柄可能被别的程序攥着，重启即可清掉。")
    else:
        print("  当前没有僵尸进程。")
    print()
    print("  ⚠️ 如果任务管理器点「结束任务」完全没反应，而且**桌面都动不了**，")
    print("     那不是僵尸，是游戏卡在**显示驱动**里了（全屏 DirectDraw 常见）。")
    print("     这种情况进程无法被终止，只能重启 —— 也正是我们要避免的状态。")

    # -- 5. 截图（可选，走屏幕 DC，纯读） ------------------------------
    section("5. 屏幕截图（纯读，不碰游戏窗口）")
    game_pid = next((p for p, n in rel
                     if is_process_alive(p) and n.lower() in GAME_EXE_NAMES), None)
    if game_pid is None:
        print("  （没有活着的游戏，跳过）")
    else:
        w = find_game_window(game_pid)
        if w is None or w.client_size[0] <= 0:
            print("  （窗口不可用，跳过）")
        else:
            shot = capture_window(w)
            if shot is None:
                print("  截图失败")
            else:
                out = os.path.join(ROOT, "out", "diag_hang.png")
                try:
                    shot.save_png(out)
                    print(f"  已存 -> {out}（{shot.w}x{shot.h}）")
                except Exception as exc:  # noqa: BLE001
                    print(f"  存图失败：{exc}")

    print()
    print(LINE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

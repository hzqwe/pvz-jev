"""卡死故障的回归测试（离线，不需要开游戏）。

对应 2026-09-26 那次真实故障：**用户退出 PvZ 之后 agent 把窗口拽回前台，
然后整个卡死，连任务管理器都退不出去**。定位到两个机制：

  A. **僵尸进程**：游戏退出后 pid 仍可枚举、`OpenProcess` 仍成功，
     `attach` 会"成功"并读到**定格的残留内存**（时钟不动、ui 还是 3=对局中），
     agent 以为在正常打一局；
  B. **跨进程窗口调用阻塞**：`ShowWindow` / `SetForegroundWindow` /
     `PrintWindow` 对**不再泵消息**的目标线程会**无限阻塞**，而 Python
     **无法中断阻塞中的 ctypes 调用** → Ctrl+C 失效、整个进程死掉。

这个脚本把修复变成 6 条可执行断言，任何一条挂了就说明回归了。

    python tools/selftest_hang.py
"""

from __future__ import annotations

import os
import subprocess
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

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(name)


def dead_pid() -> int:
    """起一个进程再让它退出，拿它的 pid（= 僵尸 pid）。"""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = p.pid
    p.wait()
    time.sleep(0.2)
    return pid


# ------------------------------------------------------------------ 1
def t1_dead_window_refused() -> None:
    """① 属于已退出进程的窗口，`_make_window_info` 必须拒绝（硬闸）。

    ⚠️ 必须用**真实存在**的 hwnd 来测，否则会"碰巧通过"：
    拿 hwnd=0 去测，`GetWindowRect(0)` 本来就会失败返回 None ——
    那测的是"无效句柄"，不是"死进程的窗口"。
    这里用真实顶层窗口的 hwnd，只把进程存活判定改成 False，
    这样"返回 None"就只能是硬闸造成的。
    """
    print("\n=== 1. 窗口句柄硬闸：死进程的窗口拿不到 WindowInfo ===")
    from pvz import win32 as W
    from pvz.win32 import _make_window_info, list_windows, is_process_alive

    pid = dead_pid()
    check("僵尸 pid 被判为已退出", is_process_alive(pid) is False, f"pid={pid}")

    wins = list_windows()
    check("list_windows() 有真实窗口可用（正对照的前提）", len(wins) > 0,
          f"{len(wins)} 个顶层窗口")
    if not wins:
        return
    real = wins[0]

    # 正对照：进程活着 → 同一个 hwnd 必须拿得到 WindowInfo
    alive_info = _make_window_info(real.hwnd, real.pid)
    check("正对照：活进程的窗口能拿到 WindowInfo", alive_info is not None,
          f"hwnd=0x{real.hwnd:X} pid={real.pid}")

    # 实验组：同一个 hwnd，只把"进程是否存活"翻成 False
    orig = W.is_process_alive
    W.is_process_alive = lambda p: False
    try:
        got = _make_window_info(real.hwnd, real.pid)
        listed = list_windows(real.pid)
    finally:
        W.is_process_alive = orig

    check("实验组：同一个 hwnd 被硬闸拒绝（返回 None）", got is None,
          f"返回 {got!r}")
    check("list_windows(死 pid) 返回空", listed == [], f"返回 {len(listed)} 个")


# ------------------------------------------------------------------ 1b
def t1b_list_windows_filters_dead() -> None:
    """①b `find_game_window` 对死进程必须返回 None（所有窗口操作的入口）。"""
    print("\n=== 1b. find_game_window(死 pid) -> None ===")
    from pvz.win32 import find_game_window, list_windows

    wins = list_windows()
    if not wins:
        check("有真实窗口可用于测试", False, "系统里没有顶层窗口")
        return
    pid = wins[0].pid
    live = find_game_window(pid)
    check("正对照：活进程能找到窗口", live is not None,
          f"pid={pid} -> {live.cls if live else None}")

    from pvz import win32 as W
    orig = W.is_process_alive
    W.is_process_alive = lambda p: False
    try:
        dead = find_game_window(pid)
    finally:
        W.is_process_alive = orig
    check("死进程找不到窗口", dead is None, f"返回 {dead!r}")


# ------------------------------------------------------------------ 2
def t2_game_gone_exits_fast() -> None:
    """② 游戏消失后必须**秒级**干净退出，且一次窗口操作都不做。

    ⚠️ 这是上一轮**没修好**的那条：旧判据 `had_game = self.reader.pid is not None`
    会被 `attach()` 清空 pid 抹掉，于是"游戏刚被关掉"被误判成"游戏从没出现过"，
    傻等到 --wait-play（默认 900s）。
    这里把窗口伪装成"仍然枚举得到"（真实故障就是这样），验证新判据仍然立刻退。
    """
    print("\n=== 2. 游戏消失 → 秒级干净退出（窗口还在也要退） ===")
    import pvz.agent as A
    from pvz.agent import AgentConfig, PvZJevAgent
    from pvz.board import BoardState
    from pvz.win32 import WindowInfo

    DEAD = 99999
    dummy_win = WindowInfo(
        hwnd=0, pid=DEAD, cls="MainWindow", title="fake",
        rect=(0, 0, 2560, 1600), client_rect=(0, 0, 2560, 1600),
        client_size=(2560, 1600), visible=True,
    )

    class StubReader:
        """模拟"进程已死但内存还读得到、窗口还枚举得到"。"""

        def __init__(self) -> None:
            self.pid = DEAD
            self.notes: list[str] = []
            self.ever_attached = True
            self.attach_calls = 0

        @property
        def attached(self) -> bool:
            return True

        def attach(self) -> bool:
            self.attach_calls += 1
            return False

        def read(self) -> BoardState:
            # 定格快照：ui=3（对局中）、clock 不动
            return BoardState(ok=True, pid=DEAD, ui=3, scene=0, sun=100,
                              game_clock=1234, paused=0)

        def close(self) -> None:
            pass

    win_ops: list[str] = []
    orig = {k: getattr(A, k) for k in
            ("find_game_window", "cycle_window", "restore_window",
             "focus_window", "sendinput_click")}
    A.find_game_window = lambda *a, **k: dummy_win
    for name in ("cycle_window", "restore_window", "focus_window", "sendinput_click"):
        A.__dict__[name] = (lambda n: (lambda *a, **k: win_ops.append(n)))(name)
    A.is_process_alive = lambda pid: False          # 进程已死
    A.PvZJevAgent.dismiss_pause = lambda self, b: False
    A.PvZJevAgent.wake_game = lambda self: False

    try:
        agent = PvZJevAgent(AgentConfig(dry_run=True, allow_window_ops=False,
                                        verbose=False,game_missing_timeout_s=0))
        agent.reader = StubReader()
        t0 = time.time()
        agent.run(duration_s=30.0, wait_play_s=900.0)
        dt = time.time() - t0
    finally:
        for k, v in orig.items():
            A.__dict__[k] = v

    check("退出耗时 < 3s（旧版本会等到 --wait-play 超时）", dt < 3.0,
          f"{dt:.2f}s")
    check("窗口操作次数 = 0", len(win_ops) == 0, f"实际 {win_ops}")


# ------------------------------------------------------------------ 3
def t3_safe_mode_never_touches_window() -> None:
    """③ 安全模式下 `wake_game` 不得调用任何改变窗口状态的函数。"""
    print("\n=== 3. 安全模式：绝不改变游戏窗口状态 ===")
    import pvz.agent as A
    from pvz.agent import AgentConfig, PvZJevAgent
    from pvz.win32 import WindowInfo

    calls: list[str] = []
    orig_cycle = A.cycle_window
    orig_send = A.sendinput_click
    A.cycle_window = lambda *a, **k: calls.append("cycle_window")
    A.sendinput_click = lambda *a, **k: calls.append("sendinput_click")
    try:
        agent = PvZJevAgent(AgentConfig(dry_run=True, allow_window_ops=False,
                                        verbose=False))
        agent.win = WindowInfo(hwnd=1, pid=1, cls="MainWindow", title="t",
                               rect=(0, 0, 100, 100), client_rect=(0, 0, 100, 100),
                               client_size=(100, 100), visible=True)
        agent.reader.pid = 1
        ok = agent.wake_game()
    finally:
        A.cycle_window = orig_cycle
        A.sendinput_click = orig_send

    check("安全模式 wake_game() 返回 False", ok is False)
    check("未调用任何窗口操作", calls == [], f"实际 {calls}")
    check("AgentConfig 默认 allow_window_ops=False",
          AgentConfig().allow_window_ops is False)


# ------------------------------------------------------------------ 4
def t4_timeout_guard() -> None:
    """④ 窗口操作超时护栏：卡住的操作必须被超时切断并拉黑。"""
    print("\n=== 4. 超时护栏：卡住的窗口操作被切断并永久拉黑 ===")
    from pvz import win32 as W

    fake_hwnd = 0xDEAD0001
    t0 = time.time()
    r = W._run_bounded(fake_hwnd, lambda: time.sleep(30), timeout=1.0)
    dt = time.time() - t0
    check("超时返回 None", r is None)
    check("耗时 ≈ 1.0s（没被拖住）", 0.8 < dt < 2.0, f"{dt:.2f}s")
    check("该 hwnd 已被永久拉黑", W.is_stuck(fake_hwnd) is True)
    check("拉黑后不再重试（立刻返回 None）",
          W._run_bounded(fake_hwnd, lambda: "should not run") is None)


# ------------------------------------------------------------------ 5
def t5_attach_recovers_from_dead_cache() -> None:
    """⑤ 缓存的 pid 变成僵尸时，attach 必须**重新查找**而不是直接放弃。"""
    print("\n=== 5. 缓存 pid 已死 → attach 重新查找 ===")
    from pvz.board import BoardReader

    pid = dead_pid()
    r = BoardReader()
    r.close()
    # 手工注入一个"僵尸缓存"：模拟"游戏关掉之后 self.pid 还留着旧值"
    r.pid = pid
    r.pm = None
    from unittest.mock import patch
    # Explicitly model absence; a real game may be running during this offline test.
    with patch('pvz.board.find_pid', return_value=None):
        ok = r.attach()
    notes = " ".join(r.notes)
    check("attach 返回 False（当前没有游戏本体）", ok is False)
    check("识别出缓存是僵尸并**重新查找**", "重新查找" in notes, f"notes={r.notes}")
    check("不再把死 pid 当游戏用（pid 已被清空）", r.pid != pid,
          f"pid={r.pid}")
    r.close()


# ------------------------------------------------------------------ 6
def t6_watchdog_force_exit() -> None:
    """⑥ 看门狗：主线程被卡住时必须强制结束进程（exit code 3）。"""
    print("\n=== 6. 看门狗：主线程卡住 → 强制结束进程 ===")
    script = (
        "import sys, time\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "from pvz.agent import AgentConfig, PvZJevAgent\n"
        "a = PvZJevAgent(AgentConfig(verbose=False))\n"
        "a._wd_stuck_after = 2.0\n"
        "a._start_watchdog()\n"
        "time.sleep(60)\n"          # 模拟"主线程被 ctypes 卡死"，永不更新心跳
    )
    t0 = time.time()
    p = subprocess.run([sys.executable, "-c", script], capture_output=True,
                       text=True, encoding='utf-8', timeout=30,
                       env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    dt = time.time() - t0
    out = (p.stdout or "") + (p.stderr or "")
    check("进程被强制结束（exit code 3）", p.returncode == 3,
          f"returncode={p.returncode}")
    check("约 2s 内结束", dt < 8.0, f"{dt:.2f}s")
    check("打出了可读的原因", "看门狗" in out, out.strip().splitlines()[-1:] or "")


# ------------------------------------------------------------------ 7
def t7_no_printwindow() -> None:
    """⑦ 定位暂停按钮时**绝不能用 PrintWindow**。

    `PrintWindow` 内部是 `SendMessage(WM_PRINT)` —— 它要求**目标去渲染一帧**。
    而用户实测杂交版"切屏进游戏时游戏自己会卡 5 秒"（重建 primary surface），
    这个函数**只在时钟冻住时**被调用，也就是恰好在那 5 秒里 →
    逼一个正在做全屏模式切换的 DirectDraw 游戏渲染 = 把它卡进显示驱动。
    """
    print("\n=== 7. 暂停按钮定位：绝不用 PrintWindow ===")
    import pvz.agent as A
    from pvz.agent import AgentConfig, PvZJevAgent
    from pvz.win32 import WindowInfo

    calls: list[object] = []
    orig = A.capture_window

    def fake_capture(win, use_printwindow=False):
        calls.append(use_printwindow)
        return None

    A.capture_window = fake_capture
    try:
        agent = PvZJevAgent(AgentConfig(dry_run=True, verbose=False))
        agent.win = WindowInfo(hwnd=1, pid=1, cls="MainWindow", title="t",
                               rect=(0, 0, 100, 100), client_rect=(0, 0, 100, 100),
                               client_size=(100, 100), visible=True)
        agent._find_resume_xy()
    finally:
        A.capture_window = orig

    check("确实抓了屏（正对照：函数没被短路）", len(calls) > 0, f"{len(calls)} 次")
    check("全部 use_printwindow=False", all(c is False for c in calls),
          f"实际 {calls}")


# ------------------------------------------------------------------ 8
def t8_no_injection_when_frozen() -> None:
    """⑧ 时钟没走时，`execute()` 一条鼠标消息都不许发。

    ⚠️ 这条覆盖的是**用户实际踩到的那个 bug**：09:06 那次运行的日志里
    `clock=57`（关卡刚开始 0.57s）、`game_responsive=false`、点卡被拒 ——
    agent 的点击正好撞进了游戏切屏入场的 5 秒冻结。
    """
    print("\n=== 8. 时钟没走 → 绝不注入点击 ===")
    import pvz.agent as A
    from pvz.agent import AgentConfig, PvZJevAgent
    from pvz.board import BoardState, SeedSlot
    from pvz.plants import PlantBook
    from pvz.policy import Candidate, Decision

    injected: list[str] = []

    class StubClicker:
        def click_client(self, *a, **k):
            injected.append("click_client")

        def click_grid(self, *a, **k):
            injected.append("click_grid")

        def click_card(self, *a, **k):
            injected.append("click_card")

        def cancel_seed(self, *a, **k):
            injected.append("cancel_seed")

        def pick_and_place(self, *a, **k):
            injected.append("pick_and_place")

    class StubReader:
        def __init__(self, clock):
            self.clock = clock
            self.pid = 1

        def read(self):
            return BoardState(ok=True, pid=1, ui=3, sun=400,
                              game_clock=self.clock, slots=[SeedSlot(2,9,0,1000)])

    dec = Decision(action_id="A1", candidate=Candidate(
        cid="A1", kind="plant", row=0, col=0, slot=2, type_id=9))

    # --- 实验组：时钟冻住（决策时 57，动手前还是 57） ---
    agent = PvZJevAgent(AgentConfig(dry_run=False, verbose=False))
    agent.clicker = StubClicker()
    agent.layout = agent.layout or __import__("pvz.ui", fromlist=["Layout"]).Layout()
    agent.reader = StubReader(57)
    agent._frozen_hits = 1                      # 上一轮读到的时钟和本轮相同
    rec1: dict = {}
    agent.execute(dec, rec1, BoardState(ok=True, pid=1, ui=3, sun=400, game_clock=57))
    check("时钟没走 → 被拦下", rec1.get("executed", {}).get("kind")
          == "skipped_not_responsive", f"executed={rec1.get('executed')}")
    check("一条鼠标消息都没发", injected == [], f"实际 {injected}")

    # --- 正对照：时钟在走 → 必须放行（别把闸做成"全禁"） ---
    injected.clear()
    agent2 = PvZJevAgent(AgentConfig(dry_run=False, verbose=False))
    agent2.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
    agent2.clicker = StubClicker()
    agent2.layout = agent.layout
    agent2.reader = StubReader(200)             # 动手前时钟已经前进
    agent2._frozen_hits = 0
    agent2._advances = 99                       # 已确认"时钟在走"（见 WARMUP_ADVANCES）
    rec2: dict = {}
    agent2.execute(dec, rec2, BoardState(ok=True, pid=1, ui=3, sun=400, game_clock=57))
    check("正对照：时钟在走 → 正常动手", rec2.get("executed", {}).get("kind") != "skipped_not_responsive",
          f"executed kind={rec2.get('executed', {}).get('kind')}")
    check("正对照：确实注入了点击", len(injected) > 0, f"{injected}")

    # --- 正对照 2：时钟在走但**还没热身够** → 也要拦（开局头几轮） ---
    injected.clear()
    agent3 = PvZJevAgent(AgentConfig(dry_run=False, verbose=False))
    agent3.clicker = StubClicker()
    agent3.layout = agent.layout
    agent3.reader = StubReader(200)
    agent3._frozen_hits = 0
    agent3._advances = 0                        # 还没攒够 WARMUP_ADVANCES
    rec3: dict = {}
    agent3.execute(dec, rec3, BoardState(ok=True, pid=1, ui=3, sun=400, game_clock=57))
    check("正对照 2：还没热身够 → 也拦下", rec3.get("executed", {}).get("kind")
          == "skipped_not_responsive", f"executed={rec3.get('executed')}")


# ------------------------------------------------------------------ 9
def t9_grace_period_no_touch() -> None:
    """⑨ 时钟冻住的头 PAUSE_GRACE_S 秒：不抓屏、不点击、不碰窗口。

    复刻用户场景：关卡刚开始（clock=57 冻住）→ agent 什么都不该做。
    """
    print("\n=== 9. 冻结宽限期：头几秒完全不碰游戏 ===")
    import pvz.agent as A
    from pvz.agent import AgentConfig, PvZJevAgent
    from pvz.board import BoardState
    from pvz.win32 import WindowInfo

    captured: list[str] = []
    clicked: list[str] = []

    class StubClicker:
        def __init__(self, *a, **k):
            pass

        def click_client(self, *a, **k):
            clicked.append("click_client")

        def click_grid(self, *a, **k):
            clicked.append("click_grid")

        def click_card(self, *a, **k):
            clicked.append("click_card")

        def cancel_seed(self, *a, **k):
            clicked.append("cancel_seed")

        def pick_and_place(self, *a, **k):
            clicked.append("pick_and_place")

    class StubReader:
        def __init__(self):
            self.pid = 1
            self.notes: list[str] = []
            self.ever_attached = True

        @property
        def attached(self):
            return True

        def attach(self):
            return True

        def read(self):
            # 冻结快照：关卡刚开始、时钟一动不动
            return BoardState(ok=True, pid=1, ui=3, scene=0, sun=400,
                              game_clock=57, paused=0)

        def close(self):
            pass

    dummy = WindowInfo(hwnd=0, pid=1, cls="MainWindow", title="fake",
                       rect=(0, 0, 2560, 1600), client_rect=(0, 0, 2560, 1600),
                       client_size=(2560, 1600), visible=True)

    orig = {k: getattr(A, k) for k in ("capture_window", "Clicker",
                                       "find_game_window")}
    A.capture_window = lambda *a, **k: (captured.append("capture"), None)[1]
    A.Clicker = StubClicker
    A.find_game_window = lambda *a, **k: dummy
    A.is_process_alive = lambda pid: True
    try:
        agent = PvZJevAgent(AgentConfig(dry_run=False, allow_window_ops=False,
                                        verbose=False))
        agent.reader = StubReader()
        t0 = time.time()
        agent.run(duration_s=3.5, wait_play_s=60.0)
        dt = time.time() - t0
    finally:
        for k, v in orig.items():
            A.__dict__[k] = v

    check("进入宽限期（_frozen_since 已置位）", agent._frozen_since is not None)
    check("宽限期内一次屏都没抓", captured == [], f"实际 {captured}")
    check("宽限期内一次点击都没注入", clicked == [], f"实际 {clicked}")
    check("没有提前调用 Jev（省了钱和时间）", agent.stats.decisions == 0,
          f"decisions={agent.stats.decisions}")


# ------------------------------------------------------------------ 10
def t10_healthy_game_not_blocked() -> None:
    """⑩ 正对照：游戏正常在跑时，这些闸**不能**把正常流程一起拦死。

    没有这条，"全拦死"的实现也能让前面所有测试通过 —— 那是典型的
    "在错误的原因上通过"。
    """
    print("\n=== 10. 正对照：游戏正常在跑 → 闸必须放开 ===")
    import pvz.agent as A
    from pvz.agent import AgentConfig, PvZJevAgent
    from pvz.board import BoardState
    from pvz.policy import Decision
    from pvz.win32 import WindowInfo

    class TickReader:
        """时钟每读一次前进 5 —— 模拟"游戏正常在跑"。"""

        def __init__(self):
            self.pid = 1
            self.notes: list[str] = []
            self.ever_attached = True
            self.clock = 1000

        @property
        def attached(self):
            return True

        def attach(self):
            return True

        def read(self):
            self.clock += 5
            return BoardState(ok=True, pid=1, ui=3, scene=0, sun=400,
                              game_clock=self.clock, paused=0)

        def close(self):
            pass

    dummy = WindowInfo(hwnd=0, pid=1, cls="MainWindow", title="f",
                       rect=(0, 0, 2560, 1600), client_rect=(0, 0, 2560, 1600),
                       client_size=(2560, 1600), visible=True)

    orig_fgw, orig_decide = A.find_game_window, A.PvZJevAgent.decide
    A.find_game_window = lambda *a, **k: dummy
    A.is_process_alive = lambda pid: True
    # 不真的调 Jev：直接返回"保留阳光"。这条测的是闸，不是决策。
    A.PvZJevAgent.decide = lambda self, b: (
        Decision(action_id="W", candidate=None, hold=True), {"t": 0}
    )
    try:
        agent = PvZJevAgent(AgentConfig(dry_run=False, allow_window_ops=False,
                                        verbose=False, decide_every_s=1.0,
                                        collect_sun_every_s=1.0))
        agent.reader = TickReader()
        agent.run(duration_s=3.0, wait_play_s=60.0)
    finally:
        A.find_game_window = orig_fgw
        A.PvZJevAgent.decide = orig_decide

    check("正常游玩时决策照常发生", agent.stats.decisions >= 2,
          f"decisions={agent.stats.decisions}")
    check("没有任何一次被误拦", agent.stats.input_ignored == 0,
          f"input_ignored={agent.stats.input_ignored}")
    check("确认过时钟在走（_advances 涨起来了）", agent._advances >= 2,
          f"_advances={agent._advances}")


def main() -> int:
    print("=" * 62)
    print("  卡死故障回归测试（离线）")
    print("=" * 62)
    for fn in (t1_dead_window_refused, t1b_list_windows_filters_dead,
               t2_game_gone_exits_fast,
               t3_safe_mode_never_touches_window, t4_timeout_guard,
               t5_attach_recovers_from_dead_cache, t6_watchdog_force_exit,
               t7_no_printwindow, t8_no_injection_when_frozen,
               t9_grace_period_no_touch, t10_healthy_game_not_blocked):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILED.append(f"{fn.__name__} 抛异常: {exc}")
    print("\n" + "=" * 62)
    if FAILED:
        print(f"  ✗ 失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"    - {f}")
        return 1
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

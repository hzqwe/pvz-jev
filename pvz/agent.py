"""主循环：感知 -> 序列化 -> Jev -> 执行。

安全默认值：`dry_run=True`。默认只做决策、写日志、不点鼠标，
确认无误后再用 `--live` 真正操作游戏。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from .board import BoardReader, BoardState, placement_delta
from .jev import DecisionLog, JevClient
from .plants import PlantBook, load_lineups, match_lineup
from .policy import Decision, build_questions, generate_candidates, merge_decision, action_is_current, escalate_emergency
from .serialize import COL_LABEL, build_state, render_text
from .tactics import cell_x
from .transactions import run_transaction
from .ui import Clicker, Layout, SunTracker, collect_suns, find_pause_resume, grab
from .win32 import (
    capture_window,
    cycle_window,
    find_game_window,
    focus_window,
    is_foreground,
    is_process_alive,
    is_stuck,
    restore_window,
    sendinput_click,
)

# 时钟冻住时最多"抢救"多少次，超过就彻底收手只等。
# ⚠️ 必须有这个上限：用户切到别的虚拟桌面/别的应用时，时钟会一直冻着，
#    旧代码每 3 轮抢一次焦点、无限循环 —— 既烦人，也是在反复戳一个可能已经
#    卡住的窗口（那正是把 agent 自己卡死的那类调用）。
FOCUS_RETRY_LIMIT = 12

# ★★ 「时钟冻住」后的**静默宽限期**（秒）—— 2026-09-26 第二次修复新增。
#
# 用户实测：**杂交版切屏进入游戏时，游戏自己会卡约 5 秒**（全屏模式切换、
# 重新获取 primary surface）。那 5 秒里游戏主线程**不泵消息** ——
# 也就是所有跨进程窗口调用都会阻塞、所有点击都会被无视的**脆弱窗口期**。
#
# 而"时钟冻住"这个信号**恰好**会在这 5 秒里触发（clock 不动），
# 于是旧代码会**立刻**去抓屏 + 找暂停菜单 + 点「返回游戏」——
# 正好一头撞进最脆弱的时刻。实测证据：日志里 `clock=57`（关卡刚开始 0.57 秒）
# 那一条决策，`game_responsive=false`、点卡被拒，就是撞进了入场冻结。
#
# 所以：**时钟一冻住，先什么都别做**，只读内存，安静等它自己缓过来。
# 超过宽限期还没缓过来（说明真的是暂停了，不是入场过渡）才去点菜单。
# 8 > 5，留了余量。
PAUSE_GRACE_S = 8.0

# ★★ 启动/刚切回游戏后，必须先连续确认这么多轮"时钟在走"，才允许动手。
#
# 为什么需要：用户实测杂交版**切屏进入游戏时游戏自己会卡约 5 秒**。
# 刚启动或刚切回来时，头几轮读到的 `game_clock` 就是冻的，而
# `_frozen_hits` 要连续 3 轮才判定"暂停"，中间那 1 秒里旧代码照常
# 收阳光 + 决策 + 点卡 —— 实测证据（09:06 那次运行）：
#   `clock=57`（关卡刚开始 0.57s）就点了卡，`game_responsive=false`、被拒。
# 攒够 2 轮只多花约 0.7 秒，但把"往正在做全屏切换的消息队列里塞鼠标消息"
# 这件事从"每次开局必发生"变成"不发生"。
WARMUP_ADVANCES = 2


@dataclass
class AgentConfig:
    dry_run: bool = True
    foreground: bool = False
    decide_every_s: float = 3.0
    collect_sun_every_s: float = 1.0
    max_actions_per_min: int = 20
    log_path: str = "out/decisions.jsonl"
    verbose: bool = True
    # ★★ 默认**不允许改变游戏窗口的状态**（2026-09-26 改成安全默认值）。
    #
    # 为什么：用户实测"按回车瞬间进游戏，然后直接卡死，连任务管理器都退不出去"。
    # 一按回车 agent 就 `ShowWindow(SW_RESTORE)` + `SetForegroundWindow` 把游戏
    # 拽到前台 —— 对 **DirectDraw 游戏**来说这是最危险的动作：它会丢 primary
    # surface，一旦重建失败游戏就整个卡住、占住屏幕，桌面也跟着一起没反应。
    #
    # 而"抢焦点"本来就不是必需的：实测**后台 PostMessage 点击就能种植物**，
    # 不需要前台光标、也不需要窗口处于激活状态。
    # 所以默认只做**只读**的事（读内存、抓屏、PostMessage 点击），
    # 绝不碰窗口状态。需要时用 `--allow-window-ops` 显式打开。
    allow_window_ops: bool = False


@dataclass
class AgentStats:
    cycles: int = 0
    decisions: int = 0
    executed: int = 0
    failed_actions: int = 0
    pick_rejected: int = 0
    input_ignored: int = 0      # 点击落在"游戏没在处理输入"的状态上（暂停/最小化）
    cooling: int = 0            # 目标卡还在冷却，直接跳过（不是"太贵"）
    holds: int = 0
    fallbacks: int = 0
    jev_errors: int = 0
    suns_collected: int = 0
    started: float = field(default_factory=time.time)

    def summary(self) -> str:
        dt = time.time() - self.started
        return (
            f"运行 {dt:.0f}s | 循环 {self.cycles} | 决策 {self.decisions} "
            f"| 种成 {self.executed} | 落点失败 {self.failed_actions} "
            f"| 卡被拒 {self.pick_rejected} | 点击无效(游戏没跑) {self.input_ignored} "
            f"| 冷却中跳过 {self.cooling} | 保留阳光 {self.holds} | 兜底 {self.fallbacks} "
            f"| Jev 失败 {self.jev_errors} | 收阳光 {self.suns_collected}"
        )


class PvZJevAgent:
    def __init__(self, config: AgentConfig | None = None, api_key: str | None = None):
        self.cfg = config or AgentConfig()
        self.book = PlantBook()
        self.reader = BoardReader()
        self.jev = JevClient(api_key=api_key)
        self.log = DecisionLog(self.cfg.log_path)
        self.stats = AgentStats()
        self.layout: Layout | None = None
        self.clicker: Clicker | None = None
        self.win = None
        self._last_decision = 0.0
        self._last_sun = 0.0
        self._action_times: list[float] = []
        self._last_clock: int | None = None
        self._frozen_hits = 0
        self._binding_checked = False
        self._binding_warned = False
        # 只有允许动窗口时才需要"首次抢一次焦点"
        self._need_focus = self.cfg.allow_window_ops
        self._warned_minimized = False
        self._pause_fail_streak = 0
        # 时钟"第一次被判定为冻住"的时刻。用来实现 PAUSE_GRACE_S 宽限期：
        # 冻住的头几秒**什么都不做**（见 PAUSE_GRACE_S 的说明）。
        self._frozen_since: float | None = None
        self._grace_notified = False
        # 连续多少轮确认"时钟在走"。见 WARMUP_ADVANCES。
        self._advances = 0
        self._window_miss = 0
        # 游戏进程连续多少次读不到。用来区分"用户把游戏关了"和"窗口暂时找不到"。
        self._dead_misses = 0
        # ★★ 最近一次**亲眼见过**的游戏 pid（粘性，不随 reader.pid 被清空而丢失）。
        #    见 run() 顶部那段说明：判断"游戏是不是被关了"必须靠这个，
        #    不能靠"窗口枚举得到吗"—— 进程正在退出时窗口还会被枚举到几秒，
        #    那几秒里旧代码会拿着**定格的残留内存**去问 Jev、去点鼠标。
        self._seen_pid: int | None = None
        # 看门狗心跳：主循环每轮更新一次。超过阈值没更新 = 主线程被卡住了。
        self._heartbeat = time.time()
        self._wd_stop = threading.Event()
        # 主线程"多久没动"就认为它被卡死了。窗口操作都有 3s 超时护栏，
        # 正常一轮最多几十秒（含一次 Jev 调用），所以 180s 是非常宽松的阈值。
        self._wd_stuck_after = 180.0
        # STOP 文件出现后，给主线程多少秒自己收尾；超时就强制退出。
        self._wd_stop_grace = 10.0
        # 紧急停止开关：这个文件一出现就立刻结束本轮。见 run() 里的说明。
        self._stop_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "STOP"
        )
        # 最近落点失败的格子 -> 时间戳。见 execute() 末尾的说明。
        self._bad_cells: dict[tuple[int, int, int], float] = {}
        self._geometry_error = None
        self._terrain_context = None
        self._last_decision_clock = None
        self._bad_cell_ttl = 45.0
        # 连续 hold（等待）计数：复盘里"阳光≥400 还连等 9 轮"就是它缺位的后果。
        self._hold_streak = 0
        self._hold_warned_at = 0.0
        # 上一次"长时间卡死后重试唤醒"的时刻（ops 模式限频用）。
        self._last_stall_wake = 0.0
        # 阳光收集的假阳性抑制（见 ui.SunTracker 的长注释）。
        self.sun_tracker = SunTracker()

    # -- 卡槽绑定 -------------------------------------------------------
    def sync_binding(self, board: BoardState) -> None:
        """确保种子栏的 type_id 已经绑定到植物名字。

        ⚠️ 为什么放在主循环里而不是"跑之前手工绑一次"：**杂交版每局阵容都不同**，
        用户换个卡池 agent 就瞎了。而绑定需要的输入（卡槽顺序）只有在关卡里才读得到，
        所以最自然的时机就是第一轮读到 Board 的时候。

        ⚠️ 杂交版把原版 ID **就地替换**了（ID 2=阳光炸弹而不是樱桃炸弹、
        ID 20=樱桃辣椒而不是火爆辣椒…），所以原版名字表在这游戏上完全不可信，
        没绑定等于 Jev 在看假名字 —— 比看裸 ID 更糟。

        校验：先用 `lineups.json` 的 `type_ids` **指纹**确认是同一副牌；
        指纹一致才落盘。冷却只作参考（图鉴的「冷却速度」不是种子包冷却）。
        最终校验是运行时的阳光差值，见 `execute()`。
        """
        types = [s.type_id for s in board.slots]
        active = [t for t in types if t >= 0]
        if not active:
            return
        if not self.book.unbound_ids(active):
            self._binding_checked = True
            return
        if self._binding_warned:
            return
        lineups = load_lineups()
        lineup, how = match_lineup(lineups, types)
        if lineup is None:
            self._binding_warned = True
            print(f"[绑定] ⚠️ 卡槽有 {len(active)} 张牌、其中 "
                  f"{len(self.book.unbound_ids(active))} 个 ID 没有功能登记，"
                  f"但 data/lineups.json 里没有匹配的 lineup。"
                  f"请截图卡槽栏并补一条 lineup，然后跑 tools/bind_cards.py。")
            return
        rep = self.book.bind_lineup(
            active, list(lineup.get("order") or []),
            cd_totals=[s.cd_total for s in board.slots],
        )
        self._binding_checked = True
        if how != "type_ids":
            self._binding_warned = True
            print(f"[绑定] ⚠️ 只按「卡数」匹配到 lineup「{lineup.get('id')}」，"
                  f"**type_id 指纹不一致**（内存 {active} vs 记录 "
                  f"{lineup.get('type_ids')}），名字顺序没被证实 —— 不落盘。")
        else:
            self.book.save_ids()
            print(f"[绑定] ✅ lineup「{lineup.get('id')}」type_id 指纹一致，"
                  f"绑定 {len(rep['bound'])} 个卡槽并写入 data/plant_ids.json")
        for row in rep["bound"]:
            print(f"         slot {row['slot']:>2} -> {row['name']} "
                  f"(id={row['type_id']}, cost={row['cost']})")

    def describe_deck(self, board: BoardState) -> str:
        parts = []
        for s in board.slots:
            if s.type_id < 0:
                continue
            cost = self.book.cost(s.type_id)
            nm = self.book.name(s.type_id)
            parts.append(f"[{s.index}]{nm}({cost})")
        return "  ".join(parts)

    # -- 准备 -----------------------------------------------------------
    def refresh_window(self) -> bool:
        """重新查询窗口真实状态（**不要用缓存的 WindowInfo**）。

        ⚠️ 这个函数是被一个真实故障逼出来的：原来 `ensure_window` 判断
        `self.win.client_size[0] > 0` 就认为窗口健康 —— 但 `self.win` 是**创建时的
        快照**，窗口后来被最小化它也不会更新。于是 agent 全程以为窗口好好的，
        既不 `restore_window` 也不抢焦点，**抓屏全是废的**：
        暂停菜单按钮的像素定位拿不到 → 退回不准的兜底坐标 → 一直卡在暂停菜单里，
        表现成"点卡没反应、植物种不下去"。实测跑了 150 秒、37 次决策、一次没种上。
        """
        if not self.reader.attached and not self.reader.attach():
            return False
        win = find_game_window(self.reader.pid)
        if win is None:
            # 窗口没了有两种可能：进程重启（pid 变了）或枚举暂时失败。
            # ⚠️ 旧代码在这里直接 return False，主循环就只会打印
            #    "未找到游戏窗口/进程，2s 后重试…" 直到时长耗尽 ——
            #    实测一个 240s 的 run 里有 25 秒全在刷这一行，期间**一次恢复动作都没做**。
            #    现在重新 attach 一次（会重新按优先级找 pid）。
            if self.reader.attach():
                win = find_game_window(self.reader.pid)
            if win is None:
                return False
        if win.client_size[0] <= 0:
            # 最小化的窗口：客户区是 0x0、坐标 -32000，抓屏/点击全是废的。
            # 实测 `ShowWindow(SW_RESTORE)` 一次就能恢复（client 0x0 -> 2560x1600），
            # 但 DirectDraw 窗口重建 surface 有时慢，所以重试 3 次而不是 1 次。
            #
            # ⚠️★ 2026-09-26 起**默认不做这个恢复动作**（见 AgentConfig.allow_window_ops）。
            #    理由：对 DirectDraw 游戏 ShowWindow 是最危险的一类调用 ——
            #    它会丢 primary surface，重建失败就整个卡死、占住屏幕。
            #    安全模式下只提示用户自己把游戏切回前台。
            self.win = win
            if not self.cfg.allow_window_ops:
                if not self._warned_minimized:
                    self._warned_minimized = True
                    print("[窗口] ⚠ 游戏窗口是最小化状态，而当前是安全模式"
                          "（不动窗口）—— 请手动点一下任务栏把游戏切回前台。")
                return False
            # 进程已经没了就更不能碰：对着一个正在退出的窗口 ShowWindow
            # 会永久阻塞，把 agent 自己也卡死。
            if not is_process_alive(self.reader.pid):
                return False
            for _ in range(3):
                restore_window(win.hwnd)
                time.sleep(0.3)
                win = find_game_window(self.reader.pid) or win
                if win.client_size[0] > 0:
                    break
            if win.client_size[0] <= 0:
                self.win = win
                return False
            # 刚从最小化恢复：客户区几何变了，layout/clicker 都要重建，
            # 而且必须抢一次焦点（见下面 focus 的说明）
            self._need_focus = True
        self._warned_minimized = False
        size_changed = self.win is None or self.win.client_size != win.client_size
        self.win = win
        if size_changed or self.layout is None:
            self.layout = Layout.load(client_size=win.client_size)
            self.clicker = Clicker(win, foreground=self.cfg.foreground)
            if self.cfg.verbose:
                sx, sy = self.layout.scale()
                print(f"[窗口] client={win.client_size} 缩放=({sx:.3f},{sy:.3f}) "
                      f"卡0中心={self.layout.card_center(0)} 格(0,0)中心={self.layout.cell_center(0, 0)}")
        if self._need_focus and self.cfg.allow_window_ops:
            # ⚠️ 抢焦点**只在允许动窗口时**才做。
            #    它唯一的作用是让 PvZ 重新开始处理输入 —— 但实测**后台
            #    PostMessage 点击本来就能种植物**，不需要前台光标。
            #    而 ShowWindow(SW_RESTORE) 对 DirectDraw 窗口是危险动作
            #    （见 AgentConfig.allow_window_ops 的说明）。
            #    ⚠️ 进程没了就别抢：`SetForegroundWindow` 同样会阻塞。
            if is_process_alive(self.reader.pid) and not is_stuck(win.hwnd):
                focus_window(win.hwnd)
            self._need_focus = False
            time.sleep(0.15)
        return True

    def ensure_window(self) -> bool:
        return self.refresh_window()

    def _clock_advances(self, wait: float = 0.5) -> bool:
        """时钟在不在走 —— 判断"游戏是否真的在更新"的唯一可靠信号。"""
        a = self.reader.read().game_clock
        time.sleep(wait)
        b = self.reader.read().game_clock
        return a is not None and b is not None and a != b

    def wake_game(self) -> bool:
        """把游戏从**"静默暂停"**里拽回来。

        ⚠️ 什么是"静默暂停"，为什么单靠关菜单救不了：
        PvZ 失去焦点后会停止更新（`game_clock` 冻住），但杂交版**不一定显示
        暂停菜单** —— 实测抓屏看到的就是一块正常的草坪，`find_pause_resume`
        一个绿色按钮都找不到，所以"点返回游戏"这条路根本没有目标可点。
        此时 `SetForegroundWindow` 也无效：窗口**本来就已经是前台窗口**，
        调用是 no-op，**不会产生 WM_ACTIVATE**，游戏内部的 mActive 就一直停在
        false。迷惑性极强：窗口在最前面、画面正常、进程活着，就是时钟不动、
        点卡一点反应都没有（点卡被拒 → 还会被误记成"植物太贵"）。

        解法分两步，缺一不可：
        1. `minimize -> restore` 制造一次真实的激活转换；
        2. 还不行就补一次**真实的鼠标输入**（`SendInput`）。

        ⚠️ 为什么第 2 步也必需：实测只做第 1 步，日志会老老实实打
        `[唤醒] ❌ 时钟仍冻住` —— 光"把窗口弄到前面"不够，PvZ 需要一次
        真实的输入事件才恢复更新。点击落在**阳光数字区域**：PvZ 里点它
        没有任何副作用（不会种植物、不会开面板）。
        """
        if self.win is None:
            return False
        # ★★ 安全模式：**不做任何改变窗口状态的动作**（2026-09-26 新增）。
        #    `cycle_window`（minimize→restore）和 `sendinput_click`（真实移动光标）
        #    正是"用户一按回车就卡死"的头号嫌疑 —— 对 DirectDraw 游戏，
        #    minimize/restore 会让它丢掉 primary surface，重建失败就整个卡死、
        #    占住屏幕，连任务管理器都出不来。
        #    安全模式下只保留"点返回游戏"（后台 PostMessage，完全不改变窗口状态），
        #    拽不回来就安静等着，绝不闪窗口。
        if not self.cfg.allow_window_ops:
            if self.cfg.verbose:
                print("[唤醒] 安全模式：不闪窗口 / 不抢焦点 —— 跳过唤醒"
                      "（需要完整唤醒请用 --allow-window-ops）")
            return False
        # ⚠️ 进程已经没了、或者窗口已经被判定卡住 → **什么都别做**。
        #    对着一个正在退出的窗口 minimize/restore，可能把调用者自己也拖死
        #    （见 pvz/win32.py 顶部那段护栏说明）。
        if not is_process_alive(self.reader.pid) or is_stuck(self.win.hwnd):
            if self.cfg.verbose:
                print("[唤醒] 游戏进程已消失或窗口已卡住 → 不做任何窗口操作")
            return False
        if self.cfg.verbose:
            print("[唤醒] 画面里没有暂停菜单 → 制造一次真实的激活 + 输入")
        cycle_window(self.win.hwnd)
        # 客户区几何可能变，重建 layout/clicker；焦点已经由 cycle 拿到
        self._need_focus = False
        if not self.refresh_window():
            return False
        if self._clock_advances(0.55):
            if self.cfg.verbose:
                print("[唤醒] ✅ 时钟恢复（仅靠激活）")
            return True

        # ⚠️★ 关键补丁（2026-09-26）：**激活本身不会关掉已经弹出的暂停菜单。**
        #    旧代码在这里直接去点阳光数字区，等于隔着一层菜单在点草坪 ——
        #    菜单不消失，时钟永远不会恢复，于是日志稳定地打 `❌ 时钟仍冻住`，
        #    连续三次之后放弃，整局就卡死在菜单前面。
        #    正确顺序：激活 → **重新找一次菜单按钮** → 找不到才用真实鼠标输入。
        #    实测：游戏最小化再恢复后，菜单会变成"失焦自动弹"的那一层
        #    （按钮在 (1261,1195)，和 ESC 菜单的 (1270,1288) 不是同一个），
        #    而 `find_pause_resume` 对两者都返回正确坐标。
        if self.dismiss_pause(self.reader.read()):
            if self.cfg.verbose:
                print("[唤醒] ✅ 时钟恢复（激活 + 点掉菜单）")
            return True

        # 第 2 步：真实鼠标输入
        # ⚠️ 这是**真实移动光标并点击**（不是后台消息）。如果游戏已经不在了，
        #    这一下会点到光标底下任意一个窗口上（用户的浏览器、编辑器…），
        #    非常危险 —— 所以动手前再确认一次进程还活着。
        if not is_process_alive(self.reader.pid) or is_stuck(self.win.hwnd):
            if self.cfg.verbose:
                print("[唤醒] 游戏已退出 → 取消真实鼠标点击")
            return False
        sx0, sy0, sw, sh = self.layout.sun_display
        px, py = self.win.client_to_screen(sx0 + sw // 2, sy0 + sh // 2)
        if self.cfg.verbose:
            print(f"[唤醒] 仅激活不够 → 真实点击阳光数字区域 screen=({px},{py})")
        sendinput_click(px, py, 0.06)
        time.sleep(0.35)
        ok = self._clock_advances(0.6)
        if self.cfg.verbose:
            print(f"[唤醒] {'✅ 时钟恢复' if ok else '❌ 时钟仍冻住'}")
        return ok

    def ensure_running(self) -> bool:
        """确保游戏真的在跑（时钟在走）。不在跑就尝试恢复。

        为什么要在**第一次决策之前**单独做一次：原来只靠主循环里的
        `note_clock`（连续 3 轮同值）判断暂停，而刚启动时前 3 轮会照常
        "收阳光 + 决策 + 点卡" —— 暂停状态下这些点击全被游戏无视，
        更糟的是**会被记成"植物太贵"**（见 execute 里的说明），
        把成本模型污染掉。所以启动时先明确确认一次。

        两级恢复：
          1. 画面上有暂停菜单 → 点「返回游戏」（后台 PostMessage 就够）
          2. 画面上没有菜单（"静默暂停"）→ minimize→restore 强制激活
        """
        if self.reader is None:
            return False
        if not self.refresh_window():
            return False
        # ⚠️ 不在对局中（主菜单 ui=1 / 选卡界面 ui=2）时 **clock 本来就是 0**，
        #    不是"暂停"。绝不能在这里走唤醒流程 —— `wake_game` 会
        #    minimize→restore 闪窗口，用户在选卡时被反复打断，非常讨厌。
        board = self.reader.read()
        if not board.ok:
            if self.cfg.verbose:
                print(f"[启动] {board.reason or '未进入对局'}（ui={board.ui}）—— 等待，不做唤醒")
            return False
        if self._clock_advances(0.45):
            return True
        if self.cfg.verbose:
            print(f"[启动] 时钟冻住（clock={board.game_clock}）→ 尝试恢复")
        if self.dismiss_pause(board):
            return True
        return self.wake_game()

    # -- 单次决策 -------------------------------------------------------
    def _track_zombie_station(self, board: BoardState) -> None:
        """跨快照观测僵尸位移，标记"驻停"僵尸 —— 远程僵尸（僵尸豌豆射手类）
        的行为特征：时钟在走它却不动，且没贴着植物啃。啃食中的僵尸同样驻停，
        用"是否贴着某个植物格中心"排除。样本不足时保持 None，绝不猜。

        远程僵尸是最高优先级的威胁信号（它会隔着防线点杀高价值植物，泳池里
        一排射手被齐射团灭的根因），policy 据此给"无墙保护的昂贵射手"补墙。
        """
        if not hasattr(self, "_zombie_track"):
            self._zombie_track: dict[tuple[int, int], list[tuple[int, float]]] = {}
        clock = board.game_clock or 0
        alive: set[tuple[int, int]] = set()
        for z in board.zombies:
            if z.friendly or z.x is None:
                continue
            key = (z.index, z.type_id)
            alive.add(key)
            hist = self._zombie_track.get(key)
            if hist and abs(hist[-1][1] - z.x) > 120:
                hist = []          # 槽位被复用/瞬移 -> 重新观测
            hist = (hist or []) + [(clock, z.x)]
            hist[:] = hist[-12:]
            self._zombie_track[key] = hist
            if clock - hist[0][0] >= 300 and abs(hist[-1][1] - hist[0][1]) < 12:
                # 3s 观测窗内位移 <12px（低于一步的 8px/s 应有位移）→ 驻停
                contact = any(p.row == z.row and abs(cell_x(p.col) - z.x) <= 48
                              for p in board.plants)
                z.stationary = not contact
            else:
                z.stationary = False
        for key in list(self._zombie_track):
            if key not in alive:
                self._zombie_track.pop(key, None)

    def _track_snow_cells(self, board: BoardState) -> None:
        """撞车僵尸压过的格子有积雪，融化前**不能种植**（用户 2026-09-26 补充）。

        用 Crush 僵尸（zombie_traits.json crush=true）的位移轨迹标记被压格子：
        它从 x_max 走到当前 x 之间扫过的格子都算。融化时长 unverified，默认
        30s（zombie_traits.json 的 ice_trail_melt_s）。这也解释了部分
        "空格却种不上去"的现象 —— 不是 agent 的错，policy 会主动避开这些格。
        """
        if not hasattr(self, "_snow_cells"):
            self._snow_cells: dict[tuple[int, int], float] = {}
        now = time.time()
        self._snow_cells = {c: t for c, t in self._snow_cells.items() if t > now}
        melt = float(self.book.zombie_traits["defaults"].get("ice_trail_melt_s") or 30)
        for z in board.zombies:
            if z.friendly or z.x is None or not self.book.zombie_flag(z.type_id, "crush"):
                continue
            hist = self._zombie_track.get((z.index, z.type_id)) or []
            xs = [x for _, x in hist] + [z.x]
            x_max, x_cur = max(xs), min(xs)      # 只向左推进：被压区间 = [x_cur, x_max]
            if x_max - x_cur < 10:
                continue
            for r in range(board.rows):
                if r != z.row:
                    continue
                for c in range(board.cols):
                    edge_l, edge_r = cell_x(c) - 40, cell_x(c) + 40
                    if edge_l <= x_max and edge_r >= x_cur:
                        self._snow_cells[(r, c)] = now + melt
        board.snow_cells = dict(self._snow_cells)

    def decide(self, board: BoardState) -> tuple[Decision, dict]:
        # 动态涨价按场上株数生效（图鉴 price_increment；见 PlantBook.cost）
        self.book.sync_field_copies(board.plants)
        # 决策层的"活体"判据统一用时钟推进（0x164 paused 不可靠，见 note_clock）
        board.clock_advancing = self._responsive
        self._track_zombie_station(board)
        self._track_snow_cells(board)
        context = (board.pid,board.board,board.scene,board.rows)
        restarted = (self._last_decision_clock is not None and board.game_clock is not None
                     and board.game_clock < self._last_decision_clock)
        self._last_decision_clock = board.game_clock
        if context != self._terrain_context or restarted:
            self._bad_cells.clear()
            self.book.min_cost.clear()
            self.book.min_cost_ts.clear()
            self._geometry_error = None
            self._terrain_context = context
        state = build_state(board, self.book)
        cands = generate_candidates(board, self.book)
        # 过滤掉"刚试过、落不下去"的格子（见 execute() 里 _bad_cells 的说明）。
        # 只在还有别的可选动作时才过滤，否则会连"等待"都只剩一个。
        now = time.time()
        self._bad_cells = {
            k: t for k, t in self._bad_cells.items() if now - t < self._bad_cell_ttl
        }
        if self._bad_cells:
            keep = [
                c for c in cands
                if c.kind != "plant" or (c.type_id, c.row, c.col) not in self._bad_cells
            ]
            cands = keep
        questions = build_questions(cands, board, self.book)
        resp = self.jev.ask(state, questions)
        if not resp.ok:
            self.stats.jev_errors += 1
        dec = merge_decision(resp, cands, board, self.book)
        record = {
            "t": time.time(),
            "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
            "board_text": render_text(board, self.book),
            "state": state,
            "candidates": [
                {"cid": c.cid, "kind": c.kind, "row": c.row, "col": c.col,
                 "slot": c.slot, "type_id": c.type_id, "score": round(c.score, 1),
                 "desc": c.describe(self.book)}
                for c in cands
            ],
            "jev": {
                "ok": resp.ok,
                "error": resp.error,
                "latency_s": round(resp.latency_s, 2),
                "model": resp.model,
                "answers": {
                    qid: {"kind": a.kind, "summary": a.summary(), "raw": a.raw}
                    for qid, a in resp.answers.items()
                },
            },
            "decision": {
                "action_id": dec.action_id,
                "chosen": dec.candidate.describe(self.book) if dec.candidate else None,
                "hold": dec.hold,
                "threat_lane": dec.threat_lane,
                "urgency": dec.urgency,
                "confidence": dec.confidence,
                "fallback": dec.fallback,
                "notes": dec.notes,
            },
            "executed": None,
        }
        return dec, record

    # -- 执行 -----------------------------------------------------------
    def execute(self, dec: Decision, record: dict, board: BoardState) -> None:
        """把决定落地。分三步，每步都要有"确实生效了"的证据。

        为什么不直接"点卡 -> 点格子"了事：杂交版的植物价格和原版完全不同，
        任何硬编码的价格表都会错。价格错了，"买得起"的判断就错，点卡会被游戏
        静默拒绝 —— 表现是**阳光没扣、植物没种上**，看起来就像"点击注入失效"，
        极难查（这个坑实测踩了很久）。所以这里改成：
          1. 先右键取消可能还举在手上的种子（清空状态，让后面的阳光差值干净）
          2. 点卡，比较点卡前后的阳光 —— **掉了多少就是真实成本**，记进 PlantBook
          3. 没掉 = 游戏拒绝了这张卡（太贵/冷却）→ 记下成本下界，这轮不点格子
        """
        assert self.clicker and self.layout
        # ★★ 兜底闸（2026-09-26 第二次修复）：**只在"时钟确实在走"时才注入点击。**
        #
        # `_frozen_hits` 是 `note_clock()` 用"本轮读到的 game_clock 和上一轮相同"
        # 累加出来的。正常游玩时游戏 100 tick/s、循环间隔 ≥0.35s，
        # **每一轮读到的时钟都必然不同** —— 所以 `_frozen_hits >= 1` 在日常
        # 游玩里永远不会误伤。
        #
        # 而一旦时钟没走，就说明游戏正处于"不泵消息"的状态：切屏/入场过渡
        # （用户实测杂交版进游戏会自己卡 5 秒）、或者真的暂停了。
        # 这时点击**必然被无视**，而且会把鼠标消息塞进一个正在做全屏模式切换的
        # 消息队列里 —— 有害无益。实测证据：`clock=57`（关卡刚开始 0.57s）那条
        # 决策点了卡、`game_responsive=false`、被拒 —— 就是撞进了入场冻结。
        #
        # 阈值取 1（而不是旧版的 3）是刻意的：**第一次读到"没走"就停手**，
        # 不留那 1 秒的窗口。这比"点了之后发现被拒再取消"安全得多 ——
        # 后者已经往游戏里注入了 5 条鼠标消息（含一次右键）。
        if not self._responsive:
            self.stats.input_ignored += 1
            record["executed"] = {
                "kind": "skipped_not_responsive",
                "frozen_hits": self._frozen_hits,
                "advances": self._advances,
                "note": "时钟没走（切屏/入场/暂停）—— 本轮不注入任何点击",
            }
            if self.cfg.verbose:
                print(f"  ⏸️ 时钟没走（同值 {self._frozen_hits} 轮 / 确认在走 "
                      f"{self._advances} 轮）→ 本轮不注入任何点击")
            return
        cand = dec.candidate
        if cand is None or cand.kind == "wait" or dec.hold:
            self.stats.holds += 1
            record["executed"] = {"kind": "hold"}
            return
        if self.cfg.dry_run:
            would = (f"shovel {self.book.name(cand.type_id)} @ r{cand.row}c{cand.col}"
                     if cand.kind == "shovel" else f"card {cand.slot} -> r{cand.row}c{cand.col}")
            record["executed"] = {"kind": "dry_run", "would": would}
            return
        now = time.time()
        self._action_times = [t for t in self._action_times if now - t < 60]
        if len(self._action_times) >= self.cfg.max_actions_per_min:
            record["executed"] = {"kind": "throttled"}
            return

        # 0) 冷却中的卡直接跳过，**连点都不点**。
        #    ⚠️ 为什么必须在"点之前"拦：冷却中点击必然被拒，而"被拒"会被下面的
        #    `note_unaffordable` 记成"这张卡至少要 X 阳光"。实测踩过 ——
        #    杂交版**开局就给部分卡一段初始冷却**（实测 slot3/4/5/8/9 的 cd_past
        #    从开局就跟着 game_clock 涨、cd_total=5000，也就是前 50 秒根本点不动），
        #    这些卡全被记成"要 900 阳光"，后续决策一路"保留阳光"。
        #    光靠候选生成过滤不够：卡槽冷却读数的可信度受读取时机影响，
        #    在真正点击前再核一次，是最便宜也最可靠的一道闸。
        pre_slot = next((s for s in board.slots if s.index == cand.slot), None)
        if pre_slot is not None and not pre_slot.ready:
            self.stats.cooling += 1
            record["executed"] = {
                "kind": "cooling",
                "slot": cand.slot,
                "type_id": cand.type_id,
                "plant": self.book.name(cand.type_id),
                "cd_left": pre_slot.cd_left,
                "cd_total": pre_slot.cd_total,
                "note": "目标卡仍在冷却，本轮不点（避免把冷却误记成价格）",
            }
            if self.cfg.verbose:
                print(f"  ⏳ 卡槽 {cand.slot}({self.book.name(cand.type_id)}) 仍在冷却 "
                      f"({pre_slot.cd_left}/{pre_slot.cd_total})，跳过")
            return

        # ★★ 动手前的**最后一道闸**（2026-09-26 第二次修复）：
        #    从"读到状态、交给 Jev 决策"到"真的要点击"之间，隔着一次 Jev 调用
        #    （实测 1~3s）。这段时间里游戏完全可能进入切屏/入场的过渡
        #    （用户实测会自己卡 5 秒）。`_frozen_hits` 反映的是**决策那一刻**的
        #    状态，已经过期了。
        #    所以这里再核一次：**时钟必须比决策时前进了**，才允许动手。
        #
        #    ⚠️ 位置很关键：必须在**任何注入之前** —— 包括下面 `cancel_seed`
        #    那一下右键。只要时钟没走，就连一条鼠标消息都不往游戏里塞。
        chk = self.reader.read()
        if ((chk.pid, chk.board, chk.level, chk.scene, chk.rows) != (board.pid, board.board, board.level, board.scene, board.rows)
                or not action_is_current(cand, chk, self.book)):
            record["executed"] = {"kind": "stale_action", "note": "战场或卡槽已变化，重新决策后再操作"}
            return
        if (chk.game_clock is None or board.game_clock is None
                or chk.game_clock == board.game_clock):
            self.stats.input_ignored += 1
            record["executed"] = {
                "kind": "skipped_not_responsive",
                "clock_at_decision": board.game_clock,
                "clock_before_click": chk.game_clock,
                "note": "决策后到动手前时钟没走（游戏进入切屏/暂停）—— 一条消息都没发",
            }
            if self.cfg.verbose:
                print(f"  ⏸️ 决策后时钟没走（{board.game_clock} -> {chk.game_clock}）"
                      f"→ 放弃本次动作（游戏可能正在切屏）")
            return

        # ★★ 执行前的**威胁重评**（2026-09-26）：决策时 low 的路，Jev 返回的
        #    这几秒里可能已经 critical（僵尸每秒走 8px，一个周期就是 40~80px）。
        #    用刚读到的 chk 重算 lane_facts，出现危急路且原动作不是救场时，
        #    直接改交救场候选 —— 不让"过期威胁"骗过执行层。
        esc = escalate_emergency(cand, chk, self.book, self._bad_cells)
        if esc is not None:
            note = (f"Execution-time re-eval: a lane just turned critical; "
                    f"switched to rescue ({self.book.en(esc.type_id)} r{esc.row + 1}"
                    f"{COL_LABEL[esc.col] if 0 <= esc.col < len(COL_LABEL) else esc.col}).")
            dec.candidate = esc
            dec.action_id = esc.cid
            dec.fallback = True
            dec.notes.append(note)
            cand = esc
            record["decision"]["action_id"] = esc.cid
            record["decision"]["chosen"] = esc.describe(self.book)
            record["decision"]["fallback"] = True
            if self.cfg.verbose:
                print(f"  🚨 {note}")

        self.layout.configure_board(chk)
        geometry = (chk.scene,chk.rows,self.layout.client_w,self.layout.client_h,
                    self.layout.grid_left,self.layout.grid_top,self.layout.cell_w,self.layout.row_height())
        x,y = self.layout.cell_center(cand.row,cand.col)
        if self._geometry_error == geometry or not (0 <= x < self.layout.client_w and 0 <= y < self.layout.client_h):
            record['executed'] = {'kind':'blocked_geometry','note':'落点越界或已发现种歪；重新校准并重启后再种植'}
            return
        if chk.scene not in (None,0,1,2,3) and not (chk.rows == 6 and len(chk.row_types) == 6):
            record['executed'] = {'kind':'unsupported_layout','scene':chk.scene,'note':'未知地图几何，暂不猜测坐标'}
            return
        if cand.kind == "shovel":
            self._execute_shovel(cand, record, board)
            return

        # 1) 清空"手持种子"状态
        self.clicker.cancel_seed("pre-action reset")
        time.sleep(0.15)

        # 2) 点卡 + 用**光标状态**判定是否真的拿起来了
        #
        # ⚠️★ 这里换过一次判据，是本项目最关键的一次修正（2026-09-26）：
        # 旧代码拿"点卡后阳光有没有掉"当判据，理由是"点卡瞬间就扣阳光"。
        # **在杂交版 v3.9.9 上这个前提是错的**：实测点卡后 sun 740->740（一分没扣），
        # 落点之后才 865->740。于是**每一次点卡都被判成"被拒"** ->
        # `note_unaffordable` 把成本记成 `阳光+1` -> 几轮后每株植物都"要 900+ 阳光"
        # -> 候选生成里 `sun < cost` 把全部候选过滤掉 -> Jev 只能选"保留阳光"。
        # 现象就是用户报的「植物种不下去」，而且**一次都不会种成功**。
        #
        # 新判据读 `Board+0x138 -> CursorObject+0x30`（0=空手, 1=手持种子），
        # 外加 `+0x24` 卡槽下标 / `+0x28` type_id。这是游戏自己维护的状态，
        # 不受阳光涨落影响，也能顺便验证卡槽绑定有没有错。
        #
        # ⚠️ 阳光基线在 `cancel_seed` **之后**取（取消手持种子可能让阳光变化，
        #    在它之前取会污染"真实成本"的差值）。
        pre = self.reader.read()
        if ((pre.pid, pre.board, pre.level) != (board.pid, board.board, board.level)
                or pre.scene != board.scene or pre.rows != board.rows
                or not action_is_current(cand, pre, self.book)):
            record["executed"] = {"kind": "stale_action", "note": "选卡前复核失败，未购买植物"}
            return
        sun0 = pre.sun
        self.clicker.click_card(cand.slot, self.layout, f"pick card {cand.slot}")
        time.sleep(0.3)
        mid = self.reader.read()
        picked = bool(mid.holding) and mid.held_cursor == 1 and mid.held_slot == cand.slot and mid.held_type == cand.type_id

        if not picked:
            # 卡没拿起来。原因必须**逐个分诊**，绝不能一律当成"植物太贵"：
            #   a) 真实价格确实比我们以为的高 / 阳光不够
            #   b) 卡还在冷却
            #   c) **游戏根本没在处理输入**（暂停菜单开着、窗口最小化、失焦）
            #   d) **点到了相邻的卡槽**（点击坐标偏移）—— 光标其实拿起了卡，
            #      只是不是目标槽。这是几何问题，记成本下界会把好卡锁死。
            self.stats.pick_rejected += 1
            responsive = (mid.game_clock is not None and pre.game_clock is not None
                          and mid.game_clock != pre.game_clock)
            mid_slot = next((s for s in mid.slots if s.index == cand.slot), None)
            slot_ready = mid_slot.ready if mid_slot is not None else True
            picked_other = (mid.holding and mid.held_cursor == 1
                            and mid.held_slot != cand.slot and mid.held_slot >= 0)
            self.clicker.cancel_seed("rejected: cancel")
            if not slot_ready:
                # 冷却中被拒是**预期行为**，和"价格"毫无关系
                self.stats.cooling += 1
                note = "卡在冷却中（被拒是预期行为），未记成本"
                if self.cfg.verbose:
                    print(f"  ⏳ 卡槽 {cand.slot}({self.book.name(cand.type_id)}) 点卡被拒，"
                          f"但该卡仍在冷却（{mid_slot.cd_left}/{mid_slot.cd_total}）"
                          f"—— 与价格无关，未记成本")
            elif not responsive:
                # 游戏没在跑 —— 这次点击没有任何信息量，**不记成本下界**
                self.stats.input_ignored += 1
                note = "时钟没走：游戏在暂停/最小化，这次点击无效，未记成本"
                if self.cfg.verbose:
                    print(f"  ⏸️ 时钟没走（clock={pre.game_clock}->{mid.game_clock}）："
                          f"游戏没在处理输入，本次点击不计入成本")
            elif picked_other:
                # 光标拿着的是**另一张**卡：点击坐标偏了（几何/缩放问题）。
                # 记成本下界会污染成本模型（实测 book_cost 曾集体变成"阳光+1"），
                # 这里只标记几何疑点，让用户去重跑 measure_layout。
                note = (f"点到了相邻卡槽 {mid.held_slot}（id={mid.held_type}）—— "
                        f"点击坐标偏移，与价格无关，未记成本；建议重跑 tools/measure_layout.py")
                if self.cfg.verbose:
                    print(f"  ⚠️ {note}")
            else:
                self.book.note_unaffordable(cand.type_id, sun0 or 0)
                note = "点卡被拒（阳光不够），已记下成本下界"
                if self.cfg.verbose:
                    print(f"  ⚠️ 卡槽 {cand.slot}({self.book.name(cand.type_id)}) 阳光不足："
                          f"{sun0} < 成本 -> 成本下界 "
                          f"{self.book.min_cost.get(cand.type_id)}")
            record["executed"] = {
                "kind": "pick_rejected",
                "slot": cand.slot,
                "type_id": cand.type_id,
                "plant": self.book.name(cand.type_id),
                "sun": sun0,
                "book_cost": self.book.cost(cand.type_id),
                "game_responsive": responsive,
                "slot_ready": slot_ready,
                "holding_after": mid.holding,
                "note": note,
            }
            return

        # 拿起来了 —— 顺手用游戏给的真值核对卡槽绑定。
        # `held_type` 是这张卡**真实的 type_id**，比冷却指纹可靠得多；
        # 如果和知识库对不上，说明 lineups 的卡面顺序错了，必须让用户知道。
        if mid.held_type >= 0 and mid.held_type != cand.type_id:
            # ⚠️ 必须放弃本轮落点（2026-09-26）：继续点格子会把**另一株植物**
            #    种下去，而 placement_delta 只认 cand.type_id -> placed=False ->
            #    误记落点失败 + 把真实已被占用的格子拉黑 + 真实花费不进成本模型。
            #    决策层从此以为"那格是空的、种不下去"，快照与战场持续偏差。
            #    正确动作：右键取消手持、记告警、重新决策。
            self.clicker.cancel_seed("binding mismatch: cancel")
            msg = (f"⚠️ 绑定不符：卡槽 {cand.slot} 知识库记的是 "
                   f"{self.book.name(cand.type_id)}(id={cand.type_id})，"
                   f"但游戏说手持的是 id={mid.held_type} —— 已取消本次落点，"
                   f"请核对 data/lineups.json")
            record.setdefault("warnings", []).append(msg)
            record["executed"] = {
                "kind": "binding_mismatch",
                "slot": cand.slot,
                "expected_type": cand.type_id,
                "actual_type": mid.held_type,
                "note": msg,
            }
            if self.cfg.verbose:
                print(f"  {msg}")
            return

        # 3) 落点 + 回读验证
        self.clicker.click_grid(cand.row, cand.col, self.layout, f"place r{cand.row}c{cand.col}")
        self._action_times.append(now)
        time.sleep(0.7)
        after = self.reader.read()
        target = (cand.row, cand.col)
        placed, misplaced = placement_delta(mid,after,cand.type_id,cand.row,cand.col)
        if misplaced and not placed:
            self._geometry_error = geometry
        # 成本在**放置**这一刻才扣（实测 865 -> 740 = 125，正好是豌豆射手）。
        # 所以这里量的才是真实成本；顺便把"点卡不扣阳光"这件事变成证据留档。
        spent = None
        if placed and sun0 is not None and after.sun is not None and after.sun < sun0:
            spent = sun0 - after.sun
            self.book.set_real_cost(cand.type_id, spent)
        record["executed"] = {
            "kind": "click",
            "placed": placed,
            "actual_other_cells": misplaced,
            "grid_xy": [x,y],
            "scene": chk.scene,
            "rows": chk.rows,
            "real_cost": spent,
            "sun_before": sun0,
            "sun_after": after.sun,
            "plants_before": len(board.plants),
            "plants_after": len(after.plants),
            "card_xy": f"slot {cand.slot}",
            "grid": f"r{cand.row}c{cand.col}",
        }
        if placed:
            self.stats.executed += 1
            self._bad_cells.pop((cand.type_id,*target), None)
            if cand.supports_type is not None:
                record['followup']=run_transaction(self,after,
                    followup=(cand.supports_type,cand.row,cand.col))
        else:
            self.stats.failed_actions += 1
            # ⚠️ 记住这个落点，短时间内不再选它（见 decide() 里的过滤）。
            #    为什么必需：实测出现过 agent 连续 7 次把卡扔向**同一个**落不下的格子
            #    （r2c3）—— 因为那一轮战场没变化，候选生成器每次都给出同一个"最优"，
            #    于是反复"点卡->落空->取消"，白烧阳光、还挤掉了别的有效动作。
            #    代码侧确定性兜底比指望 Jev 换选项可靠得多。
            self._bad_cells[(cand.type_id,*target)] = time.time()
            # 落点没生效时种子可能还举在手上（实测：位置非法时游戏拒绝落点、
            # 但阳光不扣、种子继续跟随光标），必须显式取消，否则下一次
            # "收阳光"的点击会把它随手种到某个阳光的位置上。
            if after.holding:
                self.clicker.cancel_seed("failed: cancel")
            record["executed"]["note"] = "落点未生效，已取消手持种子"
            if self.cfg.verbose:
                print(f"  ⚠️ 落点 r{cand.row + 1}c{cand.col} 未生效"
                      f"（阳光 {sun0} -> {after.sun}），已取消手持种子，"
                      f"该格 {self._bad_cell_ttl:.0f}s 内不再尝试")

    # -- 铲子执行 ---------------------------------------------------------
    def _execute_shovel(self, cand, record: dict, board: BoardState) -> None:
        """执行一次"铲子"动作：点铲子按钮 → 验证光标 → 点目标植物 → 验证移除。

        和种植链路同一条纪律：**每一步都要有"确实生效了"的内存证据**，
        绝不拿"点过了"当"成功了"。
          1. 点铲子后必须读到 `mCursorType == 6`（手持铲子），否则立即收手
             —— 可能是坐标偏了、或者游戏没在处理输入；
          2. 点植物格后必须读到该格**空了**，才算回收成功；
          3. 任何一步失败都右键复位光标，绝不让铲子一直举在手上
             （否则下一次"收阳光"的点击就会铲掉一株无辜的植物）。
        """
        result=run_transaction(self,board,candidate=cand)
        record['executed']=result
        if result['completed']:
            self.stats.executed+=1
        else:
            self.stats.failed_actions+=1

    # -- 主循环 ---------------------------------------------------------
    def run(self, duration_s: float = 120.0, on_cycle=None,
            wait_play_s: float = 900.0) -> AgentStats:
        """跑 `duration_s` 秒。

        ⚠️ **等待进入对局的时间不计入 `duration_s`**，最多等 `wait_play_s`。
        为什么：实测用户在选卡界面慢慢挑卡，300 秒的时长能被等掉一大半，
        真正在玩的时间所剩无几。等待期间把 `end` 顺延即可。
        """
        # 启动前先确认游戏真的在跑。暂停状态下开局的话，前几轮的点卡
        # 会被游戏无视，而旧代码会把它记成"植物太贵"，把成本模型污染掉。
        if self.refresh_window():
            self.ensure_running()
        end = time.time() + duration_s
        give_up = time.time() + wait_play_s
        if self.cfg.verbose:
            print(f"[提示] 想停就按 Ctrl+C；万一没反应，新建一个空文件 "
                  f"{self._stop_path} 即可（看门狗会在 10 秒内强制结束进程）。")
        self._start_watchdog()
        try:
            return self._loop(end, give_up, wait_play_s)
        finally:
            self._wd_stop.set()

    def _loop(self, end: float, give_up: float,
              wait_play_s: float) -> AgentStats:
        while time.time() < end:
            self.stats.cycles += 1
            self._heartbeat = time.time()
            if self._stop_requested():
                print(f"\n[停止] 检测到 {self._stop_path} —— 本轮结束。")
                break
            # ★★ 每轮第一件事：**确认游戏进程还在**（2026-09-26 新增）。
            #    必须放在所有窗口操作和 Jev 调用**之前**，理由见 _game_still_there()。
            if not self._game_still_there():
                break
            if not self.ensure_window():
                self._window_miss += 1
                # ★★ 关键修复（2026-09-26）：**游戏进程已经没了就必须立刻收手**。
                #    旧代码在这里无限重试"重新附着 + 恢复窗口"，而窗口一旦属于
                #    一个**正在退出的进程**，`ShowWindow` / `SetForegroundWindow`
                #    就可能**永久阻塞**（跨进程同步调用，目标线程不再泵消息），
                #    把主线程连同 Ctrl+C 一起卡死 ——
                #    用户遇到的就是"退出游戏后整个卡住，只能重启电脑"。
                #    现在：先确认进程还在，不在就干净退出，一个窗口操作都不做。
                #    ⚠️ 判据用 `_seen_pid`（粘性）而**不是** `self.reader.pid`：
                #       `attach()` 失败时会把 pid 清成 None，用它判会把"游戏刚被
                #       关掉"误判成"游戏从没出现过"，于是傻等到 --wait-play 超时。
                if self._seen_pid is not None and not is_process_alive(self._seen_pid):
                    self._dead_misses += 1
                    if self._dead_misses == 1 and self.cfg.verbose:
                        print("[退出] 游戏进程已消失 —— 不再尝试恢复窗口")
                    if self._dead_misses >= 2:
                        print("\n[退出] 游戏已关闭，本轮结束（未做任何窗口操作）。")
                        break
                    time.sleep(1.0)
                    continue
                self._dead_misses = 0
                # 游戏从头到尾没出现过 → 由 --wait-play 决定等多久
                if self._seen_pid is None and time.time() > give_up:
                    if self.cfg.verbose:
                        print(f"[等待] 已等 {wait_play_s:.0f}s 仍未找到游戏，退出")
                    break
                if self.cfg.verbose and (self._window_miss == 1 or self._window_miss % 10 == 0):
                    what = ("尝试重新附着并恢复窗口" if self.cfg.allow_window_ops
                            else "重新附着（安全模式：不动窗口）")
                    print(f"[等待] 窗口/进程不可用（第 {self._window_miss} 次）—— {what}…")
                # ⚠️ 不能只是 sleep 重试：实测窗口被最小化后，
                #    `find_game_window` 仍能返回它，但 `client_size` 是 0x0，
                #    而 `refresh_window` 的恢复重试需要**进程还活着**才有效。
                #    这里再补一次强制恢复 + 重新附着，避免整局空转。
                #    ⚠️ 顺序：**先确认进程活着，再去碰窗口**（顺序反了就是上面那个坑）。
                #    ⚠️ 安全模式下这一整段跳过 —— 恢复窗口本身就是危险动作。
                if (self.cfg.allow_window_ops
                        and self.reader.attach()
                        and is_process_alive(self.reader.pid)):
                    w = find_game_window(self.reader.pid)
                    if w is not None and not is_stuck(w.hwnd):
                        restore_window(w.hwnd)
                time.sleep(1.5)
                continue
            self._window_miss = 0
            self._dead_misses = 0

            board = self.reader.read()
            if not board.ok:
                if time.time() > give_up:
                    if self.cfg.verbose:
                        print(f"[等待] 已等 {wait_play_s:.0f}s 仍未进入对局，退出")
                    break
                # 不在对局中（选卡界面 ui=2 / 主菜单 ui=1）→ 把 end 顺延，
                # 这样用户慢慢选卡不会把"实际游玩时长"耗光
                end = max(end, time.time() + 2.0)
                if self.cfg.verbose:
                    print(f"[等待] {board.reason or '未进入对局'}（ui={board.ui}）")
                time.sleep(1.5)
                continue

            # PvZ 失去焦点会自动弹暂停菜单；用"时钟冻住"判断，冻住就先关掉菜单。
            # ⚠️ 关不掉时**必须重试**，不能点一次就放弃 —— 实测"点一次没关掉"
            #    很常见（第一次点击可能落在菜单刚弹出的动画帧上），
            #    而放弃的代价是整局都卡在菜单前面（曾经 150 秒一次没种上）。
            if self.note_clock(board):
                # ★★ 宽限期（2026-09-26 第二次修复）：时钟**刚**冻住的头
                #    PAUSE_GRACE_S 秒里，**什么都不做** —— 不抓屏、不找菜单、
                #    不点任何东西，只安静地读内存。
                #
                #    为什么必须这样：用户实测杂交版"切屏进游戏时游戏自己会卡 5 秒"
                #    （全屏模式切换、重新获取 primary surface）。那 5 秒里游戏
                #    主线程**不泵消息**，是所有跨进程调用会阻塞、所有点击被无视的
                #    **脆弱窗口期**。而"时钟冻住"恰好在这 5 秒里触发 ——
                #    旧代码会**立刻**抓屏 + 点「返回游戏」，一头撞进最脆弱的时刻。
                #    实测证据：日志里 `clock=57`（关卡刚开始 0.57s）那条决策，
                #    `game_responsive=false`、点卡被拒 —— 就是撞进了入场冻结。
                if self._frozen_since is None:
                    self._frozen_since = time.time()
                waited = time.time() - self._frozen_since
                if waited < PAUSE_GRACE_S:
                    if self.cfg.verbose and not self._grace_notified:
                        self._grace_notified = True
                        print(f"[暂停] 时钟冻住 → 静默等待 {PAUSE_GRACE_S:.0f}s"
                              f"（切屏/入场时游戏会自己卡几秒，这段时间绝不碰它）")
                    time.sleep(0.5)
                    continue
                if self.cfg.verbose and self._grace_notified:
                    self._grace_notified = False
                    print(f"[暂停] 已静默等 {waited:.0f}s 仍未恢复 → 开始尝试点「返回游戏」")
                recovered = self.dismiss_pause(board)
                if not recovered and self._pause_fail_streak < 3:
                    # 画面里找不到菜单 → 是"静默暂停"，走真实的激活流程。
                    # 限次数：wake 要 minimize→restore，会闪窗口；
                    # 若连 3 次都拽不回来（比如用户把游戏切到别的虚拟桌面），
                    # 就不该反复闪，改成只等（把干扰降到最低）。
                    recovered = self.wake_game()
                if recovered:
                    board = self.reader.read()
                    self._pause_fail_streak = 0
                else:
                    self._pause_fail_streak += 1
                    if self._pause_fail_streak == 1 or self._pause_fail_streak % 5 == 0:
                        print(f"[暂停] 第 {self._pause_fail_streak} 次尝试仍未恢复"
                              f"（窗口 {self.win.client_size if self.win else '?'}）")
                    # 连续失败多半是窗口状态变了（被最小化），重建一次再试。
                    # ⚠️ 但**必须限次**：时钟一直冻住时（用户切到别的虚拟桌面、
                    #    或者把游戏关了一半），旧代码每 3 轮抢一次焦点、无限循环 ——
                    #    既烦人，也是在反复戳一个可能已经卡住的窗口。
                    #    超过上限后彻底收手，只安静地等游戏自己回来。
                    if self._pause_fail_streak % 3 == 0 and self._pause_fail_streak <= FOCUS_RETRY_LIMIT:
                        self._need_focus = True
                        self.refresh_window()
                    elif self._pause_fail_streak == FOCUS_RETRY_LIMIT + 1:
                        print(f"[暂停] 已尝试 {self._pause_fail_streak} 次仍无法恢复 —— "
                              f"停止操作窗口，改为静默等待（游戏恢复后会自动继续）")
                    elif (self.cfg.allow_window_ops
                          and self._pause_fail_streak > FOCUS_RETRY_LIMIT
                          and time.time() - self._last_stall_wake > 90.0):
                        # ★★ 长期挂起自救（2026-09-26）：超过重试上限后旧代码就永远
                        #    静默等下去 —— 实测有 425s/900s 的挂起全耗在这上面，
                        #    而一次 cycle_window 就能把用户切回来之后的时间救回来。
                        #    限频 90s 一次、每次都过 is_stuck/进程存活检查，风险可控。
                        self._last_stall_wake = time.time()
                        if self.cfg.verbose:
                            print(f"[唤醒] 已挂起 {self._pause_fail_streak} 次 —— "
                                  f"限频重试一次完整唤醒")
                        if self.wake_game():
                            self._pause_fail_streak = 0
                            continue
                    if (not self.cfg.allow_window_ops
                            and self._pause_fail_streak % 30 == 0):
                        print("[等待] 安全模式无法自愈失焦/最小化 —— 请把游戏窗口点回前台，"
                              "或改用 --allow-window-ops 让 agent 自己唤醒")
                    time.sleep(0.6)
                    continue
            else:
                # 时钟在走 → 清掉冻结计时。下次再冻住会**重新**给一遍宽限期
                # （用户每切屏进一次游戏，游戏就会自己卡几秒）。
                self._frozen_since = None
                self._grace_notified = False

            # 第一轮就把卡槽绑好：没有名字和功能，Jev 后面所有的判断都是瞎猜
            if not self._binding_checked:
                self.sync_binding(board)
                if self.cfg.verbose and board.slots:
                    print(f"[手牌] {self.describe_deck(board)}")

            # 每轮同步行数（泳池=6）：hold 轮早退也要更新，否则阳光扫描区
            # 一直按 5 行算，第 6 行的天降阳光永远收不到（2026-09-26 实测盲区）。
            self.layout.configure_board(board)

            now = time.time()
            # ★★ 收阳光 = **往草坪上点击**，所以它和种植物一样属于"注入输入"，
            #     必须服从同一个前提：**游戏真的在处理输入**（`_responsive`）。
            #
            #     ⚠️ 不看 `board.paused`(0x164) —— 实测时钟正常推进时它也读 1，
            #     完全不可靠；时钟推进已由 `_responsive` 判定。
            if (self._responsive
                    and now - self._last_sun >= self.cfg.collect_sun_every_s):
                self._last_sun = now
                shot = grab(self.win)
                if shot is not None:
                    # ★★ 回读验证（2026-09-26）：阳光只会因点击而增加，
                    #    点完 0.35s 再读一次差值，把"点了但没涨"的位置计入
                    #    SunTracker —— 持久黄色假阳性（女王/向日葵贴图）3 轮后
                    #    被抑制，不再每轮吃满点击额度（实测 535 次点击阳光没涨）。
                    sun0 = board.sun
                    hits = collect_suns(shot, self.layout, self.clicker, max_click=5,
                                        banned=self.sun_tracker.banned_keys())
                    if hits:
                        time.sleep(0.35)
                        sun1 = self.reader.read().sun
                        gained = (sun1 - sun0) if (sun0 is not None and sun1 is not None) else None
                        self.sun_tracker.feedback(hits, gained)
                        self.stats.suns_collected += len(hits)
                        if self.cfg.verbose and gained is not None and gained <= 0:
                            print(f"  ☀️ 点了 {len(hits)} 个'阳光'但阳光没涨 —— 已计入假阳性抑制")

            if (self._responsive
                    and now - self._last_decision >= self.cfg.decide_every_s):
                self._last_decision = now
                dec, record = self.decide(board)
                self.stats.decisions += 1
                if dec.fallback:
                    self.stats.fallbacks += 1
                self.execute(dec, record, board)
                self.log.append(record)
                # 连续 hold 观测（2026-09-26）：等待本身常常是对的（攒女王/攒大件），
                # 但"阳光≥400 还一路等下去"曾连出 9 轮。这里只记录 + 周期性提醒，
                # 决策层已有 bounded development / 兑现升级兜底，不在主循环里抢决策权。
                if dec.hold:
                    self._hold_streak += 1
                    if (self._hold_streak >= 4 and (board.sun or 0) >= 400
                            and now - self._hold_warned_at > 30.0):
                        self._hold_warned_at = now
                        msg = (f"已连续等待 {self._hold_streak} 轮而阳光 {(board.sun or 0)} "
                               f"—— 若非攒大件（saving_plan），复盘时应关注这段")
                        record.setdefault("warnings", []).append(msg)
                        if self.cfg.verbose:
                            print(f"  ⚠️ {msg}")
                else:
                    self._hold_streak = 0
                if self.cfg.verbose:
                    print(render_text(board, self.book))
                    chosen = dec.candidate.describe(self.book) if dec.candidate else "-"
                    print(f"  -> Jev 决定: {chosen}")
                    ex = record.get("executed") or {}
                    if ex.get("kind") == "click":
                        print(f"  -> 落点 {'✅ 种上了' if ex.get('placed') else '❌ 没种上'}"
                              f"  真实花费={ex.get('real_cost')}  植物 {ex.get('plants_before')} -> {ex.get('plants_after')}")
                    elif ex.get("kind") == "pick_rejected":
                        if not ex.get("slot_ready"):
                            print("  -> 卡被拒：该卡仍在冷却，与价格无关，未记成本")
                        elif ex.get("game_responsive"):
                            print(f"  -> 卡被拒（阳光 {ex.get('sun')} 不够），已记下成本下界")
                        else:
                            print("  -> 点击无效：游戏时钟没走（暂停/失焦），本次不计成本")
                    elif ex.get("kind") == "cooling":
                        print(f"  -> 跳过：卡槽 {ex.get('slot')} 仍在冷却 "
                              f"({ex.get('cd_left')}/{ex.get('cd_total')})")
                    if dec.notes:
                        for n in dec.notes:
                            print(f"     · {n}")
                    print(f"  {self.stats.summary()}")
            time.sleep(0.35)
        return self.stats

    def _game_still_there(self) -> bool:
        """游戏进程还在吗？返回 False 表示调用方应当**立刻干净收手**。

        为什么必须**每轮**都查、而且放在所有窗口操作与 Jev 调用之前：
          * `ensure_window()` 返回 True 只说明"找到了窗口"，**不代表进程活着** ——
            进程正在退出的那几秒里，窗口仍然会被 `EnumWindows` 枚举到；
          * 那几秒里读到的是**定格的残留内存快照**（`game_clock` 不动、
            `ui` 还是 3=对局中），上层完全看不出异常，会照常去问 Jev
            （一次调用可能十几秒）、照常去点鼠标；
          * 更要命的是对着一个**正在销毁的窗口**做窗口操作（ShowWindow /
            SetForegroundWindow / PrintWindow）会**永久阻塞** ——
            这正是用户遇到的"退出游戏后整个卡死"。
        所以"进程还在不在"必须是**独立于窗口枚举**的第一判据，且每轮都查。

        ⚠️ 判据用粘性的 `_seen_pid` 而不是 `self.reader.pid`：后者会被
        `attach()` 在失败时清成 None，用它判就分不清"游戏刚被关掉"和
        "游戏从没出现过"。
        """
        if self.reader.pid is not None:
            self._seen_pid = self.reader.pid
        if self._seen_pid is None:
            return True  # 还没见过游戏 → 交给 --wait-play 决定等多久
        if is_process_alive(self._seen_pid):
            return True
        # 进程没了。先确认一下是不是用户**重启**了游戏（pid 会变）——
        # 只重新附着，**不做任何窗口操作**。
        self._dead_misses += 1
        if self._dead_misses == 1 and self.cfg.verbose:
            print(f"[退出] 游戏进程 pid={self._seen_pid} 已消失 —— 不再尝试恢复窗口")
        if self.reader.attach():
            self._seen_pid = self.reader.pid
            self._dead_misses = 0
            if self.cfg.verbose:
                print(f"[退出] 发现新的游戏进程 pid={self._seen_pid} —— 继续运行")
            return True
        print(f"\n[退出] 游戏已关闭（pid={self._seen_pid}），本轮结束"
              f"（未做任何窗口操作）。")
        return False

    def _start_watchdog(self) -> None:
        """看门狗线程：主线程被卡住 / 用户喊停时，**强制结束进程**。

        ⚠️ 为什么必须有它（2026-09-26，用户实测）：`ShowWindow` / `PrintWindow`
        这类**跨进程同步调用**一旦阻塞，Python **无法中断**（ctypes 调用阻塞期间
        收不到 KeyboardInterrupt），用户实测"卡住之后连任务管理器都退不出去"。
        现在窗口操作都有 3s 超时护栏、窗口句柄那一层也加了硬闸，正常不该再卡；
        但**万一**还有没想到的路径，这个线程是最后一道保险。

        它只做两件完全不碰窗口的事：
          1. 主循环心跳停了 > `_wd_stuck_after` 秒 → 打印原因并 `os._exit(3)`；
          2. `out/STOP` 出现 > `_wd_stop_grace` 秒而主线程仍未退出 → 强制退出。

        用 `os._exit` 而不是 `sys.exit`：前者不等待任何清理、不跑 atexit，
        在"主线程被 ctypes 卡死"的场景下，只有它真的能把进程干掉。
        """
        def loop() -> None:
            stop_seen_at: float | None = None
            while not self._wd_stop.is_set():
                now = time.time()
                if self._stop_requested():
                    if stop_seen_at is None:
                        stop_seen_at = now
                    elif now - stop_seen_at >= self._wd_stop_grace:
                        print(f"\n[看门狗] {self._stop_path} 已存在 "
                              f"{now - stop_seen_at:.0f}s 而主循环仍未退出 —— "
                              f"强制结束。", flush=True)
                        os._exit(3)
                else:
                    stop_seen_at = None
                idle = now - self._heartbeat
                if idle > self._wd_stuck_after:
                    print(f"\n[看门狗] 主循环已 {idle:.0f}s 没有任何进展"
                          f"（多半被某个卡住的窗口调用拖住了）—— 强制结束进程。"
                          f"游戏窗口不会再被碰，可以直接重开。", flush=True)
                    os._exit(3)
                time.sleep(0.5)

        threading.Thread(target=loop, name="jev-watchdog", daemon=True).start()

    def _stop_requested(self) -> bool:
        """紧急停止开关：`out\\STOP` 一出现就结束本轮。

        ⚠️ 为什么需要它：窗口操作一旦卡住，主线程就收不到 Ctrl+C
        （Python **无法中断阻塞中的 ctypes 调用**）。这是给用户留的一个
        "不用碰游戏窗口就能喊停"的出口。
        真正的兜底是 `pvz/win32.py` 里那层**超时护栏** —— 两道一起上。
        """
        try:
            return os.path.exists(self._stop_path)
        except OSError:
            return False

    def close(self) -> None:
        # 把实测到的真实成本存下来：下一局就不用从零再学一遍
        # （杂交版每局阵容会变，但同一个植物类型的价格是固定的）
        self.book.save_costs()
        if self.reader:
            self.reader.close()

    # -- 暂停处理 -------------------------------------------------------
    def note_clock(self, board: BoardState) -> bool:
        """每轮喂进 game_clock，判断时钟是否已经冻住。

        ⚠️ **不要用 `Board+0x164` 判断暂停**。pvztoolkit 把它标为 `game_paused`，
        但实测杂交版在**时钟正常推进（60 tick/s）时它也读 1** —— 完全不可靠。
        唯一可靠的信号是 `game_clock` 本身有没有递增。

        做法：连续 N 轮读到同一个值就认为冻住了。这样零额外开销
        （本来每轮就要读一次），也不用 sleep 采样。
        """
        c = board.game_clock
        if c is None:
            return False
        if self._last_clock is not None and c == self._last_clock:
            self._frozen_hits += 1
            self._advances = 0
        else:
            self._frozen_hits = 0
            self._advances += 1
        self._last_clock = c
        return self._frozen_hits >= 3

    @property
    def _responsive(self) -> bool:
        """游戏真的在更新吗？—— **唯一**允许"动手"的前提。

        两个条件都要满足：
          * `_frozen_hits == 0`：本轮读到的 `game_clock` 和上一轮**不同**
            （正常游玩时 100 tick/s、循环间隔 ≥0.35s，必然不同）；
          * `_advances >= WARMUP_ADVANCES`：已经连续确认过几轮时钟在走。

        第二个条件是"启动 / 刚切回游戏时先观察一下再动手"。用户实测杂交版
        切屏进游戏会自己卡 5 秒，头几轮时钟就是不动 —— 那时候**任何点击都会被
        无视**，而且会往一个正在做全屏模式切换的消息队列里塞鼠标消息。
        """
        return self._frozen_hits == 0 and self._advances >= WARMUP_ADVANCES

    def _find_resume_xy(self) -> tuple[int, int] | None:
        """定位暂停菜单的「返回游戏」按钮，返回**客户区坐标**。

        ★★ 2026-09-26 **第二次修复：这里彻底禁用 `PrintWindow`。**
        之前这里会先试 `PrintWindow(PW_RENDERFULLCONTENT)`（为了免疫窗口遮挡），
        失败才退回屏幕 DC。**这是一个危险的错误，已移除。**

        为什么：`PrintWindow` 内部是 `SendMessage(WM_PRINT)` —— 它**不是只读**，
        它要求**目标窗口去渲染一帧**。对一个 DirectDraw 全屏游戏来说：
          * 用户实测"切屏进游戏时游戏本身会卡 5 秒" —— 那 5 秒里游戏正在
            重建/重新获取 primary surface，主线程**不泵消息**；
          * 而这个函数**只在"时钟冻住"时被调用** —— 也就是**恰好**在那 5 秒里；
          * 于是在游戏最脆弱的时刻逼它渲染 → 游戏卡进显示驱动里出不来 →
            进程变成"杀不掉、必须重启"的状态，桌面也被占住。
        （`_run_bounded` 的 2.5s 超时只能救**我们自己的主线程**，
        救不了那个被我们逼着渲染的目标。）

        现在只用**屏幕 DC 抓屏**（`capture_window` 默认路径 = `BitBlt` 桌面），
        它是**纯读**：不碰目标窗口、不要求目标做任何事、不需要目标泵消息。
        代价是"窗口被挡住时抓到别人的画面"—— 但那正是**安全模式的前提**
        （用户自己保证游戏在最前面），而且找不到按钮时我们本来就**拒绝猜坐标**。
        """
        if self.win is None:
            return None
        s = capture_window(self.win)
        if s is None or s.w <= 0 or s.h <= 0:
            return None
        xy = find_pause_resume(s, fallback=None)
        if xy is None:
            return None
        # find_pause_resume 返回的是**屏幕坐标**（它内部按 shot.x/shot.y
        # 加了客户区原点），而 click_client 要**客户区坐标**。
        return (xy[0] - self.win.client_rect[0], xy[1] - self.win.client_rect[1])

    def dismiss_pause(self, board: BoardState) -> bool:
        """时钟冻住了就点"返回游戏"。返回是否点成功了。

        ⚠️ 为什么必须做这件事：PvZ **一旦失去焦点就会自动弹出暂停菜单**
        （实测时钟停止递增）。也就是说只要用户切出去干别的，agent 就卡死在
        菜单前面。无人值守必须自己会关。

        安全性：坐标不是猜的，是 `find_pause_resume()` 从像素里量出来的
        （中间窄带里最下面那个亮绿文字段 = 「返回游戏」，实测 (1261,1195)）；
        量不到时才退回 layout 里记录的上次量测值（并按分辨率缩放）。
        ⚠️ 千万不要手工估坐标：实测 y 差 90px 就滑到按钮下方的石碑台阶上，
        点了完全没反应，而且看起来"像点了"—— 这种错最难查。
        """
        if self.cfg.dry_run:
            if self.cfg.verbose:
                print(f"[暂停] 时钟冻住（0x164={board.paused} 不可信），dry_run 不点")
            return False
        # ⚠️ 必须先恢复窗口再抓屏。实测踩过：窗口最小化时 `grab()` 抓到的是
        #    **桌面**而不是游戏，于是 `find_pause_resume` 在桌面上乱找"居中的
        #    绿色文字段"（浏览器标签、图标文字…），点到一个和游戏无关的坐标上，
        #    日志里看起来"点了返回游戏"，实际游戏一个字都没收到。
        #    窗口恢复（client 从 0x0 变成 2560x1600）之后，同一份像素量测
        #    在两种菜单上都返回正确坐标（(1261,1195) / (1270,1288)）。
        if not self.refresh_window():
            if self.cfg.verbose:
                print("[暂停] 窗口不可用（进程/窗口都没了），无法关菜单")
            return False
        if self.win is None or self.clicker is None or self.layout is None:
            return False

        # ⚠️ 不要 focus_window：实测**后台 PostMessage 点击就能关掉菜单**
        #    （clock 立刻从冻住变成递增），而前台 SetCursorPos+mouse_event
        #    连点 3 次都没用。抢焦点只会打断用户，还无助于解决问题。
        xy = self._find_resume_xy()
        if xy is None:
            # ⚠️ 找不到按钮时**绝不退回 layout 里的硬编码坐标**。
            #    实测"静默暂停"（失焦停更但**没有菜单**）时画面就是一块正常草坪，
            #    那个兜底坐标落在草坪中间 —— 乱点会把手上的种子种到随机格子里，
            #    或者触发别的东西，而且看起来"像点了"。宁可不点，
            #    交给 `wake_game()` 走真实的激活流程。
            if self.cfg.verbose:
                print("[暂停] 画面里找不到「返回游戏」按钮（多半没有菜单），跳过")
            return False
        src = "像素量测"
        # ⚠️ 基线必须用**刚读到的那个冻住值**，不能用 self._last_clock：
        #    新 agent 上 _last_clock 是 None，`after.game_clock != None` 恒为真，
        #    于是**菜单根本没关掉也会报"✅ 已恢复"**，把"关不掉"这个故障
        #    伪装成"关掉了"。实测踩过：日志一片 ✅，游戏其实一直停在菜单里。
        frozen = board.game_clock
        self.clicker.click_client(xy[0], xy[1], "dismiss pause menu")
        if self.cfg.verbose:
            print(f"[暂停] 点「返回游戏」@ {xy}（{src}）")
        time.sleep(0.7)
        self._frozen_hits = 0
        after = self.reader.read()
        ok = after.game_clock is not None and frozen is not None and after.game_clock != frozen
        self._last_clock = after.game_clock
        if self.cfg.verbose:
            print(f"[暂停] {'✅ 已恢复' if ok else '❌ 菜单仍在'}")
        return ok

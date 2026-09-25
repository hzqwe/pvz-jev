"""主循环：感知 -> 序列化 -> Jev -> 执行。

安全默认值：`dry_run=True`。默认只做决策、写日志、不点鼠标，
确认无误后再用 `--live` 真正操作游戏。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .board import BoardReader, BoardState
from .jev import DecisionLog, JevClient
from .plants import PlantBook, load_lineups, match_lineup
from .policy import Decision, build_questions, generate_candidates, merge_decision
from .serialize import build_state, render_text
from .ui import Clicker, Layout, collect_suns, find_pause_resume, grab
from .win32 import (
    capture_window,
    cycle_window,
    find_game_window,
    focus_window,
    is_foreground,
    restore_window,
    sendinput_click,
)


@dataclass
class AgentConfig:
    dry_run: bool = True
    foreground: bool = False
    decide_every_s: float = 3.0
    collect_sun_every_s: float = 1.0
    max_actions_per_min: int = 20
    log_path: str = "out/decisions.jsonl"
    verbose: bool = True


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
        self._need_focus = True
        self._pause_fail_streak = 0
        self._window_miss = 0
        # 最近落点失败的格子 -> 时间戳。见 execute() 末尾的说明。
        self._bad_cells: dict[tuple[int, int], float] = {}
        self._bad_cell_ttl = 45.0

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
        size_changed = self.win is None or self.win.client_size != win.client_size
        self.win = win
        if size_changed or self.layout is None:
            self.layout = Layout.load(client_size=win.client_size)
            self.clicker = Clicker(win, foreground=self.cfg.foreground)
            if self.cfg.verbose:
                sx, sy = self.layout.scale()
                print(f"[窗口] client={win.client_size} 缩放=({sx:.3f},{sy:.3f}) "
                      f"卡0中心={self.layout.card_center(0)} 格(0,0)中心={self.layout.cell_center(0, 0)}")
        if self._need_focus:
            # ⚠️ 必须抢焦点。实测证据：窗口没拿到焦点时，点卡槽会**扣掉阳光**、
            #    但落点判定不生效（PvZ 在非激活状态下不处理草坪/卡槽的鼠标输入，
            #    只有"游戏暂停"覆盖层的按钮还响应）。表现就是"阳光少了、植物没多"。
            #    只在**首次**和**从最小化恢复**时抢，避免每轮跟用户抢窗口。
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
    def decide(self, board: BoardState) -> tuple[Decision, dict]:
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
                if c.kind != "plant" or (c.row, c.col) not in self._bad_cells
            ]
            if any(c.kind == "plant" for c in keep):
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
        cand = dec.candidate
        if cand is None or cand.kind == "wait" or dec.hold:
            self.stats.holds += 1
            record["executed"] = {"kind": "hold"}
            return
        if self.cfg.dry_run:
            record["executed"] = {"kind": "dry_run", "would": f"card {cand.slot} -> r{cand.row}c{cand.col}"}
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
        pre = self.reader.read()
        sun0 = pre.sun
        self.clicker.click_card(cand.slot, self.layout, f"pick card {cand.slot}")
        time.sleep(0.3)
        mid = self.reader.read()
        picked = bool(mid.holding) and (
            mid.held_slot == cand.slot or mid.held_type == cand.type_id
        )

        if not picked:
            # 卡没拿起来。三种原因，**绝不能一律当成"植物太贵"**：
            #   a) 真实价格确实比我们以为的高 / 阳光不够
            #   b) 卡还在冷却
            #   c) **游戏根本没在处理输入**（暂停菜单开着、窗口最小化、失焦）
            self.stats.pick_rejected += 1
            responsive = (mid.game_clock is not None and pre.game_clock is not None
                          and mid.game_clock != pre.game_clock)
            mid_slot = next((s for s in mid.slots if s.index == cand.slot), None)
            slot_ready = mid_slot.ready if mid_slot is not None else True
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
            msg = (f"⚠️ 绑定不符：卡槽 {cand.slot} 知识库记的是 "
                   f"{self.book.name(cand.type_id)}(id={cand.type_id})，"
                   f"但游戏说手持的是 id={mid.held_type} —— 请核对 data/lineups.json")
            record.setdefault("warnings", []).append(msg)
            if self.cfg.verbose:
                print(f"  {msg}")

        # 3) 落点 + 回读验证
        self.clicker.click_grid(cand.row, cand.col, self.layout, f"place r{cand.row}c{cand.col}")
        self._action_times.append(now)
        time.sleep(0.7)
        after = self.reader.read()
        target = (cand.row, cand.col)
        placed = target in after.occupancy()
        # 成本在**放置**这一刻才扣（实测 865 -> 740 = 125，正好是豌豆射手）。
        # 所以这里量的才是真实成本；顺便把"点卡不扣阳光"这件事变成证据留档。
        spent = None
        if sun0 is not None and after.sun is not None and after.sun < sun0:
            spent = sun0 - after.sun
            self.book.set_real_cost(cand.type_id, spent)
        record["executed"] = {
            "kind": "click",
            "placed": placed,
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
            self._bad_cells.pop(target, None)
        else:
            self.stats.failed_actions += 1
            # ⚠️ 记住这个落点，短时间内不再选它（见 decide() 里的过滤）。
            #    为什么必需：实测出现过 agent 连续 7 次把卡扔向**同一个**落不下的格子
            #    （r2c3）—— 因为那一轮战场没变化，候选生成器每次都给出同一个"最优"，
            #    于是反复"点卡->落空->取消"，白烧阳光、还挤掉了别的有效动作。
            #    代码侧确定性兜底比指望 Jev 换选项可靠得多。
            self._bad_cells[target] = time.time()
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
        while time.time() < end:
            self.stats.cycles += 1
            if not self.ensure_window():
                self._window_miss += 1
                if self.cfg.verbose and (self._window_miss == 1 or self._window_miss % 10 == 0):
                    print(f"[等待] 窗口/进程不可用（第 {self._window_miss} 次）—— "
                          f"尝试重新附着并恢复窗口…")
                # ⚠️ 不能只是 sleep 重试：实测窗口被最小化后，
                #    `find_game_window` 仍能返回它，但 `client_size` 是 0x0，
                #    而 `refresh_window` 的恢复重试需要**进程还活着**才有效。
                #    这里再补一次强制恢复 + 重新附着，避免整局空转。
                if self.reader.attach():
                    w = find_game_window(self.reader.pid)
                    if w is not None:
                        restore_window(w.hwnd)
                time.sleep(1.5)
                continue
            self._window_miss = 0

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
                    # 连续失败多半是窗口状态变了（被最小化），重建一次再试
                    if self._pause_fail_streak % 3 == 0:
                        self._need_focus = True
                        self.refresh_window()
                    time.sleep(0.6)
                    continue

            # 第一轮就把卡槽绑好：没有名字和功能，Jev 后面所有的判断都是瞎猜
            if not self._binding_checked:
                self.sync_binding(board)
                if self.cfg.verbose and board.slots:
                    print(f"[手牌] {self.describe_deck(board)}")

            now = time.time()
            if now - self._last_sun >= self.cfg.collect_sun_every_s:
                self._last_sun = now
                # ⚠️ 暂停时**绝不点草坪**：`collect_suns` 是往草坪上的亮黄色团块点击，
                #    而暂停菜单正好压在屏幕中央、上面有金色文字和绿色按钮 ——
                #    实测菜单开着时找"阳光"会命中菜单里的金色/亮色像素，
                #    那些点击落在按钮上，可能误触「重新开始」/「主菜单」毁掉进度。
                #    `board.paused`(0x164) 实测在"菜单开着"时=1、"在跑"时=0，
                #    拿它当这道闸足够（保守：判不准就少收一点阳光，没有副作用）。
                if board.paused:
                    pass
                else:
                    shot = grab(self.win)
                    if shot is not None:
                        hits = collect_suns(shot, self.layout, self.clicker, max_click=3)
                        self.stats.suns_collected += len(hits)

            if now - self._last_decision >= self.cfg.decide_every_s:
                self._last_decision = now
                dec, record = self.decide(board)
                self.stats.decisions += 1
                if dec.fallback:
                    self.stats.fallbacks += 1
                self.execute(dec, record, board)
                self.log.append(record)
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
        else:
            self._frozen_hits = 0
        self._last_clock = c
        return self._frozen_hits >= 3

    def _find_resume_xy(self) -> tuple[int, int] | None:
        """定位暂停菜单的「返回游戏」按钮，返回**客户区坐标**。

        ⚠️ 为什么要抓两次屏（2026-09-26 新增）：旧代码只用屏幕 DC 抓屏
        （`capture_window` 默认路径 = `BitBlt` 桌面）。那抓的是**屏幕上此刻显示的
        像素**，游戏窗口被别的窗口挡住时抓到的就是别人的画面 ——
        于是 `find_pause_resume` 在一张无关的图上找不到绿色按钮，直接放弃，
        而游戏其实好好地停在菜单里。表现就是"点了半天返回游戏，游戏一动不动"。
        实测 `PrintWindow(PW_RENDERFULLCONTENT)` 在这个游戏上**能拿到完整内容**
        （非黑像素比例 0.998），而且和屏幕 DC 得到**完全一致**的按钮坐标
        （两者都返回 (1261,1195)）。所以现在优先用 PrintWindow，
        它对"窗口被遮挡/不在最前"免疫；再退回屏幕 DC 兜底。
        """
        if self.win is None:
            return None
        shots = []
        for use_pw in (True, False):
            s = capture_window(self.win, use_printwindow=use_pw)
            if s is not None and s.w > 0 and s.h > 0:
                shots.append(s)
        for s in shots:
            xy = find_pause_resume(s, fallback=None)
            if xy is not None:
                # find_pause_resume 返回的是**屏幕坐标**（它内部按 shot.x/shot.y
                # 加了客户区原点），而 click_client 要**客户区坐标**。
                return (xy[0] - self.win.client_rect[0], xy[1] - self.win.client_rect[1])
        return None

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

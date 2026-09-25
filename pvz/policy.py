"""决策层：代码生成候选动作，Jev 做取舍，代码再校验并落地。

分工原则（这是整个项目的核心设计）：
  * **代码负责确定性的事**：枚举合法动作、算威胁、判冷却、算能不能负担、兜底。
  * **Jev 负责语义判断**：在若干条都"合法"的动作里，哪一条最符合当前局势。

这样既避开了 Jev 不擅长的多目标优化（实测它会自相矛盾），又保留了它的价值：
它是那个"看着战场做取舍"的决策者。

⚠️ 一条实测教训：**候选里必须写清植物的功能，否则 Jev 只是在复述启发式。**
第一版只给 `name / role / cost`，Jev 面对"樱桃辣椒 vs 阳光炸弹"只能瞎猜 ——
因为 `role=instant` 对两者是一样的。现在每条候选都带上 effect 摘要和
"这条为什么值得考虑"（覆盖几行、几秒冷却、能不能打全屏），Jev 才有取舍空间。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .board import BoardState
from .plants import (
    PlantBook, PLATFORM, SUPPORT,
    T_PLATFORM, T_CHARM, T_TRACKING, T_SPLASH3, T_GLOBAL_FREEZE,
    T_SUN_ON_KILL, T_INSTANT, T_PRODUCER, T_SHOOTER, T_WALL, T_WALL_REGEN,
    T_REFLECT, T_TEMPORARY, T_BURN_AURA, T_FREEZE_ON_DEATH,
)
from .serialize import COL_LABEL, lane_threat

MAX_CANDIDATES = 10
MAX_PER_TYPE = 2          # 同一张卡最多出几条落点候选（见 generate_candidates 末尾）
_EFFECT_MAX = 220

# ---- 经济护栏（2026-09-26 实测加的）------------------------------------
# 实测故障：agent 开局 25 秒内连买 125 + 500 + 150 + 200 + 125，把阳光打到 0~100，
# 之后整整 60 秒里 **就绪卡有 10~13/15、草坪上有 8~32 只僵尸，却一个候选都出不来**
# —— 全被 `sun < cost` 过滤掉了，决策层只能反复"保留阳光"干等。
# 根因不是候选生成有 bug，而是**钱花得太早**：产阳光的单位只有 1~2 株就去买 500 的卡。
# 所以这里加一道确定性的护栏（不消耗 Jev 调用）：
#   产阳光株数还没到 ECON_TARGET 时，**非产阳光的卡**不允许把阳光花到 ECON_RESERVE 以下；
#   有路告急时例外（救场优先于经济）。
ECON_TARGET = 4           # 期望先建起来的产阳光株数
ECON_RESERVE = 300        # 经济未成型时，买非产阳光卡之后至少要留这么多
ECON_CHEAP = 150          # 便宜到不值得拦的卡：急救就靠它们，永远豁免
                          # （实测那次失败是"阳光 750 时买 500 的卡"，花掉 67%；
                          #  储备 150 拦不住，300 才拦得住。而 ≤150 的卡留给应急。）


@dataclass
class Candidate:
    cid: str
    kind: str                    # "plant" | "wait"
    row: int = -1
    col: int = -1
    slot: int = -1
    type_id: int = -1
    score: float = 0.0
    why: str = ""
    tags: tuple[str, ...] = ()

    def describe(self, book: PlantBook) -> str:
        if self.kind == "wait":
            return (
                "Wait / do nothing this cycle. Save the sun. Choose this when no "
                "placement would meaningfully improve the defence right now."
            )
        col = COL_LABEL[self.col] if 0 <= self.col < len(COL_LABEL) else str(self.col)
        cost = book.cost(self.type_id)
        cost_txt = f"cost {cost} sun" if cost is not None else "cost unknown"
        cd = book.cooldown_s(self.type_id)
        cd_txt = f", {cd:g}s cooldown" if cd else ""
        hp = book.hp(self.type_id)
        hp_txt = f", {hp} hp" if hp else ""
        effect = book.effect(self.type_id)
        if len(effect) > _EFFECT_MAX:
            effect = effect[: _EFFECT_MAX - 1].rstrip() + "…"
        eff_txt = f" Effect: {effect}" if effect else ""
        return (
            f"Plant \"{book.en(self.type_id)}\" ({book.role(self.type_id)}, {cost_txt}"
            f"{cd_txt}{hp_txt}) at lane {self.row + 1}, column {col}."
            f"{eff_txt} Reason: {self.why}"
        )


@dataclass
class Decision:
    action_id: str = ""
    candidate: Candidate | None = None
    hold: bool = False
    threat_lane: int | None = None
    urgency: int | None = None
    confidence: float | None = None
    fallback: bool = False
    notes: list[str] = field(default_factory=list)

    def describe(self, book: PlantBook) -> str:
        if self.candidate is None:
            return "无动作"
        return self.candidate.describe(book)


# ---------------------------------------------------------------- 小工具
def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _col_from_x(x: float | None, board: BoardState) -> int:
    """把僵尸的**游戏内部坐标**换成列号。

    内部坐标里草坪宽 800、9 列，所以一列 ≈ 80 宽、左边界 ≈ 40。
    这是 policy 第一版就在用的换算，实测可用。
    """
    if x is None:
        return 3
    return _clamp(int((x - 40) // 80), 0, board.cols - 1)


def _free_cols(board: BoardState, occ, row: int, lo: int, hi: int) -> list[int]:
    hi = min(hi, board.cols - 1)
    return [c for c in range(max(0, lo), hi + 1) if (row, c) not in occ]


def _nearest_zombie_x(board: BoardState, lanes) -> float | None:
    xs = [z.x for r in lanes for z in board.zombies_in_lane(r) if z.x is not None]
    return min(xs) if xs else None


def _lane_rank(board: BoardState, threats, descending: bool = True) -> list[int]:
    return sorted(range(board.rows),
                  key=lambda r: (-threats[r]["zombie_count"], r) if descending
                  else (threats[r]["zombie_count"], r))


# ---------------------------------------------------------------- 候选生成
def generate_candidates(board: BoardState, book: PlantBook) -> list[Candidate]:
    """枚举"现在可以做"的动作，并给一个启发式分用于排序/兜底。

    分支按**标签**走，不按 role —— 因为杂交版的植物大多是复合体
    （高冰果既是墙又是射手，向日葵女王既是产阳光又是全屏射手），
    单看 role 会把它们当成普通墙/普通产阳光，白扔掉一半价值。
    """
    cands: list[Candidate] = []
    sun = board.sun or 0
    occ = board.occupancy()
    threats = {r: lane_threat(board, r) for r in range(board.rows)}
    total_z = sum(threats[r]["zombie_count"] for r in range(board.rows))
    any_critical = any(threats[r]["threat_level"] == "critical" for r in range(board.rows))
    producers_on_field = sum(1 for p in board.plants if book.has_tag(p.type_id, T_PRODUCER))
    quiet = _lane_rank(board, threats, descending=False)
    hot = _lane_rank(board, threats, descending=True)

    for s in board.slots:
        if s.type_id < 0 or not s.ready:
            continue
        t = s.type_id
        tags = book.tags(t)
        role = book.role(t)
        cost = book.cost(t)
        if cost is not None and sun < cost:
            continue
        if T_PLATFORM in tags or role == PLATFORM:
            continue                                    # v1 不做荷叶/花盆的自动放置
        if role == SUPPORT and T_CHARM not in tags:
            continue                                    # 其余辅助植物不做

        # ---- 经济护栏（见文件顶部 ECON_TARGET 的说明）---------------------
        # 便宜卡豁免：急救（补墙、补豌豆）靠的就是这些，拦掉它们反而危险。
        # 有路告急时整条护栏让路（救场优先于经济）。
        is_producer = T_PRODUCER in tags
        if (not is_producer and producers_on_field < ECON_TARGET
                and not any_critical and cost is not None
                and cost > ECON_CHEAP and sun - cost < ECON_RESERVE):
            continue

        # ---- 1) 全屏救场（雪花寒冰菇 / 寒光菇）-----------------------------
        # ⚠️ 闸门：全屏牌冷却 80 秒，绝不能因为"僵尸多"就交出去。
        #    只有真的扛不住才提这条候选：要么整体堆了 6 只以上，
        #    要么有一路告急**并且**总量也已经不小（4 只以上）。
        #    只为了救 2~3 只僵尸交全屏牌是纯亏。
        if T_GLOBAL_FREEZE in tags:
            if not ((any_critical and total_z >= 4) or total_z >= 6):
                continue
            r = quiet[0]
            cols = _free_cols(board, occ, r, 0, 2)
            if not cols:
                continue
            bonus = 25 if any_critical else 0
            econ = 8 if T_SUN_ON_KILL in tags else 0
            cands.append(Candidate(
                cid="", kind="plant", row=r, col=cols[0], slot=s.index, type_id=t, tags=tags,
                score=45.0 + total_z * 5 + bonus + econ,
                why=(f"Board-wide: reaches all {total_z} zombie(s) at once, no matter the lane"
                     + (", and freezes the whole board" if T_GLOBAL_FREEZE in tags else "")
                     + (", converting each frozen zombie into sun" if T_SUN_ON_KILL in tags else "")
                     + "."),
            ))
            continue

        # ---- 2) 三行爆发（樱桃辣椒）--------------------------------------
        # 闸门：至少有一路已经"逼近"（nearest < 320）才值得炸，否则是浪费。
        if T_SPLASH3 in tags:
            for r in range(board.rows):
                cover = [x for x in (r - 1, r, r + 1) if 0 <= x < board.rows]
                cnt = sum(threats[x]["zombie_count"] for x in cover)
                if cnt < 2:
                    continue
                if not any(threats[x]["threat_level"] in ("high", "critical") for x in cover):
                    continue
                nx = _nearest_zombie_x(board, cover)
                col = _col_from_x(nx, board)
                if (r, col) in occ:
                    col = col - 1
                if col < 0 or (r, col) in occ:
                    continue
                crit = any(threats[x]["threat_level"] == "critical" for x in cover)
                cands.append(Candidate(
                    cid="", kind="plant", row=r, col=col, slot=s.index, type_id=t, tags=tags,
                    score=50.0 + cnt * 8 + (35.0 if crit else 0.0) - r * 0.3,
                    why=(f"One bomb clears {len(cover)} lanes at once "
                         f"({', '.join(str(x + 1) for x in cover)}) which together hold "
                         f"{cnt} zombie(s)"
                         + (", and at least one of them has a zombie about to reach the defenders"
                            if crit else "") + "."),
                ))
            continue

        # ---- 3) 单行一次性爆发（阳光炸弹）---------------------------------
        if T_INSTANT in tags:
            urgent = [r for r in range(board.rows)
                      if threats[r]["threat_level"] in ("critical", "high")]
            for r in urgent[:3]:
                zs = [z for z in board.zombies_in_lane(r) if z.x is not None]
                if not zs:
                    continue
                nearest = min(zs, key=lambda z: z.x)
                col = _clamp(int((nearest.x - 40) // 80), 0, board.cols - 1)
                if (r, col) in occ:
                    col -= 1
                if col < 0 or (r, col) in occ:
                    continue
                crit = threats[r]["threat_level"] == "critical"
                bonus = 8 if T_SUN_ON_KILL in tags else 0
                cands.append(Candidate(
                    cid="", kind="plant", row=r, col=col, slot=s.index, type_id=t, tags=tags,
                    score=50.0 + (35 if crit else 0) - r * 0.5 + bonus,
                    why=(f"Burst in lane {r + 1}: nearest zombie at {nearest.x:.0f} px "
                         f"({threats[r]['threat_level']}), {threats[r]['zombie_count']} zombie(s) there"
                         + (", and the kills turn into sun" if T_SUN_ON_KILL in tags else "") + "."),
                ))
            continue

        # ---- 4) 产阳光 ---------------------------------------------------
        # 经济优先：产阳光的启发式分刻意抬高，压过"顺手炸一下"。
        # 只出**一条**候选：产阳光放哪条路几乎无所谓（都是最安全的那条），
        # 出两条一模一样的选项只会挤掉别的候选、给 Jev 制造假选择。
        # ⚠️ 但"只出最安全那一条路的前 3 列"会让候选在**该路后场被占满时直接消失**
        #    （实测开局把 A/B/C 列种满后就再也出不了产阳光候选，经济就此停摆）。
        #    所以按"最安全的路"依次往下试，列范围放宽到 A~D，**仍然只出一条**。
        if T_PRODUCER in tags:
            for r in quiet[:2]:
                cols = _free_cols(board, occ, r, 0, 3)
                if not cols:
                    continue
                econ_bonus = 25.0 if producers_on_field < ECON_TARGET else 0.0
                extra = 10.0 if sun < 150 else 0.0
                if T_TRACKING in tags:
                    extra += 8.0                       # 顺带能打全屏，更值得
                if T_BURN_AURA in tags:
                    extra += 5.0
                cands.append(Candidate(
                    cid="", kind="plant", row=r, col=cols[0], slot=s.index, type_id=t, tags=tags,
                    score=50.0 - threats[r]["zombie_count"] * 6 + econ_bonus + extra,
                    why=("Back column of the quietest lane; grows the sun economy"
                         + (f" (only {producers_on_field} sun producer(s) on the board so far,"
                            f" want about {ECON_TARGET})"
                            if producers_on_field < ECON_TARGET else "")
                         + ("; it also attacks any lane, so it is never wasted"
                            if T_TRACKING in tags else "") + "."),
                ))
                break                                   # 只出一条，别制造假选择
            continue

        # ---- 5) 全屏追踪射手（冰瓜香蒲 / 玉米卷香蒲）----------------------
        if T_TRACKING in tags and T_SHOOTER in tags:
            if total_z == 0:
                continue
            r = quiet[0]
            cols = _free_cols(board, occ, r, 2, 4)
            if not cols:
                cols = _free_cols(board, occ, r, 0, 6)
            if not cols:
                continue
            cands.append(Candidate(
                cid="", kind="plant", row=r, col=cols[0], slot=s.index, type_id=t, tags=tags,
                score=40.0 + total_z * 6 + (10.0 if any_critical else 0.0),
                why=(f"Hits any lane (full-screen homing), so it does not need the hot lane - "
                     f"put it in the safest lane ({r + 1}) and it still covers all "
                     f"{total_z} zombie(s)."),
            ))
            continue

        # ---- 6) 直线射手 -------------------------------------------------
        if T_SHOOTER in tags:
            for r in [x for x in hot[:2] if threats[x]["zombie_count"] > 0]:
                occ_cols = sorted(p.col for p in board.plants_in_lane(r))
                anchor = (max(occ_cols) - 1) if occ_cols else 3
                anchor = _clamp(anchor, 0, board.cols - 2)
                cols = _free_cols(board, occ, r, anchor, anchor + 2)
                if not cols:
                    continue
                cands.append(Candidate(
                    cid="", kind="plant", row=r, col=cols[0], slot=s.index, type_id=t, tags=tags,
                    score=32.0 + threats[r]["zombie_count"] * 8,
                    why=(f"Adds firepower to lane {r + 1}, which carries "
                         f"{threats[r]['zombie_count']} zombie(s)."),
                ))
            continue

        # ---- 7) 策反（Cupid 魅惑菇射手）----------------------------------
        if T_CHARM in tags:
            for r in hot[:2]:
                zs = [z for z in board.zombies_in_lane(r) if z.x is not None]
                if len(zs) < 2:
                    continue
                nearest = min(zs, key=lambda z: z.x)
                col = _clamp(int((nearest.x - 40) // 80) + 1, 0, board.cols - 1)
                if (r, col) in occ:
                    col -= 1
                if col < 0 or (r, col) in occ:
                    continue
                life = ", but it only lives 15s" if T_TEMPORARY in tags else ""
                cands.append(Candidate(
                    cid="", kind="plant", row=r, col=col, slot=s.index, type_id=t, tags=tags,
                    score=50.0 + len(zs) * 5,
                    why=(f"Lane {r + 1} has {len(zs)} zombie(s); charming them turns them "
                         f"against the rest of the wave{life}."),
                ))
            continue

        # ---- 8) 阻挡 / 反伤墙 --------------------------------------------
        # 落点逻辑：僵尸从右往左走，所以墙要放在**防线的右侧**（更靠前），
        # 先挨打。已有防线 -> 最右那株的右边一格；没有防线 -> 中段，
        # 用来拖时间。**绝不能放在僵尸左边**，那样僵尸已经走过去了，墙白放。
        if T_WALL in tags:
            for r in [x for x in hot[:1] if threats[x]["zombie_count"] > 0]:
                occ_cols = sorted(p.col for p in board.plants_in_lane(r))
                col = (max(occ_cols) + 1) if occ_cols else 5
                if not (0 <= col < board.cols) or (r, col) in occ:
                    continue
                extra = 0.0
                notes = []
                if T_REFLECT in tags:
                    extra += 6.0
                    notes.append("it damages whatever bites it")
                if T_WALL_REGEN in tags:
                    extra += 6.0
                    notes.append("it can be shovelled back into your hand")
                if T_FREEZE_ON_DEATH in tags:
                    extra += 3.0
                    notes.append("its death freezes a 3x3 area")
                cands.append(Candidate(
                    cid="", kind="plant", row=r, col=col, slot=s.index, type_id=t, tags=tags,
                    score=42.0 + threats[r]["zombie_count"] * 4 + extra,
                    why=("Shield in front of the existing defenders"
                         + ("; " + ", and ".join(notes) if notes else "") + "."),
                ))
            continue

    # 按启发式分排序，去重（同一 (row,col) 只保留最高分）
    best: dict[tuple[int, int], Candidate] = {}
    for c in sorted(cands, key=lambda c: -c.score):
        key = (c.row, c.col)
        if key not in best:
            best[key] = c
    # 再按"理由文本"去重：同一个植物、同一个理由 = 假选择，会挤掉真正的候选
    # （实测平静局面下会连着出两条一模一样的「产阳光放最安全的那条路」）。
    seen_why: set[str] = set()
    deduped: list[Candidate] = []
    for c in sorted(best.values(), key=lambda c: -c.score):
        if c.why in seen_why:
            continue
        seen_why.add(c.why)
        deduped.append(c)
    # 最后限制**同一张卡**最多出 2 条候选。不加这道限制的话，一张三行爆发牌
    # 会给出 3~5 个落点选项，把候选列表挤满、其他牌全看不见
    # （实测场景 3/5/6 都出现了这个问题）。"炸哪一行"确实是真选择，
    # 但不该占掉半个选项列表。
    per_type: dict[int, int] = {}
    ordered: list[Candidate] = []
    for c in deduped:
        if c.kind != "plant":
            continue
        n = per_type.get(c.type_id, 0)
        if n >= MAX_PER_TYPE:
            continue
        per_type[c.type_id] = n + 1
        ordered.append(c)
        if len(ordered) >= MAX_CANDIDATES:
            break

    for i, c in enumerate(ordered):
        c.cid = f"A{i + 1}"
    ordered.append(Candidate(
        cid=f"A{len(ordered) + 1}", kind="wait", score=0.0,
        why="No placement is clearly worth the sun right now.",
    ))
    return ordered


# ---------------------------------------------------------------- 提问
def build_questions(candidates: list[Candidate], board: BoardState, book: PlantBook) -> dict:
    """一次性 fan-out 全部问题：同一 state 下并行，比逐条问便宜得多。"""
    threats = {r: lane_threat(board, r) for r in range(board.rows)}

    lane_opts: dict[str, str] = {}
    for r in range(board.rows):
        t = threats[r]
        defenders = board.plants_in_lane(r)
        def_txt = (
            ", ".join(f"{book.en(p.type_id)}@col{p.col + 1}" for p in sorted(defenders, key=lambda p: p.col))
            or "nothing"
        )
        lane_opts[f"lane_{r + 1}"] = (
            f"Lane {r + 1}: {t['zombie_count']} zombie(s), nearest at "
            f"{t['nearest_zombie_x']} px from the left edge ({t['nearest_closeness']}); "
            f"defenders: {def_txt}."
        )

    action_opts = {c.cid: c.describe(book) for c in candidates}

    return {
        "threat_lane": {
            "type": "choice",
            "instructions": (
                "Read the serialized Plants vs Zombies board. In this game zombies walk "
                "right-to-left toward the house; a smaller x_px means the zombie is closer "
                "to the house. Pick the single lane whose current situation is the most "
                "dangerous relative to what is defending it."
            ),
            "criteria": lane_opts,
        },
        "urgency": {
            "type": "score",
            "instructions": (
                "How urgent is the overall board right now? Consider how close the nearest "
                "zombie is to the house, how many lanes are under pressure, and whether the "
                "defences can still stop them."
            ),
            "criteria": [
                "calm - no zombie is anywhere near the house, the defence is comfortable",
                "mild - zombies are on the lawn but the defence clearly handles them",
                "pressing - some lane is close to being overrun or is thinly defended",
                "emergency - a zombie is about to reach the house or a lane has already collapsed",
            ],
        },
        "action": {
            "type": "choice",
            "instructions": (
                "Choose the single best action to take right now, or choose the wait option "
                "if no placement is worth the sun. Each option states the plant, its role, "
                "its cost and cooldown, what its effect actually does, where it would be "
                "placed, and why the code thinks it is worth considering. Prefer the action "
                "that best answers the most dangerous lane; prefer cheaper plants when the "
                "sun reserve is small, and be careful with long-cooldown one-shot plants "
                "when the board is still calm."
            ),
            "criteria": action_opts,
        },
        "hold_sun": {
            "type": "noul",
            "instructions": (
                "Should we deliberately spend nothing this cycle and keep saving sun? "
                "Answer yes only if planting right now would be wasteful, premature, or "
                "would leave the sun reserve too low for an imminent emergency."
            ),
            "criteria": {"true": "yes - hold and save the sun", "false": "no - spend sun now"},
        },
    }


# ---------------------------------------------------------------- 合并
HOLD_THRESHOLD = 0.6


def merge_decision(resp, candidates: list[Candidate], board: BoardState, book: PlantBook) -> Decision:
    """把 Jev 的回答变成一条可执行的决定，并做合法性校验与兜底。"""
    by_id = {c.cid: c for c in candidates}
    wait = next((c for c in candidates if c.kind == "wait"), None)
    best_heuristic = next((c for c in candidates if c.kind == "plant"), wait)

    if resp is None or not resp.ok:
        d = Decision(action_id=best_heuristic.cid if best_heuristic else "", candidate=best_heuristic)
        d.fallback = True
        d.notes.append(f"Jev 不可用，改用启发式最优: {resp.error if resp else '无响应'}")
        return d

    d = Decision()
    a_lane = resp.get("threat_lane")
    if a_lane and a_lane.choice:
        try:
            d.threat_lane = int(str(a_lane.choice).split("_")[-1])
        except ValueError:
            pass
    a_urg = resp.get("urgency")
    if a_urg is not None:
        d.urgency = a_urg.score
    a_hold = resp.get("hold_sun")
    a_act = resp.get("action")

    if a_act is None:
        d.candidate = best_heuristic
        d.fallback = True
        d.notes.append("Jev 未返回 action，使用启发式兜底")
        return d

    d.confidence = a_act.confidence

    if a_hold is not None and a_hold.noul is not None and a_hold.noul >= HOLD_THRESHOLD:
        d.hold = True
        d.action_id = wait.cid if wait else ""
        d.candidate = wait
        d.notes.append(f"Jev 判定保留阳光 (noul={a_hold.noul:.2f})")
        return d

    chosen = by_id.get(a_act.choice)
    if chosen is None:
        d.candidate = best_heuristic
        d.fallback = True
        d.notes.append(f"Jev 选了未知选项 {a_act.choice!r}，使用启发式兜底")
        return d

    # 合法性校验：这几件事代码说了算
    if chosen.kind == "plant":
        if (chosen.row, chosen.col) in board.occupancy():
            d.candidate = best_heuristic
            d.fallback = True
            d.notes.append(f"Jev 选的 ({chosen.row + 1},{COL_LABEL[chosen.col]}) 已被占用，兜底")
            return d
        slot = next((s for s in board.slots if s.index == chosen.slot), None)
        if slot is None or not slot.ready:
            d.candidate = best_heuristic
            d.fallback = True
            d.notes.append("Jev 选的卡槽已不可用，兜底")
            return d
        cost = book.cost(chosen.type_id)
        sun = board.sun or 0
        if cost is not None and sun < cost:
            d.candidate = best_heuristic
            d.fallback = True
            d.notes.append(f"Jev 选的植物要 {cost} 阳光但只有 {sun}，兜底")
            return d

    d.action_id = chosen.cid
    d.candidate = chosen

    if best_heuristic and best_heuristic.kind == "plant" and chosen.kind == "plant":
        if chosen.cid != best_heuristic.cid:
            d.notes.append(
                f"Jev 与启发式不一致：Jev 选 {chosen.cid}，启发式最优 {best_heuristic.cid}"
            )
    return d

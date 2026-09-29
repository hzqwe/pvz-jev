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

import time
from dataclasses import dataclass, field
from types import SimpleNamespace

from .board import BoardState
from .plants import (
    PlantBook, PLATFORM, SUPPORT,
    T_PLATFORM, T_CHARM, T_TRACKING, T_SPLASH3, T_GLOBAL_FREEZE,
    T_SUN_ON_KILL, T_INSTANT, T_PRODUCER, T_SHOOTER, T_WALL, T_WALL_REGEN,
    T_REFLECT, T_TEMPORARY, T_BURN_AURA, T_FREEZE_ON_DEATH, T_TORCH,
)
from .serialize import COL_LABEL, lane_threat
from .tactics import cell_x, lane_facts, rear_cols, saving_plan, strength, upgrade_value, stall_window, relocation_target, attack_dps, crusher_approaching, torch_path, target_dps, can_hit, economy_summary, stable_sun_producer, coverage_lanes

MAX_CANDIDATES = 14         # 2026-09-27：24 条长描述 ≈7KB/请求（每小时百万级
                            # token 额度消耗的主因）。救场优先的排序保证重要的
                            # 都在前；14 条足够覆盖"每类一张+备选落点"。


def snow_blocked(board, row: int, col: int) -> bool:
    """撞车僵尸压过的积雪格在融化前不能种植（用户 2026-09-26 补充）。
    实现在 BoardState.snow_blocked；这里保留函数形式方便测试/分支引用。"""
    return board.snow_blocked(row, col)
MAX_PER_TYPE = 2          # 同一张卡最多出几条落点候选（见 generate_candidates 末尾）

# ---- 铲子/回收护栏（2026-09-26 新增）-----------------------------------
# 只有"可回收"的墙（回收高坚果，wall_regen 标签）才允许铲。规则来自图鉴：
# 每次铲除扣 800 血，**血量 > 800 才能变回卡片**（保留剩余血量）。
# 所以：血量未知 → 不铲；血量 <= RECLAIM_MIN_HP → 铲不回卡片，不铲；
# 血量还很满 → 没必要铲，不铲。铲子候选只在"这面墙快没了"时出现。
RECLAIM_MIN_HP = 800      # 扣完 800 必须还 > 0 才变卡
RECLAIM_MAX_HP = 2400     # 血量高于这个值时铲它纯属浪费（还能挡很久）
RECLAIM_MAX_HP_BITTEN = 3600  # 正在被啃时的放宽上限：反正马上要跌破回收线，早铲早回收

# ---- 经济护栏（2026-09-26 实测加的）------------------------------------
# 实测故障：agent 开局 25 秒内连买 125 + 500 + 150 + 200 + 125，把阳光打到 0~100，
# 之后整整 60 秒里 **就绪卡有 10~13/15、草坪上有 8~32 只僵尸，却一个候选都出不来**
# —— 全被 `sun < cost` 过滤掉了，决策层只能反复"保留阳光"干等。
# 根因不是候选生成有 bug，而是**钱花得太早**：产阳光的单位只有 1~2 株就去买 500 的卡。
# 所以这里加一道确定性的护栏（不消耗 Jev 调用）：
#   产阳光株数还没到 ECON_TARGET 时，**非产阳光的卡**不允许把阳光花到 ECON_RESERVE 以下；
#   有路告急时例外（救场优先于经济）。
ECON_TARGET = 4           # 期望先建起来的产阳光株数（向日葵勤种的推力）
PREMIUM_ECON_TARGET = 3   # 高价值卡允许出手的产阳光门槛（用户 2026-09-26：
                          # 女王算一株，3 株就该攒第二个金卡增强前期强度）
ECON_RESERVE = 300        # 经济未成型时，买非产阳光卡之后至少要留这么多
ECON_CHEAP = 150          # 便宜到不值得拦的卡：急救就靠它们
                          # （实测那次失败是"阳光 750 时买 500 的卡"，花掉 67%；
                          #  储备 150 拦不住，300 才拦得住。而 ≤150 的卡留给应急。）


@dataclass
class Candidate:
    cid: str
    kind: str                    # "plant" | "shovel" | "wait"
    row: int = -1
    col: int = -1
    slot: int = -1
    type_id: int = -1
    score: float = 0.0
    why: str = ""
    tags: tuple[str, ...] = ()
    emergency: bool = False
    covers: tuple[int, ...] = ()
    supports_type: int | None = field(default=None, kw_only=True)
    relocate_to: tuple[int,int] | None = field(default=None, kw_only=True)
    salvage: bool = field(default=False, kw_only=True)   # 铲掉换阳光（不回收卡片）
    crush_recovery: bool = field(default=False, kw_only=True)  # recover before instant crushing
    intercept: bool = field(default=False, kw_only=True)  # deliberate sacrificial blocker
    drop_index: int | None = field(default=None, kw_only=True)
    replacement_type: int | None = field(default=None, kw_only=True)
    source_index: int | None = field(default=None, kw_only=True)
    hp: int | None = None        # 铲子候选：目标植物当前血量（给 Jev 看的依据）

    def total_cost(self, book):
        if self.replacement_type is not None:
            return book.cost(self.replacement_type)
        if self.kind != 'plant':
            return 0
        cost = book.cost(self.type_id)
        if self.supports_type is not None:
            extra = book.cost(self.supports_type)
            return None if cost is None or extra is None else cost + extra
        return cost

    def describe(self, book: PlantBook, cap: int = 360) -> str:
        """Keep action semantics and current reason; shared almanac lives in state."""
        if self.kind == 'wait':
            return 'Wait / save sun. Reason: ' + self.why[:max(0,cap-25)]
        col = COL_LABEL[self.col] if 0 <= self.col < len(COL_LABEL) else str(self.col)
        at = f'lane {self.row+1}, column {col}'
        if self.kind == 'shovel':
            mode = 'salvage for dropped sun (no returned seed card)' if self.salvage else 'recover seed card and relocate'
            if self.crush_recovery:
                mode = 'recover seed card before crusher; replant using existing support'
            if self.crush_recovery and self.relocate_to is None:
                mode = 'recover seed card; leave it on lawn for later safe planting'
            # Salvage must never imply the plant can be replanted.
            if self.salvage: mode = 'salvage for dropped sun; removes this plant'
            if self.replacement_type is not None:
                mode = f'replace immediately with "{book.en(self.replacement_type)}"; reserve {self.total_cost(book)} sun; preserve platform'
            head = f'Shovel up "{book.en(self.type_id)}" at {at}: {mode}, hp={self.hp}.'
        elif self.kind == 'pick':
            head = f'Pick up "{book.en(self.type_id)}" seed card lying on the lawn at {at}; replant free.'
        elif self.supports_type is not None:
            head = (f'Plant "{book.en(self.type_id)}" + "{book.en(self.supports_type)}" at {at}; '
                    f'total cost {self.total_cost(book)} sun; continuous pad-then-defender.')
        else:
            head = f'Plant "{book.en(self.type_id)}" at {at}; cost {self.total_cost(book)} sun.'
        coverage = ' Covers lanes ' + str([r+1 for r in self.covers]) + '.' if self.covers else ''
        reason = ' Reason: ' + self.why[:max(0,min(110,cap-len(head)-len(coverage)-9))]
        text = head + reason + coverage
        # Drop optional effect first, never trim away the reason to fit almanac prose.
        room = max(0,cap-len(text)-9)
        effect = book.effect(self.supports_type if self.supports_type is not None else self.type_id)
        return text + (' Effect: ' + effect[:room] if room and effect else '')

    def _describe_raw(self, book: PlantBook) -> str:
        if self.kind == "wait":
            return (
                "Wait / do nothing this cycle. Save the sun. Choose this when no "
                "placement would meaningfully improve the defence right now. " + self.why
            )
        if self.kind == "shovel":
            col = COL_LABEL[self.col] if 0 <= self.col < len(COL_LABEL) else str(self.col)
            hp_txt = f"about {self.hp} hp left" if self.hp is not None else "hp unknown"
            purpose = ('to salvage dropped sun (plant removed)' if self.salvage else
                       'to recover its seed card for later safe planting' if self.crush_recovery and self.relocate_to is None else
                       'to recover and relocate its seed card')
            return (
                f"Shovel up \"{book.en(self.type_id)}\" at lane {self.row + 1}, column {col} "
                f"({hp_txt}) {purpose}. Reason: {self.why}"
            )
        if self.kind == "pick":
            col = COL_LABEL[self.col] if 0 <= self.col < len(COL_LABEL) else str(self.col)
            return (
                f"Pick up the dropped \"{book.en(self.type_id)}\" seed card lying on the "
                f"lawn at lane {self.row + 1}, column {col}, then replant it at a valid "
                f"spot. Reason: {self.why}"
            )
        col = COL_LABEL[self.col] if 0 <= self.col < len(COL_LABEL) else str(self.col)
        cost = book.cost(self.type_id)
        cost_txt = f"cost {cost} sun" if cost is not None else "cost unknown"
        cd = book.cooldown_s(self.type_id)
        cd_txt = f", {cd:g}s cooldown" if cd else ""
        hp = book.hp(self.type_id)
        hp_txt = f", {hp} hp" if hp else ""
        effect = book.effect(self.type_id)
        eff_txt = f" Effect: {effect}" if effect else ""
        profile = book.combat(self.type_id)
        if profile:
            eff_txt += f" Almanac capabilities and limits: {profile}."
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
def _free_cols(board: BoardState, occ, row: int, lo: int, hi: int) -> list[int]:
    hi = min(hi, board.cols - 1)
    return [c for c in range(max(0, lo), hi + 1) if (row, c) not in occ]


# ---------------------------------------------------------------- 候选生成
def generate_candidates(board: BoardState, book: PlantBook) -> list[Candidate]:
    """Enumerate role-aware placements; keep distinct plants competing for a cell."""
    book.sync_field_copies(board.plants)
    facts = [lane_facts(board, r, book) for r in range(board.rows)]
    hot = sorted(range(board.rows), key=lambda r: -facts[r]["priority"])
    quiet = sorted(range(board.rows), key=lambda r: facts[r]["priority"])
    occ, sun = board.top_occupancy(book), board.sun or 0
    emergency = any(f["threat_level"] == "critical" for f in facts)
    total = sum(f["zombie_count"] for f in facts)
    # 平静期（场上没僵尸且家底够）：照常预置防线，别干等
    # —— 用户实测反馈：僵尸没出来就什么都不种不行，要保持战场整齐。
    calm = total == 0 and sun >= 500
    develop = sun >= 1000 and all(f["threat_level"] not in ("high", "critical") for f in facts)
    # 用户 2026-09-26：阳光充裕且不紧急时**把前四列种满，不许发呆** ——
    # 阵型成型后也要继续铺（多种向日葵/射手永远不亏）。给两边分支当开关。
    calm_fill = (calm or develop) and sun >= 600
    producers = economy_summary(board,book)['producer_count']
    plan = saving_plan(board, book)
    candidates = []

    # 女王开局硬约束的配套逃生口（2026-09-26，对照基线修正）：储蓄期内出现
    # 危急路 / 马上有植物被啃 / 有零防御路被压时，**只有便宜（≤150）的防御卡**
    # 允许绕过储蓄闸门 —— 500 级的射手/爆发绕过会把女王积蓄一次烧光（泳池局
    # 实测）。危急路本身会让 saving_plan 退出，这里主要兜 close_intercept 和
    # 零防御路；价格上限是硬的。
    close_intercept_any = any(
        f['nearest_zombie_x'] is not None and any(
            0 <= f['nearest_zombie_x'] - cell_x(p.col) <= 160
            and not book.has_tag(p.type_id, T_WALL)
            for p in board.plants_in_lane(f['lane']-1)) for f in facts)
    zero_defence_pressure = any(
        f['threat_level'] == 'high' and not f['blocking_walls']
        and f['shooter_support'] == 0
        and f['nearest_zombie_x'] is not None and f['nearest_zombie_x'] < 400
        for f in facts)
    rescue_now = emergency or close_intercept_any or zero_defence_pressure
    rescue_bypass_tags = {T_WALL, T_SHOOTER, T_INSTANT, T_SPLASH3, T_GLOBAL_FREEZE}
    # 进阶 2（用户 2026-09-26）：僵尸非常多或非常坦（橄榄球类灰烬一发打不死）
    # 时要**提前**交灰烬，不能拖到贴脸。eager 局面下：灰烬绕过经济储备闸，
    # 中线（<560）允许出爆发候选，全屏冻结的 6 只门槛也放开。
    eager = (total >= 6
             or any(strength(z) >= 12
                    for lane_zs in (board.zombies_in_lane(x) for x in range(board.rows))
                    for z in lane_zs)
             or any(f.get('crush_zombies') for f in facts))
    # 防撞墙现在是否"就绪且买得起"（决定撞车局面里炸弹与墙的分工）
    anti_crush_ready = any(
        s.ready and (book.combat(s.type_id).get('crush_hits')
                     or book.combat(s.type_id).get('lethal_hit_burst'))
        and (book.cost(s.type_id) or 10**9) <= sun for s in board.slots)

    # -- 兜底垫背（用户策略，2026-09-26）----------------------------------
    # 危急路上没有墙卡可用（都在冷却/买不起）时，用便宜植物垫在僵尸脚下拖时间，
    # 等墙卡冷却好转再正常拦截。落子允许与僵尸同格（游戏允许在无植物的空格种植）。
    stall_lanes = []
    if not any(T_WALL in book.tags(s.type_id) and s.ready
               and (book.cost(s.type_id) or 10**9) <= sun for s in board.slots):
        for f in facts:
            if f['threat_level'] not in ('high','critical') or f['nearest_zombie_x'] is None:
                continue
            r = f['lane'] - 1
            c0 = max(0, min(board.cols - 1, int((f['nearest_zombie_x'] - 40) // 80)))
            cell = next((c for c in (c0, c0 - 1, c0 - 2) if c >= 0 and (r, c) not in occ), None)
            if cell is not None:
                stall_lanes.append((r, cell))

    def add(slot, row, col, value, reason, covers=(), *, intercept=False):
        if board.placement_blocked(row,col): return
        nx = facts[row]['nearest_zombie_x']
        if intercept and nx is not None and cell_x(col)>nx+40:
            return  # The lead enemy already passed this interception cell.
        # 积雪格（撞车僵尸压过）在融化前不能种植 —— 种了也种不上，主动避开
        # （用户 2026-09-26 补充；这也解释了部分"空格却种不上去"）。
        if snow_blocked(board, row, col):
            return
        tags = book.tags(slot.type_id)
        rescue = any(facts[r]["threat_level"] == "critical" for r in covers)
        if T_SHOOTER in tags and T_TEMPORARY not in tags:
            utility = upgrade_value(board,book,slot.type_id,row,col)
            value += utility
            reason += f' Capability fit={utility:g} (heuristic; damage/control/economy, not price).'
            lead = min((z for z in board.zombies_in_lane(row) if z.x is not None),
                       key=lambda z:z.x,default=None)
            if torch_path(board,book,slot.type_id,row,col,lead):
                value += 8  # Combination preference, not an invented damage multiplier.
                reason += ' Peas cross a live torch; fire synergy, multiplier unverified.'
            if book.combat(slot.type_id).get('piercing'):
                hits = sum(can_hit(book,slot.type_id,row,col,z) for z in board.zombies if not z.friendly)
                reason += f' Beam reaches {hits} lane target(s).'
        if T_BURN_AURA in tags:
            # 光环只烧植物周围 3x3：只统计与落点同行/邻排、且还在光环射程内的僵尸，
            # 不当全屏伤害计分（图鉴 aura_dps=40/s，仅对贴身目标成立）。
            reach = cell_x(col) + 240
            near = sum(1 for x in range(max(0, row-1), min(board.rows, row+2))
                       for z in board.zombies_in_lane(x)
                       if z.x is not None and z.x <= reach)
            if near:
                value += 3 * min(near, 6)
                reason += f' Burn aura reaches {min(near, 6)} nearby zombie(s).'
        if (producers < ECON_TARGET and T_WALL in tags
                and facts[row]['threat_level'] == 'low'):
            # 经济优先（用户实测反馈：开局只会种冰坚果墙、不种向日葵）：
            # 产阳光没成型时，远僵尸（low）路的墙整体按 0.12 折算 —— 压到
            # 向日葵（85 分）之下，但保持各路之间的排序不被抹平。
            # 僵尸走近（high）或危急时不打折——贴脸的必须拦。
            # 注意放在能力加成（upgrade_value）之后，否则压不住总分。
            value = 65 + (value - 65) * 0.12
        if not board.can_plant(row,col,slot.type_id,book):
            if board.platform_required(row) and (row,col) not in board.occupancy():
                placement = book.placement(slot.type_id)
                allowed_terrain = placement.get('terrain')
                if (isinstance(allowed_terrain, list)
                        and board.terrain(row) not in allowed_terrain):
                    # A platform can satisfy the support layer, but it cannot
                    # turn a land-only or roof-only plant into a water plant.
                    return
                pad = next((s for s in board.slots if s.ready
                            and book.has_tag(s.type_id,T_PLATFORM)
                            and board.platform_fits(row, s.type_id, book)
                            and book.cost(s.type_id) is not None
                            and sun >= book.cost(s.type_id) + (book.cost(slot.type_id) or 0)), None)
                if pad is not None:
                    # ★ 支撑卡选择（2026-09-27 夜战教训）：以前取"槽位顺序第一张"，
                    #   结果荷叶后面接的是豌豆/阳光菇 —— 水路防线形同虚设。
                    #   现在在所有"就绪且买得起（含荷叶钱）"的卡里挑最适合上荷叶
                    #   的：墙系 > 高价射手 > 其他，顺序即优先级。
                    # Preserve the specific intended defender. Replacing it by the
                    # most expensive available wall changes the action after scoring.
                    best = slot
                    # 压力升级：受压水路（high/critical）且本路无荷叶时，荷叶+
                    # 连锁种植就是救场本体（一个事务里荷叶+墙一起落地），
                    # 不再固定封顶 45 —— 封顶曾让挨打的水路永远排不进来。
                    # 压力升级：受压水路（high/critical，或僵尸已进中圈<560）
                    # 且本路无荷叶时，荷叶+连锁种植就是救场本体（一个事务里
                    # 荷叶+墙一起落地），不再固定封顶 45 —— 封顶曾让挨打的
                    # 水路永远排不进来（2026-09-27 夜战 L4 被穿的教训）。
                    pressured = (facts[row]['threat_level'] in ('high', 'critical')
                                 or (facts[row]['zombie_count']
                                     and (facts[row]['nearest_zombie_x'] or 9999) < 560))
                    if pressured and not board.has_platform(row, col, book):
                        score = max(value, 75)
                        tag = 'Plant the platform (pad/pot) and immediately chain the defender onto it'
                    else:
                        score = value if rescue and T_WALL in tags else min(value * 0.2 if calm else value, 45)
                        tag = 'First place the platform (pad/pot) to support'
                    c = Candidate('', 'plant', row, col, pad.index, pad.type_id,
                        score,
                        f'{tag} {book.en(best.type_id)} at this supported cell; '
                        'Execute as a continuous pad-then-plant transaction; only the completed defender helps. '
                        'Re-read and confirm both placements without another model call.'
                        + (f' Why this defender: {reason}' if reason else ''),
                        book.tags(pad.type_id), rescue and T_WALL in tags, tuple(covers),
                        supports_type=best.type_id, intercept=intercept)
                    c.emergency = rescue and rescue_is_effective(c,board,book)
                    candidates.append(c)
            return
        c = Candidate('', 'plant', row, col, slot.index, slot.type_id,
                                    value * 0.2 if calm else value,
                                    reason, tags, rescue, tuple(covers), intercept=intercept)
        c.emergency = rescue and rescue_is_effective(c,board,book)
        candidates.append(c)

    for slot in board.slots:
        tid = slot.type_id
        tags, cost = book.tags(tid), book.cost(tid)
        if tid < 0 or not slot.ready or cost is None or sun < cost:
            continue
        # 白天睡觉的蘑菇（忧郁菇投手等，combat.day_sleeper）：白天场景
        # 种了也是白睡 —— 跳过。场景语义（原版 0..5 → 日/夜/泳池日/雾夜/屋顶）：
        # 0=白天、2=泳池白天、4=屋顶白天才睡觉；1=黑夜、3=雾夜（夜间！蘑菇
        # 醒着）、5=月夜照常可用。（2026-09-30 屋顶审查：此前误把雾夜 3 当白天。）
        if book.combat(tid).get('day_sleeper') and board.scene in (0, 2, 4):
            continue
        # ★ 阳光花盆（platform+producer 双身份，2026-09-30 屋顶审查）：它是
        #   屋顶上唯一能直接上空屋顶格的经济卡。平台卡在下面被无条件跳过，
        #   它只能当连锁垫子 —— 平静富余期允许**独立预铺**（低分，不抢防御
        #   和产阳光主线的优先级）：先赚阳光，同时成为下一株的落点。
        #   落点限前四列（与"把前四列种满"一致）；can_plant 自动排除预置
        #   花盆格（花盆上不能叠花盆）和被占格。
        if T_PLATFORM in tags and T_PRODUCER in tags and (calm or develop) and sun >= 600:
            nx_any = min((f['nearest_zombie_x'] for f in facts
                          if f['nearest_zombie_x'] is not None), default=None)
            for r in sorted(quiet, key=lambda r: facts[r]['priority']):
                if facts[r]['threat_level'] == 'critical':
                    continue
                cols = [c for c in range(0, 4)
                        if (r, c) not in occ
                        and board.can_plant(r, c, tid, book)
                        and (nx_any is None or cell_x(c) + 35 < nx_any)
                        and not board.snow_blocked(r, c)
                        and not board.placement_blocked(r, c)]
                if cols:
                    add(slot, r, cols[0], 30,
                        'Pre-place a sun-producing pot on this roof cell: it earns sun now '
                        'and becomes the platform for the next plant.', [r])
                    break
            continue
        if T_PLATFORM in tags or book.role(tid) == PLATFORM:
            continue
        if book.role(tid) == SUPPORT and T_CHARM not in tags:
            continue
        # A reservation also constrains repeated cheap purchases. Emergency cancels it.
        # 女王储蓄期内只放行**便宜（≤150）救场卡**（见上方 rescue_now 的说明）。
        # ★ 用户 2026-09-28 硬约束：种向日葵必须积极 —— 储蓄大件时，便宜的
        #   产阳光卡（≤150）豁免储蓄闸：种向日葵正是攒钱买大件的最快方式，
        #   锁死它等于拖延大件本身（前院告急局实测：攒冰瓜时向日葵全被闸住，
        #   阳光不够用的根因）。
        # 豁免范围（2026-09-28 修正）：便宜产阳光卡只在中盘储蓄（opening=False）
        # 时绕过 —— 开局女王储蓄期向日葵仍要让位（女王硬约束优先）。
        if (plan and tid != plan['type_id'] and sun - cost < plan['cost']
                and not (cost <= 150 and rescue_now
                         and (rescue_bypass_tags & set(tags)))
                and not (T_PRODUCER in tags and cost <= 150
                         and not plan.get('opening', True))):
            continue
        close_intercept = T_WALL in tags and any(
            f['nearest_zombie_x'] is not None and any(
                0 <= f['nearest_zombie_x'] - cell_x(p.col) <= 160
                and not book.has_tag(p.type_id,T_WALL)
                for p in board.plants_in_lane(f['lane']-1)) for f in facts)
        if (producers < PREMIUM_ECON_TARGET
                and not (emergency or close_intercept
                         or (zero_defence_pressure and cost <= 150)
                         or (eager and T_INSTANT in tags))
                and not stable_sun_producer(book,tid)
                and cost >= ECON_CHEAP and sun - cost < ECON_RESERVE):
            # ⚠️ 注意是 >=：冰冻坚果/回收高坚果恰好 150/125，曾用 > 把它们
            # 全放行了 —— 开局阳光全被墙吃掉、向日葵种不下去（用户实测反馈）。
            # 例外：eager（大波/坦克/撞车）时灰烬绕过储备闸 —— 提前交牌优先。
            continue

        # ---- 储蓄到账：plan 目标买得起就直接出高分候选 ----
        # 正常路径给不出它（平静期压分/无僵尸不出射手候选），必须显式生成，
        # 否则 merge 的"兑现升级"找不到候选、储蓄永远在等待。
        if (plan and plan.get('slot') == slot.index
                and (board.sun or 0) >= (plan.get('cost') or 10**9)):
            if not plan.get('opening', True):
                # 中段储蓄大件（2026-09-30 屋顶审查）：此前复用女王块 —— 文案
                # 硬编码"Sunflower Queen"、落点被限 (1,2,3)。大件按正常
                # rear_cols 选列，文案按植物身份生成。
                for r in sorted(range(board.rows),
                                key=lambda r: (facts[r]['priority'], r)):
                    cols = [c for c in rear_cols(board, book, r, type_id=tid)
                            if board.can_plant(r, c, tid, book)]
                    if cols:
                        candidates.append(Candidate(
                            '', 'plant', r, cols[-1], slot.index, tid, 120,
                            f'Reserved upgrade is affordable: plant the {book.en(tid)} now '
                            '(saved for its coverage/armor/control value).',
                            tags, False))
                        break
                continue
            # 用户 2026-09-26 硬约束（2026-09-29 泳池局加固）：女王**绝不**与
            # 现有女王同行 —— 排序偏好不够，泳池局水路无荷叶时直接候选失败
            # 会退回火炬行（23:05:54 实测同排两女王）。已有火炬的行直接跳过，
            # 水路无荷叶的行由荷叶+女王连锁候选接管（天然落在新行）。
            for r in sorted((r0 for r0 in range(board.rows)
                             if not any(book.has_tag(p.type_id,T_TORCH)
                                        for p in board.plants_in_lane(r0))),
                            key=lambda r:(
                    not (2 in rear_cols(board,book,r) and board.can_plant(r,2,tid,book)),
                    facts[r]['priority'],abs(r-(board.rows-1)/2))):
                cols = [c for c in rear_cols(board, book, r)
                        if c in (1, 2, 3) and board.can_plant(r, c, tid, book)]
                if cols:
                    candidates.append(Candidate(
                        '', 'plant', r, min(cols,key=lambda c:abs(c-2)), slot.index, tid, 150,
                        'Opening save matured: plant the Sunflower Queen now '
                        '(producer + fighter + torch column); sunflowers follow behind her.',
                        tags, False))
                    break
            continue

        # ---- 兜底垫背：危急路没墙可用，便宜卡垫在僵尸脚下拖时间 ----
        if (stall_lanes and cost <= 150 and T_WALL not in tags
                and not any(t in tags for t in (T_INSTANT,T_GLOBAL_FREEZE,T_TEMPORARY))):
            added_stall = False
            for r, cell in stall_lanes:
                estimate = stall_window(board,book,r,tid,cell)
                if estimate is None and facts[r]['threat_level'] != 'critical':
                    continue
                if (r, cell) in occ:
                    continue
                prior_count = len(candidates)
                add(slot,r,cell,120 + facts[r]['priority']*0.4,
                    'Sacrificial speed bump: no wall card is ready on this critical lane; '
                    'buy time for defence. '
                    + (f'Estimated firing window: {estimate}; assumes 8 px/s walking and '
                       '100 hp/s biting per nearby enemy, not live measurements.' if estimate else
                       'Last-resort house rescue; no verified kill or cooldown timing.'), [r], intercept=True)
                added_stall = added_stall or len(candidates)>prior_count
            if added_stall:
                continue

        # eager 已在候选生成开头计算（大波/坦克/撞车 -> 提前交灰烬）。
        if T_GLOBAL_FREEZE in tags:
            if not emergency and total < 6 and not eager:
                continue
            cells = [(r,c) for r in quiet for c in range(board.cols) if (r,c) not in occ]
            if cells:
                r,c = cells[0]
                per = book.combat(tid).get('sun_per_frozen', 0)
                value = 110 + sum(f['priority'] for f in facts)*0.4
                why = 'Board-wide control; buys time against the current threats.'
                if per and total:
                    value += per * 0.5 * total
                    why += (f' Up to {per:g} sun per frozen zombie'
                            ' (not guaranteed for already frozen or immune targets).')
                add(slot,r,c,value, why, range(board.rows))
            continue

        if T_SPLASH3 in tags:
            for r in range(board.rows):
                cover = list(range(max(0,r-1), min(board.rows,r+2)))
                if not any(facts[x]['threat_level'] in ('high','critical') for x in cover):
                    continue
                cols = _free_cols(board,occ,r,0,board.cols-1)
                if cols:
                    add(slot,r,cols[0],100 + sum(facts[x]['priority'] for x in cover)*0.7,
                        'Burst covers these entire lanes: '+str([x+1 for x in cover])+'.', cover)
            continue

        if T_INSTANT in tags:
            # Local burst: score only zombies actually inside its 1.5-cell radius.
            for r in range(board.rows):
                for col in _free_cols(board,occ,r,0,board.cols-1):
                    hit = [z for lane in range(board.rows) for z in board.zombies_in_lane(lane)
                           if z.x is not None and ((z.x-cell_x(col))/80)**2+(z.row-r)**2 <= 2.25]
                    cover = {z.row for z in hit}
                    mid_push = any(facts[x]['zombie_count']
                                   and (facts[x]['nearest_zombie_x'] or 9999) < 560
                                   for x in cover)
                    if not any(facts[x]['threat_level'] in ('high', 'critical') for x in cover) \
                            and not (eager and mid_push):
                        continue
                    urgent_hit = any(z.x < 160 or (board.mowers.get(z.row) is False and z.x < 240)
                                     for z in hit)
                    sun_note = ''
                    sun_bonus = 0
                    if T_SUN_ON_KILL in tags:
                        # 阳光炸弹（用户 2026-09-26 技巧）：爆炸范围是**九宫格**，
                        # 炸的僵尸越多掉落阳光越多 —— 命中数加权给足，但不限制
                        # "九宫格必须满"才允许出（紧急时该炸就炸）。
                        sun_bonus = 12 * len(hit)
                        sun_note = ' 3x3-grid burst: more zombies hit, more sun refunded.'
                    eager_note = (' Dense/tanky push: spend the burst now instead of at the '
                                  'last moment.' if eager and not any(
                                      facts[x]['threat_level'] in ('high', 'critical')
                                      for x in cover) else '')
                    # 用户 2026-09-26 实战：撞车僵尸进路而雷果子/高冰果都在冷却时，
                    # **用阳光炸弹直接炸它** —— 这是没有防撞墙时的标准答案。
                    crush_note = ''
                    crush_bonus = 0
                    if any(book.zombie_flag(z.type_id, 'crush') for z in hit):
                        crush_bonus = 60 if not anti_crush_ready else 25
                        crush_note = (' Crushing zombie in the blast - with no crush-resistant '
                                      'wall ready, bombing it is the answer.')
                    add(slot,r,col,100 + sum(min(strength(z), 1800/270)*14 for z in hit) + sun_bonus
                        + (40 if eager and mid_push else 0) + crush_bonus
                        + (1000 if urgent_hit else 0),
                        f'Local burst reaches {len(hit)} zombie(s); damage may not kill heavy armor.'
                        + sun_note + eager_note + crush_note,
                        cover if urgent_hit else ())
            continue

        if T_CHARM in tags:
            for r in hot:
                nx = facts[r]['nearest_zombie_x']
                if nx is None or facts[r]['threat_level'] not in ('high','critical'):
                    continue
                cols = [c for c in _free_cols(board,occ,r,0,board.cols-1) if cell_x(c) <= nx]
                if cols:
                    add(slot,r,cols[-1],65+facts[r]['priority']*0.6,
                        'Charm approaching enemies; temporary defence.', [r])
            continue

        # Walls take precedence over their secondary shooter tag.
        if T_WALL in tags:
            for r in hot:
                nx = facts[r]['nearest_zombie_x']
                # ★ 用户 2026-09-26 实战（23:54 冰坚果种进冰车路送死）：普通墙
                #   挡不住冰车——压扁后直接穿过去。最近僵尸就是撞车时，本路
                #   只接受防撞墙（雷果子/高冰果）候选；都不在冷却时由灰烬
                #   炸弹接管（见 T_INSTANT 分支的 crush 加成）。
                if (facts[r].get('nearest_is_crush')
                        and not (book.combat(tid).get('crush_hits')
                                 or book.combat(tid).get('lethal_hit_burst'))):
                    continue
                if nx is None:
                    # 平静期预置：这路已有射手但没有墙 → 在射手前方预放一面墙，
                    # 下一波来的时候防线是完整的（保持各路阵型整齐）。
                    if (calm or develop) and facts[r]['shooter_support'] > 0 and not facts[r]['blocking_walls']:
                        spot = next((c for c in (4,5,3) if (r,c) not in occ), None)
                        if spot is not None:
                            add(slot,r,spot,24,
                                'Pre-building: a wall in front of this quiet lane\'s shooters '
                                'before the next wave arrives.',[r])
                    continue
                if facts[r]['blocking_walls'] and facts[r]['threat_level'] != 'critical':
                    # 墙系混血的价值全在"被啃"（反伤/免死/亡语冻结都只在贴身时生效），
                    # 藏到后排当射手等于白给 —— 用户实测反馈：冰坚果总被放到最后一排。
                    # 已有墙时改成在**最前那面墙的前方**再加一层（仍在僵尸来路一侧），
                    # 形成双墙纵深；前面没空位就放弃本轮，不再退化成后排输出。
                    front_wall = max((p.col for p in board.plants_in_lane(r)
                                      if book.has_tag(p.type_id, T_WALL)), default=-1)
                    ahead = [c for c in _free_cols(board,occ,r,front_wall+1,5)
                             if cell_x(c) <= nx-10]
                    if ahead:
                        add(slot,r,max(ahead),55+facts[r]['priority']*0.6,
                            'Extra wall layer in front of the existing wall; wall hybrids '
                            'earn their value by being bitten (reflect/death effects).',[r])
                    continue
                # 用户技巧：僵尸马上要吃到前排植物（差距 <= 2 格）时，允许墙
                # 直接种到僵尸所在格（重叠落子），立即拦住它、救下脆弱前排。
                front_def = max((cell_x(p.col) for p in board.plants_in_lane(r)
                                 if cell_x(p.col) <= nx + 40), default=None)
                overlap = front_def is not None and nx - front_def <= 160
                limit = nx + 40 if overlap else nx - 10
                cols = [c for c in _free_cols(board,occ,r,0,5) if cell_x(c) <= limit]
                # 用户 2026-09-26：冰坚果等高血量墙尽量种第5列(E)及以上，
                # 除非万不得已（僵尸太深、前排格全没）才允许更靠后的列。
                cols = [c for c in cols if c >= 4] or cols
                if not cols:
                    continue
                defenders = [p.col for p in board.plants_in_lane(r)
                             if not book.has_tag(p.type_id,T_WALL) and cell_x(p.col) < nx]
                front = max(defenders,default=-1)
                # 屋顶预置花盆格是"能种的平台"而非空地：优先选真正种得下的列。
                # 否则会越过花盆选中一块没有平台的空屋顶 —— add() 里又没有
                # 花盆卡可连锁，整个墙候选被吞掉（2026-09-30 屋顶 3-5 实测）。
                plantable = [c for c in cols if board.can_plant(r, c, tid, book)]
                preferred = ([c for c in plantable if c > front] or plantable
                             or [c for c in cols if c > front])
                col = preferred[-1] if preferred else cols[-1]
                profile = book.combat(tid)
                bonus, extra = 0, ''
                if T_FREEZE_ON_DEATH in tags:
                    bonus += 6
                    extra += ' If it falls, its death ripple freezes/slows nearby zombies.'
                if T_WALL_REGEN in tags:
                    bonus += 4
                    extra += ' Reclaimable wall; reclaim only while more than 800 hp remains.'
                if T_REFLECT in tags and facts[r]['zombie_count']:
                    bonus += min(10.0, profile.get('reflect_dps', 0) * 0.1)
                    extra += ' Reflects damage while being bitten.'
                # 进阶 1（用户 2026-09-26）：昂贵的输出植物没有墙保护时会被远程
                # 僵尸（僵尸豌豆射手）隔着防线点杀 —— 泳池里一整排射手被齐射团灭。
                # 无墙的昂贵射手路，墙候选加分；路上有驻停（远程）僵尸再加急。
                uncovered = [p for p in board.plants_in_lane(r)
                             if not book.has_tag(p.type_id, T_WALL)
                             and (book.cost(p.type_id) or 0) >= 300]
                if uncovered and not facts[r]['blocking_walls']:
                    bonus += 20 + 8 * min(3, len(uncovered))
                    extra += (f' Protects {len(uncovered)} expensive plant(s) that currently '
                              'have no wall in front of them.')
                if facts[r].get('ranged_zombies'):
                    bonus += 15
                    extra += (f" {facts[r]['ranged_zombies']} stationary (likely ranged) "
                              'zombie(s) in this lane shoot uncovered plants.')
                # 进阶 3（用户 2026-09-26 修正）：冰坚果**不能**防撞；能防撞的只有
                # 雷果子/高冰果（由图鉴 crush_hits/lethal_hit_burst 识别，冰车
                # 撞上它们会直接爆胎停车）。撞车路优先防撞墙，普通墙明示风险。
                if facts[r].get('crush_zombies'):
                    crush_close = (facts[r].get('nearest_zombie_x') or 9999) < 320
                    if profile.get('crush_hits') or profile.get('lethal_hit_burst'):
                        bonus += 25 if crush_close else 15
                        extra += (' Crush-resistant: the crushing zombie pops its tire on '
                                  'this wall; this is the blocker to use when it cannot be '
                                  'stopped otherwise.')
                    else:
                        extra += ' WARNING: a crushing zombie in this lane can squash this wall (ice wall-nut does NOT resist crushing).'
                val = 65 + facts[r]['priority'] * 0.8 + bonus
                add(slot,r,col,val,
                    'Intercept on the house side of the zombie; shield the surviving rear plants.'
                    + (' Placed onto the zombie to block it instantly.' if overlap else '')+extra,[r])
            continue

        if T_PRODUCER in tags and T_SHOOTER not in tags:
            # Currency and conditional/unknown sun effects cannot rebuild the
            # recurring battle-sun economy. Other explicit combat/support tags
            # have already been evaluated above.
            if not stable_sun_producer(book, tid):
                continue
            # 产阳光上限：常规 2株/行；阳光充裕的平静期（calm_fill）放宽到
            # 4株/行 —— 用户要求把前四列种满、不许发呆（多种向日葵不亏）。
            producer_cap = max(6, board.rows*2) + (board.rows*2 if calm_fill else 0)
            if producers >= producer_cap:
                continue
            for r in sorted(quiet, key=lambda r: (min(rear_cols(board,book,r,producer=True,type_id=tid), default=99), facts[r]["priority"], r)):
                cols = rear_cols(board,book,r,producer=True,type_id=tid)
                if not cols or facts[r]['threat_level'] == 'critical':
                    continue
                # 经济重建（实战 2026-09-26 教训）：产阳光被打到 4 株以下、
                # 全路 high 时，旧规则会因"高压路不种向日葵"而完全断掉经济
                # —— 螺旋死亡。high 路只要**有墙护着**（rear_cols 本来就只给
                # 墙后空位、且僵尸未走过），就允许补种向日葵恢复产能。
                if (facts[r]['threat_level'] == 'high'
                        and not facts[r]['blocking_walls']):
                    continue
                add(slot,r,cols[0],85 if producers < max(6,board.rows*2) else 25,
                    f'Safe rear economy; {producers} producers currently alive.')
                break
            continue

        if T_SHOOTER in tags:
            tracking = T_TRACKING in tags
            rng = book.range_cells(tid)
            radius = coverage_lanes(book,tid)//2
            # 平静期（没僵尸）：照常补火力，但只补还没有射手的路，保持阵型整齐。
            for r in (quiet if tracking else
                      (list(range(board.rows)) if calm else hot)):
                cover = list(range(board.rows)) if tracking else list(range(max(0,r-radius),min(board.rows,r+radius+1)))
                if not tracking and not any(facts[x]['zombie_count'] for x in cover) and not (calm or develop):
                    continue
                # 用户 2026-09-26：平静且阳光充裕时允许同路第二个射手（填满四列、
                # 不发呆）；否则每路一个射手就够了。
                if (not (plan and tid == plan['type_id']) and not calm_fill
                        and (calm or (develop and not facts[r]['zombie_count']))
                        and any(book.has_tag(p.type_id, T_SHOOTER)
                                for p in board.plants_in_lane(r))):
                    continue
                cols = rear_cols(board,book,r,type_id=tid)
                if not cols:
                    continue
                if T_TORCH in tags and plan and tid == plan['type_id'] and not total:
                    cols = [c for c in cols if c in (1,2)]
                    if not cols:
                        continue
                nx_r = min((facts[x]['nearest_zombie_x'] for x in cover
                            if facts[x]['nearest_zombie_x'] is not None),default=None)
                targets = [z for x in cover for z in board.zombies_in_lane(x)]
                if rng is not None and not targets and total:
                    continue
                if rng is not None and targets:
                    # Forward range includes both boundaries, including adjacent
                    # lanes whose lead enemy may already have passed this column.
                    reach = [c for c in cols
                             if any(can_hit(book,tid,r,c,z) for z in targets)]
                    if not reach:
                        continue
                    col = max(reach)
                else:
                    torch = max((p.col for p in board.plants_in_lane(r)
                                 if not p.asleep and p.hp!=0 and book.has_tag(p.type_id, T_TORCH)
                                 and (nx_r is None or cell_x(p.col)<nx_r)), default=None)
                    if book.combat(tid).get('torch_compatible') and torch is not None:
                        # 用户技巧：豌豆穿过向日葵女王（火炬柱）获火焰增益，
                        # 落点优先选女王身后（更靠房子一侧）最近的空位。
                        behind = [c for c in cols if c < torch]
                        col = max(behind) if behind else min(cols,key=lambda c: abs(c-2))
                    else:
                        # Prefer middle rear cells, preserving A/B for the economy.
                        col = min(cols,key=lambda c: abs(c-(1 if book.combat(tid).get('torch_compatible') else 2)))
                value = 35 + max(facts[x]['priority'] for x in cover)*0.5
                # ★ 用户 2026-09-28 硬约束：攻击手必须优先自带追踪 ——
                #    全屏追踪 > 锁定单路，同样火力下追踪卡价值高得多。
                if tracking:
                    value += 25
                value -= facts[r]['shooter_support']*8
                if calm:
                    value = min(value, 24)
                if plan and tid == plan['type_id']:
                    value += 100
                why = ('Pre-building the line while the board is quiet; keeps the formation uniform.'
                       if calm else 'Sustained fire from behind the wall; ')
                add(slot,r,col,value,why+
                    ('covers all lanes.' if tracking else f'reinforces lane {r+1}.'),cover)
                if tracking:
                    break

    # -- 铲子候选（回收高坚果等"可回收"的墙）------------------------------
    # 为什么放在卡槽循环之外：铲子不花阳光、不占卡槽，它作用于**已经在场上的植物**。
    for p in board.plants:
        if not book.has_tag(p.type_id, T_WALL_REGEN):
            continue
        if board.shovel_target(p.row, p.col, book) != p:
            continue
        hp = p.hp
        # 正在被啃时窗口放宽到 3600（2026-09-26）：100/s 的啃食速度下 2400 只
        # 意味着几秒后必然跌破回收线，早铲早回收 = 少白给 800+ 血。
        # 没被啃的墙维持 2400 —— 满血的墙还在值勤，铲它是送冷却。
        f = facts[p.row]
        crush_recovery = crusher_approaching(board,book,p)
        eligible,reposition = recovery_status(p,f,crush_threat=crush_recovery)
        if not eligible: continue
        destination = relocation_target(board,book,p,ready_only=crush_recovery)
        if destination is None and not crush_recovery:
            continue
        urgent = f["threat_level"] == "critical"
        # 回收窗口即将关闭也算紧急：僵尸啃食 100/s，一个决策周期约 3 秒。
        # 若这面墙正在被啃、且血量只比回收线高不到 ~3 秒的量，等下一轮就来不及了
        # —— 和"近屋救场"同一逻辑：确定性判据，不指望模型自己算时间。
        if p.recently_eaten and (hp - RECLAIM_MIN_HP) <= 300:
            urgent = True
        candidates.append(Candidate(
            '', 'shovel', p.row, p.col, -1, p.type_id,
            (500 if crush_recovery else 150 if urgent else 60) + f["priority"] * 0.5,
            (f'Reclaim it before zombies finish it: shoveling costs 800 hp, the returned '
             f'card keeps the remaining {hp - 800}; once its hp drops to 800 or below it '
             f'can never be reclaimed again and is simply lost. '
             + ('It is being bitten right now and the reclaim window closes within seconds. '
                if p.recently_eaten else '')
             + ('Confirmed crusher within two cells: recover now even at full health. ' if crush_recovery else '')
             + (f'Replant at lane {destination[0]+1}, column {destination[1]+1}; '
                + ('use existing support, do not delay shoveling to buy a pad.' if crush_recovery else 'prepare water support first.')
                if destination else 'No safe supported cell: leave the verified returned card on the lawn for later.')),
            book.tags(p.type_id), False, (() if crush_recovery else (destination[0],)), hp,
            relocate_to=destination, crush_recovery=crush_recovery, source_index=p.index,
        ))

    # -- 高级技巧（用户 2026-09-26）：铲掉换阳光 --------------------------
    # 这版游戏铲植物**返阳光**，所以"快被啃死的植物"应该铲掉换阳光，而不是
    # 被僵尸白吃。两个确定性触发：
    #   a) 正在被啃、血量撑不过一个决策周期（按路上最快的啃食速度算，黑橄榄球
    #      类更快）—— 铲掉回收阳光；
    #   b) 撞车僵尸（冰车读报类）距本路最前面的**不可防撞**植物 ≤2 格 —— 它会
    #      直接压扁植物，提前铲掉换阳光。防撞植物（雷果子/高冰果）绝不铲。
    # 与回收高坚果的"回收"不同：这里不指望拿回卡片，只要阳光 > 0 就是净赚。
    for p in board.plants:
        if book.has_tag(p.type_id, T_WALL_REGEN) or p.asleep:
            continue
        if board.shovel_target(p.row, p.col, book) != p:
            continue
        profile = book.combat(p.type_id)
        anti_crush = bool(profile.get('crush_hits') or profile.get('lethal_hit_burst'))
        f = facts[p.row]
        biters = [z for z in board.zombies_in_lane(p.row)
                  if z.x is not None and z.x <= cell_x(p.col) + 48]
        bite_dps = max([book.zombie_trait(z.type_id, 'eat_dps') or 100 for z in biters] or [100])
        # a) 快被啃死：有嘴贴身就认（recently_eaten 是个约 1s 的短倒计时位，
        #    决策采样经常错过 —— 2026-09-26 夜战实测 44 次被啃只抓到 6 次），
        #    血量 ≤ 最快啃食速度×4s 触发；≤×1.5s 升级 emergency（这轮不铲下轮
        #    就没了，救场覆盖要能选中它）。
        if biters and p.hp is not None and p.hp <= bite_dps * 4:
            dying = p.hp <= bite_dps * 1.5
            candidates.append(Candidate(
                '', 'shovel', p.row, p.col, -1, p.type_id,
                (170 if dying else 120) + f["priority"] * 0.5,
                (f'Salvage shovel: zombies are chewing it and only about {p.hp} hp is left '
                 f'(biters chew ~{bite_dps:.0f}/s here) - shoveling refunds sun instead '
                 'of letting zombies destroy it for nothing. Do this immediately.'),
                book.tags(p.type_id),
                False, (p.row,),
                salvage=True, hp=p.hp, source_index=p.index,
            ))
        # b) 压扁前预铲：撞车僵尸两格内的前排不可防撞植物（还没被啃到）
        if (not anti_crush and not p.recently_eaten and f.get('crush_zombies')
                and f['nearest_zombie_x'] is not None):
            in_path = [q for q in board.plants_in_lane(p.row)
                       if cell_x(q.col) <= f['nearest_zombie_x'] + 40]
            frontmost = max(in_path, key=lambda q: cell_x(q.col), default=None)
            if (frontmost is not None and frontmost.cell == p.cell
                    and f['nearest_zombie_x'] - cell_x(p.col) <= 160):
                candidates.append(Candidate(
                    '', 'shovel', p.row, p.col, -1, p.type_id,
                    110 + f["priority"] * 0.5,
                    (f'A crushing zombie is within ~2 cells of this plant and will squash '
                     f'it outright (this plant is not crush-resistant); shovel it now to '
                     'convert it into sun instead of losing it for nothing.'),
                    book.tags(p.type_id), False, (p.row,),
                    salvage=True, hp=p.hp, source_index=p.index,
                ))

    # -- 进阶 5-2（用户 2026-09-26）：铲旧换新，让女王身后物尽其用 ---------
    # 女王是火炬柱：豌豆/狂野机枪/玉米卷从她身后穿火有伤害加成。如果中期
    # 运营结果是她身后那个黄金槽位被向日葵/豌豆这类廉价植物占着，而阳光充裕、
    # 手里有就绪的过火射手 —— 铲掉它换阳光，下一轮自然有强射手候选来补位。
    # 门槛收得很紧：无危急/无高压、该路无僵尸、目标廉价（≤150）非墙非火炬、
    # 手里有过火射手且买得起+留 300 储备。铲除与替换在同一事务中确认。
    # 2026-09-28 用户硬约束放宽：火炬柱换新优先级提高 —— 目标路保持安静、
    # 其余路不许危急即可（此前要求全图安静，实战从不触发）；储备 300→200。
    if all(f['threat_level'] != 'critical' for f in facts):
        torches = {p.row: p for p in board.plants
                   if book.has_tag(p.type_id, T_TORCH)}
        upgrades = [s for s in board.slots if s.ready
                    and book.has_tag(s.type_id, T_SHOOTER)
                    and book.combat(s.type_id).get('torch_compatible')
                    and (book.cost(s.type_id) or 10**9) >= 300]
        for r, q in torches.items():
            # 僵尸很远（>560，>1 分钟路程）时不拦换新 —— 旧门槛"该路有僵尸
            # 就跳过"让换新在实战从不触发（僵尸几乎总在路上）。
            nx_r = facts[r]['nearest_zombie_x']
            if q.col <= 0 or (nx_r is not None and nx_r <= 560):
                continue
            behind = board.top_body(r,q.col-1,book)
            if behind is None or behind.asleep or behind.recently_eaten:
                continue
            btags, bcost = book.tags(behind.type_id), book.cost(behind.type_id) or 0
            if bcost > 150 or T_WALL in btags or T_TORCH in btags:
                continue
            upgrade = next((s for s in upgrades
                            if (book.cost(s.type_id) or 10**9) + 200 <= (board.sun or 0)
                            and board.can_replace(behind,s.type_id,book)), None)
            if upgrade is None or snow_blocked(board, r, behind.col):
                continue
            candidates.append(Candidate(
                '', 'shovel', r, behind.col, -1, behind.type_id,
                85 + (book.cost(upgrade.type_id) or 0) * 0.02,
                (f'Torch-column upgrade: the cell right behind the Sunflower Queen '
                 f'(lane {r+1}) is wasted on a cheap {book.en(behind.type_id)}. Shovel it '
                 f'and immediately plant {book.en(upgrade.type_id)} using reserved sun; '
                 f'confirm the top body and preserve its supporting platform.'),
                book.tags(behind.type_id), False, (r,),
                salvage=True, hp=behind.hp, replacement_type=upgrade.type_id, source_index=behind.index,
            ))

    # -- 后期阵型改造（用户 2026-09-28 硬约束，优先级高）-------------------
    # 阵成型+阳光够用后：把前中排的廉价向日葵铲掉换成攻击/防御卡。
    # 门槛：全场非危急、目标路安静、该路 ≥3 株产阳光（拆不垮经济）、
    # 手里有就绪的攻击/防御卡（≥200）且阳光够买它再留 400。经济没起来不动。
    if all(f['threat_level'] != 'critical' for f in facts) and (board.sun or 0) >= 600:
        producers_by_lane = {}
        shooters_by_lane = {}
        for p in board.plants:
            if book.has_tag(p.type_id, T_PRODUCER):
                producers_by_lane[p.row] = producers_by_lane.get(p.row, 0) + 1
            if book.has_tag(p.type_id, T_SHOOTER) and not book.has_tag(p.type_id, T_PRODUCER):
                shooters_by_lane[p.row] = shooters_by_lane.get(p.row, 0) + 1
        refit_cards = [s for s in board.slots if s.ready
                       and (book.has_tag(s.type_id, T_SHOOTER) or book.has_tag(s.type_id, T_WALL))
                       and (book.cost(s.type_id) or 10**9) >= 200]
        for r, cnt in sorted(producers_by_lane.items()):
            if cnt < 3 or shooters_by_lane.get(r, 0) < 1:
                continue
            if facts[r]['threat_level'] in ('high', 'critical') or facts[r]['zombie_count']:
                continue
            front = [p for p in board.plants_in_lane(r)
                     if p.col <= 3 and not book.has_tag(p.type_id, T_TORCH)
                     and (book.cost(p.type_id) or 0) <= 150
                     and book.has_tag(p.type_id, T_PRODUCER)]
            target = max(front, key=lambda p: p.col, default=None)
            if target is None or target.recently_eaten or snow_blocked(board, r, target.col):
                continue
            upgrade = next((s for s in refit_cards
                            if (book.cost(s.type_id) or 10**9) + 400 <= (board.sun or 0)
                            and board.can_replace(target, s.type_id, book)), None)
            if upgrade is None:
                continue
            candidates.append(Candidate(
                '', 'shovel', r, target.col, -1, target.type_id,
                90 + (book.cost(upgrade.type_id) or 0) * 0.02,
                (f'Late-game refit: lane {r+1} has {cnt} producers and its defence is '
                 f'stable - shovel the cheap {book.en(target.type_id)} at column '
                 f'{target.col+1} (refunds sun) and replace it with {book.en(upgrade.type_id)} '
                 'in the same transaction. Economy is established; this is the '
                 'high-priority strength upgrade the user asked for.'),
                book.tags(target.type_id), False, (r,),
                salvage=True, hp=target.hp, replacement_type=upgrade.type_id,
                source_index=target.index,
            ))

    # -- 拾回掉落卡（2026-09-26 新增）--------------------------------------
    # 回收事务被对局中断/回收后没拾起时，铲掉的墙会以卡片形式躺在草坪上。
    # 没有这个候选它就一直丢在那（夜战实测两次 source_removed 后中断）。
    # 前提：手是空的、卡是已登记植物、且有合法落点（撞车路/积雪/占用已由
    # relocation_target 排除）——条件不满足就不出候选，宁可躺着也不空手乱点。
    if not board.holding:
        for d in board.dropped_seeds:
            if d.type_id not in book.kb_by_id or book.has_tag(d.type_id, T_PLATFORM):
                continue
            row = max(0, min(board.rows - 1, int((d.y + d.height / 2 - 80) // 100)))
            col = max(0, min(board.cols - 1, int((d.x + d.width / 2 - 40) // 80)))
            pseudo = SimpleNamespace(cell=(row, col), type_id=d.type_id, row=row, col=col)
            if relocation_target(board, book, pseudo,ready_only=True) is None:
                continue
            candidates.append(Candidate(
                '', 'pick', row, col, -1, d.type_id,
                90, (f'A {book.en(d.type_id)} seed card is lying on the lawn at lane '
                     f'{row+1}, column {col+1} (an interrupted recovery left it there). '
                     'Pick it up and replant it at a valid spot.'),
                book.tags(d.type_id), False, (row,), drop_index=d.index,
            ))

    # Keep rescue choices first, then rank useful alternatives. Per-type cap prevents flooding.
    candidates.sort(key=lambda c: (not c.emergency,-c.score,c.row,c.col))
    selected, seen, counts = [], set(), {}
    # First pass: one best placement per available plant, so duplicate cheap
    # placements cannot hide a distinct expensive capability from Jev.
    first, alternatives, represented = [], [], set()
    for c in candidates:
        capability = (c.kind,c.type_id,c.supports_type)
        if capability not in represented:
            first.append(c)
            represented.add(capability)
        else:
            alternatives.append(c)
    for c in first + alternatives:
        key = candidate_key(c)
        capability = (c.kind,c.type_id,c.supports_type)
        if key in seen or counts.get(capability,0) >= MAX_PER_TYPE:
            continue
        selected.append(c)
        seen.add(key)
        counts[capability] = counts.get(capability,0)+1
        if len(selected) >= MAX_CANDIDATES:
            break
    for i,c in enumerate(selected):
        c.cid = f'A{i+1}'
    why = (f"Save for {plan['plant']}: target {plan['cost']} sun, missing {plan['missing_sun']}."
           if plan else 'No placement is clearly worth the sun right now.')
    selected.append(Candidate(f'A{len(selected)+1}','wait',why=why))
    return selected


# ---------------------------------------------------------------- 提问
def build_questions(candidates: list[Candidate], board: BoardState, book: PlantBook) -> dict:
    """一次性 fan-out 全部问题：同一 state 下并行，比逐条问便宜得多。"""
    threats = {r: lane_threat(board, r, book) for r in range(board.rows)}

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
            f"defenders: {def_txt}; mower={t['mower_available']} (None=unknown), "
            f"enemy strength={t['zombie_strength']}, priority={t['priority']}."
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
                "sun reserve is small ONLY if that solves the actual danger. Follow saving_plan "
                "when safe; avoid repeated cheap spending that delays its target. Rescue near-house "
                "threats first, especially without a mower. Do not assume unknown mower status is safe. "
                "Shovel options state whether they salvage sun or recover a reusable seed card; choose one only "
                "when reclaiming it now clearly beats letting zombies destroy it. "
                "Be careful with long-cooldown one-shot plants "
                "when the board is still calm."
            ),
            "criteria": action_opts,
        },
        "hold_sun": {
            "type": "noul",
            "instructions": (
                "Should we deliberately spend nothing this cycle and keep saving sun? "
                "Answer yes only if planting right now would be wasteful, premature, or "
                "would leave the sun reserve too low for an imminent emergency. Honor saving_plan "
                "when safe. A quiet board still needs economy and missing-lane defence; do not hoard "
                "surplus sun indefinitely. Never hold if a listed rescue can address a near-house threat."
            ),
            "criteria": {"true": "yes - hold and save the sun", "false": "no - spend sun now"},
        },
    }


# ---------------------------------------------------------------- 合并
HOLD_THRESHOLD = 0.6


def candidate_key(c):
    return c.kind, c.slot, c.type_id, c.row, c.col, c.supports_type, c.salvage, c.relocate_to, c.intercept, c.drop_index, c.crush_recovery, c.replacement_type, c.source_index


def recovery_status(p,f,*,crush_threat=False):
    """Shared purpose test; ranking truncation cannot invalidate a safe recovery."""
    nx=f['nearest_zombie_x']
    reposition = p.col<4 and not p.recently_eaten and (nx is None or nx>cell_x(p.col)+160)
    limit=RECLAIM_MAX_HP_BITTEN if p.recently_eaten else RECLAIM_MAX_HP
    contact=nx is not None and nx<=cell_x(p.col)+30
    if crush_threat:
        return p.hp is not None and p.hp>RECLAIM_MIN_HP, False
    eligible=(p.hp is not None and p.hp>RECLAIM_MIN_HP and (p.hp<=limit or reposition)
              and (f['zombie_count'] or reposition)
              and (reposition or contact or f['threat_level'] in ('high','critical')))
    return bool(eligible),reposition


def burst_targets(candidate, board, book):
    tid = candidate.supports_type if candidate.supports_type is not None else candidate.type_id
    if book.has_tag(tid,T_GLOBAL_FREEZE):
        return [z for r in range(board.rows) for z in board.zombies_in_lane(r)]
    if book.has_tag(tid,T_SPLASH3):
        return [z for r in range(max(0,candidate.row-1),min(board.rows,candidate.row+2))
                for z in board.zombies_in_lane(r)]
    return [z for r in range(board.rows) for z in board.zombies_in_lane(r)
            if z.x is not None and ((z.x-cell_x(candidate.col))/80)**2+(z.row-candidate.row)**2<=2.25]


def rescue_is_effective(candidate, board, book):
    """Conservative screening, not a simulator: can this action affect the lead threat
    before it reaches the house? Shared trackers get only their estimated damage share.
    Unknown timings and movement use explicit margins; do not promise an instant save.
    """
    if candidate.kind != 'plant': return False
    tid = candidate.supports_type if candidate.supports_type is not None else candidate.type_id
    tags, profile = book.tags(tid), book.combat(tid)
    critical = [r for r in candidate.covers if lane_facts(board,r,book)['threat_level']=='critical']
    for row in critical:
        zs = [z for z in board.zombies_in_lane(row) if z.x is not None]
        if not zs: continue
        lead = min(zs,key=lambda z:z.x)
        # Walk speed is not measured yet; default to a conservative 12 px/s.
        speed = max(12,book.zombie_trait(lead.type_id,'speed_px_s') or 0)
        available = max(0,lead.x/speed-1)
        setup = 1.0 + (1.0 if candidate.supports_type is not None else 0)
        if available <= setup: continue
        if T_WALL in tags or candidate.intercept:
            anti = bool(profile.get('crush_hits') or profile.get('lethal_hit_burst'))
            if row==candidate.row and cell_x(candidate.col)<=lead.x+40 and (
                    not book.zombie_flag(lead.type_id,'crush') or anti): return True
        elif T_GLOBAL_FREEZE in tags:
            if setup + profile.get('freeze_delay_s',0) < available: return True
        elif T_INSTANT in tags or T_SPLASH3 in tags:
            if lead in burst_targets(candidate,board,book) and (
                    profile.get('burst_damage',0) >= (lead.hp or 270)+(lead.armor_hp or 0)): return True
        elif T_SHOOTER in tags:
            dps = target_dps(board,book,tid,candidate.row,candidate.col,lead)
            # Allow for the first firing interval and projectile travel.
            first_hit = 1.5 + abs(lead.x-cell_x(candidate.col))/200
            if dps>0 and setup+first_hit+((lead.hp or 270)+(lead.armor_hp or 0))/dps < available:
                return True
    return False


def board_active(board) -> bool:
    """游戏真的在跑吗？—— 决策层唯一的"活体"判据。

    ⚠️ 不能用 `Board+0x164`(paused)：agent.py 实测**杂交版时钟正常推进时它也
    读 1，完全不可靠**（那是"莫名全轮等待"的根：所有候选被判 stale、收阳光
    停摆）。可靠信号是 game_clock 是否在推进，由 agent 每轮写进
    `board.clock_advancing`；读不到（None，比如合成战场/第一轮）按"在跑"处理，
    执行层另有时钟闸兜底。
    """
    return board.clock_advancing is not False


def adapt_stale_candidate(candidate, board, book, bad_cells=None):
    """执行前发现原候选过期时，在同一路的**新快照**里找同类候选顶上。

    为什么需要：决策到执行隔 1~6s，僵尸移动会让"同一个键"的候选消失
    （墙的拦截列随僵尸前移、格子被占、卡冷却……）。第二局实测 36 次
    stale_action 浪费了三分之一的决策周期 —— 模型选的意图是"用这张卡守
    这条路"，落点允许代码按新战场适配：同卡、同路、合法，就执行新落点。
    只对种植类候选适配；铲子候选的键失效=目标植物没了，适配没有意义。
    """
    if (candidate is None or candidate.kind != 'plant' or candidate.row < 0
            or board is None or not board.ok or not board_active(board)):
        return None
    fresh = [c for c in generate_candidates(board, book)
             if c.kind == 'plant' and c.type_id == candidate.type_id
             and c.row == candidate.row and c.supports_type == candidate.supports_type
             and not (bad_cells and (c.type_id, c.row, c.col) in bad_cells)]
    if not fresh:
        return None
    fresh.sort(key=lambda c: (abs(c.col - candidate.col), -c.score))
    return fresh[0]


def escalate_emergency(candidate, board, book, bad_cells=None):
    """Refresh house rescue first, then protect a reusable wall from instant crushing.

    为什么需要：决策 -> Jev 返回 -> 动手之间隔着 1~6s，僵尸每周期走 40~80px。
    merge_decision 的"fresh 重算"用的是决策前那张旧快照，只有这里拿的是
    执行前刚读的 board —— 决策时 low 的路此刻可能已经 critical。
    返回 None 表示无需升级；防压回收保全资产，不算房屋救场。
    """
    if board is None or not board.ok or not board_active(board):
        return None
    facts = [lane_facts(board, r, book) for r in range(board.rows)]
    if candidate is not None and candidate.emergency and rescue_is_effective(candidate,board,book):
        return None
    fresh = generate_candidates(board, book)
    esc = [c for c in fresh if c.emergency and c.kind in ("plant", "shovel")
           and not (bad_cells and c.kind == "plant"
                    and (c.type_id, c.row, c.col) in bad_cells)]
    if esc and any(f['threat_level']=='critical' for f in facts):
        esc.sort(key=lambda c: (-c.score, c.row, c.col))
        return esc[0]
    recovery = next((c for c in fresh if c.crush_recovery), None)
    if recovery is not None and (candidate is None or candidate_key(candidate)!=candidate_key(recovery)):
        return recovery
    return None


def action_invalid_reason(candidate, board, book):
    """Hard placement and purpose checks, independent of shortlist ranking."""
    if not board.ok or not board_active(board): return 'board inactive'
    if candidate.kind == 'wait': return None
    if candidate.kind == 'pick':
        drop=next((d for d in board.dropped_seeds if d.type_id==candidate.type_id
                   and (candidate.drop_index is None or d.index==candidate.drop_index)),None)
        if drop is None: return 'dropped seed disappeared'
        from types import SimpleNamespace
        source=SimpleNamespace(cell=(candidate.row,candidate.col),row=candidate.row,
                               type_id=candidate.type_id)
        return None if relocation_target(board,book,source,ready_only=True) is not None else 'no supported recovery destination'
    if candidate.kind == 'shovel':
        source = board.shovel_target(candidate.row, candidate.col, book)
        if source is None or source.type_id != candidate.type_id or (
                candidate.source_index is not None and source.index != candidate.source_index):
            return 'shovel source layer or identity changed'
    if candidate.kind == 'shovel' and candidate.replacement_type is not None:
        source = board.top_body(candidate.row,candidate.col,book)
        if source is None or source.type_id!=candidate.type_id or (
                candidate.source_index is not None and source.index!=candidate.source_index):
            return 'upgrade source changed'
        if not board.can_replace(source,candidate.replacement_type,book):
            return 'upgrade cell unsupported or occupied'
        if board.snow_blocked(candidate.row,candidate.col): return 'confirmed ice trail'
        if not any(s.type_id==candidate.replacement_type and s.ready for s in board.slots):
            return 'replacement card changed or cooling'
        cost = book.cost(candidate.replacement_type)
        if cost is None or cost+ECON_RESERVE>(board.sun or 0): return 'insufficient reserved sun for replacement'
        if board.zombies_in_lane(candidate.row): return 'upgrade lane no longer calm'
        return None
    if candidate.kind == 'shovel' and not candidate.salvage:
        source=next((p for p in board.plants if p.cell==(candidate.row,candidate.col)
                     and p.type_id==candidate.type_id),None)
        if source is None or not book.has_tag(source.type_id,T_WALL_REGEN): return 'recovery source changed'
        if source.hp is None or source.hp<=800: return 'recovery health window closed'
        crush_threat=crusher_approaching(board,book,source)
        if candidate.crush_recovery and not crush_threat: return 'crusher no longer approaching source'
        if not recovery_status(source,lane_facts(board,source.row,book),crush_threat=crush_threat)[0]:
            return 'recovery purpose no longer valid'
        if candidate.crush_recovery: return None  # Missing destination means verified recovery for later.
        return None if relocation_target(board,book,source) is not None else 'no relocation destination'
    if candidate.kind != 'plant':
        return None if any(candidate_key(c)==candidate_key(candidate) for c in generate_candidates(board,book)) else 'recovery target or purpose changed'
    book.sync_field_copies(board.plants)
    c = candidate
    slot = next((s for s in board.slots if s.index==c.slot and s.type_id==c.type_id),None)
    if slot is None or not slot.ready: return 'slot changed or cooling'
    if c.total_cost(book) is None or c.total_cost(book) > (board.sun or 0): return 'insufficient sun for complete action'
    if board.snow_blocked(c.row,c.col): return 'confirmed ice trail'
    if not board.can_plant(c.row,c.col,c.type_id,book): return 'occupied or unsupported cell'
    tid = c.supports_type if c.supports_type is not None else c.type_id
    tags = book.tags(tid)
    if c.supports_type is not None and not any(s.ready and s.type_id==tid for s in board.slots):
        return 'followup card changed or cooling'
    if (T_SHOOTER in tags and T_WALL not in tags and T_TRACKING not in tags
            and not c.intercept and book.range_cells(tid) is not None):
        radius = coverage_lanes(book,tid)//2
        targets = [z for r in range(max(0,c.row-radius),min(board.rows,c.row+radius+1))
                   for z in board.zombies_in_lane(r)]
        if any(not z.friendly for z in board.zombies) and not any(can_hit(book,tid,c.row,c.col,z) for z in targets):
            return 'short-range shooter has no reachable targets in covered lanes'
    nx = lane_facts(board,c.row,book)['nearest_zombie_x']
    if (T_WALL in tags or c.intercept) and nx is not None and cell_x(c.col)>nx+40: return 'zombie passed blocking cell'
    if T_WALL in tags and lane_facts(board,c.row,book)['nearest_is_crush'] and not (
            book.combat(tid).get('crush_hits') or book.combat(tid).get('lethal_hit_burst')):
        return 'blocker cannot stop confirmed crusher'
    if not c.intercept and not any(t in tags for t in (T_WALL,T_INSTANT,T_GLOBAL_FREEZE,T_SPLASH3,T_TEMPORARY)):
        if c.col not in rear_cols(board,book,c.row,producer=T_PRODUCER in tags and T_SHOOTER not in tags,type_id=tid): return 'rear placement no longer safe'
    if any(t in tags for t in (T_INSTANT,T_SPLASH3,T_GLOBAL_FREEZE)) and not burst_targets(c,board,book):
        return 'no targets remain in affected area'
    return None


def action_is_current(candidate, board, book):
    return action_invalid_reason(candidate,board,book) is None


def merge_decision(resp, candidates: list[Candidate], board: BoardState, book: PlantBook) -> Decision:
    active = board_active(board)
    fresh = {candidate_key(c): c for c in generate_candidates(board,book)} if board.ok and active else {}
    valid = []
    for c in candidates:
        current = fresh.get(candidate_key(c))
        if c.kind == 'wait' or current is not None:
            if current is not None:
                c.emergency, c.covers, c.score = current.emergency, current.covers, current.score
                c.supports_type, c.relocate_to = current.supports_type, current.relocate_to
                c.salvage = current.salvage
            valid.append(c)
    wait = next((c for c in valid if c.kind == 'wait'), Candidate('WAIT','wait'))
    # 铲子和种植是同一层"可执行动作"，一起参与择优与救场覆盖。
    plants = sorted((c for c in valid if c.kind in ('plant', 'shovel', 'pick')),
                    key=lambda c:(not c.emergency,-c.score))
    best = plants[0] if plants else wait
    emergency = next((c for c in plants if c.emergency),None)
    d = Decision()
    if resp is None or not resp.ok:
        d.fallback = True
        err = (resp.error or '') if resp is not None else ''
        timed_out = any(k in err.lower() for k in ('timed out', 'timeout', 'http 000', 'curl: (28'))
        if timed_out:
            # 超时兜底走**保守**方向：等待，让下一轮重新决策。
            # 选 best（最高分）会在没有模型确认的情况下连续把钱花在最贵的卡上。
            # 但危急路例外 —— 救场覆盖不能因为模型超时就缺席。
            if emergency is not None:
                chosen = emergency
                d.notes.append(f'Jev timed out ({err[:80]}); rescue overrides the conservative wait.')
            else:
                chosen = wait
                d.notes.append(f'Jev timed out ({err[:80]}); wait conservatively this cycle.')
        else:
            chosen = best
            d.notes.append('Jev unavailable; use currently valid tactical fallback.')
    else:
        lane, urgency = resp.get('threat_lane'), resp.get('urgency')
        if lane and lane.choice:
            try:
                d.threat_lane = int(str(lane.choice).split('_')[-1])
            except ValueError:
                pass
        d.urgency = urgency.score if urgency else None
        act, hold = resp.get('action'), resp.get('hold_sun')
        d.confidence = act.confidence if act else None
        chosen = next((c for c in valid if act and c.cid == act.choice), None)
        if chosen is None:
            chosen = best
            d.fallback = True
            d.notes.append('Missing, stale or invalid action; revalidated fallback.')
        if hold and hold.noul is not None and hold.noul >= HOLD_THRESHOLD and not emergency:
            chosen = wait
        if emergency and (chosen.kind == 'wait' or not chosen.emergency):
            chosen = emergency
            d.fallback = True
            d.notes.append('Immediate house threat overrides waiting or unrelated spending.')
    plan = saving_plan(board, book)
    # ★★ 零防线路禁等（2026-09-26 泳池局实战教训）：L6 僵尸 223px、零墙零射手、
    #    小推车状态未知，模型却选了 wait（conf 0.6），8 秒后僵尸进门。"危急"
    #    定义只覆盖贴脸（<160px），中间存在无人防守的空档。补一道确定性兜底：
    #    "有僵尸、零防御、推车不可用/未知、已过 400px 中线"的路**不允许等待**
    #    —— 从已验证候选里挑覆盖该路的最高分防御动作。
    #    ⚠️ 女王储蓄期内同样生效（配合生成侧 ≤150 的便宜绕过）：保命优先，
    #       女王只推迟十几秒；但危急路仍走上面的 emergency 覆盖。
    if chosen.kind == 'wait' and not emergency:
        facts = [lane_facts(board, r, book) for r in range(board.rows)]
        soft_rows = {
            f['lane'] - 1 for f in facts
            if f['threat_level'] == 'high' and not f['blocking_walls']
            and f['shooter_support'] == 0
            and f['mower_available'] is not True
            and f['nearest_zombie_x'] is not None and f['nearest_zombie_x'] < 400
        }
        if soft_rows:
            soft_c = [c for c in plants if c.kind == 'plant' and c.covers
                      and set(c.covers) & soft_rows]
            if soft_c:
                soft_c.sort(key=lambda c: (-c.score, c.row, c.col))
                chosen = soft_c[0]
                d.fallback = True
                d.notes.append(
                    'Lane(s) ' + str(sorted(r + 1 for r in soft_rows)) +
                    ' have zombies but zero defenders and no ready mower; waiting is not allowed.')

    # ★ 草坪上有 stranded 掉落卡（被中断的回收）且模型在等待 → 先拾回：
    #   那是已经付过钱的墙（夜战实测 7300 血躺在草坪上），捡回来纯赚。
    #   只覆盖"等待"；模型的主动动作和危急救场都保持优先。
    pick_c = next((c for c in plants if c.kind == 'pick'), None)
    if pick_c is not None and chosen.kind == 'wait':
        chosen = pick_c
        d.fallback = True
        d.notes.append('A stranded seed card lies on the lawn; recover it before anything else.')

    if chosen.kind == 'wait' and plan and plan['missing_sun'] == 0:
        upgrade = next((c for c in plants if c.type_id == plan['type_id'] or c.supports_type == plan['type_id']), None)
        if upgrade:
            chosen = upgrade
            d.fallback = True
            d.notes.append('Saving target is affordable and ready; complete the upgrade instead of waiting indefinitely.')
    # Bounded development: fill missing economy/firepower/walls, never arbitrary spending.
    # Reuse freshly validated candidates; reserve applies even when the model says wait.
    if chosen.kind == 'wait' and not plan and plants:
        facts = [lane_facts(board, r, book) for r in range(board.rows)]
        if all(f['threat_level'] not in ('high', 'critical') for f in facts):
            producers = economy_summary(board,book)['producer_count']
            # 用户 2026-09-26：阳光充裕的平静期把前四列种满、不许发呆 ——
            # 阵型成型后模型再选等待，代码也从候选里挑一个保持储备的建设项。
            # "满"的判据：前四列还有空格才继续填；真满员时允许等待。
            calm_fill = ((not board.zombies and (board.sun or 0) >= 600 or (board.sun or 0) >= 1000)
                         and any((r, c) not in board.top_occupancy(book)
                                 and not board.snow_blocked(r, c)
                                 for r in range(board.rows) for c in range(4)))
            for c in plants:
                effective_tags = book.tags(c.supports_type) if c.supports_type is not None else c.tags
                if c.kind != 'plant' or T_TEMPORARY in effective_tags or T_INSTANT in effective_tags:
                    continue
                cost = c.total_cost(book)
                is_producer = stable_sun_producer(book,c.supports_type if c.supports_type is not None else c.type_id)
                reserve = 50 if is_producer and producers < 4 else (100 if is_producer and producers < max(6,board.rows*2) else ECON_RESERVE)
                if cost is None or (board.sun or 0) - cost < reserve:
                    continue
                producer_cap = max(6,board.rows*2) + (board.rows*2 if calm_fill else 0)
                economy = is_producer and producers < producer_cap
                missing_fire = T_SHOOTER in effective_tags and not any(
                    book.has_tag(p.type_id,T_SHOOTER) for p in board.plants_in_lane(c.row))
                missing_wall = T_WALL in effective_tags and not facts[c.row]['blocking_walls']
                if economy or missing_fire or missing_wall or c.supports_type is not None \
                        or calm_fill:
                    chosen = c
                    d.fallback = True
                    d.notes.append('Safe development fills a missing formation role while keeping a sun reserve.'
                                   if not calm_fill else
                                   'Rich and calm: keep filling the front four columns instead of idling.')
                    break
    if chosen.kind == 'wait' and not plan:
        tidy = next((c for c in plants if c.kind=='shovel' and c.col<4 and c.relocate_to),None)
        if tidy and not any(lane_facts(board,r,book)['threat_level'] in ('high','critical') for r in range(board.rows)):
            chosen=tidy
            d.notes.append('Move an idle rear reusable wall forward using verified recovery.')
    recovery = next((c for c in plants if c.crush_recovery),None)
    if recovery is not None and not chosen.emergency and chosen is not recovery:
        chosen=recovery
        d.fallback=True
        d.notes.append('Confirmed crusher approaching reusable wall: recover before instant loss; house rescue retains priority.')
    d.candidate, d.action_id = chosen, chosen.cid
    d.hold = chosen.kind == 'wait'
    return d

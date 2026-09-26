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

from .board import BoardState
from .plants import (
    PlantBook, PLATFORM, SUPPORT,
    T_PLATFORM, T_CHARM, T_TRACKING, T_SPLASH3, T_GLOBAL_FREEZE,
    T_SUN_ON_KILL, T_INSTANT, T_PRODUCER, T_SHOOTER, T_WALL, T_WALL_REGEN,
    T_REFLECT, T_TEMPORARY, T_BURN_AURA, T_FREEZE_ON_DEATH, T_TORCH,
)
from .serialize import COL_LABEL, lane_threat
from .tactics import cell_x, lane_facts, rear_cols, saving_plan, strength, upgrade_value, stall_window, relocation_target

MAX_CANDIDATES = 24


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
ECON_TARGET = 4           # 期望先建起来的产阳光株数
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
    hp: int | None = None        # 铲子候选：目标植物当前血量（给 Jev 看的依据）

    def describe(self, book: PlantBook) -> str:
        if self.kind == "wait":
            return (
                "Wait / do nothing this cycle. Save the sun. Choose this when no "
                "placement would meaningfully improve the defence right now. " + self.why
            )
        if self.kind == "shovel":
            col = COL_LABEL[self.col] if 0 <= self.col < len(COL_LABEL) else str(self.col)
            hp_txt = f"about {self.hp} hp left" if self.hp is not None else "hp unknown"
            return (
                f"Shovel up \"{book.en(self.type_id)}\" at lane {self.row + 1}, column {col} "
                f"({hp_txt}) and take it back as a seed card. Reason: {self.why}"
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
    producers = sum(book.has_tag(p.type_id, T_PRODUCER) for p in board.plants)
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

    def add(slot, row, col, value, reason, covers=()):
        # 积雪格（撞车僵尸压过）在融化前不能种植 —— 种了也种不上，主动避开
        # （用户 2026-09-26 补充；这也解释了部分"空格却种不上去"）。
        if snow_blocked(board, row, col):
            return
        tags = book.tags(slot.type_id)
        rescue = any(facts[r]["threat_level"] == "critical" for r in covers)
        if T_SHOOTER in tags and T_TEMPORARY not in tags:
            utility = upgrade_value(board,book,slot.type_id,row)
            value += utility
            reason += f' Capability fit={utility:g} (heuristic; damage/control/economy, not price).'
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
            if board.is_water(row) and (row,col) not in board.occupancy():
                pad = next((s for s in board.slots if s.ready
                            and book.has_tag(s.type_id,T_PLATFORM)
                            and book.cost(s.type_id) is not None
                            and sun >= book.cost(s.type_id) + (book.cost(slot.type_id) or 0)), None)
                if pad is not None:
                    # 荷叶是"施工前置"，不是救场牌：光秃秃的荷叶既不攻击也不阻挡
                    # （实战 2026-09-26：危急水路种了裸荷叶，眼睁睁看僵尸进门）。
                    # 永远不进 emergency 池，分值封顶 45——危急水路的救场应该由
                    # 炸弹/垫背承担，荷叶只是给下一轮的防御铺地基。
                    candidates.append(Candidate('', 'plant', row, col, pad.index, pad.type_id,
                        value if rescue and T_WALL in tags else min(value * 0.2 if calm else value, 45),
                        f'First place Lily Pad to support {book.en(slot.type_id)} at this water cell; '
                        'Execute as a continuous pad-then-plant transaction; only the completed wall blocks. '
                        'Re-read and confirm both placements without another model call.',
                        book.tags(pad.type_id), rescue and T_WALL in tags, tuple(covers), supports_type=slot.type_id))
            return
        candidates.append(Candidate('', 'plant', row, col, slot.index, slot.type_id,
                                    value * 0.2 if calm else value,
                                    reason, tags, rescue, tuple(covers)))

    for slot in board.slots:
        tid = slot.type_id
        tags, cost = book.tags(tid), book.cost(tid)
        if tid < 0 or not slot.ready or cost is None or sun < cost:
            continue
        if T_PLATFORM in tags or book.role(tid) == PLATFORM:
            continue
        if book.role(tid) == SUPPORT and T_CHARM not in tags:
            continue
        # A reservation also constrains repeated cheap purchases. Emergency cancels it.
        # 女王储蓄期内只放行**便宜（≤150）救场卡**（见上方 rescue_now 的说明）。
        if (plan and tid != plan['type_id'] and sun - cost < plan['cost']
                and not (cost <= 150 and rescue_now
                         and (rescue_bypass_tags & set(tags)))):
            continue
        close_intercept = T_WALL in tags and any(
            f['nearest_zombie_x'] is not None and any(
                0 <= f['nearest_zombie_x'] - cell_x(p.col) <= 160
                and not book.has_tag(p.type_id,T_WALL)
                for p in board.plants_in_lane(f['lane']-1)) for f in facts)
        if (producers < ECON_TARGET
                and not (emergency or close_intercept
                         or (zero_defence_pressure and cost <= 150)
                         or (eager and T_INSTANT in tags))
                and T_PRODUCER not in tags
                and cost >= ECON_CHEAP and sun - cost < ECON_RESERVE):
            # ⚠️ 注意是 >=：冰冻坚果/回收高坚果恰好 150/125，曾用 > 把它们
            # 全放行了 —— 开局阳光全被墙吃掉、向日葵种不下去（用户实测反馈）。
            # 例外：eager（大波/坦克/撞车）时灰烬绕过储备闸 —— 提前交牌优先。
            continue

        # ---- 开局储蓄到账：女王（plan 目标）买得起就直接出高分候选 ----
        # 正常路径给不出她（平静期压分/无僵尸不出射手候选），必须显式生成，
        # 否则 merge 的"兑现升级"找不到候选、开局永远在等待。
        # 落点 B/C/D 三列都允许（2026-09-26 扩展）：B/C 被垫背/救场占住时，
        # 卡在 (1,2) 硬筛会让到账的女王永远出不了候选、储蓄死循环。
        if (plan and plan.get('slot') == slot.index
                and (board.sun or 0) >= (plan.get('cost') or 10**9)):
            # 用户 2026-09-26：女王不该种在同一行 —— 她是火炬柱，多行各立一支
            # 才能让更多路的豌豆吃到过火加成。无火炬的行排最前。
            for r in sorted(range(board.rows),key=lambda r:(
                    any(book.has_tag(p.type_id,T_TORCH) for p in board.plants_in_lane(r)),
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
                add(slot,r,cell,120 + facts[r]['priority']*0.4,
                    'Sacrificial speed bump: no wall card is ready on this critical lane; '
                    'buy time for defence. '
                    + (f'Estimated firing window: {estimate}; assumes 8 px/s walking and '
                       '100 hp/s biting per nearby enemy, not live measurements.' if estimate else
                       'Last-resort house rescue; no verified kill or cooldown timing.'), [r])
                added_stall = True
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
                    add(slot,r,col,100 + sum(min(strength(z), 1800/270)*14 for z in hit) + sun_bonus
                        + (40 if eager and mid_push else 0) + (1000 if urgent_hit else 0),
                        f'Local burst reaches {len(hit)} zombie(s); damage may not kill heavy armor.'
                        + sun_note + eager_note,
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
                preferred = [c for c in cols if c > front]
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
            # 产阳光上限：常规 2株/行；阳光充裕的平静期（calm_fill）放宽到
            # 4株/行 —— 用户要求把前四列种满、不许发呆（多种向日葵不亏）。
            producer_cap = max(6, board.rows*2) + (board.rows*2 if calm_fill else 0)
            if producers >= producer_cap:
                continue
            for r in sorted(quiet, key=lambda r: (min(rear_cols(board,book,r,producer=True), default=99), facts[r]["priority"], r)):
                cols = rear_cols(board,book,r,producer=True)
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
            # 平静期（没僵尸）：照常补火力，但只补还没有射手的路，保持阵型整齐。
            for r in (quiet if tracking else
                      (list(range(board.rows)) if calm else hot)):
                if not tracking and not facts[r]['zombie_count'] and not (calm or develop):
                    continue
                # 用户 2026-09-26：平静且阳光充裕时允许同路第二个射手（填满四列、
                # 不发呆）；否则每路一个射手就够了。
                if (not (plan and tid == plan['type_id']) and not calm_fill
                        and (calm or (develop and not facts[r]['zombie_count']))
                        and any(book.has_tag(p.type_id, T_SHOOTER)
                                for p in board.plants_in_lane(r))):
                    continue
                cols = rear_cols(board,book,r)
                if not cols:
                    continue
                if T_TORCH in tags and plan and tid == plan['type_id'] and not total:
                    cols = [c for c in cols if c in (1,2)]
                    if not cols:
                        continue
                nx_r = facts[r]['nearest_zombie_x']
                if rng is not None:
                    # 短射程植物（喷菇系≈4格等）：种下了就必须够得着最近僵尸，
                    # 并尽量靠前贴住射程边缘 —— 放最后一排是纯浪费
                    # （用户实测反馈：激光大喷菇被放到后排打不着人）。
                    reach = [c for c in cols
                             if nx_r is not None and nx_r <= cell_x(c) + rng*80]
                    if not reach:
                        continue
                    col = max(reach)
                else:
                    torch = max((p.col for p in board.plants_in_lane(r)
                                 if book.has_tag(p.type_id, T_TORCH)), default=None)
                    if book.combat(tid).get('torch_compatible') and torch is not None:
                        # 用户技巧：豌豆穿过向日葵女王（火炬柱）获火焰增益，
                        # 落点优先选女王身后（更靠房子一侧）最近的空位。
                        behind = [c for c in cols if c < torch]
                        col = max(behind) if behind else min(cols,key=lambda c: abs(c-2))
                    else:
                        # Prefer middle rear cells, preserving A/B for the economy.
                        col = min(cols,key=lambda c: abs(c-(1 if book.combat(tid).get('torch_compatible') else 2)))
                cover = list(range(board.rows)) if tracking else [r]
                value = 35 + max(facts[x]['priority'] for x in cover)*0.5
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
        hp = p.hp
        # 正在被啃时窗口放宽到 3600（2026-09-26）：100/s 的啃食速度下 2400 只
        # 意味着几秒后必然跌破回收线，早铲早回收 = 少白给 800+ 血。
        # 没被啃的墙维持 2400 —— 满血的墙还在值勤，铲它是送冷却。
        max_hp = RECLAIM_MAX_HP_BITTEN if p.recently_eaten else RECLAIM_MAX_HP
        f = facts[p.row]
        # Tidy a rear emergency wall only after contact has ended.
        reposition = p.col < 4 and not p.recently_eaten and (
            f['nearest_zombie_x'] is None or f['nearest_zombie_x'] > cell_x(p.col)+160)
        destination = relocation_target(board,book,p)
        if destination is None:
            continue
        if hp is None or hp <= RECLAIM_MIN_HP or hp > max_hp and not reposition:
            # 血量未知/扣完 800 就没了/还很满 —— 三种情况都不值得铲。
            # 血量未知绝不猜：铲子候选宁可缺席，也不能拿垃圾血量赌。
            continue
        f = facts[p.row]
        if not f["zombie_count"] and not reposition:
            continue
        contact = (f["nearest_zombie_x"] is not None
                   and f["nearest_zombie_x"] <= cell_x(p.col) + 30)
        if not reposition and not contact and f["threat_level"] not in ("high", "critical"):
            # 没僵尸在啃它、路线也不告急 → 慢慢等它挡，铲它是送 800 血。
            continue
        urgent = f["threat_level"] == "critical"
        # 回收窗口即将关闭也算紧急：僵尸啃食 100/s，一个决策周期约 3 秒。
        # 若这面墙正在被啃、且血量只比回收线高不到 ~3 秒的量，等下一轮就来不及了
        # —— 和"近屋救场"同一逻辑：确定性判据，不指望模型自己算时间。
        if p.recently_eaten and (hp - RECLAIM_MIN_HP) <= 300:
            urgent = True
        candidates.append(Candidate(
            '', 'shovel', p.row, p.col, -1, p.type_id,
            (150 if urgent else 60) + f["priority"] * 0.5,
            (f'Reclaim it before zombies finish it: shoveling costs 800 hp, the returned '
             f'card keeps the remaining {hp - 800}; once its hp drops to 800 or below it '
             f'can never be reclaimed again and is simply lost. '
             + ('It is being bitten right now and the reclaim window closes within seconds. '
                if p.recently_eaten else '')
             + f'Recover the dropped card and replant at lane {destination[0]+1}, column {destination[1]+1}; prepare water support first.'),
            book.tags(p.type_id), urgent, (destination[0],), hp, relocate_to=destination,
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
        profile = book.combat(p.type_id)
        anti_crush = bool(profile.get('crush_hits') or profile.get('lethal_hit_burst'))
        f = facts[p.row]
        biters = [z for z in board.zombies_in_lane(p.row)
                  if z.x is not None and z.x <= cell_x(p.col) + 48]
        bite_dps = max([book.zombie_trait(z.type_id, 'eat_dps') or 100 for z in biters] or [100])
        # a) 快被啃死：血量 <= 一个决策周期(约3s)的啃食量，且确实有嘴在啃
        if (p.recently_eaten and p.hp is not None and biters
                and p.hp <= bite_dps * 3):
            candidates.append(Candidate(
                '', 'shovel', p.row, p.col, -1, p.type_id,
                120 + f["priority"] * 0.5,
                (f'Salvage shovel: it is being eaten and only about {p.hp} hp is left '
                 f'(biters chew ~{bite_dps:.0f}/s here) - shoveling refunds sun instead '
                 'of letting zombies destroy it for nothing. Do this immediately.'),
                book.tags(p.type_id), f["threat_level"] == "critical", (p.row,),
                salvage=True, hp=p.hp,
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
                    salvage=True, hp=p.hp,
                ))

    # -- 进阶 5-2（用户 2026-09-26）：铲旧换新，让女王身后物尽其用 ---------
    # 女王是火炬柱：豌豆/狂野机枪/玉米卷从她身后穿火有伤害加成。如果中期
    # 运营结果是她身后那个黄金槽位被向日葵/豌豆这类廉价植物占着，而阳光充裕、
    # 手里有就绪的过火射手 —— 铲掉它换阳光，下一轮自然有强射手候选来补位。
    # 门槛收得很紧：无危急/无高压、该路无僵尸、目标廉价（≤150）非墙非火炬、
    # 手里有过火射手且买得起+留 300 储备。规划靠"铲完自然补位"，不强行连招。
    if all(f['threat_level'] in ('low', 'none') for f in facts):
        torches = {p.row: p for p in board.plants
                   if book.has_tag(p.type_id, T_TORCH)}
        upgrades = [s for s in board.slots if s.ready
                    and book.has_tag(s.type_id, T_SHOOTER)
                    and book.combat(s.type_id).get('torch_compatible')
                    and (book.cost(s.type_id) or 10**9) >= 300]
        for r, q in torches.items():
            if q.col <= 0:
                continue
            behind = next((p for p in board.plants_in_lane(r)
                           if p.col == q.col - 1), None)
            if behind is None or behind.asleep or behind.recently_eaten:
                continue
            btags, bcost = book.tags(behind.type_id), book.cost(behind.type_id) or 0
            if bcost > 150 or T_WALL in btags or T_TORCH in btags:
                continue
            upgrade = next((s for s in upgrades
                            if (book.cost(s.type_id) or 10**9) + 300 <= (board.sun or 0)), None)
            if upgrade is None or snow_blocked(board, r, behind.col):
                continue
            candidates.append(Candidate(
                '', 'shovel', r, behind.col, -1, behind.type_id,
                70 + (book.cost(upgrade.type_id) or 0) * 0.02,
                (f'Torch-column upgrade: the cell right behind the Sunflower Queen '
                 f'(lane {r+1}) is wasted on a cheap {book.en(behind.type_id)}. Shovel it '
                 f'(refunds sun) and a torch-boosted {book.en(upgrade.type_id)} is ready '
                 f'in hand to take that slot next cycle.'),
                book.tags(behind.type_id), False, (r,),
                salvage=True, hp=behind.hp,
            ))

    # Keep rescue choices first, then rank useful alternatives. Per-type cap prevents flooding.
    candidates.sort(key=lambda c: (not c.emergency,-c.score,c.row,c.col))
    selected, seen, counts = [], set(), {}
    # First pass: one best placement per available plant, so duplicate cheap
    # placements cannot hide a distinct expensive capability from Jev.
    first, alternatives, represented = [], [], set()
    for c in candidates:
        if c.type_id not in represented:
            first.append(c)
            represented.add(c.type_id)
        else:
            alternatives.append(c)
    for c in first + alternatives:
        key = (c.slot,c.row,c.col)
        if key in seen or counts.get(c.type_id,0) >= MAX_PER_TYPE:
            continue
        selected.append(c)
        seen.add(key)
        counts[c.type_id] = counts.get(c.type_id,0)+1
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
                "A shovel option removes an existing plant and returns it as a card; choose it only "
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
    return c.kind, c.slot, c.type_id, c.row, c.col


def board_active(board) -> bool:
    """游戏真的在跑吗？—— 决策层唯一的"活体"判据。

    ⚠️ 不能用 `Board+0x164`(paused)：agent.py 实测**杂交版时钟正常推进时它也
    读 1，完全不可靠**（那是"莫名全轮等待"的根：所有候选被判 stale、收阳光
    停摆）。可靠信号是 game_clock 是否在推进，由 agent 每轮写进
    `board.clock_advancing`；读不到（None，比如合成战场/第一轮）按"在跑"处理，
    执行层另有时钟闸兜底。
    """
    return board.clock_advancing is not False


def escalate_emergency(candidate, board, book, bad_cells=None):
    """执行前用**最新快照**重算威胁；出现危急路而原动作不是救场时，改交救场候选。

    为什么需要：决策 -> Jev 返回 -> 动手之间隔着 1~6s，僵尸每周期走 40~80px。
    merge_decision 的"fresh 重算"用的是决策前那张旧快照，只有这里拿的是
    执行前刚读的 board —— 决策时 low 的路此刻可能已经 critical。
    返回 None 表示无需升级（没有危急路，或原动作本来就是救场）。
    """
    if board is None or not board.ok or not board_active(board):
        return None
    facts = [lane_facts(board, r, book) for r in range(board.rows)]
    if not any(f["threat_level"] == "critical" for f in facts):
        return None
    if candidate is not None and candidate.emergency:
        return None
    fresh = generate_candidates(board, book)
    esc = [c for c in fresh if c.emergency and c.kind in ("plant", "shovel")
           and not (bad_cells and c.kind == "plant"
                    and (c.type_id, c.row, c.col) in bad_cells)]
    if not esc:
        return None
    esc.sort(key=lambda c: (-c.score, c.row, c.col))
    return esc[0]


def action_is_current(candidate, board, book):
    """Re-evaluate legality AND tactical usefulness against a fresh snapshot."""
    return (board.ok and board_active(board) and any(
        candidate_key(c) == candidate_key(candidate)
        for c in generate_candidates(board, book)))


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
    plants = sorted((c for c in valid if c.kind in ('plant', 'shovel')),
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
            producers = sum(book.has_tag(p.type_id, T_PRODUCER) for p in board.plants)
            # 用户 2026-09-26：阳光充裕的平静期把前四列种满、不许发呆 ——
            # 阵型成型后模型再选等待，代码也从候选里挑一个保持储备的建设项。
            # "满"的判据：前四列还有空格才继续填；真满员时允许等待。
            calm_fill = (not board.zombies and (board.sun or 0) >= 600
                         and any((r, c) not in board.top_occupancy(book)
                                 and not board.snow_blocked(r, c)
                                 for r in range(board.rows) for c in range(4)))
            for c in plants:
                if c.kind != 'plant' or T_TEMPORARY in c.tags or T_INSTANT in c.tags:
                    continue
                cost = book.cost(c.type_id)
                reserve = 100 if T_PRODUCER in c.tags and producers < max(6,board.rows*2) else ECON_RESERVE
                if cost is None or (board.sun or 0) - cost < reserve:
                    continue
                producer_cap = max(6,board.rows*2) + (board.rows*2 if calm_fill else 0)
                economy = T_PRODUCER in c.tags and producers < producer_cap
                missing_fire = T_SHOOTER in c.tags and not any(
                    book.has_tag(p.type_id,T_SHOOTER) for p in board.plants_in_lane(c.row))
                missing_wall = T_WALL in c.tags and not facts[c.row]['blocking_walls']
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
    d.candidate, d.action_id = chosen, chosen.cid
    d.hold = chosen.kind == 'wait'
    return d

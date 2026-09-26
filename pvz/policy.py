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
from .tactics import cell_x, lane_facts, rear_cols, saving_plan, strength, upgrade_value

MAX_CANDIDATES = 24
MAX_PER_TYPE = 2          # 同一张卡最多出几条落点候选（见 generate_candidates 末尾）

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
    kind: str                    # "plant" | "wait"
    row: int = -1
    col: int = -1
    slot: int = -1
    type_id: int = -1
    score: float = 0.0
    why: str = ""
    tags: tuple[str, ...] = ()
    emergency: bool = False
    covers: tuple[int, ...] = ()

    def describe(self, book: PlantBook) -> str:
        if self.kind == "wait":
            return (
                "Wait / do nothing this cycle. Save the sun. Choose this when no "
                "placement would meaningfully improve the defence right now. " + self.why
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
    facts = [lane_facts(board, r, book) for r in range(board.rows)]
    hot = sorted(range(board.rows), key=lambda r: -facts[r]["priority"])
    quiet = sorted(range(board.rows), key=lambda r: facts[r]["priority"])
    occ, sun = board.occupancy(), board.sun or 0
    emergency = any(f["threat_level"] == "critical" for f in facts)
    total = sum(f["zombie_count"] for f in facts)
    producers = sum(book.has_tag(p.type_id, T_PRODUCER) for p in board.plants)
    plan = saving_plan(board, book)
    candidates = []

    def add(slot, row, col, value, reason, covers=()):
        tags = book.tags(slot.type_id)
        rescue = any(facts[r]["threat_level"] == "critical" for r in covers)
        if T_SHOOTER in tags and T_TEMPORARY not in tags:
            utility = upgrade_value(board,book,slot.type_id,row)
            value += utility
            reason += f' Capability fit={utility:g} (heuristic; damage/control/economy, not price).'
        candidates.append(Candidate('', 'plant', row, col, slot.index, slot.type_id,
                                    value, reason, tags, rescue, tuple(covers)))

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
        if plan and tid != plan['type_id'] and sun - cost < plan['cost']:
            continue
        if (producers < ECON_TARGET and not emergency and T_PRODUCER not in tags
                and cost > ECON_CHEAP and sun - cost < ECON_RESERVE):
            continue

        if T_GLOBAL_FREEZE in tags:
            if not emergency and total < 6:
                continue
            cells = [(r,c) for r in quiet for c in range(board.cols) if (r,c) not in occ]
            if cells:
                r,c = cells[0]
                add(slot,r,c,110 + sum(f['priority'] for f in facts)*0.4,
                    'Board-wide control; buys time against the current threats.', range(board.rows))
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
                    if not any(facts[x]['threat_level'] in ('high','critical') for x in cover):
                        continue
                    urgent_hit = any(z.x < 160 or (board.mowers.get(z.row) is False and z.x < 240)
                                     for z in hit)
                    add(slot,r,col,100 + sum(min(strength(z), 1800/270)*14 for z in hit)
                        + (1000 if urgent_hit else 0),
                        f'Local burst reaches {len(hit)} zombie(s); damage may not kill heavy armor.',
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
                    continue
                if facts[r]['blocking_walls'] and facts[r]['threat_level'] != 'critical':
                    if T_SHOOTER in tags and facts[r]['pressure'] > 0:
                        rear = rear_cols(board,book,r)
                        if rear:
                            add(slot,r,min(rear,key=lambda c:abs(c-2)),
                                40+facts[r]['priority']*0.5,
                                'Hybrid shooter reinforces behind the existing wall.',[r])
                    continue
                cols = [c for c in _free_cols(board,occ,r,0,5) if cell_x(c) <= nx-10]
                if not cols:
                    continue
                defenders = [p.col for p in board.plants_in_lane(r)
                             if not book.has_tag(p.type_id,T_WALL) and cell_x(p.col) < nx]
                front = max(defenders,default=-1)
                preferred = [c for c in cols if c > front]
                col = preferred[-1] if preferred else cols[-1]
                add(slot,r,col,65+facts[r]['priority']*0.8,
                    'Intercept on the house side of the zombie; shield the surviving rear plants.',[r])
            continue

        if T_PRODUCER in tags and T_SHOOTER not in tags:
            if producers >= max(6, board.rows*2):
                continue
            for r in quiet:
                cols = rear_cols(board,book,r,producer=True)
                if not cols or facts[r]['threat_level'] in ('critical','high'):
                    continue
                add(slot,r,cols[0],85 if producers < ECON_TARGET else 25,
                    f'Safe rear economy; {producers} producers currently alive.')
                break
            continue

        if T_SHOOTER in tags:
            if not total:
                continue
            tracking = T_TRACKING in tags
            for r in quiet if tracking else hot:
                if not tracking and not facts[r]['zombie_count']:
                    continue
                cols = rear_cols(board,book,r)
                if not cols:
                    continue
                # Prefer middle rear cells, preserving A/B for the economy.
                col = min(cols,key=lambda c: abs(c-2))
                cover = list(range(board.rows)) if tracking else [r]
                value = 35 + max(facts[x]['priority'] for x in cover)*0.5
                value -= facts[r]['shooter_support']*8
                if plan and tid == plan['type_id']:
                    value += 100
                add(slot,r,col,value,'Sustained fire from behind the wall; '+
                    ('covers all lanes.' if tracking else f'reinforces lane {r+1}.'),cover)
                if tracking:
                    break

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
                "when safe. Never hold if a listed rescue can address a near-house threat."
            ),
            "criteria": {"true": "yes - hold and save the sun", "false": "no - spend sun now"},
        },
    }


# ---------------------------------------------------------------- 合并
HOLD_THRESHOLD = 0.6


def candidate_key(c):
    return c.kind, c.slot, c.type_id, c.row, c.col


def action_is_current(candidate, board, book):
    """Re-evaluate legality AND tactical usefulness against a fresh snapshot."""
    return board.ok and not board.paused and any(
        candidate_key(c) == candidate_key(candidate)
        for c in generate_candidates(board, book))


def merge_decision(resp, candidates: list[Candidate], board: BoardState, book: PlantBook) -> Decision:
    fresh = {candidate_key(c): c for c in generate_candidates(board,book)} if board.ok and not board.paused else {}
    valid = []
    for c in candidates:
        current = fresh.get(candidate_key(c))
        if c.kind == 'wait' or current is not None:
            if current is not None:
                c.emergency, c.covers, c.score = current.emergency, current.covers, current.score
            valid.append(c)
    wait = next((c for c in valid if c.kind == 'wait'), Candidate('WAIT','wait'))
    plants = sorted((c for c in valid if c.kind == 'plant'),key=lambda c:(not c.emergency,-c.score))
    best = plants[0] if plants else wait
    emergency = next((c for c in plants if c.emergency),None)
    d = Decision()
    if resp is None or not resp.ok:
        chosen = best
        d.fallback = True
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
    if chosen.kind == 'wait' and plan and plan['missing_sun'] == 0:
        upgrade = next((c for c in plants if c.type_id == plan['type_id']), None)
        if upgrade:
            chosen = upgrade
            d.fallback = True
            d.notes.append('Saving target is affordable and ready; complete the upgrade instead of waiting indefinitely.')
    d.candidate, d.action_id = chosen, chosen.cid
    d.hold = chosen.kind == 'wait'
    return d

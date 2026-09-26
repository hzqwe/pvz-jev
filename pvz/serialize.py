"""把 BoardState 序列化成给 Jev 的 state，以及给人看的文字版战场。

序列化的目标不是"完整"，而是"让 Jev 能做出那一个判断"：
它需要知道每条路承受多大压力、手上有什么牌、牌能不能用、哪里是空的。
所有计算（威胁量化、可放位置枚举）都在代码里做完，Jev 只做取舍。
"""

from __future__ import annotations

from .board import BoardState
from .plants import PlantBook
from .tactics import lane_facts, saving_plan

SCENE_NAMES = {
    0: "白天草地 day_lawn",
    1: "夜晚草地 night_lawn",
    2: "泳池 pool",
    3: "浓雾泳池 fog_pool",
    4: "屋顶 roof",
    5: "月夜月地 moon_night",
}

COL_LABEL = "ABCDEFGHI"


def _closeness(x: float | None) -> str:
    """把僵尸的横向位置翻译成"离房子多近"。

    ⚠️ 单位是**游戏内部的逻辑坐标**（草坪宽 800），不是屏幕像素。
    实测僵尸 `y = 50 + 100*row`，草坪在内部坐标里正好是 x 0..800
    （屏幕上是 386..2178，缩放系数 2.24）。所以这里的 160/320/560
    是按"草坪 800 宽"定的：560 = 走过草坪 70%，160 = 到家门口。
    **不要拿屏幕像素来比这几个阈值**（那样几乎所有僵尸都会显示成"贴脸"）。
    """
    if x is None:
        return "unknown"
    if x < 160:
        return "critical(已到家门口)"
    if x < 320:
        return "close(即将接触防线)"
    if x < 560:
        return "mid(已进入草坪)"
    return "far(刚出现)"


def lane_threat(board: BoardState, row: int, book=None) -> dict:
    facts = lane_facts(board, row, book)
    facts['nearest_closeness'] = _closeness(facts['nearest_zombie_x'])
    return facts


def build_state(board: BoardState, book: PlantBook) -> dict:
    """产出给 Jev 的 state（JSON 友好、语义化）。"""
    lanes = []
    for r in range(board.rows):
        zs = board.zombies_in_lane(r)
        ps = sorted(board.plants_in_lane(r), key=lambda p: p.col)
        lanes.append(
            {
                "lane": r + 1,
                "defenders": [
                    {
                        "plant": book.name(p.type_id),
                        "role": book.role(p.type_id),
                        "column": COL_LABEL[p.col] if 0 <= p.col < len(COL_LABEL) else str(p.col),
                        "asleep": p.asleep,
                    }
                    for p in ps
                ],
                "zombies": [
                    {
                        "kind": f"zombie_type_{z.type_id} (hybrid identity unverified)",
                        "body_hp": z.hp, "armor_hp": z.armor_hp,
                        "x_px": round(z.x, 0) if z.x is not None else None,
                        "closeness": _closeness(z.x),
                    }
                    for z in sorted(zs, key=lambda z: (z.x if z.x is not None else 9999))
                ],
                "threat_level": lane_threat(board, r, book)["threat_level"],
                "tactical_assessment": lane_threat(board, r, book),
            }
        )

    seeds = []
    for s in board.slots:
        if s.type_id < 0:
            continue
        info = book.describe(s.type_id)
        seeds.append(
            {
                "slot": s.index,
                "plant": info["en"],
                "role": info["role"],
                "tags": info["tags"],
                "cost": info["cost"],
                "effect": info["effect"],
                "combat": info["combat"],
                "hp": info["hp"],
                "almanac_cooldown_s": info["cooldown_s"],
                "ready": s.ready,
                "cooldown_left_frac": round(s.cooldown_left_frac, 2),
                "registered": info["registered"],
                "bound": info["bound"],
            }
        )

    return {
        "game": {
            "scene": SCENE_NAMES.get(board.scene if board.scene is not None else 0, f"scene#{board.scene}"),
            "level": board.level,
            "sun": board.sun,
            "rows": board.rows,
            "cols": board.cols,
            "clock": board.game_clock,
        },
        "lanes": lanes,
        "seed_cards": seeds,
        "saving_plan": saving_plan(board, book),
        "assessment_note": "Pressure/support are heuristic estimates, not measured DPS or time-to-kill. Mower null means unknown.",
        "empty_cell_count": len(board.empty_cells()),
        "unregistered_plant_ids": book.unregistered_ids(
            [p.type_id for p in board.plants] + [s.type_id for s in board.slots]
        ),
        # 有名字但还没绑定 ID 的：说明卡池换过了，得重跑 tools/bind_cards.py。
        # 这个字段是给 agent 的启动自检用的 —— 绑不上就等于 Jev 在看天书。
        "unbound_plant_ids": book.unbound_ids(
            [p.type_id for p in board.plants] + [s.type_id for s in board.slots]
        ),
    }


def render_text(board: BoardState, book: PlantBook) -> str:
    """给人看的 ASCII 战场。"""
    occ = board.occupancy()
    lines = []
    sun = board.sun if board.sun is not None else "?"
    lines.append(
        f"场景={SCENE_NAMES.get(board.scene or 0, board.scene)}  关卡={board.level}  "
        f"阳光={sun}  植物={len(board.plants)}  僵尸={len(board.zombies)}"
    )
    header = "      " + "".join(f"{COL_LABEL[c]:^9}" for c in range(board.cols))
    lines.append(header)
    for r in range(board.rows):
        cells = []
        for c in range(board.cols):
            ps = occ.get((r, c))
            if not ps:
                cells.append("    .    ")
            else:
                nm = book.name(ps[0].type_id).split(" ")[0]
                cells.append(f"{nm[:7]:^9}")
        zs = board.zombies_in_lane(r)
        ztxt = f"  <僵尸x{len(zs)}"
        if zs:
            xs = [z.x for z in zs if z.x is not None]
            if xs:
                ztxt += f" 最近{min(xs):.0f}px"
        ztxt += ">"
        lines.append(f"R{r + 1}  " + "|".join(cells) + ztxt)
    cards = []
    for s in board.slots:
        if s.type_id < 0:
            continue
        flag = "就绪" if s.ready else f"冷却{s.cooldown_left_frac * 100:.0f}%"
        cards.append(f"[{s.index}]{book.name(s.type_id)}({flag})")
    lines.append("手牌: " + "  ".join(cards))
    return "\n".join(lines)

"""把 BoardState 序列化成给 Jev 的 state，以及给人看的文字版战场。

序列化的目标不是"完整"，而是"让 Jev 能做出那一个判断"：
它需要知道每条路承受多大压力、手上有什么牌、牌能不能用、哪里是空的。
所有计算（威胁量化、可放位置枚举）都在代码里做完，Jev 只做取舍。
"""

from __future__ import annotations

import json
import os

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

# -- 作战教条（2026-09-26 新增，纯信息层）--------------------------------
# data/plant_playbook.json 存放 14 株登记植物的使用教条、全局原则和敌人应对
# （来源：用户图鉴照片 + 官方 Wiki pvzhe.wiki + 社区攻略定性共识）。
# 这里只负责把它**按需**注入给 Jev：usage 只发当前这局卡槽里有的植物，
# 原则/敌人说明是固定几行。不参与任何评分，读取失败就静默缺席 ——
# 决策层的启发式一分都不改，这是"让模型更懂，而不是让代码更聪明"。
_PLAYBOOK_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "plant_playbook.json")
_PLAYBOOK: dict | None = None


def load_playbook() -> dict:
    global _PLAYBOOK
    if _PLAYBOOK is None:
        try:
            with open(_PLAYBOOK_FILE, "r", encoding="utf-8") as fh:
                _PLAYBOOK = json.load(fh)
        except (OSError, ValueError):
            _PLAYBOOK = {}
    return _PLAYBOOK


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
    book.sync_field_copies(board.plants)
    # 性能（2026-09-27 审查）：occ/lane_threat 每路被重复全量计算 2~60 次
    # —— 一次缓存，整个 build_state 复用。
    occ_cache = board.top_occupancy(book)

    def lane_threat_cached(r):
        return lane_threat(board, r, book)

    lanes = []
    for r in range(board.rows):
        zs = board.zombies_in_lane(r)
        ps = sorted(board.plants_in_lane(r), key=lambda p: p.col)
        lanes.append(
            {
                "lane": r + 1,
                "terrain": "water: Lily Pad required below ordinary plants" if board.is_water(r) else "land",
                "available_platform_columns": [COL_LABEL[c] for c in range(board.cols) if board.has_platform(r,c,book) and (r,c) not in occ_cache],
                "defenders": [
                    {
                        "plant": book.name(p.type_id),
                        "role": book.role(p.type_id),
                        "column": COL_LABEL[p.col] if 0 <= p.col < len(COL_LABEL) else str(p.col),
                        "asleep": p.asleep,
                        "hp": p.hp,
                        "recently_eaten": p.recently_eaten,
                    }
                    for p in ps
                ],
                "zombies": [
                    {
                        "kind": f"zombie_type_{z.type_id} (hybrid identity unverified)",
                        "body_hp": z.hp, "armor_hp": z.armor_hp,
                        "x_px": round(z.x, 0) if z.x is not None else None,
                        "closeness": _closeness(z.x),
                        # 特征只在有信息时出现：crush 来自 zombie_traits.json，
                        # stationary 是 agent 跨快照的驻停观测（远程僵尸嫌疑）。
                        **({'traits': tr} if (tr := _zombie_traits(book, z)) else {}),
                    }
                    for z in sorted(zs, key=lambda z: (z.x if z.x is not None else 9999))
                ],
                "tactical_assessment": lane_threat_cached(r),
            }
        )

    seeds = []
    playbook = load_playbook()
    usage_all = playbook.get("usage_en") or {}
    for s in board.slots:
        if s.type_id < 0:
            continue
        info = book.describe(s.type_id)
        # usage 只发本局卡槽里有的植物：教条跟着牌走，不白占 token。
        usage = usage_all.get(info["name"])
        seeds.append(
            {
                "slot": s.index,
                "plant": info["en"],
                "role": info["role"],
                "tags": info["tags"],
                "cost": info["cost"],
                "price_increment_per_field_copy": book.price_increment(s.type_id),
                "field_copies": book.copies_on_field.get(s.type_id,0),
                "effect": info["effect"],
                "combat": info["combat"],
                "usage": usage or "",
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
        # 出怪表提示（2026-09-26 新增，**unverified**）：读的是 pvztoolkit 同款
        # spawn_list 指针 + 僵尸池分配游标估算，杂交版语义未实战核验。
        # 只给 Jev 当"后面还有多少怪"的参考，代码侧不据此做任何确定性决策。
        "wave_info_unverified": _wave_info(board),
        "lanes": lanes,
        "seed_cards": seeds,
        "doctrine": {
            "principles": playbook.get("principles_en") or [],
            "enemy_notes": playbook.get("enemy_notes_en") or [],
        },
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


def _zombie_traits(book: PlantBook, z) -> dict:
    """给 Jev 的僵尸特征标记；没有任何信息时返回空 dict（不编造）。"""
    tr: dict = {}
    if book.zombie_flag(z.type_id, 'crush'):
        tr['crush'] = True
        tr['note'] = 'Crushes (squashes) plants outright; only crush-resistant walls hold it.'
    if getattr(z, 'stationary', None):
        tr['stationary_maybe_ranged'] = True
        tr.setdefault('note', 'Standing still while the clock advances: likely a ranged '
                              'zombie (e.g. zombie peashooter) shooting uncovered plants.')
    return tr


def _wave_info(board: BoardState) -> dict | None:
    """把出怪表读数翻成给 Jev 的提示；读不到就是 None（绝不编造）。"""
    if board.spawn_total is None and board.spawnable_types is None:
        return None
    info: dict = {
        "note": ("Approximate spawn-table hint read from memory, UNVERIFIED in the "
                 "hybrid build - treat as background context only."),
    }
    if board.spawn_total is not None:
        info["level_total_spawns"] = board.spawn_total
    if board.spawn_spawned is not None:
        info["spawned_estimate"] = board.spawn_spawned
    if board.spawn_upcoming is not None:
        info["upcoming_estimate"] = board.spawn_upcoming
    if board.spawn_upcoming_kinds:
        info["upcoming_kinds"] = {
            f"zombie_type_{t} (identity unverified)": n
            for t, n in sorted(board.spawn_upcoming_kinds.items(), key=lambda kv: -kv[1])
        }
    if board.spawnable_types is not None:
        info["spawnable_type_ids"] = board.spawnable_types
    return info


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

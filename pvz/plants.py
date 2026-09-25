"""植物类型 ID -> 名称 / 角色 / 阳光成本 / **功能**。

前 49 项是 PvZ 原版 1.0.0.1051 的固定编号（社区公认，pvztoolkit 与 PVZ_Helper
都依赖同一套编号）。杂交版在这个编号基础上向后扩展了大量新植物。

杂交版那部分**不写死在代码里**，而是走数据文件：

  data/hybrid_plants.json   按**名字**索引的功能知识库（成本/角色/标签/效果说明）
  data/lineups.json         一局开始时种子栏从左到右的卡面顺序
  data/plant_ids.json       名字 -> type_id 的绑定结果（由 tools/bind_cards.py 写入）
  data/plant_costs.json     运行时实测出来的真实成本

为什么要分成「名字」和「ID」两层：**名字是稳定事实，ID 要靠实测**。杂交版每局
阵容都不同，而且同一个植物在不同版本/不同卡池里 ID 未必一致；硬编码一张
`id -> 名字` 表一定会错。所以流程是：

  1. 人看图鉴/截图，把植物的**功能**登记进 hybrid_plants.json（key 是中文名）；
  2. 进关卡后读一次种子栏，按 lineups.json 的顺序把 slot 的 type_id 绑到名字上；
  3. 用**冷却时间**交叉验证绑定（图鉴给了 cooldown_s，内存里的 cd_total 是
     总冷却 tick，两者应满足 cd_total ≈ cooldown_s * 100）—— 零成本、零风险；
  4. 结果落盘到 plant_ids.json，以后每次启动直接复用。

绑定这一步是必要的，不是锦上添花：**没有它，Jev 看到的就是 `#161` 这种裸 ID，
它没法判断"这株植物该不该种"**。角色的价值也在于此 —— 候选动作生成要靠它。

角色（role）用于粗分类，标签（tag）用于细粒度策略，见 policy.py。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 角色常量
PRODUCER = "producer"      # 产阳光
SHOOTER = "shooter"        # 射手
LOBBER = "lobber"          # 抛射
WALL = "wall"              # 肉盾 / 阻挡
INSTANT = "instant"        # 一次性爆发
GROUND = "ground"          # 地面持续伤害
PLATFORM = "platform"      # 荷叶 / 花盆
SUPPORT = "support"        # 辅助
SPECIAL = "special"        # 模仿者等
UNKNOWN = "unknown"

# ---------------------------------------------------------------- 标签常量
# 这些标签是 policy.py 生成候选动作的依据。命名规则：能表达"这条植物
# 在策略上意味着什么"才加，纯数值差异（威力高低）不加。
T_INSTANT = "instant"
T_PRODUCER = "producer"
T_SHOOTER = "shooter"
T_WALL = "wall"
T_TRACKING = "tracking"            # 全屏追踪：可以打**任意行**，不必种在告急行
T_SPREAD = "spread"                # 散射弹道：单行但容错高
T_SLOW = "slow"                    # 命中减速
T_SPLASH3 = "splash3"              # 一次性，覆盖自身行 + 相邻两行（共 3 行）
T_GLOBAL_FREEZE = "global_freeze"  # 一次性，全屏冻结（救场按钮）
T_GLOBAL_DAMAGE = "global_damage"  # 一次性，全屏伤害
T_SUN_ON_KILL = "sun_on_kill"      # 击杀/冻结会产阳光（经济型爆发）
T_AOE_CIRCLE = "aoe_circle"        # 圆形小范围爆发
T_REFLECT = "reflect"              # 被啃咬时反伤
T_STUN = "stun"                    # 概率僵直
T_CHARM = "charm"                  # 策反僵尸
T_TEMPORARY = "temporary"          # 在场若干秒后自己消失
T_WALL_REGEN = "wall_regen"        # 铲除后变回卡片（可回收）
T_ANTI_JUMP = "anti_jump"          # 抵挡跳跃僵尸
T_FREEZE_ON_DEATH = "freeze_on_death"  # 亡语释放寒冰菇效果
T_DEATH_BOOM = "death_boom"        # 免死 + 爆炸
T_BURN_AURA = "burn_aura"          # 灼烧周围 3x3
T_GROWS = "grows"                  # 会成长（后期产能翻倍）
T_PLATFORM = "platform"

# id: (名称, 成本, 角色) —— 原版固定编号
BASE_PLANTS: dict[int, tuple[str, int, str]] = {
    0: ("豌豆射手 Peashooter", 100, SHOOTER),
    1: ("向日葵 Sunflower", 50, PRODUCER),
    2: ("樱桃炸弹 Cherry Bomb", 150, INSTANT),
    3: ("坚果墙 Wall-nut", 50, WALL),
    4: ("土豆雷 Potato Mine", 25, INSTANT),
    5: ("寒冰射手 Snow Pea", 175, SHOOTER),
    6: ("大嘴花 Chomper", 150, INSTANT),
    7: ("双发射手 Repeater", 200, SHOOTER),
    8: ("小喷菇 Puff-shroom", 0, SHOOTER),
    9: ("阳光菇 Sun-shroom", 25, PRODUCER),
    10: ("大喷菇 Fume-shroom", 75, SHOOTER),
    11: ("墓碑吞噬者 Grave Buster", 75, SUPPORT),
    12: ("魅惑菇 Hypno-shroom", 75, SUPPORT),
    13: ("胆小菇 Scaredy-shroom", 25, SHOOTER),
    14: ("寒冰菇 Ice-shroom", 75, INSTANT),
    15: ("毁灭菇 Doom-shroom", 125, INSTANT),
    16: ("睡莲 Lily Pad", 25, PLATFORM),
    17: ("窝瓜 Squash", 50, INSTANT),
    18: ("三线射手 Threepeater", 325, SHOOTER),
    19: ("缠绕水草 Tangle Kelp", 25, INSTANT),
    20: ("火爆辣椒 Jalapeno", 125, INSTANT),
    21: ("地刺 Spikeweed", 100, GROUND),
    22: ("火炬树桩 Torchwood", 175, SUPPORT),
    23: ("高坚果 Tall-nut", 125, WALL),
    24: ("海蘑菇 Sea-shroom", 0, SHOOTER),
    25: ("路灯花 Plantern", 25, SUPPORT),
    26: ("仙人掌 Cactus", 125, SHOOTER),
    27: ("三叶草 Blover", 100, INSTANT),
    28: ("裂荚射手 Split Pea", 125, SHOOTER),
    29: ("杨桃 Starfruit", 125, SHOOTER),
    30: ("南瓜头 Pumpkin", 125, WALL),
    31: ("磁力菇 Magnet-shroom", 100, SUPPORT),
    32: ("卷心菜投手 Cabbage-pult", 100, LOBBER),
    33: ("花盆 Flower Pot", 25, PLATFORM),
    34: ("玉米投手 Kernel-pult", 100, LOBBER),
    35: ("咖啡豆 Coffee Bean", 75, SUPPORT),
    36: ("大蒜 Garlic", 50, WALL),
    37: ("叶子保护伞 Umbrella Leaf", 100, SUPPORT),
    38: ("金盏花 Marigold", 50, PRODUCER),
    39: ("西瓜投手 Melon-pult", 300, LOBBER),
    40: ("机枪射手 Gatling Pea", 250, SHOOTER),
    41: ("双子向日葵 Twin Sunflower", 150, PRODUCER),
    42: ("忧郁菇 Gloom-shroom", 150, SHOOTER),
    43: ("香蒲 Cattail", 225, SHOOTER),
    44: ("冰瓜 Winter Melon", 200, LOBBER),
    45: ("吸金磁 Gold Magnet", 50, SUPPORT),
    46: ("地刺王 Spikerock", 125, GROUND),
    47: ("玉米加农炮 Cob Cannon", 500, LOBBER),
    48: ("模仿者 Imitater", 0, SPECIAL),
}

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
HYBRID_FILE = os.path.join(_DIR, "plant_names.json")
COST_FILE = os.path.join(_DIR, "plant_costs.json")
KB_FILE = os.path.join(_DIR, "hybrid_plants.json")
IDS_FILE = os.path.join(_DIR, "plant_ids.json")
LINEUPS_FILE = os.path.join(_DIR, "lineups.json")


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path: str, data) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


@dataclass
class KBEntry:
    """一个植物在知识库里的全部信息。"""
    cn: str
    en: str
    cost: int | None = None
    role: str = UNKNOWN
    tags: tuple[str, ...] = ()
    hp: int | None = None
    cooldown_s: float | None = None
    damage: str = ""
    effect_en: str = ""
    effects: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, cn: str, d: dict) -> "KBEntry":
        return cls(
            cn=cn,
            en=str(d.get("en") or cn),
            cost=int(d["cost"]) if d.get("cost") is not None else None,
            role=str(d.get("role") or UNKNOWN),
            tags=tuple(d.get("tags") or ()),
            hp=int(d["hp"]) if d.get("hp") is not None else None,
            cooldown_s=float(d["cooldown_s"]) if d.get("cooldown_s") is not None else None,
            damage=str(d.get("damage") or ""),
            effect_en=str(d.get("effect_en") or ""),
            effects=tuple(d.get("effects") or ()),
            raw=dict(d),
        )


class PlantBook:
    def __init__(
        self,
        hybrid_file: str = HYBRID_FILE,
        cost_file: str = COST_FILE,
        kb_file: str = KB_FILE,
        ids_file: str = IDS_FILE,
    ):
        # table: id -> (显示名, 成本, 角色)   —— 兼容旧接口
        self.table: dict[int, tuple[str, int, str]] = dict(BASE_PLANTS)
        # kb_by_id: id -> KBEntry（只有杂交版登记过的植物才有）
        self.kb_by_id: dict[int, KBEntry] = {}
        # kb_by_name: 中文名 -> KBEntry
        self.kb_by_name: dict[str, KBEntry] = {}
        self.hybrid_file = hybrid_file
        self.cost_file = cost_file
        self.kb_file = kb_file
        self.ids_file = ids_file
        self.loaded_hybrid = 0
        self.bound_ids: dict[str, int] = {}          # 名字 -> id
        self.binding_notes: list[str] = []
        # 运行时校准出来的真实成本 / 已知下界
        self.real_cost: dict[int, int] = {}
        self.min_cost: dict[int, int] = {}
        # 绑定后学到的成本与知识库不符的告警（用于发现绑错）
        self.cost_mismatch: dict[int, tuple[int, int]] = {}

        self.load()            # plant_names.json（原版重复项，保持兼容）
        self.load_kb()         # hybrid_plants.json（功能知识库）
        self.load_ids()        # plant_ids.json（已绑定结果）
        self.load_costs()      # plant_costs.json（实测成本）

    # -- 载入 -----------------------------------------------------------
    def load(self) -> None:
        data = load_json(self.hybrid_file, {})
        for k, v in (data or {}).items():
            try:
                pid = int(k)
            except ValueError:
                continue
            if isinstance(v, dict):
                self.table[pid] = (
                    v.get("name", f"#{pid}"),
                    int(v.get("cost", -1)),
                    v.get("role", UNKNOWN),
                )
            elif isinstance(v, list) and len(v) >= 3:
                self.table[pid] = (str(v[0]), int(v[1]), str(v[2]))
            self.loaded_hybrid += 1

    def load_kb(self) -> None:
        data = load_json(self.kb_file, {}) or {}
        for cn, d in (data.get("plants") or {}).items():
            if isinstance(d, dict):
                self.kb_by_name[cn] = KBEntry.from_json(cn, d)

    def load_ids(self) -> None:
        """载入已绑定的 名字 -> type_id。"""
        data = load_json(self.ids_file, {}) or {}
        for name, pid in (data.get("ids") or {}).items():
            try:
                i = int(pid)
            except (TypeError, ValueError):
                continue
            self.bind_one(i, name)

    def bind_one(self, type_id: int, name: str) -> bool:
        """把某个 type_id 绑定到知识库里的一个名字。"""
        ent = self.kb_by_name.get(name)
        if ent is None:
            return False
        self.bound_ids[name] = type_id
        self.kb_by_id[type_id] = ent
        self.table[type_id] = (name, ent.cost if ent.cost is not None else -1, ent.role)
        return True

    def save_ids(self) -> bool:
        if not self.bound_ids:
            return False
        return save_json(self.ids_file, {
            "_meta": {
                "note": "由 tools/bind_cards.py 写入：名字 -> type_id。",
                "verified_by": "cd_total ≈ cooldown_s * 100",
            },
            "ids": {k: v for k, v in sorted(self.bound_ids.items(), key=lambda kv: kv[1])},
        })

    # -- 真实成本校准 ---------------------------------------------------
    def load_costs(self) -> None:
        data = load_json(self.cost_file, {})
        for k, v in (data or {}).items():
            try:
                self.real_cost[int(k)] = int(v)
            except (TypeError, ValueError):
                continue

    def save_costs(self) -> None:
        if not self.real_cost:
            return
        save_json(self.cost_file, {str(k): v for k, v in sorted(self.real_cost.items())})

    def set_real_cost(self, type_id: int, cost: int) -> None:
        """记录实测到的真实成本。

        ⚠️ 为什么必须有这个机制：**杂交版把植物价格全改了**，而且每局的卡槽
        阵容都不同，所以任何硬编码价格表都会错。实测踩到的坑：代码以为火爆辣椒
        125 阳光，实际要 275 —— 于是"买得起"的判断是错的，点卡被游戏拒绝，
        表现为 **阳光没扣、植物没种上**，极容易被误判成"点击注入失效"。
        可靠来源只有一个：点卡之后阳光掉了多少。
        """
        if cost <= 0:
            return
        self.real_cost[type_id] = cost
        # 顺带校验绑定：如果实测价格和知识库差太多，八成是把卡绑错了
        ent = self.kb_by_id.get(type_id)
        if ent and ent.cost is not None and cost != ent.cost:
            self.cost_mismatch[type_id] = (ent.cost, cost)

    def note_unaffordable(self, type_id: int, sun: int) -> None:
        """点卡被游戏拒绝 -> 真实成本至少是 sun+1。

        先记下这个下界，这样后续的"买得起吗"判断会立刻变严，不会反复白点同一张卡。
        等阳光涨过真实成本后，点卡会成功，`set_real_cost` 就会把它精确下来。
        """
        self.min_cost[type_id] = max(self.min_cost.get(type_id, 0), sun + 1)

    # -- 查询 -----------------------------------------------------------
    def name(self, type_id: int) -> str:
        if type_id in self.table:
            return self.table[type_id][0]
        if type_id < 0:
            return "(空)"
        return f"#{type_id}"

    def en(self, type_id: int) -> str:
        """给 Jev 用的英文名。Jev 主要用英文训练，英文问题更稳。"""
        ent = self.kb_by_id.get(type_id)
        if ent:
            return ent.en
        nm = self.name(type_id)
        if " " in nm:
            tail = nm.split(" ", 1)[1]
            if tail and tail.isascii():
                return tail
        return nm

    def cost(self, type_id: int) -> int | None:
        """真实成本优先，其次知识库/原版表，最后取"已知下界"。"""
        if type_id in self.real_cost:
            return self.real_cost[type_id]
        ent = self.table.get(type_id)
        base = ent[1] if (ent and ent[1] >= 0) else None
        floor = self.min_cost.get(type_id)
        if floor is not None:
            return max(base or 0, floor)
        return base

    def role(self, type_id: int) -> str:
        ent = self.table.get(type_id)
        return ent[2] if ent else UNKNOWN

    def tags(self, type_id: int) -> tuple[str, ...]:
        """机器可读的标签。知识库里没登记的（含原版植物）退化成 (role,)。"""
        ent = self.kb_by_id.get(type_id)
        if ent and ent.tags:
            return ent.tags
        return (self.role(type_id),)

    def has_tag(self, type_id: int, tag: str) -> bool:
        return tag in self.tags(type_id)

    def effect(self, type_id: int) -> str:
        """给 Jev 看的一句英文效果说明。**这是它能不能做对判断的关键输入。**"""
        ent = self.kb_by_id.get(type_id)
        if ent and ent.effect_en:
            return ent.effect_en
        return ""

    def cooldown_s(self, type_id: int) -> float | None:
        ent = self.kb_by_id.get(type_id)
        return ent.cooldown_s if ent else None

    def hp(self, type_id: int) -> int | None:
        ent = self.kb_by_id.get(type_id)
        return ent.hp if ent else None

    def is_known(self, type_id: int) -> bool:
        return type_id in self.table

    def describe(self, type_id: int) -> dict:
        ent = self.kb_by_id.get(type_id)
        return {
            "id": type_id,
            "name": self.name(type_id),
            "en": self.en(type_id),
            "cost": self.cost(type_id),
            "role": self.role(type_id),
            "tags": list(self.tags(type_id)),
            "hp": ent.hp if ent else None,
            "cooldown_s": ent.cooldown_s if ent else None,
            "effect": ent.effect_en if ent else "",
            "registered": self.is_known(type_id),
            "bound": type_id in self.kb_by_id,
        }

    def unregistered_ids(self, ids) -> list[int]:
        return sorted({i for i in ids if i >= 0 and not self.is_known(i)})

    def unbound_ids(self, ids) -> list[int]:
        """有名字但还没绑定到 ID 的（= 还差一次 bind_cards 的）。"""
        return sorted({i for i in ids if i >= 0 and i not in self.kb_by_id})

    # -- 绑定 + 校验 -----------------------------------------------------
    def bind_lineup(self, slot_types: list[int], order: list[str],
                    cd_totals: list[int] | None = None) -> dict:
        """按卡面顺序把 slot 的 type_id 绑到名字上。

        校验分三层，按可靠性排序：

        1. **type_id 指纹**（硬校验，由 `match_lineup` 先做）：内存读到的
           type_id 序列和 `lineups.json` 里记录的完全一致 → 确认是同一副牌。
        2. **运行时阳光差值**（最终校验，在 agent 里）：点卡后阳光掉了多少，
           和知识库里的成本比对。这是唯一直接观测到"游戏认这张卡多少钱"的信号。
        3. 冷却时间**只作参考，不作闸门**。实测杂交版图鉴的「冷却速度」不是
           种子包冷却：樱桃辣椒图鉴写 100 秒、内存 `cd_total=5000`(50 秒)；
           冰瓜香蒲图鉴 30 秒、内存 2000(20 秒)。比值在 50~100 tick/s 之间飘，
           拿它当闸门会把**正确**的绑定拦下来（这个坑刚踩过）。
        """
        report = {
            "bound": [],
            "cooldown_ok": True,
            "mismatches": [],
            "unbound_names": [],
            "extra_slots": [],
        }
        n = min(len(slot_types), len(order))
        if len(slot_types) != len(order):
            self.binding_notes.append(
                f"卡槽数 {len(slot_types)} 与 lineup 顺序长度 {len(order)} 不一致，只绑前 {n} 格"
            )
        for i in range(n):
            tid = slot_types[i]
            nm = order[i]
            ent = self.kb_by_name.get(nm)
            if ent is None:
                report["unbound_names"].append(nm)
                continue
            if tid < 0:
                continue
            self.bind_one(tid, nm)
            cd_total = cd_totals[i] if cd_totals and i < len(cd_totals) else None
            exp_cd = ent.cooldown_s
            agree = None
            if cd_total and cd_total > 0 and exp_cd:
                # 只判断"量级差得离谱"，不要求精确吻合（tick 率本身就不确定）
                ratio = cd_total / exp_cd
                agree = 25.0 <= ratio <= 200.0
            row = {
                "slot": i,
                "type_id": tid,
                "name": nm,
                "en": ent.en,
                "cost": ent.cost,
                "cd_total": cd_total,
                "almanac_cd_s": exp_cd,
                "cd_agrees": agree,
            }
            report["bound"].append(row)
            if agree is False:
                report["mismatches"].append(row)
        if len(slot_types) > n:
            report["extra_slots"] = list(range(n, len(slot_types)))
        # cooldown_ok 现在只是"参考项"，不再是写盘的门槛（见 docstring）
        report["cooldown_ok"] = not report["mismatches"]
        return report


def match_lineup(lineups: list[dict], slot_types: list[int]) -> tuple[dict | None, str]:
    """挑出当前这副牌对应的 lineup，并说明凭什么匹配的。

    优先按 **type_id 指纹**精确匹配（`type_ids` 字段）—— 这是"同一副牌"的
    强证据。匹配不到再退化成"按卡数匹配"，但那种情况下名字顺序**没有被证实**，
    调用方应该只告警、不落盘。

    返回 `(lineup, how)`，`how` ∈ {"type_ids", "length", "none"}。
    """
    active = [t for t in slot_types if t >= 0]
    for lu in lineups:
        ids = lu.get("type_ids")
        if ids and list(ids) == active:
            return lu, "type_ids"
    same_len = [lu for lu in lineups if len(lu.get("order") or []) == len(active)]
    if len(same_len) == 1:
        return same_len[0], "length"
    return None, "none"


def load_lineups(path: str = LINEUPS_FILE) -> list[dict]:
    data = load_json(path, {}) or {}
    return list(data.get("lineups") or [])

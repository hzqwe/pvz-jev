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
import copy
import hashlib
import math
from pathlib import Path
from .catalog import CatalogIdentity, VersionCatalog, DEFAULT_CATALOG, load_catalog, load_profile_bindings, _unique_keys
from .mechanics import load_mechanics
import os
import time
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
T_TORCH = "torch"                  # 火炬柱：豌豆穿过它获火焰增益（向日葵女王）
T_PLATFORM = "platform"

# 原版系短射程植物的射程估计（格，1 格 = 80 逻辑px）。
# 杂交版大喷菇 = 前方四格（pvzhe.wiki/new.pvzhe.wiki 实测）；其余取原版公认值。
# 用途：policy 生成落点时做"够不够得着最近僵尸"的校验。估计值宁可保守
# （偏小），偏大会把植物放到够不着的位置。
SHORT_RANGE_CELLS = {
    8: 3,    # 小喷菇
    10: 4,   # 大喷菇（杂交版实为前方四格）
    13: 4,   # 胆小菇
    24: 3,   # 海蘑菇
    42: 3,   # 忧郁菇（自身 3x3，等效短射程）
}

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
ZOMBIE_TRAITS_FILE = os.path.join(_DIR, "zombie_traits.json")

# 成本下界（min_cost）的有效期（秒）。点卡被拒记下的"成本 ≥ 阳光+1"只在
# 短期内可信：坐标偏一点、消息晚处理一拍都会造成假"被拒"。没有衰减的话
# 这个下界只涨不跌，`cost()` 取 max 后这张卡就永远买不起、永远没机会用
# 一次成功的购买把真实成本校准回来 —— 单向棘轮死锁（2026-09-26 实测）。
MIN_COST_TTL = 90.0


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


def _confirmed(raw: dict, path: str) -> bool:
    source = (raw.get('field_sources') or {}).get(path) or {}
    if source.get('confidence') == 'user_confirmed':
        return True
    # Legacy almanac records predate field-level provenance.
    marker = str(raw.get('combat_source') or '')
    return marker.startswith('user_') and path.split('.')[0] in (
        'cost', 'hp', 'cooldown_s', 'role', 'tags', 'combat', 'effect_en')


def _merge_profile(raw: dict, patch: dict, evidence: dict) -> dict:
    """Apply sourced fields, keeping confirmed facts and explicit capabilities."""
    result = copy.deepcopy(raw)
    old_sources = result.setdefault('field_sources', {})
    confidence_rank = {
        'unknown': 0,
        'classic_reference': 1,
        'classic_inferred': 2,
        'version_verified': 3,
        'runtime_confirmed': 4,
        'user_confirmed': 5,
    }
    def apply(target, values, prefix=''):
        for key, value in values.items():
            if key in ('field_sources', 'unverified_fields'):
                continue
            path = prefix + key
            source = evidence.get(path) or evidence.get(path.split('.')[0]) or {}
            old_source = old_sources.get(path) or old_sources.get(path.split('.')[0]) or {}
            old_conf = confidence_rank.get(old_source.get('confidence', 'unknown'), 0)
            new_conf = confidence_rank.get(source.get('confidence', 'unknown'), 0)
            if old_source and old_conf > new_conf and target.get(key) not in (None, '', UNKNOWN):
                # A later broad/reference overlay cannot downgrade an already
                # corroborated or runtime-confirmed field.
                continue
            if isinstance(value, dict):
                if not isinstance(target.get(key), dict):
                    target[key] = {}
                apply(target[key], value, path + '.')
                continue
            if (_confirmed(raw, path) or _confirmed(raw, path.split('.')[0])) and source.get('confidence') != 'user_confirmed':
                continue
            if key == 'tags' and isinstance(value, list):
                target[key] = list(dict.fromkeys(list(target.get(key) or []) + value))
            else:
                confidence = source.get('confidence', '')
                # Old card-face text is merely a placeholder; it cannot downgrade
                # an existing almanac capability or replace explicit effects.
                weak = confidence not in ('user_confirmed', 'runtime_confirmed', 'version_verified')
                if weak and path in ('cost', 'hp', 'cooldown_s', 'role', 'effect_en') and target.get(key) not in (None, '', UNKNOWN):
                    continue
                target[key] = copy.deepcopy(value)
            if source and (new_conf >= old_conf or not old_source):
                old_sources[path] = copy.deepcopy(source)
    apply(result, patch)
    result.setdefault('unverified_fields', {}).update(copy.deepcopy(patch.get('unverified_fields', {})))
    return result


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
        d = copy.deepcopy(d)
        combat = d.setdefault('combat', {})
        if not isinstance(combat, dict):
            raise ValueError('Plant combat must be an object')
        if d.get('hp') is None and combat.get('hp') is not None:
            d['hp'] = combat['hp']
        if 'piercing' not in combat and 'pierce' in combat:
            combat['piercing'] = combat['pierce']
        for key in ('cost', 'hp', 'cooldown_s'):
            value = d.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError(f'Invalid plant {key}')
        tags = d.get('tags') or []
        if not isinstance(tags, (list, tuple)) or any(not isinstance(t, str) for t in tags):
            raise ValueError('Plant tags must be strings')
        if not isinstance(d.get('placement', {}), dict):
            raise ValueError('Plant placement must be an object')
        # Explicit legacy profile aliases establish support ability. No arbitrary
        # plant-name substring or active catalog ID is treated as a platform.
        legacy_platforms = {'睡莲': ('lily', ['water']), '豌豆睡莲': ('lily', ['water']),
                            '花盆': ('pot', ['land', 'roof']), '阳光花盆': ('pot', ['land', 'roof'])}
        if cn in legacy_platforms:
            support, terrain = legacy_platforms[cn]
            placement = d.setdefault('placement', {})
            placement.setdefault('support_kind', support)
            placement.setdefault('terrain', terrain)
            placement.setdefault('layer', 'platform')
        economy = combat.get('economy') or {}
        if not isinstance(economy, dict):
            raise ValueError('Plant economy must be an object')
        for source, target in (('economy_resource', 'resource'), ('economy_trigger', 'trigger')):
            if source in combat:
                economy.setdefault(target, combat[source])
        if economy:
            combat['economy'] = economy
            for source, target in (('resource', 'economy_resource'), ('trigger', 'economy_trigger')):
                if source in economy:
                    combat[target] = economy[source]
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
        encyclopedia_file: str | None = None,
        *, _snapshot_data: dict | None = None,
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
        self.encyclopedia_file = str(DEFAULT_CATALOG.with_name('encyclopedia.json')) if encyclopedia_file is None else encyclopedia_file
        self.domain_knowledge: dict = {}
        self._snapshot_data = _snapshot_data or {}
        self._playbook_snapshot = load_json(os.path.join(_DIR, 'plant_playbook.json'), {})
        self.knowledge_generation = 0
        self.knowledge_revision = ''
        self.quarantined_costs: dict[int, dict] = {}
        self._loaded_cost_ids: set[int] = set()
        self.reference_cost_evidence: dict[int, dict] = {}
        self.loaded_hybrid = 0
        self.bound_ids: dict[str, int] = {}          # 名字 -> id
        self.binding_notes: list[str] = []
        self.catalog: VersionCatalog | None = None
        self._catalog_profiles: dict[int, str] = {}
        self._catalog_mechanics: dict[int, dict] = {}
        self._catalog_restore: dict[int, dict] = {}
        # 运行时校准出来的真实成本 / 已知下界
        self.real_cost: dict[int, int] = {}
        self.min_cost: dict[int, int] = {}
        # 每条成本下界的记录时刻（秒）。见 MIN_COST_TTL：下界必须有衰减，
        # 否则一次误判（坐标偏 1px / 消息延迟）就把这张卡永久锁死 ——
        # 实测日志里 book_cost 全部等于"当时阳光+1"随阳光水涨船高，就是它。
        self.min_cost_ts: dict[int, float] = {}
        # 场上每种植物的株数（agent 每轮同步）。图鉴写明部分植物"场上每多一张 +100"
        # （高冰果 combat.price_increment），成本必须跟着株数走，否则点卡被拒。
        self.copies_on_field: dict[int, int] = {}
        # 绑定后学到的成本与知识库不符的告警（用于发现绑错）
        self.cost_mismatch: dict[int, tuple[int, int]] = {}
        # 每种植物成功种下的次数（agent 重置时清零）。从未用过的卡获得
        # 试探加成（policy +12）：让 agent 轮流把新卡用起来，同时用
        # 运行时价格学习校准它们的成本。
        self.planted_counts: dict[int, int] = {}
        # 僵尸特征表（crush/远程/啃食速度/移速），见 data/zombie_traits.json。
        # 杂交版可能就地替换 id：标了 confirmed 的才硬性可信，其余是候选。
        self.zombie_traits = {"defaults": {"eat_dps": 100, "speed_px_s": 8}, "types": {}}
        # 实战学到的压扁僵尸 type（植物瞬移消失事件的观察结果）—— 与静态
        # Reserved for explicitly verified runtime traits. Unexplained plant loss
        # goes into soft evidence and never promotes itself into this hard set.
        self.runtime_crush: set[int] = set()
        self.crush_evidence: dict[int, list] = {}

        self.load()            # plant_names.json（原版重复项，保持兼容）
        self.load_kb()         # hybrid_plants.json（功能知识库）
        self.load_ids()        # plant_ids.json（已绑定结果）
        self.load_costs()      # plant_costs.json（实测成本）
        self.load_zombie_traits()  # zombie_traits.json（僵尸特征）
        self._refresh_revision()

    def _refresh_revision(self) -> None:
        effective = {'table': self.table,
            'profiles': {str(t): e.raw for t, e in self.kb_by_id.items()},
            'by_name': {name: e.raw for name, e in self.kb_by_name.items()},
            'zombies': self.zombie_traits, 'domain': self.domain_knowledge,
            'playbook': self._playbook_snapshot,
            'catalog_profiles': self._catalog_profiles,
            'catalog_mechanics': self._catalog_mechanics,
            'catalog_revision': self.catalog.source_revision if self.catalog else None}
        self.knowledge_revision = hashlib.sha256(json.dumps(effective, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

    def reload_data(self) -> dict:
        """Validate and build a complete candidate, then publish it as one snapshot."""
        try:
            snapshot_data = {}
            # Configured malformed inputs must never silently turn into empty data.
            for path in (self.hybrid_file, self.cost_file, self.kb_file, self.ids_file,
                         ZOMBIE_TRAITS_FILE):
                if path:
                    data = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_unique_keys)
                    if not isinstance(data, dict):
                        raise ValueError(f'Expected JSON object: {path}')
                    snapshot_data[path] = data
            for path, section in ((self.kb_file, 'plants'), (self.ids_file, 'ids'),
                                  (ZOMBIE_TRAITS_FILE, 'types')):
                if path and not isinstance(snapshot_data[path].get(section, {}), dict):
                    raise ValueError(f'Expected {section} object: {path}')
            if self.kb_file:
                for name, raw in snapshot_data[self.kb_file].get('plants', {}).items():
                    if not isinstance(name, str) or not isinstance(raw, dict):
                        raise ValueError('Invalid named knowledge record')
                    KBEntry.from_json(name, raw)
            if self.ids_file:
                for name, tid in snapshot_data[self.ids_file].get('ids', {}).items():
                    if not isinstance(name, str) or isinstance(tid, bool) or int(tid) < 0:
                        raise ValueError('Invalid manual binding')
            if self.catalog and self.encyclopedia_file and Path(self.encyclopedia_file).exists():
                snapshot_data[self.encyclopedia_file] = json.loads(Path(self.encyclopedia_file).read_text(encoding='utf-8'), object_pairs_hook=_unique_keys)
            from . import serialize
            if Path(serialize._PLAYBOOK_FILE).exists():
                doctrine = json.loads(Path(serialize._PLAYBOOK_FILE).read_text(encoding='utf-8'))
                if not isinstance(doctrine, dict):
                    raise ValueError('Playbook must be a JSON object')
            else:
                doctrine = {}
            candidate = PlantBook(self.hybrid_file, self.cost_file, self.kb_file,
                                  self.ids_file, self.encyclopedia_file, _snapshot_data=snapshot_data)
            candidate._playbook_snapshot = doctrine
            # In-session manual bindings take priority over stale disk bindings.
            for name, tid in self.bound_ids.items():
                for old_name, old_tid in list(candidate.bound_ids.items()):
                    if old_name == name or old_tid == tid:
                        candidate.bound_ids.pop(old_name)
                        candidate.kb_by_id.pop(old_tid, None)
                        candidate.table.pop(old_tid, None)
                if not candidate.bind_one(tid, name):
                    candidate.bound_ids[name] = tid
            candidate.real_cost.update({tid: cost for tid, cost in self.real_cost.items()
                                        if tid not in self._loaded_cost_ids})
            candidate.quarantined_costs.update(copy.deepcopy(self.quarantined_costs))
            if self.catalog:
                candidate.activate_catalog(self.catalog.edition, self.catalog.game_version)
                for tid in self._catalog_restore:
                    candidate.bind_identity(tid)
                    if tid in candidate._catalog_restore:
                        # Rebuild static restore state from the new disk snapshot,
                        # but keep the facts that preceded version activation.
                        for attribute in ('real_cost', 'min_cost', 'min_cost_ts',
                                          'cost_mismatch', 'copies_on_field'):
                            candidate._catalog_restore[tid][attribute] = copy.deepcopy(
                                self._catalog_restore[tid][attribute])
            candidate._refresh_revision()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return {'ok': False, 'generation': self.knowledge_generation,
                    'revision': self.knowledge_revision, 'error': str(exc)}
        static = ('table', 'kb_by_id', 'kb_by_name', 'loaded_hybrid', 'bound_ids',
                  'catalog', '_catalog_profiles', '_catalog_mechanics', '_catalog_restore',
                  'domain_knowledge', 'zombie_traits', 'real_cost', '_loaded_cost_ids',
                  'quarantined_costs', 'knowledge_revision')
        static += ('_playbook_snapshot',)
        for name in static:
            setattr(self, name, getattr(candidate, name))
        self.knowledge_generation += 1
        serialize._PLAYBOOK = doctrine
        return {'ok': True, 'generation': self.knowledge_generation,
                'revision': self.knowledge_revision, 'bound_ids': len(self.kb_by_id)}

    def _read_json(self, path, default):
        return copy.deepcopy(self._snapshot_data[path]) if path in self._snapshot_data else load_json(path, default)

    def load_zombie_traits(self) -> None:
        data = self._read_json(ZOMBIE_TRAITS_FILE, {}) or {}
        defaults = dict(data.get("defaults") or {})
        types: dict[int, dict] = {}
        for k, v in (data.get("types") or {}).items():
            try:
                tid = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, dict):
                types[tid] = v
        self.zombie_traits = {"defaults": defaults, "types": types}

    # -- 僵尸特征查询 ----------------------------------------------------
    def zombie_flag(self, type_id: int, key: str) -> bool:
        """布尔型特征（crush / tanky）。静态表 + 实战学习双来源：
        crush 在 runtime_crush 里也成立（植物瞬移消失的观察结果，
        比 id 猜测更可靠）。未登记 = False，绝不猜。"""
        if key == 'crush' and type_id in self.runtime_crush:
            return True
        ent = self.zombie_traits["types"].get(type_id) or {}
        return bool(ent.get(key)) and (key not in ("crush", "ice_trail") or ent.get("confirmed") is True)

    def zombie_trait(self, type_id: int, key: str) -> float | None:
        """Confirmed numeric traits; unverified IDs use labelled default estimates."""
        ent = self.zombie_traits["types"].get(type_id) or {}
        v = ent.get(key, self.zombie_traits["defaults"].get(key)) if ent.get('confirmed') is True else self.zombie_traits['defaults'].get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # -- 载入 -----------------------------------------------------------
    def load(self) -> None:
        data = self._read_json(self.hybrid_file, {})
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
        data = self._read_json(self.kb_file, {}) or {}
        for cn, d in (data.get("plants") or {}).items():
            if isinstance(d, dict):
                entry = KBEntry.from_json(cn, d)
                self.kb_by_name[cn] = entry
                for alias in d.get('aliases', []):
                    if isinstance(alias, str) and alias not in self.kb_by_name:
                        self.kb_by_name[alias] = entry

    def load_ids(self) -> None:
        """载入已绑定的 名字 -> type_id。"""
        data = self._read_json(self.ids_file, {}) or {}
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
        # An explicit user binding promotes this ID out of the temporary catalog overlay.
        self._catalog_restore.pop(type_id, None)
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
        data = self._read_json(self.cost_file, {})
        for k, v in (data or {}).items():
            try:
                tid, value = int(k), int(v)
                baseline = self.table.get(tid)
                # Legacy files contain noisy net sun changes and already-increased prices.
                if baseline and baseline[1] >= 0 and value != baseline[1]:
                    self.quarantined_costs[tid] = {'observed_base': value, 'known_base': baseline[1],
                                                  'reason': 'legacy_file_mismatch'}
                    continue
                if value < 0:
                    continue
                self.real_cost[tid] = value
                self._loaded_cost_ids.add(tid)
            except (TypeError, ValueError):
                continue

    def save_costs(self) -> None:
        if not self.real_cost:
            return
        # 保留文件里的 _meta 来源标注（图鉴照片校准记录），别让运行时学习抹掉它
        meta = load_json(self.cost_file, {}).get("_meta")
        out = {str(k): v for k, v in sorted(self.real_cost.items())}
        if meta is not None:
            out["_meta"] = meta
        save_json(self.cost_file, out)

    def set_real_cost(self, type_id: int, cost: int) -> bool:
        """Check an observed net sun delta against normalized known prices.

        Sun production/collection can overlap placement, so a net delta is not a price
        oracle. Known almanac/user-confirmed bases are never overwritten by a different
        delta. Dynamic increments are removed using pre-placement field counts.
        Unknown plants can still learn a positive base for later identification.
        """
        if cost <= 0:
            return False
        ent = self.kb_by_id.get(type_id)
        base = cost - self.price_increment(type_id) * self.copies_on_field.get(type_id, 0)
        if base <= 0:
            return False
        known = ent.cost if ent and ent.cost is not None else self.table.get(type_id, ('', -1, UNKNOWN))[1]
        if known >= 0 and base != known:
            self.cost_mismatch[type_id] = (known, base)
            return False
        self.real_cost[type_id] = base
        self._loaded_cost_ids.discard(type_id)
        self.min_cost.pop(type_id, None)
        self.min_cost_ts.pop(type_id, None)
        return True

    def observe_placement_cost(self, type_id: int, sun_before: int | None,
                               sun_after: int | None, *, isolated=False, observation_id=None) -> dict:
        """Net balance changes can corroborate a known price, never invent one."""
        delta = sun_before - sun_after if sun_before is not None and sun_after is not None else None
        entry = self.kb_by_id.get(type_id)
        base = self.real_cost.get(type_id, entry.cost if entry and entry.cost is not None else self.table.get(type_id, ('', -1, UNKNOWN))[1])
        expected = base + self.price_increment(type_id) * self.copies_on_field.get(type_id, 0) if base >= 0 else None
        accepted = expected is not None and delta == expected and expected > 0
        if accepted:
            accepted = type_id in self.real_cost or self.set_real_cost(type_id, delta)
        elif delta is not None and delta > 0 and base >= 0:
            self.cost_mismatch[type_id] = (base, delta - self.price_increment(type_id) * self.copies_on_field.get(type_id, 0))
            provisional = bool(entry and str(type_id) in self.domain_knowledge.get('plants', {})
                and not _confirmed(entry.raw, 'cost') and
                (entry.raw.get('field_sources', {}).get('cost') or {}).get('confidence')
                not in ('user_confirmed', 'runtime_confirmed', 'version_verified'))
            candidate_base = delta - self.price_increment(type_id) * self.copies_on_field.get(type_id, 0)
            if provisional and isolated and observation_id and candidate_base > 0:
                evidence = self.reference_cost_evidence.get(type_id)
                if evidence is None or evidence['base'] != candidate_base:
                    evidence = {'base': candidate_base, 'observation_ids': []}
                    self.reference_cost_evidence[type_id] = evidence
                if observation_id not in evidence['observation_ids']:
                    evidence['observation_ids'].append(observation_id)
                if len(evidence['observation_ids']) >= 3:
                    self.real_cost[type_id] = candidate_base
                    self._loaded_cost_ids.discard(type_id)
                    self.min_cost.pop(type_id, None); self.min_cost_ts.pop(type_id, None)
                    self.cost_mismatch.pop(type_id, None)
                    accepted = True
                    expected = delta
        return {'net_sun_delta': delta, 'expected_price': expected, 'accepted': accepted,
                'reason': 'known_price_match' if accepted else 'unknown_price_net_balance' if expected is None else 'net_balance_not_price'}

    def _quarantine_loaded_cost(self, type_id: int) -> None:
        entry = self.kb_by_id.get(type_id)
        if type_id not in self._loaded_cost_ids or entry is None or entry.cost is None:
            return
        value = self.real_cost.get(type_id)
        if value is not None and value != entry.cost:
            self.quarantined_costs[type_id] = {'observed_base': value, 'known_base': entry.cost,
                                              'reason': 'loaded_effective_profile_mismatch'}
            self.real_cost.pop(type_id, None)
            self._loaded_cost_ids.discard(type_id)

    def note_unaffordable(self, type_id: int, sun: int) -> None:
        """点卡被游戏拒绝 -> 真实成本至少是 sun+1。

        先记下这个下界，这样后续的"买得起吗"判断会立刻变严，不会反复白点同一张卡。
        等阳光涨过真实成本后，点卡会成功，`set_real_cost` 就会把它精确下来。

        ⚠️ 两个护栏（2026-09-26）：
        1. 调用方必须先做**失败分诊**——冷却中 / 时钟没走 / 输入无效的拒绝
           毫无信息量，绝不能记（见 agent.execute）。
        2. 下界存的是"按当前场上株数归一化的基价"，且带 **TTL 衰减**
           （MIN_COST_TTL）：假"被拒"（坐标偏 1px、消息晚一拍）记出的下界
           过期自动失效，否则只涨不跌的单向棘轮会把这张卡整局锁死。
        """
        base = (sun + 1) - self.price_increment(type_id) * self.copies_on_field.get(type_id, 0)
        if base <= 0:
            # 阳光不够付"基价+已囤溢价"里的溢价部分？下界至少也要 ≥1 才有意义
            base = 1
        if base > self.min_cost.get(type_id, 0):
            self.min_cost[type_id] = base
        self.min_cost_ts[type_id] = time.time()

    def sync_field_copies(self, plants) -> None:
        """每轮把场上株数喂给成本模型（动态涨价按株数生效）。"""
        counts: dict[int, int] = {}
        for p in plants:
            counts[p.type_id] = counts.get(p.type_id, 0) + 1
        self.copies_on_field = counts

    def price_increment(self, type_id: int) -> int:
        """「场上每多一张 +X」的图鉴涨价（高冰果=100；未登记=0）。"""
        ent = self.kb_by_id.get(type_id)
        v = (ent.raw.get('combat') or {}).get('price_increment') if ent else None
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    def _floor(self, type_id: int) -> int | None:
        """带 TTL 的成本下界。过期即失效，回落到图鉴基线。"""
        floor = self.min_cost.get(type_id)
        if floor is None:
            return None
        ts = self.min_cost_ts.get(type_id)
        if ts is not None and time.time() - ts > MIN_COST_TTL:
            self.min_cost.pop(type_id, None)
            self.min_cost_ts.pop(type_id, None)
            return None
        return floor

    # -- 查询 -----------------------------------------------------------
    def name(self, type_id: int) -> str:
        if self.catalog and type_id not in self.kb_by_id:
            identity = self.identity(type_id)
            return identity.canonical_name if identity else ('(空)' if type_id < 0 else f'#{type_id}')
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
        """查询"当前这一张"的真实成本（含动态涨价），供买得起判断用。

        组成：max(实测基价, 图鉴基线, 未过期的成本下界) + 图鉴每多一张的溢价 × 场上株数。
        实测基价是"1 张时的总价"（set_real_cost 已归一化），所以这里统一按当前
        场上株数把溢价加回去 —— 场上已有 2 张高冰果时，第 3 张的正确价格是
        基价 + 200，用静态值去判断必然点卡被拒（2026-09-26 实测）。
        """
        if self.catalog and type_id not in self.kb_by_id:
            return None
        inc = self.price_increment(type_id) * self.copies_on_field.get(type_id, 0)
        vals: list[int] = []
        base = self.real_cost.get(type_id)
        if base is not None:
            vals.append(base + inc)
        else:
            ent = self.table.get(type_id)
            if ent and ent[1] >= 0:
                vals.append(ent[1] + inc)
        floor = self._floor(type_id)
        if floor is not None:
            vals.append(floor + inc)
        return max(vals) if vals else None

    def role(self, type_id: int) -> str:
        if self.catalog and type_id not in self.kb_by_id:
            return UNKNOWN
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

    def combat(self, type_id: int) -> dict:
        """Structured almanac facts; absent values remain unknown."""
        entry = self.kb_by_id.get(type_id)
        return dict(entry.raw.get('combat', {})) if entry else {}

    def placement(self, type_id: int) -> dict:
        entry = self.kb_by_id.get(type_id)
        return copy.deepcopy(entry.raw.get('placement') or {}) if entry else {}

    def range_cells(self, type_id: int) -> int | None:
        """攻击射程（格）。None = 整行/全屏，没有摆位限制。

        知识库 combat.range_cells 优先；没登记的原版系植物查 SHORT_RANGE_CELLS。
        这不是装饰数据：短射程植物（喷菇系≈4格）种在最后一排够不着僵尸，
        policy 生成落点时必须做射程校验（2026-09-26 用户实测反馈）。
        """
        if self.catalog and type_id not in self.kb_by_id:
            return None
        ent = self.kb_by_id.get(type_id)
        v = (ent.raw.get('combat') or {}).get('range_cells') if ent else None
        if v is None and not self.catalog:
            v = SHORT_RANGE_CELLS.get(type_id)
        return v

    def is_known(self, type_id: int) -> bool:
        return type_id in (self.kb_by_id if self.catalog else self.table)

    def describe(self, type_id: int) -> dict:
        ent = self.kb_by_id.get(type_id)
        reference = bool(ent and str(type_id) in self.domain_knowledge.get('plants', {})
                         and type_id not in self._catalog_profiles
                         and type_id not in self.bound_ids.values())
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
            "combat": self.combat(type_id),
            "placement": self.placement(type_id),
            "field_sources": copy.deepcopy(ent.raw.get('field_sources') or {}) if ent else {},
            "registered": self.is_known(type_id),
            "bound": type_id in self.kb_by_id,
            "knowledge_status": "mechanics_reference" if reference else "mechanics_present" if ent else "identity_only" if self.identity(type_id) else "unknown",
            "source_confidence": 'classic_reference_unverified' if reference else None,
        }

    def unregistered_ids(self, ids) -> list[int]:
        return sorted({i for i in ids if i >= 0 and not self.is_known(i)})

    def unbound_ids(self, ids) -> list[int]:
        """有名字但还没绑定到 ID 的（= 还差一次 bind_cards 的）。"""
        return sorted({i for i in ids if i >= 0 and i not in self.kb_by_id})

    # -- 绑定 + 校验 -----------------------------------------------------
    def activate_catalog(self, edition: str, game_version: str) -> None:
        """A failed version check cannot leave a stale identity table enabled."""
        self.deactivate_catalog()
        catalog = load_catalog(str(DEFAULT_CATALOG), edition, game_version)
        profiles = load_profile_bindings(catalog)
        mechanics = load_mechanics(catalog, profiles)
        domain = self._load_domain(catalog)
        self.catalog, self._catalog_profiles = catalog, profiles
        self._catalog_mechanics = mechanics
        self.domain_knowledge = domain
        for tid in list(self.kb_by_id):
            if self.kb_by_id[tid].cn == profiles.get(tid):
                self._remember_catalog_binding(tid)
                self.kb_by_id[tid] = self.catalog_entry(tid)
                ent = self.kb_by_id[tid]
                self.table[tid] = (ent.cn, ent.cost if ent.cost is not None else -1, ent.role)
                self._quarantine_loaded_cost(tid)
        self._merge_domain_zombies()
        self._refresh_revision()

    def _load_domain(self, catalog) -> dict:
        if not self.encyclopedia_file or not Path(self.encyclopedia_file).exists():
            return {}
        data = (copy.deepcopy(self._snapshot_data[self.encyclopedia_file])
                if self.encyclopedia_file in self._snapshot_data else
                json.loads(Path(self.encyclopedia_file).read_text(encoding='utf-8'), object_pairs_hook=_unique_keys))
        if (not isinstance(data, dict) or data.get('schema_version') != 1 or
                (data.get('edition'), data.get('game_version'), data.get('identity_source_revision')) !=
                (catalog.edition, catalog.game_version, catalog.source_revision)):
            raise ValueError('Encyclopedia namespace/schema mismatch')
        for kind, section, fields in (('plant', 'plants', 'profile'), ('zombie', 'zombies', 'traits')):
            records = data.get(section)
            if not isinstance(records, dict):
                raise ValueError(f'Encyclopedia {section} must be ID keyed')
            for key, record in records.items():
                if not isinstance(key, str) or not key.isdigit() or str(int(key)) != key or not isinstance(record, dict):
                    raise ValueError('Invalid encyclopedia identity')
                identity = catalog.resolve(kind, int(key))
                if identity is None or identity.canonical_name != record.get('canonical_name'):
                    raise ValueError('Encyclopedia identity conflict')
                profile = record.get(fields)
                if not isinstance(profile, dict):
                    raise ValueError('Missing encyclopedia profile/traits')
                evidence = profile.get('field_sources', {}) if kind == 'plant' else record.get('field_sources', {})
                if not isinstance(evidence, dict):
                    raise ValueError('Invalid encyclopedia field sources')
                for source in evidence.values():
                    if (not isinstance(source, dict) or not source.get('confidence') or
                            not (source.get('source_url') or source.get('local_evidence') or source.get('source_id'))):
                        raise ValueError('Unsourced encyclopedia field')
                if kind == 'plant':
                    KBEntry.from_json(identity.canonical_name, profile)
        if not isinstance(data.get('relations', []), list) or not isinstance(data.get('scenes', {}), dict):
            raise ValueError('Invalid encyclopedia relations/scenes')
        return data

    def _merge_domain_zombies(self) -> None:
        for key, record in self.domain_knowledge.get('zombies', {}).items():
            tid = int(key)
            current = self.zombie_traits['types'].get(tid) or {}
            if current.get('confirmed') is True:
                continue
            traits = copy.deepcopy(record['traits'])
            # A provisional reference must never promote a dangerous runtime flag.
            sources = record.get('field_sources') or {}
            for flag in ('crush', 'ice_trail'):
                if traits.get(flag) and (sources.get(flag) or {}).get('confidence') != 'user_confirmed':
                    traits.pop(flag)
            traits['confirmed'] = False
            traits['field_sources'] = sources
            self.zombie_traits['types'][tid] = {**current, **traits}

    def deactivate_catalog(self) -> None:
        """Undo derived profiles and their observations, retaining prior user facts."""
        for tid, snapshot in self._catalog_restore.items():
            self.kb_by_id.pop(tid, None)
            for attribute, (present, value) in snapshot.items():
                mapping = getattr(self, attribute)
                if present:
                    mapping[tid] = value
                else:
                    mapping.pop(tid, None)
        self._catalog_restore.clear()
        self._catalog_profiles.clear()
        self._catalog_mechanics.clear()
        self.domain_knowledge = {}
        self.catalog = None
        self.load_zombie_traits()
        self._refresh_revision()

    def catalog_entry(self, type_id: int, catalog=None) -> KBEntry | None:
        """Resolve bound/available mechanics without mutating binding or cost state."""
        catalog = catalog or self.catalog
        if catalog is None:
            return self.kb_by_id.get(type_id)
        if catalog is self.catalog:
            profiles, mechanics = self._catalog_profiles, self._catalog_mechanics
        else:
            profiles = load_profile_bindings(catalog)
            mechanics = load_mechanics(catalog, profiles)
        name = profiles.get(type_id)
        entry = self.kb_by_id.get(type_id) or self.kb_by_name.get(name)
        record = mechanics.get(type_id)
        domain = self.domain_knowledge if catalog is self.catalog else self._load_domain(catalog)
        domain_record = domain.get('plants', {}).get(str(type_id))
        if entry is None and domain_record:
            entry = KBEntry.from_json(domain_record['canonical_name'], domain_record['profile'])
        if entry is None:
            return None
        raw = entry.raw
        if record is not None and entry.cn == name:
            raw = _merge_profile(raw, record['patch'], record['field_sources'])
            raw.setdefault('unverified_fields', {}).update(copy.deepcopy(record.get('unverified_fields', {})))
        if domain_record:
            profile = domain_record['profile']
            raw = _merge_profile(raw, profile, profile.get('field_sources') or {})
        # These old overlays were card-face/economy placeholders. Retain explicit
        # tracking shots, and model sky/contact sun as events rather than salaries.
        if type_id in (155, 160):
            raw = copy.deepcopy(raw)
            combat = raw.setdefault('combat', {})
            trigger = 'sky_amplifier' if type_id == 155 else 'contact_trigger'
            combat.setdefault('economy', {}).update(resource='battle_sun', trigger=trigger)
            if not _confirmed(raw, 'combat.sun_per_25s'):
                combat.pop('sun_per_25s', None)
        return KBEntry.from_json(entry.cn, raw)

    def _remember_catalog_binding(self, type_id: int) -> None:
        attributes = ('table', 'kb_by_id', 'real_cost', 'min_cost', 'min_cost_ts',
                      'cost_mismatch', 'copies_on_field')
        self._catalog_restore.setdefault(type_id, {
            attribute: (type_id in getattr(self, attribute), getattr(self, attribute).get(type_id))
            for attribute in attributes})

    def identity(self, type_id: int) -> CatalogIdentity | None:
        return self.catalog.resolve('plant', type_id) if self.catalog else None

    def zombie_name(self, type_id: int) -> str:
        ent = self.catalog.resolve('zombie', type_id) if self.catalog else None
        return ent.canonical_name if ent else f'zombie_type_{type_id} (hybrid identity unverified)'

    def bind_identity(self, type_id: int) -> bool:
        """Use an explicit version/ID assignment; never infer mechanics from a name."""
        if type_id in self.kb_by_id:
            return True
        identity = self.identity(type_id)
        if not identity:
            return False
        entry = self.catalog_entry(type_id)
        if entry is None:
            return False
        # Keep automatic assignments out of bound_ids/save_ids. Restore prior
        # table and cost observations on a version switch, including failed loads.
        self._remember_catalog_binding(type_id)
        self.kb_by_id[type_id] = entry
        self.table[type_id] = (entry.cn, entry.cost if entry.cost is not None else -1, entry.role)
        self._quarantine_loaded_cost(type_id)
        self._refresh_revision()
        return True

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

"""从运行中的 PvZ 进程读出战场状态。

只读，不写游戏内存——所有"修改游戏"的能力都刻意不实现。
读失败一律降级为 None 并记录到 notes，绝不抛异常打断循环。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from . import offsets as O
from .win32 import (
    ProcessMemory,
    find_pid,
    is_process_alive,
    module_base,
    process_name,
)

PROCESS_NAMES = [
    "PlantsVsZombies.exe",
    "PlantsVsZombies",
    "pvzHE-Launcher.exe",
    "pvzHE-Launcher",
]

# ⚠️★ 只有这几个 exe 名才是**游戏本体**（2026-09-26 实测确认）。
#
# `pvzHE-Launcher.exe` 虽然也在 PROCESS_NAMES 里（工具脚本需要"看得见"它），
# 但它是个**没有任何顶层窗口**的加载器：主模块基址 0xD30000（游戏是 0x400000），
# 内存里没有 LawnApp。把它当游戏读的后果：
#   * 游戏关掉后 `find_pid` 落到 launcher 上、`attach` 报成功，
#     上层以为游戏还在；
#   * `find_game_window(launcher_pid)` 可能找到 launcher 的窗口，
#     完整模式下就会去动一个和游戏无关的窗口。
GAME_EXE_NAMES = ("plantsvszombies.exe", "plantsvszombies")


# ---------------------------------------------------------------- 数据类
@dataclass
class Plant:
    index: int
    row: int
    col: int
    type_id: int
    imitater: int = -1
    asleep: bool = False
    # 血量未知时是 None（读取失败/越界），绝不用 0 冒充"快死了"。
    hp: int | None = None
    max_hp: int | None = None
    recently_eaten: bool = False   # mRecentlyEatenCountdown > 0：正在被啃

    @property
    def cell(self) -> tuple[int, int]:
        return self.row, self.col


@dataclass
class DroppedSeed:
    index: int
    type_id: int
    x: float
    y: float
    width: int
    height: int


@dataclass
class Zombie:
    index: int
    row: int
    type_id: int
    x: float | None = None
    phase: int | None = None
    hp: int | None = None
    armor_hp: int | None = None
    friendly: bool = False

    @property
    def close_to_house(self) -> bool:
        return self.x is not None and self.x < 180


@dataclass
class SeedSlot:
    """卡槽。

    `cd_left` 是**剩余**冷却 tick，倒数到 0 就绪；`cd_total` 是总冷却 tick。
    证据：杂交版实测樱桃炸弹 cd_total=5000，而 PvZ 樱桃炸弹冷却正是 50 秒，
    按 100 tick/秒 = 5000 tick —— 与"0x4C 是剩余量、0x50 是总量"完全吻合。
    """

    index: int
    type_id: int
    cd_left: int
    cd_total: int

    @property
    def ready(self) -> bool:
        return self.cd_total <= 0 or self.cd_left <= 0

    @property
    def cooldown_left_frac(self) -> float:
        if self.cd_total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.cd_left / self.cd_total))


@dataclass
class BoardState:
    ok: bool = False
    reason: str = ""
    pid: int | None = None
    lawn_app: int = 0
    board: int = 0
    ui: int | None = None
    scene: int | None = None
    level: int | None = None
    sun: int | None = None
    game_clock: int | None = None
    paused: int | None = None
    # 时钟是否在推进（agent 每轮用前后两拍 game_clock 判定后写回）。
    # ⚠️ 这是决策层唯一的"活体"判据 —— 0x164 paused 实测不可靠（时钟正常
    #    推进时也读 1）。None = 未知（合成战场/第一轮），按"在跑"处理。
    clock_advancing: bool | None = None
    rows: int = O.LAWN_ROWS
    cols: int = O.LAWN_COLS
    plants: list[Plant] = field(default_factory=list)
    dropped_seeds: list[DroppedSeed] = field(default_factory=list)
    zombies: list[Zombie] = field(default_factory=list)
    slots: list[SeedSlot] = field(default_factory=list)
    # -- 光标（"手上有没有拿东西"）--------------------------------------
    # 这是判断"点卡有没有成功"的**唯一可靠信号**，见 offsets.OFF_CURSOR 的说明。
    # holding 现在泛指"手上有东西"（种子或铲子）；具体是什么看 held_cursor：
    # 1=手持种子（这时 held_slot/held_type 才有意义），6=手持铲子。
    holding: bool = False
    held_cursor: int = 0
    held_slot: int = -1
    held_type: int = -1
    notes: list[str] = field(default_factory=list)
    # Validated memory terrain: 1=land, 2=water. Empty means scene fallback.
    row_types: dict[int, int] = field(default_factory=dict)
    # Missing key means unknown; False means a validated row has no ready mower.
    mowers: dict[int, bool] = field(default_factory=dict)
    # -- 出怪/波次（信息层，语义未经杂交版实战核验，仅供 Jev 参考）----------
    # spawn_list 是本关整张出怪表（僵尸 type 序列，-1 结尾）；spawned 估算用
    # 僵尸池分配游标。任何一步读不出合法值就保持 None，绝不猜。
    spawn_total: int | None = None
    spawn_spawned: int | None = None
    spawn_upcoming: int | None = None
    spawn_upcoming_kinds: dict[int, int] | None = None
    spawnable_types: list[int] | None = None

    @property
    def holding_shovel(self) -> bool:
        return self.held_cursor == O.CUR_SHOVEL

    # --- 派生视图 -----------------------------------------------------
    def occupancy(self) -> dict[tuple[int, int], list[Plant]]:
        g: dict[tuple[int, int], list[Plant]] = {}
        for p in self.plants:
            g.setdefault(p.cell, []).append(p)
        return g

    def is_water(self, row: int) -> bool:
        if row in self.row_types:
            return self.row_types[row] == 2
        return self.scene in (2, 3) and row in (2, 3)

    def top_occupancy(self, book):
        return {cell: ps for cell, stack in self.occupancy().items()
                if (ps := [p for p in stack if not book.has_tag(p.type_id, 'platform')])}

    def has_platform(self, row, col, book):
        return any(book.has_tag(p.type_id, 'platform')
                   for p in self.occupancy().get((row,col), []))

    def can_plant(self, row, col, type_id, book):
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            return False
        stack = self.occupancy().get((row,col), [])
        if book.has_tag(type_id,'platform'):
            return self.is_water(row) and not stack
        if (row,col) in self.top_occupancy(book):
            return False
        return not self.is_water(row) or self.has_platform(row,col,book)

    def empty_cells(self) -> list[tuple[int, int]]:
        occ = self.occupancy()
        return [
            (r, c)
            for r in range(self.rows)
            for c in range(self.cols)
            if (r, c) not in occ
        ]

    def zombies_in_lane(self, row: int) -> list[Zombie]:
        return [z for z in self.zombies if z.row == row and not z.friendly]

    def plants_in_lane(self, row: int) -> list[Plant]:
        return [p for p in self.plants if p.row == row]


# ---------------------------------------------------------------- 读取器
class BoardReader:
    """附着到游戏进程并读取 Board。"""

    def __init__(self, pid: int | None = None):
        self.pid = pid
        self.pm: ProcessMemory | None = None
        self.notes: list[str] = []
        # ★★ 「这一轮里**曾经**成功附着到游戏本体」—— 一旦为 True 就不再变回 False。
        #
        # 为什么必须是**粘性**的、不能拿 `self.pid is not None` 代替（2026-09-26 实测踩过）：
        #   `attach()` 在发现 pid 已退出时会**把 self.pid 清成 None**，
        #   于是"游戏刚刚还在、现在没了"这个事实被抹掉了 ——
        #   上层只看到"pid 是 None"，就分不清
        #     (a) 游戏**从来没出现过**（该耐心等 --wait-play）
        #     (b) 游戏**刚才还在，现在被用户关了**（该立刻干净退出）
        #   实测 (b) 被误判成 (a)，agent 就傻等到 --wait-play 超时（默认 900s），
        #   用户看到的就是"退出游戏后它还在那儿转"。
        self.ever_attached = False
        self.attach()

    # -- 生命周期 -------------------------------------------------------
    def attach(self) -> bool:
        self.notes = []
        if self.pm is not None:
            self.pm.close()
            self.pm = None
        # ⚠️★ 缓存的 pid 可能是**僵尸**（2026-09-26 实测踩过）：游戏退出后 pid
        #    仍可枚举、OpenProcess 仍会成功，于是 attach 会"成功"并读到
        #    **定格不动的残留内存**，上层就会一直以为游戏在跑。
        #    ⚠️ 但**不能直接 return False**：那样等于"缓存一旦失效就再也找不回来"，
        #    用户关掉游戏再重开（pid 变了）时 agent 就永远看不见新进程了。
        #    正确做法：**丢掉缓存、重新按优先级查一次**。
        pid = self.pid
        if pid is not None and not is_process_alive(pid):
            self.notes.append(f"缓存的 pid={pid} 已退出（僵尸）—— 重新查找")
            pid = None
        if pid is None:
            pid = find_pid(PROCESS_NAMES)
        if pid is None:
            self.notes.append("未找到游戏进程（PlantsVsZombies.exe）")
            self.pid = None
            return False
        # 查回来的 pid 也要复核一次（find_pid 已过滤僵尸，这里是第二道闸）
        if not is_process_alive(pid):
            self.notes.append(f"pid={pid} 已经退出（进程对象仍在，属于僵尸）")
            self.pid = None
            return False
        self.pid = pid
        try:
            base = module_base(pid) or O.PVZ_IMAGE_BASE
            self.pm = ProcessMemory(pid, base=base)
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 故意捕获所有异常：`attach()` 是"试探"型函数，任何失败都只该
            #    返回 False 让上层重试。实测踩过 —— 只捕 OSError 时，ctypes 的
            #    ArgumentError 会直接打崩整个 agent（pid 被回收给 64 位进程时）。
            self.notes.append(str(exc))
            return False
        if self.pm.base != O.PVZ_IMAGE_BASE:
            self.notes.append(
                f"模块基址 0x{self.pm.base:X}（非 0x{O.PVZ_IMAGE_BASE:X}），已按重定位换算"
            )
        # ⚠️★ 光"OpenProcess 成功"不等于"找对了进程"（2026-09-26 实测）：
        #    `pvzHE-Launcher.exe` 和游戏本体常常同时在跑，而 launcher 的内存里
        #    当然没有 LawnApp。不加这道校验的话，游戏关掉后 `find_pid` 会落到
        #    launcher 上、`attach` 报成功，上层却以为游戏还在 —— 而且这时
        #    `find_game_window(launcher_pid)` 还可能找到 launcher 的窗口，
        #    在完整模式下就会去动一个完全无关的窗口。
        if not self._plausible_game(pid):
            self.notes.append(
                f"pid={pid}（{process_name(pid)}）不是游戏本体 —— 已拒绝附着"
            )
            self.pm.close()
            self.pm = None
            self.pid = None
            return False
        # ★ 到这里才算"真的见过游戏本体"。粘性标记，供上层区分
        #   "从没出现过"（耐心等）和"刚才还在现在没了"（立刻退）。
        self.ever_attached = True
        return True

    def _plausible_game(self, pid: int) -> bool:
        """判断这个 pid 到底是不是游戏本体。

        两道判据，**都过才算**：
        1. **exe 名**必须是 `PlantsVsZombies*` —— launcher 名字就对不上；
        2. `LawnApp` 指针必须非 0 且可读（主菜单/选卡下它也存在，
           所以这道判据是宽松的，不会把"还没进关卡"误判成"不是游戏"）。
        """
        name = (process_name(pid) or "").lower()
        if name not in GAME_EXE_NAMES:
            return False
        pm = self.pm
        if pm is None:
            return False
        try:
            return bool(pm.u32(pm.va(O.LAWN_PTR)))
        except Exception:  # noqa: BLE001 —— 校验失败一律当作"不是游戏"
            return False

    @property
    def attached(self) -> bool:
        return self.pm is not None

    def close(self) -> None:
        """释放进程句柄。agent 退出时必须调，否则句柄泄漏。"""
        if self.pm is not None:
            self.pm.close()
            self.pm = None

    # -- 指针链 ---------------------------------------------------------
    def lawn_app_ptr(self) -> int | None:
        assert self.pm
        return self.pm.u32(self.pm.va(O.LAWN_PTR))

    def board_ptr(self) -> int | None:
        assert self.pm
        lawn = self.lawn_app_ptr()
        if not lawn:
            return None
        return self.pm.u32(lawn + O.OFF_BOARD)

    def rows_hint(self) -> int:
        """当前场景的行数上限（结构体步长探测时用来判断 row 是否合法）。"""
        assert self.pm
        board = self.board_ptr()
        if not board:
            return O.LAWN_ROWS
        scene = self.pm.i32(board + O.OFF_SCENE)
        return O.SCENE_ROWS.get(scene if scene is not None else 0, O.LAWN_ROWS)

    # -- 读一个完整快照 --------------------------------------------------
    def read(self) -> BoardState:
        st = BoardState(pid=self.pid)
        if not self.attached:
            st.reason = "未附着到进程"
            st.notes = list(self.notes)
            return st
        pm = self.pm
        assert pm

        lawn = self.lawn_app_ptr()
        st.lawn_app = lawn or 0
        if not lawn:
            st.reason = "LawnApp 指针为空（可能还在主菜单/加载中）"
            st.notes = list(self.notes)
            return st

        board = pm.u32(lawn + O.OFF_BOARD)
        st.board = board or 0
        if not board:
            st.reason = "Board 指针为空（未进入关卡）"
            st.notes = list(self.notes)
            return st

        st.ui = pm.i32(lawn + O.OFF_GAME_UI)
        st.scene = pm.i32(board + O.OFF_SCENE)
        st.level = pm.i32(board + O.OFF_ADVENTURE_LEVEL)
        st.sun = pm.i32(board + O.OFF_SUN)
        st.game_clock = pm.i32(board + O.OFF_GAME_CLOCK)
        st.paused = pm.u8(board + O.OFF_GAME_PAUSED)
        st.rows = O.SCENE_ROWS.get(st.scene if st.scene is not None else 0, O.LAWN_ROWS)

        raw_rows = [pm.i32(board + O.OFF_ROW_TYPE + r*4) for r in range(6)]
        # Reject partially invalid snapshots; never infer water from arbitrary scene IDs.
        if st.scene not in (0,1,4) and all(v in (1,2) for v in raw_rows):
            st.rows = 6
            st.row_types = dict(enumerate(raw_rows))
        elif all(v in (1,2) for v in raw_rows[:st.rows]):
            st.row_types = dict(enumerate(raw_rows[:st.rows]))
        st.plants = self._read_plants(board)
        st.zombies = self._read_zombies(board)
        st.slots = self._read_slots(board)
        st.dropped_seeds = self._read_dropped_seeds(board)
        st.mowers = self._read_mowers(board, st.rows) if st.ui == O.UI_PLAYING else {}
        self._read_cursor(board, st)
        if st.ui == O.UI_PLAYING:
            self._read_spawn(board, st)

        st.ok = st.ui == O.UI_PLAYING
        if not st.ok:
            st.reason = f"当前界面 ui={st.ui}，不在对局中（3=对局中，1=主菜单）"
        st.notes = list(self.notes)
        return st

    # -- 植物 -----------------------------------------------------------
    def _slot_is_live(self, addr: int, lawn: int | None, board: int) -> bool:
        """槽位里到底有没有真实对象（而不是一个全零的空槽）。

        ⚠️ 这是本项目踩过最深的坑，值得单独写一个函数。PvZ 的对象数组是
        **预分配池**，未使用槽位**全零**；而零值恰好是合法数据 ——
        type=0 是豌豆射手、row=col=0 是 A1、dead=0 是"活着"。于是每个空槽
        都伪装成一个真实单位：实测凭空多出 3 株"R1A 的豌豆射手"、
        5 只"R1 的僵尸且 x=0"。**光靠 dead 标志区分不了，空槽的 dead 也是 0。**

        可靠判据：真实对象在槽首存了 **LawnApp / Board 反指针**
        （实测植物 0x00=0x040AA000=LawnApp、0x04=0x63DA89E0=Board；僵尸同构），
        空槽这两位是 0。要求两个都精确相等，误判概率极低。
        """
        pm = self.pm
        assert pm
        if not lawn:
            return False
        return pm.u32(addr + O.LIVE_LAWN_PTR) == lawn and pm.u32(addr + O.LIVE_BOARD_PTR) == board

    def _read_plants(self, board: int) -> list[Plant]:
        pm = self.pm
        assert pm
        lawn = self.lawn_app_ptr()
        base = pm.u32(board + O.OFF_PLANT)
        if not base:
            self.notes.append("植物数组基址为空")
            return []
        cap = pm.i32(board + O.OFF_PLANT_COUNT_MAX) or 0
        used = pm.i32(board + O.OFF_PLANT_NEXT_POS) or 0
        # 用 max(容量, 已用, 下限) 作上界：两个字段都可能短暂落后于实际，
        # 取大的那个更安全。多读出来的槽位会被 `_slot_is_live()` 精确剔除，
        # 所以"多读几个"没有副作用（和之前靠 fudge 系数硬凑是两回事）。
        # ⚠️ 那个 128 的下限是必要的：实测杂交版这两个字段读出来是
        # `cap=8 used=7`，而**草坪上真的有 8 株以上植物** —— 光按 8 扫会漏。
        # 好在 `_slot_is_live()` 要求槽首同时存着 LawnApp 和 Board 反指针，
        # 这个判据足够强，多扫几十个槽不会引入假植物。
        limit = max(cap, used, 128)
        if not (0 < limit <= 2048):
            self.notes.append(f"植物数组为空（cap={cap} used={used}）")
            return []

        out: list[Plant] = []
        for i in range(limit):
            a = base + i * O.PLANT_STRUCT
            if not self._slot_is_live(a, lawn, board):
                continue
            if pm.u8(a + O.P_DEAD) or pm.u8(a + O.P_SQUISHED):
                continue
            t = pm.i32(a + O.P_TYPE)
            r = pm.i32(a + O.P_ROW)
            c = pm.i32(a + O.P_COL)
            if t is None or r is None or c is None:
                continue
            if not (0 <= t < 4096) or not (0 <= r < 12) or not (0 <= c < 12):
                continue
            out.append(
                Plant(
                    index=i,
                    row=r,
                    col=c,
                    type_id=t,
                    imitater=pm.i32(a + O.P_IMITATER) if pm.i32(a + O.P_IMITATER) is not None else -1,
                    asleep=bool(pm.u8(a + O.P_ASLEEP)),
                    hp=self._health(a + O.P_HP, a + O.P_MAX_HP),
                    recently_eaten=(pm.i32(a + O.P_RECENTLY_EATEN) or 0) > 0,
                )
            )
        return out

    # -- 僵尸 -----------------------------------------------------------
    def _read_zombies(self, board: int) -> list[Zombie]:
        pm = self.pm
        assert pm
        lawn = self.lawn_app_ptr()
        base = pm.u32(board + O.OFF_ZOMBIE)
        if not base:
            self.notes.append("僵尸数组基址为空")
            return []
        cap = pm.i32(board + O.OFF_ZOMBIE_COUNT_MAX) or 0
        nxt = pm.i32(board + O.OFF_ZOMBIE_NEXT_POS) or 0
        live = pm.i32(board + O.OFF_ZOMBIE_COUNT) or 0
        limit = max(cap, nxt, live)
        if not (0 < limit <= 512):
            self.notes.append(f"僵尸数组为空（cap={cap} next={nxt} live={live}）")
            return []

        out: list[Zombie] = []
        for i in range(limit):
            a = base + i * O.ZOMBIE_STRUCT
            if not self._slot_is_live(a, lawn, board):
                continue
            if pm.u8(a + O.Z_DEAD):
                continue
            t = pm.i32(a + O.Z_TYPE)
            r = pm.i32(a + O.Z_ROW)
            if t is None or r is None:
                continue
            if not (0 <= t < 4096) or not (0 <= r < 12):
                continue
            x = pm.f32(a + O.Z_X)
            if x is not None and not (-2000.0 < x < 5000.0):
                x = None
            out.append(
                Zombie(index=i, row=r, type_id=t, x=x, phase=pm.i32(a + O.Z_PHASE),
                       hp=self._health(a + O.Z_HP, a + O.Z_MAX_HP),
                       armor_hp=self._armor_health(a),
                       friendly=pm.u8(a + O.Z_FRIENDLY) == 1)
            )
        return out

    def _health(self, address: int, max_address: int) -> int | None:
        hp, maximum = self.pm.i32(address), self.pm.i32(max_address)
        if hp is None or maximum is None or not (0 <= hp <= maximum <= 1000000):
            return None
        return hp

    def _armor_health(self, address: int) -> int | None:
        helmet = self._health(address + O.Z_HELM_HP, address + O.Z_HELM_MAX_HP)
        shield = self._health(address + O.Z_SHIELD_HP, address + O.Z_SHIELD_MAX_HP)
        return helmet + shield if helmet is not None and shield is not None else None

    def _read_mowers(self, board: int, rows: int) -> dict[int, bool]:
        """Validate object pool before treating absent rows as lost mowers."""
        pm = self.pm
        base = pm.u32(board + O.OFF_MOWER)
        cap = pm.i32(board + O.OFF_MOWER_COUNT_MAX)
        count = pm.i32(board + O.OFF_MOWER_COUNT)
        if not base or cap is None or count is None or not (0 < cap <= 64 and 0 <= count <= cap):
            return {}
        lawn = self.lawn_app_ptr()
        seen = {}
        live = 0
        for i in range(cap):
            a = base + i * O.MOWER_STRUCT
            if not self._slot_is_live(a, lawn, board):
                continue
            row, dead, state = pm.i32(a + O.M_ROW), pm.u8(a + O.OFF_MOWER_DEAD), pm.i32(a + O.M_STATE)
            if row is None or not 0 <= row < rows or dead not in (0, 1) or state not in range(4):
                return {}  # incompatible layout: never invent lost lanes
            live += dead == 0
            ready = dead == 0 and state == O.M_READY
            seen[row] = seen.get(row, False) or ready
        if not seen or live != count:
            return {}
        return {r: seen.get(r, False) for r in range(rows)}

    # -- 种子栏 ---------------------------------------------------------
    def _read_slots(self, board: int) -> list[SeedSlot]:
        pm = self.pm
        assert pm
        bank = pm.u32(board + O.OFF_SLOT)
        if not bank:
            self.notes.append("种子栏基址为空")
            return []
        count = pm.i32(bank + O.S_COUNT)
        if count is None or not (0 < count <= 20):
            self.notes.append(f"种子栏格数异常: {count}")
            count = 10
        out: list[SeedSlot] = []
        for i in range(count):
            a = bank + i * O.SLOT_STRUCT
            t = pm.i32(a + O.S_SEED_TYPE)
            cd_left = pm.i32(a + O.S_CD_PAST) or 0
            cd_total = pm.i32(a + O.S_CD_TOTAL) or 0
            if t is None or not (-1 <= t < 4096):
                continue
            out.append(SeedSlot(index=i, type_id=t, cd_left=cd_left, cd_total=cd_total))
        return out

    # -- 光标 -----------------------------------------------------------
    def _read_dropped_seeds(self, board):
        pm=self.pm
        base=pm.u32(board+O.OFF_COINS)
        cap=pm.i32(board+O.OFF_COINS_CAP)
        if not base or cap is None or not 0 <= cap <= 1024:
            return []
        result=[]
        lawn=self.lawn_app_ptr()
        for i in range(cap):
            a=base+i*O.COIN_STRUCT
            if not self._slot_is_live(a,lawn,board):continue
            if pm.u8(a+0x38)!=0 or pm.u8(a+0x50)!=0:continue
            if pm.i32(a+0x58)!=O.COIN_USABLE_SEED:continue
            tid=pm.i32(a+0x68);x=pm.f32(a+0x24);y=pm.f32(a+0x28)
            w=pm.i32(a+0x10);h=pm.i32(a+0x14)
            if (tid is not None and 0<=tid<4096 and x is not None and y is not None
                    and math.isfinite(x) and math.isfinite(y) and -100<x<1000 and -100<y<700
                    and w is not None and h is not None and 10<=w<=150 and 10<=h<=150):
                result.append(DroppedSeed(i,tid,x,y,w,h))
        return result

    def _read_cursor(self, board: int, st: "BoardState") -> None:
        """读"手上拿着什么"。

        `CursorObject+0x30` 是 **mCursorType 枚举**（反编译 ConstEnums.h）：
        0=空手、1=手持种子、6=手持铲子。`+0x24` 卡槽下标、`+0x28` type_id
        只在手持种子时有意义。三者在"点卡是否成功"和"卡槽↔名字绑定"两件事上
        都是 ground truth（见 `offsets.OFF_CURSOR` 的长注释）。
        铲子支持（回收高坚果）需要区分"手持种子"和"手持铲子"。
        """
        pm = self.pm
        assert pm
        cur = pm.u32(board + O.OFF_CURSOR)
        if not cur:
            return
        grab = pm.i32(cur + O.C_GRAB)
        st.held_cursor = grab if grab is not None else 0
        st.holding = st.held_cursor != O.CUR_NORMAL
        if st.held_cursor in (O.CUR_PLANT_FROM_BANK,O.CUR_PLANT_FROM_DROP):
            slot = pm.i32(cur + O.C_SLOT)
            typ = pm.i32(cur + O.C_TYPE)
            st.held_slot = slot if slot is not None else -1
            st.held_type = typ if typ is not None else -1

    # -- 出怪/波次 -------------------------------------------------------
    def _read_spawn(self, board: int, st: "BoardState") -> None:
        """读出怪表与"还没出"的估算（信息层，**语义在杂交版未实战核验**）。

        出处：pvztoolkit `GetSpawnList()` 读 `ReadMemory<int,1000>({lawn, board,
        spawn_list})` —— Board+0x6B4 是指向 1000 个 int 的指针，内容是本关的
        僵尸 type 序列，-1 结尾。"已经出了多少"没有权威字段，这里用僵尸池的
        分配游标（OFF_ZOMBIE_NEXT_POS）估算；杂交版池语义未定，所以：
          * 任何一步越界/读失败 -> 全部保持 None；
          * 结果只进 Jev 的 state（标 unverified），**不驱动任何确定性逻辑**。
        """
        pm = self.pm
        assert pm
        ptr = pm.u32(board + O.OFF_SPAWN_LIST)
        if not ptr:
            return
        entries: list[int] = []
        for i in range(1000):
            v = pm.i32(ptr + i * 4)
            if v is None:
                return
            if v < 0:
                break
            if v > 4095:
                return  # 明显不是 type 序列，整表作废
            entries.append(v)
        if not entries:
            return
        st.spawn_total = len(entries)
        nxt = pm.i32(board + O.OFF_ZOMBIE_NEXT_POS)
        if nxt is not None and 0 <= nxt <= len(entries):
            st.spawn_spawned = nxt
            upcoming = entries[nxt:]
            st.spawn_upcoming = len(upcoming)
            kinds: dict[int, int] = {}
            for t in upcoming[:300]:
                kinds[t] = kinds.get(t, 0) + 1
            st.spawn_upcoming_kinds = kinds
        flags = [pm.u8(board + O.OFF_SPAWN_TYPE + i) for i in range(33)]
        if all(f in (0, 1) for f in flags) and any(flags):
            st.spawnable_types = [i for i, f in enumerate(flags) if f]

    # -- 诊断 -----------------------------------------------------------
    def slot_variants(self, board: int | None = None) -> list[dict]:
        """种子栏布局存在歧义（指针 vs 内联）。返回各解释的原始读数供 probe 判断。"""
        pm = self.pm
        if pm is None:
            return []
        board = board or self.board_ptr()
        if not board:
            return []
        raw = pm.u32(board + O.OFF_SLOT)
        out = []
        for label, bank in (("deref(*(Board+0x144))", raw), ("inline(Board+0x144)", board + O.OFF_SLOT)):
            if not bank:
                out.append({"variant": label, "bank": None})
                continue
            count = pm.i32(bank + O.S_COUNT)
            sample = []
            for i in range(6):
                a = bank + i * O.SLOT_STRUCT
                sample.append(
                    {
                        "i": i,
                        "seed_type": pm.i32(a + O.S_SEED_TYPE),
                        "cd_left": pm.i32(a + O.S_CD_PAST),
                        "cd_total": pm.i32(a + O.S_CD_TOTAL),
                        "type_im": pm.i32(a + O.S_SEED_TYPE_IM),
                    }
                )
            out.append(
                {
                    "variant": label,
                    "bank": f"0x{bank:X}",
                    "count": count,
                    "sample": sample,
                }
            )
        return out


def placement_delta(before, after, type_id, row, col):
    """Require a new matching plant, not just an occupied cell (e.g. an old pad)."""
    old = {(p.index,p.type_id,p.row,p.col) for p in before.plants}
    new = [p for p in after.plants if p.type_id == type_id
           and (p.index,p.type_id,p.row,p.col) not in old]
    return any(p.cell == (row,col) for p in new), [p.cell for p in new if p.cell != (row,col)]

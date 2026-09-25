"""从运行中的 PvZ 进程读出战场状态。

只读，不写游戏内存——所有"修改游戏"的能力都刻意不实现。
读失败一律降级为 None 并记录到 notes，绝不抛异常打断循环。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import offsets as O
from .win32 import ProcessMemory, find_pid, module_base

PROCESS_NAMES = [
    "PlantsVsZombies.exe",
    "PlantsVsZombies",
    "pvzHE-Launcher.exe",
    "pvzHE-Launcher",
]


# ---------------------------------------------------------------- 数据类
@dataclass
class Plant:
    index: int
    row: int
    col: int
    type_id: int
    imitater: int = -1
    asleep: bool = False

    @property
    def cell(self) -> tuple[int, int]:
        return self.row, self.col


@dataclass
class Zombie:
    index: int
    row: int
    type_id: int
    x: float | None = None
    phase: int | None = None

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
    rows: int = O.LAWN_ROWS
    cols: int = O.LAWN_COLS
    plants: list[Plant] = field(default_factory=list)
    zombies: list[Zombie] = field(default_factory=list)
    slots: list[SeedSlot] = field(default_factory=list)
    # -- 光标（"手上有没有拿种子"）--------------------------------------
    # 这是判断"点卡有没有成功"的**唯一可靠信号**，见 offsets.OFF_CURSOR 的说明。
    holding: bool = False
    held_slot: int = -1
    held_type: int = -1
    notes: list[str] = field(default_factory=list)

    # --- 派生视图 -----------------------------------------------------
    def occupancy(self) -> dict[tuple[int, int], list[Plant]]:
        g: dict[tuple[int, int], list[Plant]] = {}
        for p in self.plants:
            g.setdefault(p.cell, []).append(p)
        return g

    def empty_cells(self) -> list[tuple[int, int]]:
        occ = self.occupancy()
        return [
            (r, c)
            for r in range(self.rows)
            for c in range(self.cols)
            if (r, c) not in occ
        ]

    def zombies_in_lane(self, row: int) -> list[Zombie]:
        return [z for z in self.zombies if z.row == row]

    def plants_in_lane(self, row: int) -> list[Plant]:
        return [p for p in self.plants if p.row == row]


# ---------------------------------------------------------------- 读取器
class BoardReader:
    """附着到游戏进程并读取 Board。"""

    def __init__(self, pid: int | None = None):
        self.pid = pid
        self.pm: ProcessMemory | None = None
        self.notes: list[str] = []
        self.attach()

    # -- 生命周期 -------------------------------------------------------
    def attach(self) -> bool:
        self.notes = []
        if self.pm is not None:
            self.pm.close()
            self.pm = None
        pid = self.pid or find_pid(PROCESS_NAMES)
        if pid is None:
            self.notes.append("未找到游戏进程（PlantsVsZombies.exe）")
            return False
        self.pid = pid
        try:
            base = module_base(pid) or O.PVZ_IMAGE_BASE
            self.pm = ProcessMemory(pid, base=base)
        except OSError as exc:
            self.notes.append(str(exc))
            return False
        if self.pm.base != O.PVZ_IMAGE_BASE:
            self.notes.append(
                f"模块基址 0x{self.pm.base:X}（非 0x{O.PVZ_IMAGE_BASE:X}），已按重定位换算"
            )
        return True

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

        st.plants = self._read_plants(board)
        st.zombies = self._read_zombies(board)
        st.slots = self._read_slots(board)
        self._read_cursor(board, st)

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
                Zombie(index=i, row=r, type_id=t, x=x, phase=pm.i32(a + O.Z_PHASE))
            )
        return out

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
    def _read_cursor(self, board: int, st: "BoardState") -> None:
        """读"手上拿着什么"。

        `CursorObject+0x30` 是 0/1 的抓取标志；`+0x24` 是卡槽下标、`+0x28` 是
        type_id。这三者在"点卡是否成功"和"卡槽↔名字绑定"两件事上都是 ground truth
        （见 `offsets.OFF_CURSOR` 的长注释）。
        """
        pm = self.pm
        assert pm
        cur = pm.u32(board + O.OFF_CURSOR)
        if not cur:
            return
        grab = pm.i32(cur + O.C_GRAB)
        st.holding = bool(grab)
        if st.holding:
            slot = pm.i32(cur + O.C_SLOT)
            typ = pm.i32(cur + O.C_TYPE)
            st.held_slot = slot if slot is not None else -1
            st.held_type = typ if typ is not None else -1

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

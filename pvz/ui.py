"""屏幕几何、坐标换算、抓屏与阳光收集。

PvZ 1.0.0.1051 的客户区是 800x600。所有几何参数都可以被 `data/layout.json`
覆盖 —— 杂交版改了界面（卡槽更多、位置不同），必须能校准而不是硬编码。

抓屏走屏幕 DC 而不是窗口 DC：游戏用 DirectDraw，窗口 DC 在全屏下是黑的。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict, field

from .win32 import Screen, WindowInfo, capture_window, post_click, post_rclick, sendinput_click, post_key

LAYOUT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "layout.json")

# --- 原版 800x600 的默认几何（**杂交版不适用，仅作兜底**）-----------------
# ⚠️ 实测杂交版全屏 2560x1600 时，草坪宽高比 ≈0.76，和原版 80:100=0.80 对不上，
#    说明杂交版重画过草坪底图。这些原版值套上去点击坐标会整体偏掉。
#    正确做法：跑 tools/measure_layout.py 量出真实值写进 data/layout.json。
BASE_W, BASE_H = 800, 600
GRID_LEFT, GRID_TOP = 40, 80
CELL_W, CELL_H = 80, 100
CARD_FIRST_X, CARD_Y = 78, 8      # 第一张卡片左上角
CARD_W, CARD_H = 50, 70
CARD_STRIDE = 52
CARD_ROWS = 1                      # 杂交版可能有多行卡槽


@dataclass
class Layout:
    """界面几何。

    `base_w/base_h` 是**这些数值被量测时的客户区尺寸**。默认 800x600（原版），
    但杂交版必须用 tools/measure_layout.py 量出真实值、并把 base 设成当时的
    客户区尺寸（例如 2560x1600）。`scale()` 会把当前客户区换算回量测基准，
    这样窗口尺寸变了也不会错位。
    """

    base_w: int = BASE_W
    base_h: int = BASE_H
    client_w: int = BASE_W
    client_h: int = BASE_H
    grid_left: int = GRID_LEFT
    grid_top: int = GRID_TOP
    cell_w: int = CELL_W
    cell_h: int = CELL_H
    card_first_x: int = CARD_FIRST_X
    card_y: int = CARD_Y
    card_w: int = CARD_W
    card_h: int = CARD_H
    card_stride: int = CARD_STRIDE
    card_rows: int = CARD_ROWS
    card_per_row: int = 9
    sun_display: tuple[int, int, int, int] = (18, 52, 90, 32)  # 阳光数字区域(客户区)
    # 暂停菜单"返回游戏"按钮中心（客户区坐标）。None 表示未知，需要 tools/scan_green.py 量。
    pause_resume_xy: tuple[int, int] | None = None
    # 铲子按钮中心（客户区坐标，2560x1600 基准下量测）。
    # 2026-09-26 从两份不同对局的截图（out/board.png、out/live_now2.png）里
    # 用金色像素定位：铲子 plaque 在卡条右端、最后一张卡之后，
    # 金色区域 x≈2150..2294、y≈5..196，中心 ≈ (2220, 95)。
    # 和 pause_resume_xy 一样按 scale() 缩放，分辨率变了也不会错位。
    shovel_xy: tuple[int, int] = (2220, 95)

    board_rows: int = 5
    pool_cell_h: float | None = None  # Optional separately measured pool row spacing.

    def configure_board(self, board):
        self.board_rows = board.rows

    def row_height(self):
        # Original logical grid: grass 100, pool 85 pixels. Keep measured scaling.
        return (self.pool_cell_h if self.pool_cell_h is not None else self.cell_h * 0.85) if self.board_rows == 6 else self.cell_h

    # -- 载入 / 保存 -----------------------------------------------------
    @classmethod
    def load(cls, path: str = LAYOUT_FILE, client_size: tuple[int, int] | None = None) -> "Layout":
        lay = cls()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            for k, v in raw.items():
                if hasattr(lay, k):
                    if k in ("sun_display", "pause_resume_xy", "shovel_xy") and isinstance(v, list):
                        v = tuple(v)
                    setattr(lay, k, v)
        except (OSError, ValueError):
            pass
        if client_size and client_size[0] > 0 and client_size[1] > 0:
            lay.client_w, lay.client_h = client_size
        else:
            # ⚠️ 窗口最小化时 client_size 是 (0,0)，**不能**用它 ——
            # 否则 scale() = (0,0)，cell_center/card_center 全返回 (0,0)，
            # 每次点击都落到客户区左上角，表现为"点击完全没反应"。
            # 这种情况回退到量测基准，至少坐标还是合理的。
            lay.client_w, lay.client_h = lay.base_w, lay.base_h
        return lay

    def save(self, path: str = LAYOUT_FILE) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, ensure_ascii=False, indent=2)
        return path

    # -- 坐标 -----------------------------------------------------------
    def scale(self) -> tuple[float, float]:
        return self.client_w / self.base_w, self.client_h / self.base_h

    def cell_center(self, row: int, col: int) -> tuple[int, int]:
        sx, sy = self.scale()
        x = self.grid_left + col * self.cell_w + self.cell_w / 2
        y = self.grid_top + (row + 0.5) * self.row_height()
        return int(x * sx), int(y * sy)

    def dropped_seed_center(self, seed):
        sx,sy=self.scale()
        # Coin positions are logical board coordinates, independent of row spacing.
        return (int((self.grid_left+(seed.x+seed.width/2-40)*self.cell_w/80)*sx),
                int((self.grid_top+(seed.y+seed.height/2-80)*self.cell_h/100)*sy))

    def card_center(self, index: int) -> tuple[int, int]:
        sx, sy = self.scale()
        per_row = self.card_per_row
        r, c = divmod(index, per_row)
        x = self.card_first_x + c * self.card_stride + self.card_w / 2
        y = self.card_y + r * (self.card_h + 4) + self.card_h / 2
        return int(x * sx), int(y * sy)

    def card_rect(self, index: int) -> tuple[int, int, int, int]:
        sx, sy = self.scale()
        per_row = self.card_per_row
        r, c = divmod(index, per_row)
        x = self.card_first_x + c * self.card_stride
        y = self.card_y + r * (self.card_h + 4)
        return (int(x * sx), int(y * sy), int(self.card_w * sx), int(self.card_h * sy))

    def lawn_rect(self, rows: int | None = None, cols: int = 9) -> tuple[int, int, int, int]:
        # 行数默认取 configure_board() 同步进来的 board_rows（泳池=6）。
        # ⚠️ 写死 5 的后果：泳池第 6 行的天降阳光永远不在扫描区里（2026-09-26 盲区）。
        if rows is None:
            rows = self.board_rows
        sx, sy = self.scale()
        return (
            int(self.grid_left * sx),
            int(self.grid_top * sy),
            int((self.grid_left + cols * self.cell_w) * sx),
            int((self.grid_top + rows * self.row_height()) * sy),
        )

    def pause_resume(self) -> tuple[int, int] | None:
        """暂停菜单「返回游戏」按钮中心的**兜底**坐标（客户区坐标，已按尺寸缩放）。

        为什么必须缩放：存的是量测基准尺寸下的值。实测 2560x1600 时 y 差 90px
        就从按钮上滑到下方石碑台阶、点了完全没反应 —— 分辨率一变差得更多。
        正常路径是 `find_pause_resume()` 从像素里量；这个只在量不到时兜底。
        """
        if self.pause_resume_xy is None:
            return None
        sx, sy = self.scale()
        return int(self.pause_resume_xy[0] * sx), int(self.pause_resume_xy[1] * sy)

    def shovel_button(self) -> tuple[int, int]:
        """铲子按钮中心（客户区坐标，已按尺寸缩放）。见 shovel_xy 字段的量测说明。"""
        sx, sy = self.scale()
        return int(self.shovel_xy[0] * sx), int(self.shovel_xy[1] * sy)


# ---------------------------------------------------------------- 点击
class Clicker:
    """默认后台点击（PostMessage）。全屏 DDraw 下若无效可切 foreground=True。"""

    def __init__(self, win: WindowInfo, foreground: bool = False, settle: float = 0.06):
        self.win = win
        self.foreground = foreground
        self.settle = settle
        self.history: list[tuple[int, int, str]] = []

    def click_client(self, x: int, y: int, note: str = "") -> None:
        x, y = int(x), int(y)
        if self.foreground:
            sx, sy = self.win.client_to_screen(x, y)
            sendinput_click(sx, sy, self.settle)
        else:
            post_click(self.win.hwnd, x, y, self.settle)
        self.history.append((x, y, note))

    def click_grid(self, row: int, col: int, layout: Layout, note: str = "") -> tuple[int, int]:
        x, y = layout.cell_center(row, col)
        self.click_client(x, y, note or f"grid r{row}c{col}")
        return x, y

    def click_card(self, index: int, layout: Layout, note: str = "") -> tuple[int, int]:
        x, y = layout.card_center(index)
        self.click_client(x, y, note or f"card {index}")
        return x, y

    def shovel_hotkey(self):
        post_key(self.win.hwnd,0x31)

    def click_shovel(self, layout: Layout, note: str = "pick up shovel") -> tuple[int, int]:
        """点铲子按钮。拿起成功与否由调用方读 `held_cursor == 6` 验证。"""
        x, y = layout.shovel_button()
        self.click_client(x, y, note)
        return x, y

    def pick_and_place(self, card_index: int, row: int, col: int, layout: Layout,
                       gap: float = 0.35) -> dict:
        """选卡 -> 等 gap -> 放格子。

        ⚠️ 中间这个 gap 是**必需的**，不是"保险起见"。PvZ 的"手持种子"状态是在
        **tick 里**更新的（~16ms 一帧），而 PostMessage 投递的消息会被游戏在
        一个 tick 内成批处理完。两次点击之间不隔开的话，游戏会在同一帧里处理完
        "点卡"和"点格子"，此时"手持种子"还没建立，落点判定直接失败 ——
        实测表现是 **阳光被扣了、植物却没种上**，极容易被误判成"点击没生效"。
        （tools/click_test.py 里因为两次点击之间恰好有 0.35s 间隔才成功，
        而 agent 里是连点，所以一直种不上。）
        """
        cx, cy = self.click_card(card_index, layout, f"pick card {card_index}")
        time.sleep(gap)
        px, py = self.click_grid(row, col, layout, f"place r{row}c{col}")
        return {"card": card_index, "card_xy": (cx, cy), "grid": (row, col), "grid_xy": (px, py)}

    def cancel_seed(self, note: str = "cancel held seed") -> None:
        """右键取消"手持种子"。

        为什么必须做：点种子卡的一瞬间游戏就扣了阳光；如果随后落点失败，种子会
        一直举在手上。而收集阳光的点击正好落在草坪上 —— 下一次收阳光就会把这颗
        种子种到某个阳光的位置（不花额外阳光，所以最难察觉）。
        右键点屏幕中心：那里既不在卡槽也不在草坪格子外，安全。
        """
        x = self.win.client_size[0] // 2
        y = self.win.client_size[1] // 2
        if self.foreground:
            sx, sy = self.win.client_to_screen(x, y)
            sendinput_click(sx, sy, self.settle)
        else:
            post_rclick(self.win.hwnd, x, y, self.settle)
        self.history.append((x, y, note))


# ---------------------------------------------------------------- 阳光
class SunTracker:
    """阳光收集的**回读验证与假阳性抑制**。

    为什么需要：屏幕上不止阳光是亮黄色的——向日葵女王/阳光向日葵的贴图、
    黄油、部分水面前照都过 `find_suns` 的颜色闸。2026-09-26 泳池局实测：
    244 个循环点了 535 次"阳光"，阳光却几乎没涨 —— 大量点击反复砸在
    持久黄色贴图上，真阳光反而轮不到额度。

    原理：点击前后各读一次内存阳光差值。阳光**只会**因点击而增加（种植
    扣钱，且收集与种植在主循环里顺序执行、不同帧），所以：
      * `gained >= 25×点击数`  → 这批全是真阳光，清空可疑计数；
      * `gained < 25`          → 这批全是假阳性，每个位置计一次 miss；
      * 介于两者之间           → 混合，无法归因，不动计数（保守）。
    同一位置累计 miss ≥ 3（约 3 个收集周期）→ 抑制 90s。真阳光被抑制的
    代价（一颗 25）远小于假阳性每轮吃满额度的代价。
    """

    KEY_PX = 48          # 位置量化：key = (x//48, y//48)
    MISS_LIMIT = 3
    BAN_S = 90.0

    def __init__(self, clock=None):
        self._clock = clock or time.time
        self._miss: dict[tuple[int, int], int] = {}
        self._banned_until: dict[tuple[int, int], float] = {}

    @classmethod
    def key_of(cls, x: int, y: int) -> tuple[int, int]:
        return (int(x) // cls.KEY_PX, int(y) // cls.KEY_PX)

    def banned_keys(self) -> set:
        now = self._clock()
        self._banned_until = {k: t for k, t in self._banned_until.items() if t > now}
        return set(self._banned_until)

    def forgive_cell(self, row: int, col: int, layout) -> None:
        """解除某格周围 3x3 的假阳性抑制（2026-09-26）。

        用途：铲掉换阳光的返还**落在草坪上要拾取**（夜战实测 9 次 salvage
        5 次即时读数为 0），而该格之前很可能因"点到植物贴图没涨阳光"被
        计入抑制 —— 不解除的话，返还的阳光会在抑制期内烂掉。"""
        cx, cy = layout.cell_center(row, col)
        k = self.key_of(cx, cy)
        for dk in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1),
                   (1, 1), (1, -1), (-1, 1), (-1, -1)):
            self._miss.pop((k[0] + dk[0], k[1] + dk[1]), None)
            self._banned_until.pop((k[0] + dk[0], k[1] + dk[1]), None)

    def feedback(self, hits: list[tuple[int, int]], gained: int | None,
                 per_sun: int = 25) -> None:
        if gained is None or not hits:
            return
        keys = [self.key_of(x, y) for x, y, *_ in hits]
        if gained >= per_sun * len(hits):
            for k in keys:
                self._miss.pop(k, None)
            return
        if gained >= per_sun:
            return          # 混合命中，无法归因，不动计数
        now = self._clock()
        for k in keys:
            n = self._miss.get(k, 0) + 1
            self._miss[k] = n
            if n >= self.MISS_LIMIT:
                self._banned_until[k] = now + self.BAN_S
                self._miss.pop(k, None)


def find_suns(shot: Screen, layout: Layout, min_blob: int = 24) -> list[tuple[int, int, int]]:
    """在草坪区域找阳光（亮黄色团块）。返回客户区坐标的 [(x, y, 像素数), ...]。

    纯 Python 实现，按 step 降采样；不引入 numpy/PIL。
    """
    lx0, ly0, lx1, ly1 = layout.lawn_rect()
    # 转到截图局部坐标
    x0 = max(0, lx0 - shot.x)
    y0 = max(0, ly0 - shot.y)
    x1 = min(shot.w, lx1 - shot.x)
    y1 = min(shot.h, ly1 - shot.y)
    if x1 <= x0 or y1 <= y0:
        return []

    step = 3
    w = (x1 - x0) // step
    h = (y1 - y0) // step
    if w <= 0 or h <= 0:
        return []

    px = shot.pixels
    stride = shot.w * 4
    mask = bytearray(w * h)
    for gy in range(h):
        sy = y0 + gy * step
        rowbase = sy * stride
        mbase = gy * w
        for gx in range(w):
            i = rowbase + (x0 + gx * step) * 4
            b, g, r = px[i], px[i + 1], px[i + 2]
            if r > 185 and g > 165 and b < 165 and (r + g - 2 * b) > 110:
                mask[mbase + gx] = 1

    # 连通域（4 邻域，迭代 BFS）
    seen = bytearray(w * h)
    blobs: list[tuple[int, int, int]] = []
    for start in range(w * h):
        if not mask[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        cells = []
        while stack:
            cur = stack.pop()
            cells.append(cur)
            cy, cx = divmod(cur, w)
            for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                if 0 <= nx < w and 0 <= ny < h:
                    ni = ny * w + nx
                    if mask[ni] and not seen[ni]:
                        seen[ni] = 1
                        stack.append(ni)
        n = len(cells)
        if n >= max(1, min_blob // (step * step)):
            sx = sum(c % w for c in cells) / n
            sy = sum(c // w for c in cells) / n
            blobs.append((x0 + int(sx * step), y0 + int(sy * step), n))

    blobs.sort(key=lambda t: (-t[2], t[0]))
    # 合并过近的团块
    merged: list[tuple[int, int, int]] = []
    for bx, by, n in blobs:
        if all((bx - mx) ** 2 + (by - my) ** 2 > 40 ** 2 for mx, my, _ in merged):
            merged.append((bx, by, n))
    # 合并过近的团块。按**团块大小**优先合并/收集（大的是真阳光，小的是噪声）：
    # ⚠️ 不能按 x 排序——2026-09-26 泳池局实测：女王/向日葵都在左侧且是黄色
    #    贴图，"从左到右"让假阳性每轮吃满点击额度，真阳光在右边烂掉。
    merged.sort(key=lambda t: (-t[2], t[0]))
    return merged


def collect_suns(shot: Screen, layout: Layout, clicker: Clicker, max_click: int = 4,
                 banned: set | None = None) -> list[tuple[int, int]]:
    """点击收集阳光。`banned` 是被抑制的位置 key（(x//48, y//48)），见 agent 的
    SunTracker：点了但阳光没涨的位置是持久黄色假阳性（植物贴图/黄油），
    不能每轮反复浪费点击额度。"""
    hits = []
    for x, y, _n in find_suns(shot, layout):
        if banned and (x // 48, y // 48) in banned:
            continue
        clicker.click_client(x, y, "collect sun")
        hits.append((x, y))
        if len(hits) >= max_click:
            break
    return hits


def grab(win: WindowInfo) -> Screen | None:
    return capture_window(win)


# ---------------------------------------------------------------- 暂停菜单
def _is_button_green(r: int, g: int, b: int) -> bool:
    return g > 110 and g > r + 35 and g > b + 35


def find_green_buttons(
    shot: Screen,
    x0: int, x1: int, y0: int, y1: int,
    min_px: int = 6,
    min_height: int = 12,
) -> list[dict]:
    """在指定区域里找"绿色文字按钮"，返回按 y 排序的按钮列表。

    PvZ 暂停菜单的按钮是**竖排**的（查看图鉴 / 重新开始 / 主菜单 / 返回游戏），
    按估算坐标盲点非常危险 —— 算错一点就会点到"重新开始"或"主菜单"，毁掉进度。
    所以位置必须从像素里量出来。

    每个按钮返回 {y0,y1,cy,x0,x1,cx,peak}（截图局部坐标）。
    """
    sx0 = max(0, x0 - shot.x)
    sx1 = min(shot.w, x1 - shot.x)
    sy0 = max(0, y0 - shot.y)
    sy1 = min(shot.h, y1 - shot.y)
    if sx1 <= sx0 or sy1 <= sy0:
        return []

    px, stride = shot.pixels, shot.w * 4
    rows: list[int] = []
    xhits: list[tuple[int, int]] = []
    for y in range(sy0, sy1):
        n = 0
        lo = hi = -1
        base = y * stride
        for x in range(sx0, sx1):
            i = base + x * 4
            b, g, r = px[i], px[i + 1], px[i + 2]
            if _is_button_green(r, g, b):
                n += 1
                if lo < 0:
                    lo = x
                hi = x
        rows.append(n)
        xhits.append((lo, hi))

    segs: list[dict] = []
    cur = None
    for idx, n in enumerate(rows):
        y = sy0 + idx
        if n >= min_px:
            if cur is None:
                cur = [y, y, 0]
            cur[1] = y
            cur[2] = max(cur[2], n)
        else:
            if cur is not None:
                if cur[1] - cur[0] >= min_height:
                    segs.append({"y0": cur[0], "y1": cur[1], "cy": (cur[0] + cur[1]) // 2,
                                 "peak": cur[2], "_idx": (cur[0], cur[1])})
                cur = None
    if cur is not None and cur[1] - cur[0] >= min_height:
        segs.append({"y0": cur[0], "y1": cur[1], "cy": (cur[0] + cur[1]) // 2,
                     "peak": cur[2], "_idx": (cur[0], cur[1])})

    for s in segs:
        a, b = s.pop("_idx")
        xs = [(lo, hi) for lo, hi in xhits[a - sy0: b - sy0 + 1] if lo >= 0]
        if xs:
            blo = min(t[0] for t in xs)
            bhi = max(t[1] for t in xs)
            s.update(x0=blo + shot.x, x1=bhi + shot.x, cx=(blo + bhi) // 2 + shot.x)
        else:
            s.update(x0=None, x1=None, cx=None)
    segs.sort(key=lambda s: s["cy"])
    return segs


# ⚠️ 杂交版 v3.9.9 的暂停菜单是**自定义覆盖层**，和原版完全不同：
#    一块居中的石碑，上面是金色标题「游戏暂停」+ 海盗僵尸立绘 + 金色提示
#    「点击返回游戏」，最下面**只有一个按钮「返回游戏」**（浅蓝灰底 + 亮绿字）。
#    原版那种"查看图鉴/重新开始/主菜单"竖排四按钮在这里**根本不存在**
#    —— 图鉴被挪到了右上角常驻入口。
#    教训：曾按"必须恰好 4 个按钮"当安全阀，结果把金色标题误当成按钮、
#    又用 layout 里手工估的旧坐标 (1270,1288) 兜底，y 偏低了 90px，
#    正好落在按钮下方的石碑台阶上，点了等于没点。
PAUSE_RESUME_HINT = "返回游戏"


def find_pause_resume(shot: Screen, fallback: tuple[int, int] | None = None) -> tuple[int, int] | None:
    """定位暂停菜单的「返回游戏」按钮中心（截图坐标）。

    判据：**中间窄带里最下面那个"亮绿文字"段**。

    为什么是"绿色文字"而不是"浅色按钮框"：实测按钮框的面色 (95,97,129) 和
    它下方石碑台阶的色 (95,97,129) 几乎一样，亮度分不开；但绿色文字是
    **整屏唯一**的（两行提示都是金色），所以绿色反而是最干净的锚点。

    为什么用"居中窄带"而不是整屏找绿色：草坪的绿和按钮文字的绿是同一族。
    整屏扫会把整片草坪合并成巨型区域、按钮被吞掉（实测踩过）。石碑水平居中，
    取中间 24% 宽的竖带正好落在石碑内部。

    为什么窄带要"被边界截断就丢弃"：石碑上下有缺口，草坪会透出来，
    这些段的左右边界正好等于窄带边界 —— 据此可以精确剔除。

    为什么还要"水平居中 + 够宽 + 够密"两道额外判据：ESC 菜单里
    「3D 加速」「全屏」后面有**绿色对勾**，也是绿字，实测 w≈53、peak≈36；
    而按钮文字 w≥123、peak≥98 且 **cx 恒等于屏幕中心**（实测 1270~1279，
    中心 1280）。只靠 y 阈值区分是"刚好没踩到"的运气，不能依赖。
    """
    x0 = shot.x + int(shot.w * 0.38)
    x1 = shot.x + int(shot.w * 0.62)
    y0 = shot.y + int(shot.h * 0.14)      # 跳过顶部卡槽栏里的绿色卡面
    y1 = shot.y + int(shot.h * 0.96)
    btns = find_green_buttons(shot, x0, x1, y0, y1, min_px=4, min_height=14)

    mid = shot.x + shot.w / 2
    cands: list[dict] = []
    for b in btns:
        if b["x1"] is None:
            continue
        w = b["x1"] - b["x0"]
        h = b["y1"] - b["y0"]
        if not (90 <= w <= 460 and 14 <= h <= 100):
            continue
        if b["peak"] < 60:
            continue
        # 被窄带左右边界截断的段一定是"草坪透过石碑缺口露出来的绿"
        if b["x0"] <= x0 + 4 and b["x1"] >= x1 - 4:
            continue
        # 按钮文字水平居中（实测 cx 1270~1279 vs 中心 1280），绿对勾不居中
        if abs(b["cx"] - mid) > shot.w * 0.06:
            continue
        # 按钮在石碑下半部（实测 rel_y ≈ 0.52~0.80），太靠上的不是它
        rel_y = (b["cy"] - shot.y) / shot.h
        if not (0.40 <= rel_y <= 0.95):
            continue
        cands.append(b)

    if cands:
        last = cands[-1]      # 最下面那个
        return last["cx"], last["cy"]
    return fallback

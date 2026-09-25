"""量出杂交版的真实界面几何，写进 data/layout.json。

为什么必须量：杂交版全屏 2560x1600 时草坪宽高比 ≈0.76，和原版 80:100=0.80
对不上 —— 它重画过草坪底图。直接套原版 (grid_left=40, cell=80x100,
card_stride=52) 会把点击坐标整体打偏，而 PvZ 里点错格子 = 把植物种错位置。

三个量测锚点，都尽量用"游戏自己算出来的"数据而不是猜：

  1. **行距 cell_h** ← 小推车的 y 间距。
     小推车由游戏按行索引绘制，5 台车给出 4 个间距，中位数就是 cell_h。
     实测 266.7px，4 个间距只有 ±0.5px 抖动 —— 这是全场最硬的数。

  2. **草坪左右外沿** ← 多条水平采样线上"绿色连续段"的中位数。
     必须排除暂停菜单石碑（它会把绿段切断，导致量出半截草坪）。
     左侧是小推车/门廊，右侧是马路，都不是绿色，所以绿段边界=可种植区边界。

  3. **卡槽** ← 就绪卡的底部米色条（写阳光数字那条）。
     冷却中的卡会被整体压暗，米色条不亮 —— 所以只能拿就绪卡定位，
     再用步长外推到全部 16 张。

关于"草坪上下沿"：绿段上沿（232）会被装饰草簇撑大，不能直接当 grid_top。
改用「grid_top = 绿段上沿」并**用 cell_h 反推**行心 —— 见下面的自检。

用法：
    python tools/measure_layout.py             # 只量、只打印
    python tools/measure_layout.py --write     # 量 + 写 data/layout.json
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvz import win32 as W                      # noqa: E402
from pvz.ui import Layout, LAYOUT_FILE          # noqa: E402

PROCESS_NAMES = ["PlantsVsZombies.exe", "pvzHE-Launcher.exe"]

ROWS, COLS = 5, 9
CARD_COUNT = 16


def is_green(px, st, x, y):
    i = y * st + x * 4
    b, g, r = px[i], px[i + 1], px[i + 2]
    return g > 70 and g > r + 18 and g > b + 18


def is_mower_red(px, st, x, y):
    i = y * st + x * 4
    b, g, r = px[i], px[i + 1], px[i + 2]
    return r > 110 and r > g + 55 and r > b + 55


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--save", default="out/measure.png")
    args = ap.parse_args()

    pid = W.find_pid(PROCESS_NAMES)
    win = W.find_game_window(pid) if pid else None
    if not win:
        print("没找到游戏窗口")
        return 1
    if win.client_size[0] <= 0:
        W.restore_window(win.hwnd)
        win = W.find_game_window(pid) or win
    scr = W.capture_window(win)
    if not scr:
        print("抓屏失败（窗口可能仍是最小化）")
        return 1
    scr.save_png(args.save)
    px, st, w, h = scr.pixels, scr.w * 4, scr.w, scr.h
    print(f"抓屏 {w}x{h} @ ({scr.x},{scr.y}) -> {args.save}")

    # ---------------- 1. 草坪左右外沿 ----------------
    # 每列统计绿色像素数；用"占整幅高度比例"判断，低阈值以便把被石碑
    # 盖住一半的中间列也算进来（石碑只盖中间，左右两半仍露着）。
    col_green = [0] * w
    for x in range(0, w):
        n = 0
        for y in range(int(h * 0.15), h, 3):
            if is_green(px, st, x, y):
                n += 1
        col_green[x] = n
    ymax = len(range(int(h * 0.15), h, 3))
    thr = ymax * 0.12
    ok = [col_green[x] >= thr for x in range(w)]

    # 草坪左右沿：**不能**用"最长连续段" —— 暂停菜单石碑会把草坪切成好几段，
    # 选出来的是"石碑右边那块"而不是整片草坪。正确做法是要求一段**连续 span
    # 列都是绿的**才认作草坪边界，这样能跨过石碑留下的碎片化空洞。
    span = 60

    def sustained(vals_ok, reverse=False):
        rng = range(len(vals_ok) - 1, -1, -1) if reverse else range(len(vals_ok))
        run = 0
        for x in rng:
            run = run + 1 if vals_ok[x] else 0
            if run >= span:
                return (x + span - 1) if reverse else (x - span + 1)
        return None

    L = sustained(ok)
    R = sustained(ok, reverse=True)
    if L is None or R is None or R <= L:
        print("没找到草坪（左右各 60 列的连续绿色区域）")
        return 1
    print(f"\n草坪左右: x {L}..{R}  (宽 {R - L + 1})")
    print(f"  9 列 -> 单元宽 {(R - L + 1) / COLS:.2f}px")

    # ---------------- 2. 草坪上下沿（只在石碑左右两侧量）----------------
    # 先找石碑的水平范围：石碑是灰的，在草坪高度范围内大量出现
    stone_xs = []
    for x in range(L, R, 4):
        n = sum(1 for y in range(300, 1400, 20)
                if (lambda i: abs(px[i + 2] - px[i + 1]) < 18 and abs(px[i + 1] - px[i]) < 18
                    and 60 < px[i + 1] < 190)(y * st + x * 4))
        if n >= 40:
            stone_xs.append(x)
    if stone_xs:
        sx0, sx1 = min(stone_xs), max(stone_xs)
        print(f"  暂停菜单石碑水平范围 x {sx0}..{sx1}（量上下沿时跳过）")
    else:
        sx0 = sx1 = -1

    tops, bots = [], []
    for x in range(L + 5, R - 5, 20):
        if sx0 <= x <= sx1:
            continue
        run = None
        best = None
        for y in range(150, h):
            if is_green(px, st, x, y):
                if run is None:
                    run = [y, y]
                run[1] = y
            else:
                if run and (best is None or run[1] - run[0] > best[1] - best[0]):
                    best = tuple(run)
                run = None
        if run and (best is None or run[1] - run[0] > best[1] - best[0]):
            best = tuple(run)
        if best and best[1] - best[0] > 300:
            tops.append(best[0])
            bots.append(best[1])
    T = int(statistics.median(tops)) if tops else int(h * 0.15)
    B = int(statistics.median(bots)) if bots else h - 1
    print(f"草坪上下: y {T}..{B}  (高 {B - T + 1})")

    # ---------------- 3. 小推车 -> 行距 ----------------
    Y0, Y1 = 150, h
    # 窗口要刚好套住小推车那一竖条：太宽会把树/自行车/门廊的红色一起扫进来，
    # 把一台车切成好几段（踩过：16 个碎片、行距中位数变成 76）。
    X0, X1 = max(0, L - 110), min(w, L + 30)
    clusters, cur = [], None
    for y in range(Y0, Y1):
        n = sum(1 for x in range(X0, X1) if is_mower_red(px, st, x, y))
        if n >= 2:
            if cur is None:
                cur = [y, y, 0]
            cur[1] = y
            cur[2] += n
        else:
            if cur and cur[1] - cur[0] >= 12:
                clusters.append(tuple(cur))
            cur = None
    if cur and cur[1] - cur[0] >= 12:
        clusters.append(tuple(cur))
    print(f"\n在 x {X0}..{X1} 检到 {len(clusters)} 个红色块（期望 {ROWS} 个小推车）:")
    centers = []
    for a, b, tot in clusters:
        centers.append((a + b) // 2)
        print(f"   y {a:5d}..{b:5d} 高 {b - a + 1:4d} 像素 {tot:6d} 中心 {(a + b) // 2}")

    # 小推车是"高 25~75、像素数 >=300"的块；第 5 台常和自行车粘连（高 >100）要剔掉
    clean = [c for c in clusters if 25 <= c[1] - c[0] <= 75 and c[2] >= 300]
    cc = [(a + b) // 2 for a, b, _ in clean]
    cell_h = None
    if len(cc) >= 3:
        d = [cc[i + 1] - cc[i] for i in range(len(cc) - 1)]
        cell_h = statistics.median(d)
        print(f"  小推车中心 y = {cc}")
        print(f"  间距 {d} -> 中位数 {cell_h:.1f}  <- cell_h")
    else:
        print("  小推车识别不足，退回用绿段高度/5")
        cell_h = (B - T + 1) / ROWS

    # ---------------- 4. 自检：网格必须放得进屏幕 ----------------
    # 小推车在行心**下方**（实测约 0.25 个格高），所以 grid_top ≈ 绿段上沿。
    grid_top = T
    grid_bottom = grid_top + ROWS * cell_h
    print(f"\n自检: grid_top={grid_top}  grid_bottom={grid_bottom:.0f}  屏幕高={h}")
    if grid_bottom > h:
        print("  ✗ 网格超出屏幕 —— 说明小推车其实在行心附近，改用「以推车为行心」")
        first = min(cc) if cc else grid_top
        grid_top = first - cell_h / 2
        grid_bottom = grid_top + ROWS * cell_h
        print(f"  修正后 grid_top={grid_top:.0f} grid_bottom={grid_bottom:.0f}")
    else:
        print("  ✓ 放得下")

    # ---------------- 5. 卡槽（用就绪卡的米色条）----------------
    # 就绪卡底部有一条亮的米色横条；冷却卡被压暗，条不亮。
    bar_y = None
    for y in range(100, 260):
        n = 0
        for x in range(150, min(w, 2500), 3):
            i = y * st + x * 4
            b, g, r = px[i], px[i + 1], px[i + 2]
            if r > 165 and g > 155 and b > 100 and abs(r - g) < 50 and b < r - 15:
                n += 1
        if bar_y is None or n > bar_y[1]:
            bar_y = (y, n)
    y = bar_y[0]
    print(f"\n卡槽米色条所在行 y={y} (命中 {bar_y[1]})")

    runs, s = [], None
    for x in range(120, min(w, 2500)):
        i = y * st + x * 4
        b, g, r = px[i], px[i + 1], px[i + 2]
        ok = r > 160 and g > 150 and b > 95 and abs(r - g) < 55 and b < r - 10
        if ok:
            if s is None:
                s = x
        else:
            if s is not None and x - s >= 40:
                runs.append((s, x - 1))
            s = None
    if s is not None and 2500 - s >= 40:
        runs.append((s, 2499))
    print(f"  就绪卡米色段 {len(runs)} 段: {runs}")

    card_w = card_stride = card_first_x = None
    if len(runs) >= 2:
        lefts = [a for a, _ in runs]
        ws = [b - a + 1 for a, b in runs]
        d = [lefts[i + 1] - lefts[i] for i in range(len(lefts) - 1)]
        # 步长应为最小间距的整数倍；取最大公约数式的估计
        base = min(d)
        for dd in d:
            if base and abs(dd / base - round(dd / base)) < 0.15:
                continue
            base = min(base, dd)
        card_stride = base
        card_w = statistics.median(ws) + 10        # 米色条比卡略窄
        # 反推第 0 张卡：找 lefts 中能被 (lefts - first) 整除 stride 的 first
        first = lefts[0]
        while first - card_stride > 100:
            first -= card_stride
        card_first_x = first
        print(f"  米色段左边界 {lefts}  间距 {d}")
        print(f"  -> card_stride={card_stride}  card_w≈{card_w:.0f}  "
              f"card_first_x≈{card_first_x}")
        print(f"  外推 {CARD_COUNT} 张卡: 覆盖 x {card_first_x}.."
              f"{card_first_x + (CARD_COUNT - 1) * card_stride + card_w:.0f}")

    # ---------------- 6. 落盘 ----------------
    lay = Layout.load(client_size=(w, h))
    lay.base_w, lay.base_h = w, h                 # 数值就是按这个尺寸量的
    lay.grid_left = round(L)
    lay.grid_top = round(grid_top)
    lay.cell_w = round((R - L + 1) / COLS)
    lay.cell_h = round(cell_h)
    if card_stride:
        lay.card_first_x = round(card_first_x)
        lay.card_stride = round(card_stride)
        lay.card_w = round(card_w)
        lay.card_per_row = CARD_COUNT
    lay.card_y = 18
    lay.card_h = 172

    print("\n量测结果（客户区像素，base=%dx%d）:" % (lay.base_w, lay.base_h))
    for k in ("grid_left", "grid_top", "cell_w", "cell_h",
              "card_first_x", "card_y", "card_w", "card_h",
              "card_stride", "card_per_row"):
        print(f"    {k:14s} = {getattr(lay, k)}")
    print("\n各格中心（客户区像素）:")
    for r in range(ROWS):
        row = "  ".join(f"{lay.cell_center(r, c)}" for c in range(COLS))
        print(f"  R{r + 1}  {row}")

    if args.write:
        lay.save()
        print(f"\n已写入 {LAYOUT_FILE}")
        print("下一步：python tools/overlay_grid.py  用红框叠在截图上肉眼复核")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

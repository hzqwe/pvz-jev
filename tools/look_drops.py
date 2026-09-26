"""只读核验铲卡掉落（DroppedSeed）的内存读数与点击换算公式。

背景（2026-09-26 交接）：transactions.relocate 铲掉可回收墙后，要点击草坪上
掉落的种子卡把它拾起来。点击坐标由 `Layout.dropped_seed_center` 把 Coin 结构的
逻辑坐标换算成屏幕像素 —— 这个公式是按字段布局**推断**的，还没在游戏里
对着截图核过。拾卡点歪的代价：铲掉的墙收不回来（卡片留在草坪上）。

本工具**只读**：每 0.5s 读一次 dropped_seeds，打印每张掉落卡的
  * 逻辑坐标 (x, y, w, h) 与换算出的屏幕点击中心
  * 该中心按几何反推落进哪个格子（和肉眼对照）
  * 同一行/列 cell_center 的坐标（作为"正确落点"的参照）
用法：进入关卡、铲掉一面回收高坚果（或让 agent 铲），同时跑：
    python tools/look_drops.py            # 持续轮询 30s
    python tools/look_drops.py --seconds 60
核验标准：换算出的点击中心应落在掉落卡片贴图的中心附近（±半格内）。
"""

from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit("tools", 1)[0])

from pvz.board import BoardReader  # noqa: E402
from pvz.ui import Layout  # noqa: E402


def dump(reader: BoardReader, layout: Layout) -> None:
    st = reader.read()
    if not st.ok:
        print(f"[等待] {st.reason or '未进入对局'}")
        return
    if not st.dropped_seeds:
        print(f"[{time.strftime('%H:%M:%S')}] 场上没有掉落卡（植物={len(st.plants)}）")
        return
    print(f"[{time.strftime('%H:%M:%S')}] 掉落卡 x{len(st.dropped_seeds)}：")
    for d in st.dropped_seeds:
        cx, cy = layout.dropped_seed_center(d)
        # 反推该点击落进哪个格子（用与 cell_center 相同的几何）
        sx, sy = layout.scale()
        ux = (cx / sx - layout.grid_left) / layout.cell_w - 0.5
        uy = (cy / sy - layout.grid_top) / layout.row_height() - 0.5
        cell = (int(round(uy)), int(round(ux)))
        ref = layout.cell_center(*cell) if all(0 <= v < 12 for v in cell) else None
        print(f"  type_id={d.type_id:<4} 逻辑=({d.x:.0f},{d.y:.0f} {d.w}x{d.h})"
              f"  点击中心=({cx},{cy})  反推格=r{cell[0] + 1}c{cell[1]}"
              f"  该格中心={ref}（对照用）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=30.0)
    args = ap.parse_args()
    reader = BoardReader()
    layout = Layout.load()
    print(f"轮询 {args.seconds:.0f}s —— 现在在游戏里铲一面回收高坚果，观察下面打印的坐标。")
    end = time.time() + args.seconds
    while time.time() < end:
        dump(reader, layout)
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

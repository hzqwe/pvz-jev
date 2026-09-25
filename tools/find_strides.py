"""实测杂交版的结构体步长。

原版 1.0.0.1051：Plant 0x14C / Zombie 0x15C。杂交版加了很多新植物新僵尸，
结构体很可能被加长了 —— 步长一错，第 0 个元素还读得对，之后全是乱码
（这正是 probe 看到的现象：8 株植物只有 1 株正常）。

做法：把数组原始字节 dump 下来，对候选步长 P 打分 ——
"按 P 跨步读到的 (row, col, type) 有多少组落在合法范围内"。
合法组合最多的 P 就是真步长。同时也测试"数组里存的是指针"这种可能。

    python tools/find_strides.py
    python tools/find_strides.py --dump 512
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pvz import offsets as O      # noqa: E402
from pvz.board import BoardReader  # noqa: E402

RULE = "=" * 78


def hr(t: str) -> None:
    print(f"\n{RULE}\n{t}\n{RULE}")


def hexdump(data: bytes, base: int, rows: int = 32, width: int = 32) -> None:
    for i in range(0, min(len(data), rows * width), width):
        chunk = data[i:i + width]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {base + i:08X}  {hx:<{width * 3}}  {asc}")


def score_plant(pm, addr: int, rows_max: int) -> tuple[int, int]:
    row = pm.u32(addr + O.P_ROW)
    col = pm.u32(addr + O.P_COL)
    typ = pm.u32(addr + O.P_TYPE)
    dead = pm.u8(addr + O.P_DEAD)
    if None in (row, col, typ):
        return 0, 0
    ok = (0 <= row < rows_max) + (0 <= col < 9) + (0 <= typ <= 400)
    ok += 1 if dead in (0, 1) else 0
    return (4 if ok == 4 else 0), ok


def score_zombie(pm, addr: int, rows_max: int) -> tuple[int, int]:
    row = pm.u32(addr + O.Z_ROW)
    typ = pm.u32(addr + O.Z_TYPE)
    x = pm.f32(addr + O.Z_X)
    if None in (row, typ, x):
        return 0, 0
    ok = (0 <= row < rows_max) + (0 <= typ <= 600)
    ok += 1 if (x == x and -200 <= x <= 2000) else 0  # x==x 排除 NaN
    return (3 if ok == 3 else 0), ok


def sweep(pm, base: int, kind: str, rows_max: int, max_k: int = 12) -> list[tuple[int, int, int]]:
    scorer = score_plant if kind == "plant" else score_zombie
    need = 4 if kind == "plant" else 3
    out = []
    for P in range(0x40, 0x401, 4):
        full = sum(1 for k in range(max_k) if scorer(pm, base + k * P, rows_max)[0] == need)
        part = sum(scorer(pm, base + k * P, rows_max)[1] for k in range(max_k))
        out.append((P, full, part))
    out.sort(key=lambda t: (-t[1], -t[2]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", type=int, default=256, help="每个数组 dump 多少字节")
    ap.add_argument("--pid", type=int)
    args = ap.parse_args()

    reader = BoardReader(pid=args.pid)
    if not reader.attached:
        print("✗ 无法附着：")
        for n in reader.notes:
            print(f"   · {n}")
        return 1
    pm = reader.pm
    board = reader.board_ptr()
    if not board:
        print("✗ Board 为空（未进入关卡）")
        return 1

    rows_max = reader.rows_hint()
    print(f"pid={pm.pid} base=0x{pm.base:X} board=0x{board:X} 行数上限={rows_max}")

    for kind, off, cnt_off, sz_off, name in (
        ("plant", O.OFF_PLANT, O.OFF_PLANT_COUNT_MAX, O.OFF_PLANT_NEXT_POS, "植物"),
        ("zombie", O.OFF_ZOMBIE, O.OFF_ZOMBIE_COUNT_MAX, O.OFF_ZOMBIE_COUNT, "僵尸"),
    ):
        hr(f"{name}数组  基址字段 Board+0x{off:X}")
        base = pm.ptr(board + off)
        cnt = pm.u32(board + cnt_off)
        used = pm.u32(board + sz_off)
        print(f"  *(Board+0x{off:X}) = {hex(base) if base else 'NULL'}")
        print(f"  count_max(Board+0x{cnt_off:X}) = {cnt}    next_pos(Board+0x{sz_off:X}) = {used}")
        if not base:
            continue

        raw = pm.read(base, max(0x400, args.dump * 4)) or b""
        print(f"  原始前 {args.dump} 字节：")
        hexdump(raw, base, rows=max(1, args.dump // 32))

        res = sweep(pm, base, kind, rows_max)
        print("  候选步长（命中数 / 部分合法数）：")
        for P, full, part in res[:6]:
            mark = ""
            if kind == "plant" and P == O.PLANT_STRUCT:
                mark = "  <- 原版值"
            if kind == "zombie" and P == O.ZOMBIE_STRUCT:
                mark = "  <- 原版值"
            print(f"    0x{P:03X} ({P:4d})  命中 {full:2d}/{12}  部分 {part:3d}/36{mark}")

        # 指针数组假设：数组里存的是指针
        print("  指针数组假设（*(base + 4k) 再解引用）：")
        hits = 0
        for k in range(6):
            p = pm.ptr(base + 4 * k)
            if p and 0x1000 < p < 0x7FFFFFFF:
                s = score_plant(pm, p, rows_max) if kind == "plant" else score_zombie(pm, p, rows_max)
                need = 4 if kind == "plant" else 3
                if s[0] == need:
                    hits += 1
                    print(f"    k={k} ptr=0x{p:X} -> 合法")
        print(f"    合法命中 {hits}/6")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

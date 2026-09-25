"""dump 植物数组原始数据，用来判定"槽位怎么组织、植物到底在哪一格"。

起因：click_test 里用后台点击在 r4c0 种了一株，读回来却是
  before: idx0=Sun-shroom@r1c0  idx1=Peashooter@r0c0  idx2=Peashooter@r0c0
  after : idx1=Peashooter@r0c0  idx2=Peashooter@r0c0  idx3=Peashooter@r0c0
阳光菇那个槽位"变成"了豌豆射手 —— 说明要么槽位被复用（死掉后被新植物占用），
要么我读的字段/上限本来就不对。光看摘要分不出来，必须看原始字节。

    python tools/dump_plants.py
    python tools/dump_plants.py --slots 24 --raw
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvz import offsets as O                    # noqa: E402
from pvz import win32 as W                      # noqa: E402
from pvz.plants import PlantBook                # noqa: E402

PROCESS_NAMES = ["PlantsVsZombies.exe", "pvzHE-Launcher.exe"]

# Plant 结构体里我们关心的字段（相对槽首）
FIELDS = [
    (0x00, "u32", "f000"),
    (0x04, "u32", "f004"),
    (0x08, "u32", "f008"),
    (0x0C, "u32", "f00C"),
    (0x10, "u32", "f010"),
    (0x14, "u32", "f014"),
    (0x18, "u32", "f018"),
    (0x1C, "i32", "row?"),
    (0x20, "i32", "f020"),
    (0x24, "i32", "type?"),
    (0x28, "i32", "col?"),
    (0x2C, "f32", "x?"),
    (0x30, "f32", "y?"),
    (0x138, "i32", "imitater"),
    (0x141, "u8", "dead"),
    (0x142, "u8", "squished"),
    (0x143, "u8", "asleep"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slots", type=int, default=12)
    ap.add_argument("--raw", action="store_true", help="额外打印每槽前 0x40 字节的 hex")
    args = ap.parse_args()

    pid = W.find_pid(PROCESS_NAMES)
    win = W.find_game_window(pid) if pid else None
    if win and win.client_size[0] <= 0:
        W.restore_window(win.hwnd)

    with W.ProcessMemory(pid, "PlantsVsZombies.exe") as pm:
        lawn = pm.u32(pm.va(O.LAWN_PTR))
        board = pm.u32(lawn + O.OFF_BOARD) if lawn else None
        if not board:
            print(f"指针链断了: LawnApp={lawn!r}")
            return 1
        print(f"LawnApp=0x{lawn:X}  Board=0x{board:X}")
        print(f"game_ui={pm.i32(lawn + O.OFF_GAME_UI)}  scene={pm.i32(board + O.OFF_SCENE)}"
              f"  sun={pm.i32(board + O.OFF_SUN)}  clock={pm.i32(board + O.OFF_GAME_CLOCK)}")

        print("\n--- Board 头部的几个计数字段 ---")
        for off in (0x90, 0x94, 0x98, 0x9C, 0xA0, 0xA4, 0xA8, 0xAC, 0xB0, 0xB4, 0xB8, 0xBC, 0xC0):
            v = pm.u32(board + off)
            mark = ""
            if off == O.OFF_PLANT:
                mark = "  <- 植物数组指针"
            elif off == O.OFF_PLANT_COUNT_MAX:
                mark = "  <- OFF_PLANT_COUNT_MAX"
            elif off == O.OFF_PLANT_NEXT_POS:
                mark = "  <- OFF_PLANT_NEXT_POS"
            elif off == O.OFF_ZOMBIE:
                mark = "  <- 僵尸数组指针"
            print(f"  Board+0x{off:03X} = {v!s:>12}  (0x{(v or 0) & 0xFFFFFFFF:08X}){mark}")

        base = pm.u32(board + O.OFF_PLANT)
        cap = pm.i32(board + O.OFF_PLANT_COUNT_MAX) or 0
        used = pm.i32(board + O.OFF_PLANT_NEXT_POS) or 0
        print(f"\n植物数组 base=0x{base:X}  cap(Board+0x{O.OFF_PLANT_COUNT_MAX:03X})={cap} "
              f" used(Board+0x{O.OFF_PLANT_NEXT_POS:03X})={used}  struct=0x{O.PLANT_STRUCT:X}")
        if not base:
            return 1

        book = PlantBook()
        n = max(args.slots, min(max(cap, used, 0) + 2, 40))
        print(f"\n--- 逐槽 dump（前 {n} 槽）---")
        for i in range(n):
            a = base + i * O.PLANT_STRUCT
            vals = {}
            for off, kind, _name in FIELDS:
                if kind == "u8":
                    vals[off] = pm.u8(a + off)
                elif kind == "i32":
                    vals[off] = pm.i32(a + off)
                elif kind == "u32":
                    vals[off] = pm.u32(a + off)
                elif kind == "f32":
                    vals[off] = pm.f32(a + off)
            dead = vals.get(0x141)
            squished = vals.get(0x142)
            asleep = vals.get(0x143)
            t = vals.get(0x24)
            r = vals.get(0x1C)
            c = vals.get(0x28)
            tag = "死" if dead else ("压扁" if squished else "活")
            name = book.name(t) if isinstance(t, int) and 0 <= t < 4096 else "?"
            print(f"  [{i:2d}] {tag:4s} row={r!s:>4} col={c!s:>4} type={t!s:>5} {name:<22s} "
                  f"imit={vals.get(0x138)!s:>5} asleep={asleep} "
                  f"x={vals.get(0x2C)!s:>10} y={vals.get(0x30)!s:>8} "
                  f"f20={vals.get(0x20)!s:>5} f00={vals.get(0x00)!s:>5} f04={vals.get(0x04)!s:>5}")
            if args.raw:
                b = pm.read(a, 0x40)
                if b:
                    for k in range(0, 0x40, 16):
                        print(f"        +0x{k:02X}: " + " ".join(f"{x:02X}" for x in b[k:k + 16]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

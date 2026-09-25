"""诊断工具：把"游戏进程里到底能读到什么"一次摊开。

用法：
    python tools/probe.py                 # 全量诊断
    python tools/probe.py --list          # 只列进程和窗口
    python tools/probe.py --shot out.png  # 顺带存一张游戏截图
    python tools/probe.py --jev           # 顺带测一次 Jev 连通性
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pvz import offsets as O                       # noqa: E402
from pvz.board import BoardReader, PROCESS_NAMES   # noqa: E402
from pvz.jev import JevClient                      # noqa: E402
from pvz.plants import PlantBook                   # noqa: E402
from pvz.serialize import build_state, render_text  # noqa: E402
from pvz.ui import Layout, grab                    # noqa: E402
from pvz.win32 import (                            # noqa: E402
    DPI_MODE, PVZ_IMAGE_BASE, find_game_window, find_pid, list_processes,
    list_windows, module_base,
)

RULE = "=" * 78


def hr(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def main() -> int:
    ap = argparse.ArgumentParser(description="PvZ 内存/窗口/接口诊断")
    ap.add_argument("--list", action="store_true", help="只列进程与窗口")
    ap.add_argument("--shot", metavar="PATH", help="保存一张游戏截图")
    ap.add_argument("--jev", action="store_true", help="测试 Jev 连通性")
    ap.add_argument("--json", metavar="PATH", help="把序列化后的 state 存成 JSON")
    ap.add_argument("--pid", type=int, help="手动指定进程 id")
    args = ap.parse_args()

    print(f"DPI 模式: {DPI_MODE}")

    hr("1. 进程")
    procs = list_processes()
    print(f"系统进程总数: {len(procs)}")
    # 按 PROCESS_NAMES 的优先级排序，而不是按系统枚举顺序 ——
    # 否则 launcher 可能排在游戏本体前面，导致后续全读错进程。
    prio = {x.lower(): i for i, x in enumerate(PROCESS_NAMES)}
    hits = [(p, n) for p, n in procs if n.lower() in prio]
    hits.sort(key=lambda t: prio[t[1].lower()])
    for p, n in hits:
        base = module_base(p)
        print(f"  ★ pid={p} {n}  base=0x{base:X}" if base else f"  ★ pid={p} {n}  (base 未知)")
    if not hits:
        print("  ✗ 没有找到游戏进程。请先启动 pvzHE-Launcher.exe 并进入一个关卡。")
        print(f"  目标进程名: {', '.join(PROCESS_NAMES)}")
        if args.list:
            return 1

    pid = args.pid or (hits[0][0] if hits else find_pid(PROCESS_NAMES))
    if pid:
        hr("2. 窗口")
        wins = list_windows(pid)
        if not wins:
            print("  该进程没有顶层窗口")
        for w in wins:
            print(f"  {w.describe()}")

    if args.list:
        return 0

    hr("3. 指针链")
    reader = BoardReader(pid=pid)
    if not reader.attached:
        print("  ✗ 无法附着到进程：")
        for n in reader.notes:
            print(f"     · {n}")
        return 1
    pm = reader.pm
    assert pm
    print(f"  进程 pid={pm.pid}  模块基址=0x{pm.base:X}  (期望 0x{PVZ_IMAGE_BASE:X})")
    lawn = reader.lawn_app_ptr()
    print(f"  *(0x{O.LAWN_PTR:X})                  = {hex(lawn) if lawn else 'NULL'}   <- LawnApp*")
    board = reader.board_ptr()
    print(f"  *(LawnApp+0x{O.OFF_BOARD:X})            = {hex(board) if board else 'NULL'}   <- Board*")
    if not board:
        print("\n  ⚠ Board 为空 —— 说明还没进入关卡（或在主菜单）。请进入任意关卡后重跑。")
        return 2

    hr("4. 关键标量（若数值明显不合理，说明杂交版改了偏移）")
    checks = [
        ("sun        (Board+0x5560)", board + O.OFF_SUN, "i32", (0, 9990)),
        ("scene      (Board+0x554C)", board + O.OFF_SCENE, "i32", (0, 6)),
        ("level      (Board+0x5550)", board + O.OFF_ADVENTURE_LEVEL, "i32", (0, 100)),
        ("game_clock (Board+0x5568)", board + O.OFF_GAME_CLOCK, "i32", (0, 10**9)),
        ("ui  (LawnApp+0x7FC)", (lawn or 0) + O.OFF_GAME_UI, "i32", (0, 7)),
    ]
    for label, addr, kind, (lo, hi) in checks:
        v = pm.i32(addr)
        ok = v is not None and lo <= v <= hi
        print(f"  {'✓' if ok else '✗'} {label:28s} = {v}")
    ui = pm.i32((lawn or 0) + O.OFF_GAME_UI)
    print(f"\n  ui=2 表示正在对局（当前 {ui}）")

    hr("5. 完整快照")
    st = reader.read()
    print(f"  ok={st.ok} reason={st.reason!r}")
    print(f"  植物 {len(st.plants)} 株, 僵尸 {len(st.zombies)} 只, 卡槽 {len(st.slots)} 个")
    if st.notes:
        for n in st.notes:
            print(f"  · {n}")
    print()
    print(render_text(st, PlantBook()))

    hr("6. 种子栏布局歧义检测")
    for var in reader.slot_variants(board):
        print(f"  [{var['variant']}] bank={var.get('bank')} count={var.get('count')}")
        for row in var.get("sample", []):
            print(f"      slot{row['i']}: type={row['seed_type']:<5} cd_left={row['cd_left']}/{row['cd_total']} im={row['type_im']}")

    hr("7. 未登记的植物 ID（杂交版新增，需补 data/plant_names.json）")
    book = PlantBook()
    ids = book.unregistered_ids([p.type_id for p in st.plants] + [s.type_id for s in st.slots])
    print(f"  {ids if ids else '无（全部已登记）'}")

    if args.json:
        state = build_state(st, book)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        print(f"\n  已写出 state -> {args.json}")

    if args.shot:
        win = find_game_window(pid)
        if win:
            shot = grab(win)
            if shot:
                path = shot.save_bmp(args.shot)
                print(f"\n  截图已保存 -> {path}  ({shot.w}x{shot.h} @ {shot.x},{shot.y})")
            else:
                print("\n  ✗ 截图失败")
        else:
            print("\n  ✗ 没找到窗口，无法截图")

    if args.jev:
        hr("8. Jev 连通性")
        try:
            cli = JevClient()
            print(f"  模型列表: {[m['name'] for m in cli.models()['models']]}")
            t0 = time.time()
            resp = cli.ask(
                "A board with 3 lanes. Lane 2 has two zombies at 150px and 400px.",
                {
                    "probe": {
                        "type": "choice",
                        "instructions": "Which lane is under the most pressure?",
                        "criteria": {"lane_1": "one zombie at 700px",
                                     "lane_2": "two zombies at 150px and 400px",
                                     "lane_3": "no zombies"},
                    }
                },
            )
            print(f"  ok={resp.ok} latency={time.time() - t0:.2f}s model={resp.model}")
            for qid, a in resp.answers.items():
                print(f"    {qid}: {a.summary()}")
            if resp.error:
                print(f"  error: {resp.error}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

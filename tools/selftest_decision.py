"""决策链路自测：不依赖游戏，用合成战场把 序列化 -> 提问 -> Jev -> 合并 跑通。

用途有两个：
  1. 证明决策层真的在工作（不需要游戏在跑就能验证）；
  2. 把 Jev 在同一块战场上的判断留档，方便对比不同措辞/状态设计下的稳定性。

    python tools/selftest_decision.py
    python tools/selftest_decision.py --scenario late
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

from pvz.board import BoardState, Plant, SeedSlot, Zombie  # noqa: E402
from pvz.jev import DecisionLog, JevClient                 # noqa: E402
from pvz.plants import PlantBook, load_lineups             # noqa: E402
from pvz.policy import build_questions, generate_candidates, merge_decision  # noqa: E402
from pvz.report import render_report                       # noqa: E402
from pvz.serialize import build_state, render_text         # noqa: E402

SCENARIOS: dict[str, BoardState] = {}

# 杂交版阵容用假的 type_id 离线绑定（真 ID 只有进关卡才读得到）。
# 这里验证的是"名字/功能/标签挂上去之后，候选描述和 Jev 的判断对不对"，
# 跟 ID 的具体数值无关。
HYBRID_FAKE_BASE = 200


def _mk(scene, level, sun, plants, zombies, slots, rows=5):
    return BoardState(
        ok=True, pid=0, lawn_app=0x1234, board=0x5678, ui=2, scene=scene, level=level,
        sun=sun, game_clock=1000, rows=rows, cols=9,
        plants=[Plant(index=i, row=r, col=c, type_id=t) for i, (r, c, t) in enumerate(plants)],
        zombies=[Zombie(index=i, row=r, type_id=t, x=x, phase=0) for i, (r, t, x) in enumerate(zombies)],
        slots=[SeedSlot(index=i, type_id=t, cd_left=p, cd_total=tot) for i, (t, p, tot) in enumerate(slots)],
    )


# 开局：阳光少，只有基础牌，一条路开始有僵尸
SCENARIOS["early"] = _mk(
    scene=0, level=1, sun=75,
    plants=[(0, 0, 1), (1, 0, 1), (2, 1, 0)],
    zombies=[(2, 0, 640)],
    slots=[(1, 0, 0), (0, 0, 0), (3, 0, 0), (4, 0, 0)],
)

# 中期：多路受压，有坚果和樱桃炸弹，其中樱桃还在冷却
SCENARIOS["mid"] = _mk(
    scene=0, level=5, sun=175,
    plants=[(0, 0, 1), (1, 0, 1), (2, 0, 1), (2, 3, 0), (4, 3, 0), (0, 2, 0)],
    zombies=[(2, 0, 660), (2, 0, 520), (4, 4, 300), (4, 0, 700)],
    slots=[(1, 0, 0), (0, 0, 0), (3, 0, 0), (2, 1800, 3000), (4, 0, 0)],
)

# 后期：僵尸贴脸，必须用一次性爆发
SCENARIOS["late"] = _mk(
    scene=0, level=12, sun=325,
    plants=[(0, 0, 1), (0, 1, 0), (1, 0, 1), (2, 0, 1), (2, 3, 0), (3, 1, 0)],
    zombies=[(3, 0, 120), (3, 0, 260), (1, 5, 140), (0, 4, 620), (2, 4, 700)],
    slots=[(1, 0, 0), (0, 0, 0), (3, 0, 0), (2, 0, 0), (20, 0, 0)],
)


def bind_hybrid(book: PlantBook) -> bool:
    """把 data/lineups.json 里的杂交版阵容绑到假 ID 上，并补一个 hybrid 场景。"""
    lineups = load_lineups()
    if not lineups:
        return False
    lu = lineups[0]
    order = list(lu["order"])
    types = [HYBRID_FAKE_BASE + i for i in range(len(order))]
    book.bind_lineup(types, order,
                     cd_totals=list(lu.get("observed_cd_total") or []))
    cd = {t: c for t, c in zip(types, lu.get("observed_cd_total") or [0] * len(types))}
    SCENARIOS["hybrid"] = _mk(
        scene=0, level=1, sun=650,
        plants=[(2, 6, 0), (2, 5, 0)],                 # R3 已有 2 株豌豆射手
        zombies=[(0, 0, 120.0), (0, 0, 300.0),
                 (2, 0, 250.0), (2, 0, 420.0), (2, 0, 600.0),
                 (4, 0, 700.0)],
        slots=[(t, 0, cd[t]) for t in types],
    )
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=list(SCENARIOS) + ["all", "hybrid"], default="all")
    ap.add_argument("--log", default=os.path.join(ROOT, "out", "selftest.jsonl"))
    args = ap.parse_args()

    book = PlantBook()
    if not bind_hybrid(book):
        print("⚠️ data/lineups.json 为空，杂交版场景跳过")
    cli = JevClient()
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]

    if os.path.exists(args.log):
        os.remove(args.log)
    dlog = DecisionLog(args.log)

    print(f"Jev 模型: {cli.model} | 场景: {', '.join(names)}\n")

    for name in names:
        board = SCENARIOS[name]
        print("=" * 78)
        print(f"场景 {name}")
        print("=" * 78)
        print(render_text(board, book))
        print()

        state = build_state(board, book)
        cands = generate_candidates(board, book)
        questions = build_questions(cands, board, book)

        print(f"代码枚举出 {len(cands)} 条候选动作：")
        for c in cands:
            print(f"  {c.cid}  [分 {c.score:5.1f}] {c.describe(book)}")
        print()

        t0 = time.time()
        resp = cli.ask(state, questions)
        dt = time.time() - t0

        if not resp.ok:
            print(f"✗ Jev 调用失败: {resp.error}")
            continue

        print(f"Jev 回答（{dt:.2f}s, model={resp.model}, 尝试 {resp.attempts} 次）：")
        for qid, a in resp.answers.items():
            print(f"  {qid:12s} {a.summary()}")

        dec = merge_decision(resp, cands, board, book)
        print()
        print(f"→ 合并后的决定: {dec.candidate.describe(book) if dec.candidate else '无'}")
        print(f"  保留阳光={dec.hold}  最危险路={dec.threat_lane}  紧迫度={dec.urgency}/3  "
              f"置信度={dec.confidence}  兜底={dec.fallback}")
        for n in dec.notes:
            print(f"  · {n}")

        dlog.append({
            "t": time.time(), "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scenario": name,
            "board_text": render_text(board, book),
            "state": state,
            "candidates": [{"cid": c.cid, "kind": c.kind, "row": c.row, "col": c.col,
                            "slot": c.slot, "type_id": c.type_id, "score": round(c.score, 1),
                            "desc": c.describe(book)} for c in cands],
            "jev": {"ok": resp.ok, "error": resp.error, "latency_s": round(dt, 2), "model": resp.model,
                    "answers": {q: {"kind": a.kind, "summary": a.summary(), "raw": a.raw}
                                for q, a in resp.answers.items()}},
            "decision": {"action_id": dec.action_id,
                         "chosen": dec.candidate.describe(book) if dec.candidate else None,
                         "hold": dec.hold, "threat_lane": dec.threat_lane, "urgency": dec.urgency,
                         "confidence": dec.confidence, "fallback": dec.fallback, "notes": dec.notes},
            "executed": None,
        })
        print()

    print("=" * 78)
    print(f"Jev 统计: {cli.stats()}")
    out = render_report(args.log, os.path.join(ROOT, "out", "selftest_report.html"))
    print(f"复盘报告 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

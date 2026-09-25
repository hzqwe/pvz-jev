"""离线自检：不给游戏、不连 Jev，只看候选生成器的排序合不合理。

为什么需要它：候选生成器是"代码替 Jev 做的部分"，它错了 Jev 再聪明也没用。
而且它错了很难在真实对局里发现 —— 因为真实对局每次都不一样，没法对比。
所以这里用几个**构造好的场景**把排序钉死：

  平静局面  -> 榜首必须是产阳光 / 射手，不能是爆发牌
  单路告急  -> 该路的防御牌和爆发牌要上来
  多路堆叠  -> 三行爆发的排序要压过单行爆发
  全屏堆叠  -> 全屏冻结才允许出现
  刚开局    -> 经济牌第一

用法：
    python tools/selftest_policy.py
    python tools/selftest_policy.py --lineup 2026-09-25-campaign
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvz.board import BoardState, Plant, SeedSlot, Zombie     # noqa: E402
from pvz.plants import PlantBook, load_lineups                # noqa: E402
from pvz.policy import generate_candidates                    # noqa: E402
from pvz.serialize import render_text                         # noqa: E402

# 绑定用的假 type_id（真 ID 要进关卡读，离线自检不需要真 ID，
# 只要"槽位 -> 名字"这一层关系对就行）
FAKE_BASE = 200


def make_book(lineup: dict) -> PlantBook:
    book = PlantBook()
    types = [FAKE_BASE + i for i in range(len(lineup["order"]))]
    book.bind_lineup(types, lineup["order"],
                     cd_totals=list(lineup.get("observed_cd_total") or []))
    return book


def make_board(book: PlantBook, lineup: dict, sun: int, zombies, plants=()) -> BoardState:
    st = BoardState(ok=True, sun=sun, rows=5, cols=9, scene=0, level=1, game_clock=1000)
    st.zombies = list(zombies)
    st.plants = list(plants)
    st.slots = [SeedSlot(i, FAKE_BASE + i, 0, 0) for i in range(len(lineup["order"]))]
    return st


def show(title: str, st: BoardState, book: PlantBook, top: int = 6) -> None:
    print("=" * 96)
    print(f"■ {title}")
    print("-" * 96)
    print(render_text(st, book))
    print("-" * 96)
    cands = generate_candidates(st, book)
    for c in cands[:top]:
        cost = book.cost(c.type_id) if c.kind == "plant" else None
        name = book.en(c.type_id) if c.kind == "plant" else "WAIT"
        print(f"  [{c.cid:>3}] {c.score:6.1f}  {name:<32} "
              f"{'cost ' + str(cost) if cost else '':<10} {c.why}")
    print(f"  （共 {len(cands)} 条候选）")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lineup", default=None)
    args = ap.parse_args()

    lineups = load_lineups()
    if not lineups:
        print("data/lineups.json 为空")
        return 1
    lineup = next((l for l in lineups if l.get("id") == args.lineup), lineups[0]) \
        if args.lineup else lineups[0]
    book = make_book(lineup)
    print(f"lineup = {lineup['id']}（{len(lineup['order'])} 张卡）\n")

    # 1) 刚开局：没有僵尸、阳光少 -> 经济牌应该第一
    show("场景 1 · 刚开局（无僵尸，阳光 150）",
         make_board(book, lineup, 150, []), book)

    # 2) 平静：僵尸都在远处 -> 不应该出现爆发牌
    show("场景 2 · 平静（僵尸都在 x>560 的远处，阳光 700）",
         make_board(book, lineup, 700, [
             Zombie(0, 0, 0, x=700.0), Zombie(1, 1, 0, x=760.0), Zombie(2, 3, 0, x=680.0),
         ]), book)

    # 3) 单路告急：R1 一只僵尸贴脸，阳光 400
    show("场景 3 · 单路告急（R1 僵尸 x=120，阳光 400）",
         make_board(book, lineup, 400, [
             Zombie(0, 0, 0, x=120.0), Zombie(1, 0, 0, x=300.0),
             Zombie(2, 4, 0, x=650.0),
         ]), book)

    # 4) 多路堆叠：R2/R3/R4 各若干只 -> 三行爆发应该排前
    show("场景 4 · 多路堆叠（R2/R3/R4 共 7 只，阳光 900）",
         make_board(book, lineup, 900, [
             Zombie(0, 1, 0, x=200.0), Zombie(1, 1, 0, x=420.0),
             Zombie(2, 2, 0, x=240.0), Zombie(3, 2, 0, x=500.0), Zombie(4, 2, 0, x=660.0),
             Zombie(5, 3, 0, x=260.0), Zombie(6, 3, 0, x=520.0),
         ]), book)

    # 5) 全屏堆叠：6 只以上且有一路告急 -> 全屏冻结才允许出现
    show("场景 5 · 全屏堆叠（9 只僵尸铺满 5 路，阳光 1200）",
         make_board(book, lineup, 1200, [
             Zombie(i, i % 5, 0, x=140.0 + i * 90.0) for i in range(9)
         ]), book)

    # 6) 已有防线：R3 已有两株豌豆 -> 射手/墙应该排在防线前后
    show("场景 6 · 已有防线（R3 已有 2 株豌豆，R3 有 3 只僵尸）",
         make_board(book, lineup, 800, [
             Zombie(0, 2, 0, x=260.0), Zombie(1, 2, 0, x=430.0), Zombie(2, 2, 0, x=600.0),
         ], plants=[Plant(0, 2, 6, 0), Plant(1, 2, 5, 0)]), book)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

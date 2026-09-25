"""把内存里读到的卡槽 type_id 绑定到 hybrid_plants.json 里的植物名字。

为什么需要这一步
----------------
Jev 看到 `#161` 这种裸 ID 是没法判断"该不该种"的。名字、功能、角色、标签
全都要靠一次绑定才能挂上去。而杂交版每局阵容都不同，所以**绑定不能硬编码**，
必须每换一次卡池就重做一次。

⚠️ 特别注意：**杂交版把原版 ID 就地替换了**。ID 2 不是樱桃炸弹而是阳光炸弹、
ID 20 不是火爆辣椒而是樱桃辣椒、ID 23 不是高坚果而是高冰果、ID 9 不是阳光菇
而是阳光向日葵、ID 28 不是裂荚射手而是玉米卷香蒲。所以 `BASE_PLANTS` 里那套
原版名字在这游戏上**完全不可信**，必须以绑定结果为准。

怎么绑
------
1. 读内存拿到种子栏每一格的 `type_id`（顺序 = 卡面从左到右，末尾的 `-1` 是空槽）；
2. 先拿这串 type_id 和 `data/lineups.json` 里各 lineup 的 `type_ids` 指纹比对 ——
   **完全一致才认为"是同一副牌"**，然后才按 `order` 把名字贴上去；
3. 冷却时间只打印作参考，**不作门槛**（原因见下）；
4. 落盘到 `data/plant_ids.json`，以后每次启动自动复用。

⚠️ 为什么冷却不能当校验：实测杂交版图鉴的「冷却速度」**不是种子包冷却**。
樱桃辣椒图鉴写 100 秒、内存 `cd_total=5000`（按 100tick/s 是 50 秒）；
冰瓜香蒲图鉴 30 秒、内存 2000（20 秒）。比值在 50~100 tick/s 之间飘，
拿它当闸门会把**正确**的绑定拦下来（这个坑刚踩过）。
真正的最终校验是运行时的**阳光差值**：点卡掉了多少阳光，和知识库成本比对，
在 `agent.py` 里做。

用法（**必须在关卡里跑**，选卡界面读不到种子栏）：
    python tools/bind_cards.py                 # 绑定 + 落盘
    python tools/bind_cards.py --dry           # 只看结果，不写文件
    python tools/bind_cards.py --force         # 指纹不匹配也照样写（慎用）
    python tools/bind_cards.py --lineup 2026-09-25-campaign
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvz.board import BoardReader                      # noqa: E402
from pvz.plants import PlantBook, load_lineups, match_lineup   # noqa: E402

PROCESS_NAMES = ["PlantsVsZombies.exe", "pvzHE-Launcher.exe"]


def _print_report(rep: dict) -> None:
    print(f"{'槽':>3} {'type':>5}  {'名字':<22} {'成本':>5} {'cd_total':>9} "
          f"{'图鉴冷却':>8}  参考")
    print("-" * 80)
    for row in rep["bound"]:
        cd = row["cd_total"]
        al = row["almanac_cd_s"]
        agree = row["cd_agrees"]
        mark = "—" if agree is None else ("≈" if agree else "✗")
        print(f"{row['slot']:>3} {row['type_id']:>5}  {row['name']:<22} "
              f"{str(row['cost']):>5} {str(cd) if cd else '?':>9} "
              f"{f'{al:g}s' if al else '?':>8}  {mark}")
    print("-" * 80)
    if rep["unbound_names"]:
        print("⚠️ 这些名字在 hybrid_plants.json 里找不到，没绑上：", rep["unbound_names"])
    if rep["extra_slots"]:
        print("ℹ️ 内存里还有空槽（type=-1）没绑：", rep["extra_slots"])
    if rep["mismatches"]:
        print("ℹ️ 冷却量级差得远的槽位（**仅供参考，不拦绑定**）：")
        for m in rep["mismatches"]:
            print(f"   slot {m['slot']} {m['name']}: 图鉴 {m['almanac_cd_s']:g}s，"
                  f"内存 cd_total={m['cd_total']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lineup", default=None, help="指定 lineups.json 里的 id")
    ap.add_argument("--dry", action="store_true", help="只打印，不写 plant_ids.json")
    ap.add_argument("--force", action="store_true", help="type_id 指纹不匹配也照样写")
    args = ap.parse_args()

    lineups = load_lineups()
    if not lineups:
        print("data/lineups.json 里没有 lineup，先补一条。")
        return 1

    reader = BoardReader()
    if not reader.attached:
        print("没找到游戏进程。")
        return 1
    st = reader.read()
    types = [s.type_id for s in st.slots]
    cds = [s.cd_total for s in st.slots]
    active = [t for t in types if t >= 0]
    print(f"内存读到 {len(types)} 个卡槽（其中 {len(active)} 格有卡）")
    print(f"  type_id : {types}")
    print(f"  cd_total: {cds}")
    if not st.ok:
        print(f"⚠️ 现在不在对局中（{st.reason}）。选卡界面读不到种子栏，"
              f"请在关卡里再跑一次。")
        if not active:
            reader.close()
            return 1

    if args.lineup:
        cand = [l for l in lineups if l.get("id") == args.lineup]
        if not cand:
            print(f"找不到 lineup id={args.lineup}。可用的：{[l.get('id') for l in lineups]}")
            reader.close()
            return 1
        lineup, how = cand[0], "forced"
    else:
        lineup, how = match_lineup(lineups, types)

    if lineup is None:
        print(f"\n❌ 没有 lineup 匹配当前 {len(active)} 张卡的牌组。可用的：")
        for l in lineups:
            print(f"  {l.get('id')}  type_ids={l.get('type_ids')}")
        print("请截图卡槽栏并新增一条 lineup。")
        reader.close()
        return 1

    print(f"\n使用 lineup: {lineup.get('id')}（{lineup.get('source', '')}）")
    if how == "type_ids":
        print("✅ type_id 指纹**完全一致** —— 确认是同一副牌，名字顺序可信。")
    elif how == "length":
        print("⚠️ 只按「卡数」匹配上了，**指纹不一致** —— 名字顺序没有被证实。"
              "请核对 data/lineups.json 的 type_ids / order。")
    else:
        print(f"ℹ️ 匹配方式：{how}")

    book = PlantBook()
    rep = book.bind_lineup(active, list(lineup.get("order") or []), cd_totals=cds)
    print()
    _print_report(rep)

    ok_to_write = how == "type_ids" or args.force
    if args.dry:
        print("\n(--dry，未写文件)")
    elif ok_to_write:
        if book.save_ids():
            print(f"\n已写入 data/plant_ids.json（{len(book.bound_ids)} 条绑定）")
        else:
            print("\n写文件失败")
    else:
        print("\n未写文件（指纹不匹配；确认顺序无误可以用 --force）")

    missing = [t for t in active if t >= 0 and t not in book.kb_by_id]
    if missing:
        print(f"\n还有 {len(missing)} 个卡槽没有功能登记：{missing}"
              f"（把这些植物的图鉴补进 data/hybrid_plants.json）")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

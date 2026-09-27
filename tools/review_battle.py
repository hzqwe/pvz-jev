"""战斗复盘器：读一局的决策日志，找出坏决策模式，生成带证据的改进建议。

设计原则（与项目一脉相承）：
  * 检测全部是**确定性规则**——每条发现都带时间戳/路号/原始证据，可复核；
  * 只**提出**建议，绝不自动改评分或教条——策略进化的闸门始终是
    「人确认 + 回归测试」（见 docs/strategy-upgrade.md）；
  * 未知字段一律 .get() 容错——日志格式随版本演化，复盘器坏了自己会静默少报，
    不该反过来弄崩用户的工作流。

用法：
    python tools/review_battle.py                 # 复盘最新一局
    python tools/review_battle.py out/decisions_xxx.jsonl
"""

from __future__ import annotations

import glob
import json
import os
import sys

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
STALL_S = 15.0          # 两次决策间隔超过它 = 节奏空档
COLLAPSE_CLOCK = 9000   # 100 ticks/s：开局 90 秒以后才评估中盘赤字
STARVE_WINDOW = 6       # 连续这么多条记录 producers==0 才算"零产出窗口"


def latest_decisions() -> str | None:
    files = sorted(p for p in glob.glob(os.path.join(OUT_DIR, "decisions_*.jsonl"))
                   if not p.endswith('.events.jsonl'))
    return files[-1] if files else None


def load_records(path: str) -> list[dict]:
    recs = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if isinstance(record,dict) and record.get('record_type') != 'event':
                    recs.append(record)
            except ValueError:
                continue
    return recs


def _g(rec, *path, default=None):
    """容错取嵌套字段：_g(rec,'state','game','sun')。"""
    cur = rec
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p, default)
    return cur


def _producers(rec) -> int:
    lanes = _g(rec, "state", "lanes", default=[]) or []
    n = 0
    for lane in lanes:
        for d in lane.get("defenders", []) or []:
            if d.get("role") == "producer":
                n += 1
    return n


def _max_threat(rec) -> str:
    lanes = _g(rec, "state", "lanes", default=[]) or []
    level = "none"
    for lane in lanes:
        t = _g(lane,'tactical_assessment','threat_level') or lane.get("threat_level") or "none"
        if t == "critical":
            return "critical"
        if t == "high":
            level = "high"
        elif t == "low" and level == "none":
            level = "low"
    return level


def _any_critical_lane(rec) -> bool:
    return _max_threat(rec) == "critical"


# ---------------------------------------------------------------- 检测器
def _plant_name(rec) -> str:
    ex = _g(rec, "executed", default={}) or {}
    if ex.get("plant"):
        return str(ex["plant"])
    import re
    m = re.search(r'Plant "([^"]+)"', str(_g(rec, "decision", "chosen", default="")))
    return m.group(1) if m else "unknown"


def split_battles(recs):
    groups=[]
    previous=None
    for rec in recs:
        clock=_g(rec,'state','game','clock')
        prev_clock=_g(previous,'state','game','clock')
        battle=rec.get('battle_id'); prev_battle=(previous or {}).get('battle_id')
        changed = previous is not None and (
            (battle is not None and prev_battle is not None and battle!=prev_battle)
            or (clock is not None and prev_clock is not None and clock<prev_clock)
            or _g(rec,'state','game','scene')!=_g(previous,'state','game','scene'))
        if not groups or changed: groups.append([])
        groups[-1].append(rec);previous=rec
    return groups


def detect(recs: list[dict]) -> list[dict]:
    groups=split_battles(recs)
    if len(groups)>1:
        return [{**f,'battle':i,'evidence':f"第 {i} 局：{f['evidence']}"}
                for i,group in enumerate(groups,1) for f in detect(group)]
    findings: list[dict] = []

    def add(severity, kind, evidence, suggestion):
        findings.append(dict(severity=severity, kind=kind,
                             evidence=evidence, suggestion=suggestion))

    # 1) 落点失败（按格聚合）+ 失败浪费的阳光
    #    实测（泳池局 2026-09-26）：落点未生效时游戏**仍可能扣阳光**——
    #    这是最贵的失败模式，浪费量必须单独量化报出来。
    fails: dict[tuple, int] = {}
    cell_failures={}
    wasted = 0
    wasted_n = 0
    misplaced: list[str] = []
    for rec in recs:
        ex = _g(rec, "executed", default={}) or {}
        if ex.get("kind") in ("click", "shovel") and not ex.get("placed", ex.get("removed", True)):
            cell = tuple(ex.get("grid", "").strip("r c").split("c")) if isinstance(ex.get("grid"), str) else None
            try:
                cell = (int(cell[0]), int(cell[1])) if cell and len(cell) == 2 else None
            except (ValueError, TypeError):
                cell = None
            key = (_plant_name(rec), cell)
            fails[key] = fails.get(key, 0) + 1
            if cell is not None and ex.get('kind')=='click':
                cell_failures.setdefault(cell,[]).append((float(rec.get('t',0)),_plant_name(rec)))
            loss = (ex.get("sun_before") or 0) - (ex.get("sun_after") or 0)
            if loss > 0:
                wasted += loss
                wasted_n += 1
        for c in ex.get("actual_other_cells") or []:
            misplaced.append(f"{rec.get('iso','?')} 落到 {c}（应为 {ex.get('grid')}）")
    if wasted:
        add("critical", "落点失败还扣了阳光",
            f"{wasted_n} 次失败共浪费 {wasted} 阳光（游戏扣了钱但目标格没有植物）",
            "最贵的失败模式。复盘这些记录的 grid 与 scene：先确认 row_types/坐标正确，"
            "再检查是不是水面无荷叶被游戏拒绝。placement_delta 熔断会止血，"
            "但根因要靠坐标/地形校准消除")
    for (plant, cell), n in sorted(fails.items(), key=lambda kv: -kv[1]):
        if n >= 2:
            add("high", "落点反复失败",
                f"{plant} 在 {cell} 累计 {n} 次未确认种植成功",
                "先跑一次该格点击校准（tools/measure_layout.py）；若格子本身合法，"
                "检查是否被障碍/荷叶层规则拦截")
    for cell,attempts in cell_failures.items():
        if any(t2-t1<=15 and name1!=name2
               for (t1,name1),(t2,name2) in zip(attempts,attempts[1:])):
            add('high','同格换卡仍失败',
                f"格 {cell} 在 15 秒内出现不同植物落点失败，共 {len(attempts)} 次未确认种植成功",
                "看 after_failure 的光标：同类种子仍在手中才确认格子被拒，短暂避让该格；"
                "若种子已消失，还需区分瞬发效果和快速被吃，不能直接认定种歪或扣款浪费")
    if misplaced:
        add("critical", "种歪（落到了别的格）",
            "；".join(misplaced[:5]) + (f" 等共 {len(misplaced)} 次" if len(misplaced) > 5 else ""),
            "几何已熔断。重新校准 data/layout.json（必要时量 pool_cell_h），"
            "校准前 agent 不会继续种植——这是设计行为")

    # 2) 节奏空档——几乎都是窗口失焦/最小化
    gaps = []
    for a, b in zip(recs, recs[1:]):
        try:
            dt = float(b.get("t", 0)) - float(a.get("t", 0))
        except (TypeError, ValueError):
            continue
        if dt > STALL_S:
            gaps.append((a.get("iso", "?"), round(dt, 1)))
    if gaps:
        total = sum(g[1] for g in gaps)
        add("medium" if total < 60 else "high", "决策节奏空档",
            f"{len(gaps)} 次空档共 {total:.0f}s，最长的："
            + "；".join(f"{iso} 停 {dt}s" for iso, dt in gaps[:3]),
            "对照同名 .events.jsonl 的运行阶段、decision_started/finished 和异常记录，"
            "区分暂停、窗口不可用、请求变慢与执行耗时；旧日志不能仅凭间隔确定原因")

    # 3) 经济崩盘：中盘还在赤字
    poor = [(r.get("iso"), _g(r, "state", "game", "clock"), _producers(r))
            for r in recs
            if (_g(r, "state", "game", "sun") or 0) < 100
            and _producers(r)<2
            and (_g(r, "state", "game", "clock") or 0) > COLLAPSE_CLOCK]
    if len(poor) >= 3:
        add("high", "经济崩盘（中盘赤字）",
            f"{len(poor)} 条记录同时阳光<100 且产阳光株数<2：首个 {poor[0][0]}（clock={poor[0][1]}, "
            f"产阳光={poor[0][2]}）",
            "检查当时产阳光株数：若已被吃光，说明防线该更早建立；"
            "若从没建起来，对照 playbook 的开局教条复核 opening 决策")

    # 4) 零产出窗口：中盘连续无产阳光
    run = best = 0
    start_iso = None
    for rec in recs:
        if (_g(rec, "state", "game", "clock") or 0) > 12000 and _producers(rec) == 0:
            run += 1
            if run == 1:
                start_iso = rec.get("iso")
            best = max(best, run)
        else:
            run = 0
    if best >= STARVE_WINDOW:
        add("high", "零产出窗口",
            f"连续 {best} 条决策场上没有任何产阳光植物（自 {start_iso} 起）",
            "中盘把向日葵全丢了的局，复盘防线为什么守不住后排；"
            "考虑经济学教条里\"后排必须保留产能\"的强化")

    # 5) 危急时按了等待/兜底率异常
    holds_under_crit = sum(
        1 for rec in recs
        if _any_critical_lane(rec) and (_g(rec, "executed", "kind") in ("hold", "wait")
                                        or _g(rec, "decision", "hold") is True))
    fallbacks = sum(1 for rec in recs if _g(rec, "decision", "fallback") is True)
    decided = sum(1 for rec in recs if _g(rec, "jev", "ok") is True)
    if holds_under_crit:
        add("high", "危急时仍在等待",
            f"{holds_under_crit} 条记录：存在 critical 路却选择了 hold/wait",
            "正常应被 emergency 覆盖兜底。若反复出现，检查对应路是否真的有"
            "可用救场候选（可能被护栏/冷却/买不起过滤光了）")
    if decided >= 10 and fallbacks / decided > 0.3:
        add("medium", "模型兜底率偏高",
            f"{fallbacks}/{decided} 条决策走了兜底（模型答案过期/无效）",
            "常见原因是决策与执行之间战场变化太快——检查是否有节奏空档；"
            "若兜底集中在某类动作，把该类动作的候选生成逻辑调稳")

    # 6) 未知执行结果占比（排除正常 hold）
    kinds: dict[str, int] = {}
    for rec in recs:
        k = _g(rec, "executed", "kind")
        if k:
            kinds[k] = kinds.get(k, 0) + 1
    bad_kinds = {k: n for k, n in kinds.items()
                 if k in ("blocked_geometry", "unsupported_layout", "skipped_not_responsive")}
    for k, n in sorted(bad_kinds.items(), key=lambda kv: -kv[1]):
        add("high" if k == "blocked_geometry" else "medium", f"执行被拦截：{k}",
            f"{n} 次", "见对应拦截的 note 字段；blocked_geometry=几何熔断，"
            "unsupported_layout=未知地图，skipped_not_responsive=时钟没走")

    # 7) 点卡被拒率异常（2026-09-26 新增）：分诊后仍然"记了成本下界"的拒绝
    #    说明账面价和游戏实价对不上（动态涨价/换卡池/绑定错位），每次都是一个
    #    白费的决策周期。同步捕捉"点到了相邻卡槽"——那是几何偏移的新信号。
    rejected = [rec for rec in recs if _g(rec, "executed", "kind") == "pick_rejected"]
    price_rejected = [
        rec for rec in rejected
        if _g(rec, "executed", "game_responsive") is True
        and _g(rec, "executed", "slot_ready") is not False
        and "未记成本" not in (_g(rec, "executed", "note") or "")
        and "坐标偏移" not in (_g(rec, "executed", "note") or "")
    ]
    offslot = [rec for rec in rejected if "坐标偏移" in (_g(rec, "executed", "note") or "")]
    if price_rejected and len(price_rejected) / max(1, len(recs)) > 0.05:
        samples = "；".join(
            f"{_g(r, 'executed', 'plant')} 账面价{_g(r, 'executed', 'book_cost')}"
            f"/阳光{_g(r, 'executed', 'sun')}"
            for r in price_rejected[:4])
        add("high", "点卡被拒偏多（价格校准失真？）",
            f"{len(price_rejected)}/{len(recs)} 条决策点卡被拒且记了成本下界：{samples}",
            "图鉴写了部分植物「场上每多一张 +100」（price_increment）——成本模型"
            "应已按株数动态加价；若仍被拒，核对该卡 runtime 学习值（plant_costs.json）"
            "和卡面价，必要时删除污染条目重学")
    if offslot:
        add("medium", "点卡点到了相邻卡槽",
            f"{len(offslot)} 次点击拿起了别的卡（光标证据）",
            "卡条坐标偏移信号：重跑 tools/measure_layout.py 并肉眼复核 card_center")

    # 8) 铲掉换阳光：返还校验（2026-09-26 夜战结论：返还**落在草坪上要拾取**，
    #    即时读数为 0 是常态 —— 所以这里只报"阳光反而变少"的真异常）
    salvs = [rec for rec in recs
             if _g(rec, "executed", "kind") == "salvaged"
             and _g(rec, "executed", "completed") is True]
    if salvs:
        losses = []
        for rec in salvs:
            steps = _g(rec, "executed", "steps") or []
            last = steps[-1] if steps else {}
            before, after = last.get("sun_before"), last.get("sun_after")
            if before is not None and after is not None and after < before - 25:
                losses.append(f"{rec.get('iso', '?')} 铲前{before}/铲后{after}")
        if losses:
            add("high", "salvage 后阳光反而变少（铲错目标或返还丢失）",
                f"{len(losses)}/{len(salvs)} 次 salvage 阳光下降超 25："
                + "；".join(losses[:4]),
                "看 steps 的 cell 是否铲到了别的植物；若目标正确，检查返还掉落"
                "是否被 SunTracker 抑制没能拾取（salvage 后应 forgive 该格）")
        elif salvs:
            add("medium", "salvage 已启用（返还为草坪拾取，即时读数≈0 属正常）",
                f"{len(salvs)} 次 salvage，即时阳光差普遍为 0 —— 返还是草坪掉落物，"
                "由收阳光流程拾取；确认下一两拍阳光有上涨即为健康",
                "长期目标：若 coin 池验证阳光在内存中，可改为内存驱动拾取，"
                "返还即时入账且零漏收")

    return findings


def kpis(recs: list[dict]) -> dict:
    placed = sum(1 for r in recs if _g(r, "executed", "placed") is True)
    shovels = sum(1 for r in recs if _g(r,'executed','removed') is True
                  or any(s.get('step') in ('source_removed','salvaged')
                         for s in (_g(r,'executed','steps',default=[]) or [])))
    transaction_placements=sum(s.get('step')=='plant_confirmed' for r in recs
                              for key in ('executed','followup')
                              for s in (_g(r,key,'steps',default=[]) or []))
    holds = sum(1 for r in recs if _g(r, "executed", "kind") == "hold")
    fallbacks = sum(1 for r in recs if _g(r, "decision", "fallback") is True)
    lat = [_g(r, "jev", "latency_s") for r in recs if _g(r, "jev", "ok") is True]
    suns = [_g(r, "state", "game", "sun") for r in recs]
    return dict(
        decisions=len(recs),
        placed=placed,
        placement_failures=sum(_g(r,'executed','kind')=='click' and _g(r,'executed','placed') is False for r in recs),
        transaction_placements=transaction_placements,
        incomplete_transactions=sum(_g(r,k,'kind')=='transaction_incomplete' for r in recs for k in ('executed','followup')),
        shovels=shovels,
        holds=holds,
        fallbacks=fallbacks,
        jev_ok=sum(1 for r in recs if _g(r, "jev", "ok") is True),
        jev_latency_avg=round(sum(lat) / len(lat), 2) if lat else None,
        sun_first=suns[0] if suns else None,
        sun_last=suns[-1] if suns else None,
        clock_last=_g(recs[-1], "state", "game", "clock") if recs else None,
        scene=_g(recs[0], "state", "game", "scene") if recs else None,
    )


def render(recs: list[dict], path: str, findings: list[dict]) -> str:
    k = kpis(recs)
    lines = [
        f"# 战斗复盘 · {os.path.basename(path)}",
        "",
        f"- 场景：{k['scene']}　最后 clock：{k['clock_last']}　阳光：{k['sun_first']} → {k['sun_last']}",
        f"- 决策 {k['decisions']} 条｜普通种植确认 {k['placed']}｜铲除 {k['shovels']}｜"
        f"等待 {k['holds']}｜兜底 {k['fallbacks']}",
        f"- Jev 可用 {k['jev_ok']}/{k['decisions']}，平均延迟 {k['jev_latency_avg']}s",
        f"- 普通落点未确认 {k['placement_failures']}｜事务种植确认 {k['transaction_placements']}｜事务中断 {k['incomplete_transactions']}",
        "",
        "## 发现（按严重度）",
        "",
    ]
    summaries=[]
    for i,group in enumerate(split_battles(recs),1):
        b=kpis(group)
        summaries.append(f"- 第 {i} 局：决策 {b['decisions']}｜种上 {b['placed']}｜等待 {b['holds']}｜"
                         f"阳光 {b['sun_first']} → {b['sun_last']}｜末帧 clock {b['clock_last']}")
    lines[7:7]=['## 分局统计','']+summaries+['']
    if not findings:
        lines.append("本局没有触发任何已知坏决策模式。👍")
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for f in sorted(findings, key=lambda f: order.get(f["severity"], 9)):
        lines += [f"### [{f['severity'].upper()}] {f['kind']}",
                  f"- 证据：{f['evidence']}",
                  f"- 建议：{f['suggestion']}", ""]
    lines += ["## 闸门", "",
              "以上只是**建议**。任何评分/教条改动必须：人确认 → 改代码/教条 →",
              "补回归测试 → 全套测试通过 → 提交。复盘器永远不会自动改策略。", ""]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else latest_decisions()
    if not path or not os.path.exists(path):
        print("找不到决策日志（out/decisions_*.jsonl）", file=sys.stderr)
        return 2
    recs = load_records(path)
    if not recs:
        print(f"{path} 里没有可解析的记录", file=sys.stderr)
        return 2
    findings = detect(recs)
    report = render(recs, path, findings)
    stamp = os.path.splitext(os.path.basename(path))[0].replace("decisions_", "")
    out_path = os.path.join(OUT_DIR, f"battle_review_{stamp}.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    k = kpis(recs)
    print(f"复盘完成：{out_path}")
    print(f"  决策 {k['decisions']} ｜ 种上 {k['placed']} ｜ 铲除 {k['shovels']} ｜ "
          f"兜底 {k['fallbacks']} ｜ Jev ok {k['jev_ok']}")
    for f in sorted(findings, key=lambda f: {"critical": 0, "high": 1, "medium": 2}.get(f["severity"], 9)):
        print(f"  [{f['severity'].upper()}] {f['kind']} —— {f['evidence'][:90]}")
    if not findings:
        print("  未触发任何已知坏决策模式 👍")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

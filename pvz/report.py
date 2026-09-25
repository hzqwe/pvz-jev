"""把 decisions.jsonl 渲染成一份自包含的 HTML 复盘。

关注点：让"Jev 在每一步看到了什么、怎么选、有多大把握"一目了然。
"""

from __future__ import annotations

import html
import json
import os
from collections import Counter

CSS = """
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1e222b;--line:#2a2f3a;
--fg:#e6e9ef;--dim:#9aa3b2;--accent:#7cc4ff;--good:#5ad19a;--warn:#ffc46b;--bad:#ff7a7a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{padding:24px 32px;border-bottom:1px solid var(--line);background:var(--panel)}
h1{margin:0 0 6px;font-size:20px;font-weight:600}
h1 span{color:var(--accent)}
.sub{color:var(--dim);font-size:13px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-top:16px}
.stat{background:var(--panel2);border:1px solid var(--line);border-radius:8px;
padding:10px 14px;min-width:110px}
.stat b{display:block;font-size:19px;color:var(--accent)}
.stat i{font-style:normal;color:var(--dim);font-size:12px}
main{padding:24px 32px;max-width:1180px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-bottom:16px}
.card h2{margin:0 0 10px;font-size:15px;color:var(--dim);font-weight:500}
.head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.idx{font-weight:600;color:var(--accent)}
.ts{color:var(--dim);font-size:12px}
pre{background:#0b0d11;border:1px solid var(--line);border-radius:8px;padding:12px;
overflow:auto;font:12px/1.5 "Cascadia Mono",Consolas,monospace;color:#cbd5e1;margin:10px 0}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:860px){.grid2{grid-template-columns:1fr}}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;
border:1px solid var(--line);color:var(--dim);margin-left:6px}
.tag.good{color:var(--good);border-color:#245c43}
.tag.warn{color:var(--warn);border-color:#5c4a24}
.tag.bad{color:var(--bad);border-color:#5c2a2a}
.ans{margin:8px 0}
.ans .q{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.ans .v{font-weight:600}
.bar{height:8px;background:#0b0d11;border-radius:4px;overflow:hidden;margin-top:4px}
.bar>i{display:block;height:100%;background:var(--accent)}
.bar>i.sel{background:var(--good)}
.row{display:flex;align-items:center;gap:10px;margin:4px 0;font-size:13px}
.row .lbl{min-width:64px;color:var(--dim)}
.row .bar{flex:1;margin:0}
.dec{border-left:3px solid var(--accent);padding-left:12px;margin-top:10px}
.dec.hold{border-left-color:var(--warn)}
.dec.fb{border-left-color:var(--bad)}
.note{color:var(--warn);font-size:12px;margin-top:6px}
.err{color:var(--bad)}
details summary{cursor:pointer;color:var(--dim);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px}
td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left}
th{color:var(--dim);font-weight:500}
"""


def _esc(x) -> str:
    return html.escape(str(x))


def _pbar(prob: float, selected: bool = False) -> str:
    cls = "sel" if selected else ""
    return f'<div class="bar"><i class="{cls}" style="width:{max(0.0, min(1.0, prob)) * 100:.1f}%"></i></div>'


def render_report(log_path: str, out_path: str) -> str:
    records: list[dict] = []
    try:
        with open(log_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except OSError:
        records = []

    n = len(records)
    errs = sum(1 for r in records if not r.get("jev", {}).get("ok"))
    holds = sum(1 for r in records if r.get("decision", {}).get("hold"))
    fbs = sum(1 for r in records if r.get("decision", {}).get("fallback"))
    lat = [r["jev"].get("latency_s", 0) for r in records if r.get("jev")]
    avg_lat = sum(lat) / len(lat) if lat else 0.0
    confs = [r["decision"].get("confidence") for r in records]
    confs = [c for c in confs if isinstance(c, (int, float))]
    avg_conf = sum(confs) / len(confs) if confs else 0.0
    chosen = Counter(
        (r.get("decision", {}).get("chosen") or "无")[:70] for r in records
    )
    urgency = Counter(
        r["jev"]["answers"].get("urgency", {}).get("summary", "-") for r in records if r.get("jev")
    )

    parts: list[str] = []
    parts.append(f"""<header>
<h1>PvZ <span>×</span> Jev — 决策复盘</h1>
<div class="sub">日志 {_esc(os.path.abspath(log_path))}</div>
<div class="stats">
  <div class="stat"><b>{n}</b><i>决策次数</i></div>
  <div class="stat"><b>{holds}</b><i>选择保留阳光</i></div>
  <div class="stat"><b>{fbs}</b><i>启发式兜底</i></div>
  <div class="stat"><b class="{'err' if errs else ''}">{errs}</b><i>Jev 调用失败</i></div>
  <div class="stat"><b>{avg_lat:.2f}s</b><i>平均延迟</i></div>
  <div class="stat"><b>{avg_conf:.2f}</b><i>平均置信度</i></div>
</div></header><main>""")

    parts.append('<div class="card"><h2>Jev 最常选择的动作</h2><table><tr><th>动作</th><th>次数</th></tr>')
    for k, v in chosen.most_common(12):
        parts.append(f"<tr><td>{_esc(k)}</td><td>{v}</td></tr>")
    parts.append("</table></div>")

    if urgency:
        parts.append('<div class="card"><h2>局势紧迫度分布（Jev score 问题）</h2><table><tr><th>判断</th><th>次数</th></tr>')
        for k, v in urgency.most_common():
            parts.append(f"<tr><td>{_esc(k)}</td><td>{v}</td></tr>")
        parts.append("</table></div>")

    for i, r in enumerate(records):
        dec = r.get("decision", {})
        jev = r.get("jev", {})
        cls = "dec"
        if dec.get("hold"):
            cls += " hold"
        if dec.get("fallback"):
            cls += " fb"
        tags = []
        if dec.get("hold"):
            tags.append('<span class="tag warn">保留阳光</span>')
        if dec.get("fallback"):
            tags.append('<span class="tag bad">兜底</span>')
        if jev.get("ok") is False:
            tags.append('<span class="tag bad">Jev 失败</span>')
        if dec.get("threat_lane"):
            tags.append(f'<span class="tag">最危险: 第 {dec["threat_lane"]} 路</span>')
        if dec.get("urgency") is not None:
            tags.append(f'<span class="tag">紧迫度 {dec["urgency"]}/3</span>')
        if isinstance(dec.get("confidence"), (int, float)):
            tags.append(f'<span class="tag">conf {dec["confidence"]:.2f}</span>')

        parts.append(f'<div class="card"><div class="head"><div>'
                     f'<span class="idx">#{i + 1}</span> {_esc(r.get("iso", ""))}{"".join(tags)}</div>'
                     f'<div class="ts">Jev {jev.get("latency_s", "-")}s · {_esc(jev.get("model", ""))}</div></div>')

        parts.append(f'<div class="{cls}"><b>决定：</b>{_esc(dec.get("chosen") or "无")}')
        if r.get("executed"):
            parts.append(f'<div class="ts">执行：{_esc(json.dumps(r["executed"], ensure_ascii=False))}</div>')
        for note in dec.get("notes", []):
            parts.append(f'<div class="note">· {_esc(note)}</div>')
        parts.append("</div>")

        if jev.get("error"):
            parts.append(f'<div class="err">Jev 错误: {_esc(jev["error"])}</div>')

        ans = jev.get("answers", {})
        if ans:
            parts.append('<div class="grid2" style="margin-top:12px">')
            # 左：choice 概率
            parts.append('<div>')
            for qid, a in ans.items():
                if a.get("kind") != "choice":
                    continue
                raw = a.get("raw", {})
                probs = raw.get("probabilities") or {}
                parts.append(f'<div class="ans"><div class="q">{_esc(qid)} → <span class="v">{_esc(a.get("summary"))}</span></div>')
                for k, v in sorted(probs.items(), key=lambda kv: -kv[1])[:6]:
                    parts.append(f'<div class="row"><span class="lbl">{_esc(k)}</span>'
                                 f'{_pbar(float(v), k == raw.get("choice"))}<span class="ts">{float(v):.2f}</span></div>')
                parts.append("</div>")
            parts.append("</div>")
            # 右：noul / score
            parts.append('<div>')
            for qid, a in ans.items():
                if a.get("kind") == "choice":
                    continue
                parts.append(f'<div class="ans"><div class="q">{_esc(qid)} ({_esc(a.get("kind"))})</div>'
                             f'<div class="v">{_esc(a.get("summary"))}</div></div>')
            parts.append('<details style="margin-top:10px"><summary>候选动作（代码枚举，共 '
                         f'{len(r.get("candidates", []))} 条）</summary><table>'
                         '<tr><th>ID</th><th>启发式分</th><th>说明</th></tr>')
            for c in r.get("candidates", []):
                parts.append(f'<tr><td>{_esc(c["cid"])}</td><td>{c["score"]}</td><td>{_esc(c["desc"])}</td></tr>')
            parts.append("</table></details>")
            parts.append("</div></div>")

        parts.append('<details style="margin-top:10px"><summary>当时的战场（序列化前的文字视图）</summary>'
                     f'<pre>{_esc(r.get("board_text", ""))}</pre></details>')
        parts.append('<details style="margin-top:6px"><summary>发给 Jev 的 state (JSON)</summary>'
                     f'<pre>{_esc(json.dumps(r.get("state", {}), ensure_ascii=False, indent=2))}</pre></details>')
        parts.append("</div>")

    if not records:
        parts.append('<div class="card">日志为空。先跑一次 <code>tools/run_agent.py</code>。</div>')
    parts.append("</main>")

    html_doc = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                f"<title>PvZ × Jev 决策复盘</title><style>{CSS}</style></head><body>"
                + "".join(parts) + "</body></html>")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html_doc)
    return out_path

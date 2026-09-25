"""跑起来：让 Jev 接管这一局。

    python tools/run_agent.py --duration 120            # 试运行（不点鼠标，只决策+记日志）
    python tools/run_agent.py --live --duration 300     # 真操作游戏
    python tools/run_agent.py --live --foreground       # 全屏下 PostMessage 无效时用
    python tools/run_agent.py --report                  # 把日志渲染成 HTML 复盘
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pvz.agent import AgentConfig, PvZJevAgent  # noqa: E402
from pvz.jev import DecisionLog                 # noqa: E402
from pvz.report import render_report            # noqa: E402


def main() -> int:
    # ⚠️ stdout 重定向到文件时 Python 会**块缓冲**，于是 `> out/live.log` 得到的
    #    文件在整个运行期间都是 0 字节，看起来像"agent 没启动"（实测踩过两次）。
    #    打开行缓冲，日志才能实时 tail。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="PvZ × Jev 自动对战")
    ap.add_argument("--live", action="store_true", help="真正操作游戏（默认只决策不点击）")
    ap.add_argument("--foreground", action="store_true", help="用真实光标点击（全屏推荐）")
    ap.add_argument("--duration", type=float, default=120.0, help="运行秒数")
    ap.add_argument("--interval", type=float, default=3.0, help="每隔多少秒向 Jev 要一次决策")
    ap.add_argument("--wait-play", type=float, default=900.0,
                    help="最多等多少秒进入对局（选卡界面的等待**不计入** --duration）")
    ap.add_argument("--log", default=os.path.join(ROOT, "out", "decisions.jsonl"))
    ap.add_argument("--report", action="store_true", help="只把已有日志渲染成 HTML 后退出")
    ap.add_argument("--report-out", default=os.path.join(ROOT, "out", "report.html"))
    args = ap.parse_args()

    if args.report:
        path = render_report(args.log, args.report_out)
        print(f"复盘报告 -> {path}")
        return 0

    cfg = AgentConfig(
        dry_run=not args.live,
        foreground=args.foreground,
        decide_every_s=args.interval,
        log_path=args.log,
    )
    agent = PvZJevAgent(cfg)
    mode = "真实操作" if args.live else "试运行（不点击）"
    print(f"=== PvZ × Jev 启动 | 模式: {mode} | 时长: {args.duration:.0f}s ===")
    try:
        stats = agent.run(duration_s=args.duration, wait_play_s=args.wait_play)
        print("\n" + stats.summary())
    except KeyboardInterrupt:
        print("\n已中断。" + agent.stats.summary())
    finally:
        agent.close()
        print(f"Jev 调用统计: {agent.jev.stats()}")

    if os.path.exists(args.log):
        print(f"复盘报告 -> {render_report(args.log, args.report_out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

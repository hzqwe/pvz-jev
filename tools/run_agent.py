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
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pvz.agent import AgentConfig, PvZJevAgent  # noqa: E402
from pvz.jev import DecisionLog                 # noqa: E402
from pvz.report import render_report            # noqa: E402


class _Tee:
    """把 stdout / stderr **同时**写到控制台和一个文件。

    ⚠️ 为什么必需（2026-09-26 排查"卡死"时吃了这个亏）：用户是**双击 .bat**
    跑的，窗口一关，控制台里那些 `[暂停]` / `[窗口]` / `[唤醒]` 的诊断行
    就全没了 —— 只剩决策日志，而决策日志里**看不到窗口/暂停这条线**，
    根本没法定位"到底是哪一步撞进了游戏的脆弱期"。
    有了它，用户只要把 `out/console_*.log` 发过来就行。
    """

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
            except Exception:  # noqa: BLE001
                pass
        return len(s)

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:  # noqa: BLE001
                pass

    def reconfigure(self, **kw):
        for st in self._streams:
            try:
                st.reconfigure(**kw)
            except Exception:  # noqa: BLE001
                pass

    def isatty(self) -> bool:
        return False


def tee_console(path: str):
    """开始把控制台输出同时写进 `path`。返回文件对象（调用方负责关）。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fh = open(path, "w", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = _Tee(sys.__stdout__, fh)          # type: ignore[assignment]
    sys.stderr = _Tee(sys.__stderr__, fh)          # type: ignore[assignment]
    return fh


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
    ap.add_argument("--allow-window-ops", action="store_true",
                    help="允许改变游戏窗口状态（恢复最小化 / 抢焦点 / minimize-restore 唤醒）。"
                         "⚠️ 默认关闭：对 DirectDraw 游戏这些动作可能把游戏连同桌面一起卡死")
    ap.add_argument("--duration", type=float, default=0.0, help="运行秒数；0 为持续运行（默认）")
    ap.add_argument("--interval", type=float, default=3.0, help="每隔多少秒向 Jev 要一次决策")
    ap.add_argument("--wait-play", type=float, default=900.0,
                    help="启动时最多等多少秒找到游戏进程；已有游戏的菜单/选卡不会超时")
    ap.add_argument("--game-missing-timeout", type=float, default=120.0,
                    help="曾检测到的游戏进程持续消失多少秒后退出（默认 120）")
    ap.add_argument("--log", default=os.path.join(ROOT, "out", "decisions.jsonl"))
    ap.add_argument("--console-log", default=None,
                    help="把控制台输出同时写进这个文件（默认 out/console_<时间戳>.log）。"
                         "双击 .bat 跑时窗口会关掉，这份日志是唯一证据来源。")
    ap.add_argument("--no-console-log", action="store_true",
                    help="不要写控制台日志文件")
    ap.add_argument("--report", action="store_true", help="只把已有日志渲染成 HTML 后退出")
    ap.add_argument("--report-out", default=os.path.join(ROOT, "out", "report.html"))
    args = ap.parse_args()

    # ⚠️ 必须在打印任何东西**之前**挂上 tee，否则启动横幅就漏了。
    console_fh = None
    if not args.no_console_log:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        cpath = args.console_log or os.path.join(ROOT, "out", f"console_{stamp}.log")
        console_fh = tee_console(cpath)
        print(f"控制台日志 -> {cpath}")

    try:
        return _run(args)
    finally:
        # 把控制台日志刷盘再关。卡死场景下进程可能被看门狗 os._exit 掉，
        # 所以这里也依赖行缓冲（buffering=1）已经写出去的内容。
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        if console_fh is not None:
            try:
                console_fh.flush()
                console_fh.close()
            except Exception:  # noqa: BLE001
                pass


def _run(args) -> int:
    if args.report:
        path = render_report(args.log, args.report_out)
        print(f"复盘报告 -> {path}")
        return 0

    cfg = AgentConfig(
        dry_run=not args.live,
        foreground=args.foreground,
        allow_window_ops=args.allow_window_ops,
        decide_every_s=args.interval,
        log_path=args.log,
        game_missing_timeout_s=max(0.0,args.game_missing_timeout),
    )
    agent = PvZJevAgent(cfg)
    mode = "真实操作" if args.live else "试运行（不点击）"
    win_mode = "可动窗口" if args.allow_window_ops else "安全模式（不动窗口）"
    duration = f'{args.duration:.0f}s' if args.duration > 0 else '持续运行'
    print(f"=== PvZ × Jev 启动 | 模式: {mode} | {win_mode} | 时长: {duration} ===")
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

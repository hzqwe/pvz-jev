"""双击启动器：不经过 WorkBuddy，自己把 Jev 拉起来玩一局。

正常用法是双击项目根目录的 `启动Jev.bat`；也可以直接：

    python tools/launch.py

这里只做三件事：**检查环境 → 给个菜单 → 调 run_agent 真正开跑**。
决策与操作逻辑一律不在这里重复实现（免得两处行为不一致）。

设计上刻意保持零第三方依赖 —— 只 import 标准库和本项目自己的模块。
"""

from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
OUT = os.path.join(ROOT, "out")

for p in (ROOT, TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)

# 中文/emoji 打到 cp936 控制台会 UnicodeEncodeError 直接崩。
# errors="replace" 保证最坏情况只是显示成 '?'，不会把启动器弄死。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True, errors="replace")
    except (AttributeError, ValueError):
        pass


LINE = "=" * 62
STOP_FILE = os.path.join(OUT, "STOP")
# 实战模式开始前的倒计时：留时间让用户把焦点点回游戏窗口。
COUNTDOWN_S = 5


def clear_stop() -> None:
    """清掉上一轮可能留下的紧急停止标记。

    ⚠️ 必须在**任何分支之前**无条件执行：否则用户上次按过 STOP、这次在菜单里
    就退出，标记会一直留着，下一次真开跑时第一轮就立刻停 —— 看起来像"启动器坏了"。
    """
    try:
        if os.path.exists(STOP_FILE):
            os.remove(STOP_FILE)
    except OSError:
        pass

# 菜单：(按键, 说明, 配置)。配置为 None 表示退出。
# duration 是"真正对局的秒数"，选卡等待不算在内。
# window_ops=False（安全模式）：绝不改变游戏窗口状态，只读内存 + 后台点击。
#   实测"按回车瞬间卡死"就是 agent 去 ShowWindow 一个 DirectDraw 窗口造成的，
#   所以安全模式是**默认**。
MENU: list[tuple[str, str, dict | None]] = [
    ("1", "实战运行 30 分钟   （安全模式：绝不动游戏窗口）",
     {"live": True, "duration": 1800, "window_ops": False}),
    ("2", "实战运行 5 分钟    （安全模式）",
     {"live": True, "duration": 300, "window_ops": False}),
    ("3", "试运行：只看 Jev 怎么决策，不点鼠标",
     {"live": False, "duration": 180, "window_ops": False}),
    ("4", "完整模式（允许恢复最小化/抢焦点 —— 有卡死风险，需手打 YES）",
     {"live": True, "duration": 1800, "window_ops": True}),
    ("0", "退出", None),
]
DEFAULT_KEY = "1"


def banner() -> None:
    print(LINE)
    print("  PvZ 杂交版 × Jev 自动对战")
    print("  感知/枚举/量化 = 代码    取舍 = Jev    校验/执行 = 代码")
    print(LINE)
    # 由 启动Jev.bat 传进来，出问题时能一眼看出用的是哪个解释器
    py = os.environ.get("PvZJEV_PY")
    if py:
        print(f"  解释器: {py}")
    print()


def check_api_key() -> str | None:
    """返回 key；取不到就打印修法并返回 None。"""
    from pvz.jev import JevError, load_api_key

    try:
        return load_api_key()
    except JevError as exc:
        print(f"✗ {exc}")
        print()
        print("  两种修法（选一个）：")
        print("    1) 把 key 写进文件：")
        print(f"       {os.path.join(os.path.expanduser('~'), '.workbuddy-ai', 'jev_api_key')}")
        print("    2) 设环境变量 TYPESAFE_API_KEY")
        return None


def game_status() -> tuple[int | None, str]:
    """返回 (pid, 说明)。**必须真的附着成功一次**才算数。

    ⚠️ 不能只看 `find_pid`（2026-09-26 实测）：
      * 游戏退出后 pid 仍会被枚举到、`OpenProcess` 也仍会成功（僵尸进程），
        于是"找到了"是假的，而且读到的还是定格的残留内存；
      * `pvzHE-Launcher.exe` 一直活着，也会被 `find_pid` 命中。
    只有 `BoardReader.attach()`（内含 exe 名 + LawnApp 双重校验）才算真找到。
    """
    from pvz.board import BoardReader

    r = BoardReader()
    try:
        if r.attach():
            return r.pid, "已附着游戏本体"
        return None, (r.notes[-1] if r.notes else "未找到游戏进程")
    finally:
        r.close()


def preflight() -> bool:
    """环境检查。返回 False 表示别继续了。"""
    entry = os.path.join(TOOLS, "run_agent.py")
    if not os.path.exists(entry):
        print(f"✗ 找不到 {entry}")
        print("  这个启动器必须在 pvz-jev 项目里运行。")
        return False

    if check_api_key() is None:
        return False

    pid, why = game_status()
    if pid:
        print(f"✓ 已附着游戏进程 (pid={pid})")
    else:
        # 这是最常见的失败原因：忘了先开游戏。让用户自己决定要不要等。
        print(f"⚠ 没检测到可用的游戏进程 —— {why}")
        print("  请先启动 PvZ 杂交版并进入关卡。启动后 Jev 会自己等，最多等 15 分钟。")
        try:
            ans = input("  按回车继续，输入 q 退出：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if ans == "q":
            return False

    print()
    return True


def confirm_dangerous() -> bool:
    """完整模式前的二次确认 —— 必须**手打** `YES` 才放行。

    ⚠️ 为什么不能只是"回车确认"（2026-09-26，用户实测踩过）：
    用户描述"**按了回车瞬间进游戏，然后直接卡死**"。完整模式会做
    `ShowWindow(SW_RESTORE)` / `SetForegroundWindow` —— 对 DirectDraw 游戏
    这是会把游戏连同桌面一起卡死的动作（丢 primary surface、重建失败），
    卡住之后连任务管理器都退不出去。
    回车太容易被误按（上一步"按回车继续"就是回车），所以这里要求打 3 个字母，
    让"误入危险模式"变成一件**必须主动做**的事。
    """
    print()
    print("!" * 62)
    print("  ⚠ 完整模式：会恢复最小化窗口、抢焦点、必要时 minimize→restore 唤醒。")
    print("    对 DirectDraw 游戏这些动作**可能把游戏连同桌面一起卡死**，")
    print("    卡住之后连任务管理器都可能退不出去。")
    print("    除非你明确需要（比如游戏被最小化了想让我自己救回来），")
    print("    否则请改用安全模式（选项 1 / 2）。")
    print("!" * 62)
    try:
        ans = input('  确认要进完整模式，请输入大写 YES（其他任何输入 = 改回安全模式）：')
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if ans.strip() == "YES":
        print("  >>> 已确认：完整模式。\n")
        return True
    print("  >>> 未确认：已改回**安全模式**（绝不动游戏窗口）。\n")
    return False


def ask() -> dict | None:
    print("请选择：")
    for key, label, _cfg in MENU:
        tail = "   ← 默认，直接回车" if key == DEFAULT_KEY else ""
        print(f"  [{key}] {label}{tail}")
    print()

    try:
        raw = input(f"序号 [{DEFAULT_KEY}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    if not raw:
        raw = DEFAULT_KEY

    for key, label, cfg in MENU:
        if raw == key:
            if cfg is None:
                return None
            print(f"\n>>> {label}\n")
            if cfg.get("window_ops") and not confirm_dangerous():
                cfg = dict(cfg, window_ops=False)
            return cfg

    print(f"!! 没有 [{raw}] 这个选项，按默认走。\n")
    for key, _label, cfg in MENU:
        if key == DEFAULT_KEY:
            return cfg
    return None


def run(cfg: dict) -> int:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(OUT, exist_ok=True)

    # 每次跑用一个新日志：DecisionLog 是追加模式，共用文件会把多次对局混在一起。
    log = os.path.join(OUT, f"decisions_{stamp}.jsonl")
    report = os.path.join(OUT, "report.html")

    window_ops = bool(cfg.get("window_ops"))
    argv = ["run_agent.py"]
    if cfg["live"]:
        argv.append("--live")
    if window_ops:
        argv.append("--allow-window-ops")
    argv += [
        "--duration", str(cfg["duration"]),
        "--log", log,
        "--report-out", report,
        # 控制台日志由**启动器**统一管（它从菜单那一行就开始记了），
        # 免得两边各开一个文件、内容还割成两半。
        "--no-console-log",
    ]

    # 清掉上一轮可能留下的紧急停止标记，否则新的一开跑就立刻退出
    clear_stop()

    print(f"日志 -> {log}")
    print(f"复盘 -> {report}（跑完自动打开）")
    print()
    if cfg["live"]:
        if window_ops:
            print("⚠ 完整模式：会恢复最小化窗口、抢焦点、必要时 minimize→restore 唤醒。")
            print("  对 DirectDraw 游戏这些是**有风险**的动作，可能把游戏连同桌面一起卡死。")
        else:
            print("安全模式：我**不会**去动游戏窗口（不恢复、不抢焦点、不闪窗口）。")
            print("  请自己保证游戏一直开着且不要最小化 —— 最小化了我救不回来。")
        print()
        print("运行期间不要切窗口 / Alt+Tab —— PvZ 一失焦就会暂停。")
        print()
        print("★ 切屏进游戏时游戏自己会卡几秒（全屏切换，正常现象）。")
        print("  这段时间我会**完全不动手**：不抓屏、不点卡、不点草坪，安静等它缓过来。")
        print("  所以你会看到类似「[暂停] 时钟冻住 → 静默等待 8s」，那是正常的，别慌。")
        print()
    print("想停下来：直接按 Ctrl+C。")
    print(f"万一 Ctrl+C 没反应，就新建一个空文件 {STOP_FILE}（新建即可，内容随意）。")
    print()

    # ★ 关键：双击 .bat 之后**焦点在控制台**，而 PvZ 一失焦就暂停。
    #   安全模式又不会替你把游戏抢回前台，所以必须留时间让用户自己点回去。
    if cfg["live"]:
        print(LINE)
        print()
        print("  ★ 请现在点一下游戏窗口，让游戏保持在最前面。")
        print("    （这个控制台窗口不用管，它会在后台继续跑）")
        print()
        for i in range(COUNTDOWN_S, 0, -1):
            print(f"\r    {i} 秒后开始 …", end="", flush=True)
            time.sleep(1.0)
        print("\r" + " " * 30 + "\r", end="", flush=True)
        print("    开始。\n")
    else:
        print(LINE)
        print()

    import run_agent  # tools/ 已在 sys.path 上

    saved = sys.argv
    sys.argv = argv
    try:
        rc = run_agent.main()
    except KeyboardInterrupt:
        print("\n已手动中断。")
        rc = 0
    finally:
        sys.argv = saved

    if os.path.exists(report):
        print(f"\n复盘报告 -> {report}")
        if hasattr(os, "startfile"):
            try:
                os.startfile(report)  # type: ignore[attr-defined]
            except OSError as exc:
                print(f"（浏览器没打开：{exc} —— 手动双击上面的路径即可）")

    return rc or 0


def main() -> int:
    clear_stop()

    # ★ 从一开始就把控制台输出记到文件里（2026-09-26）。
    #   用户是**双击 .bat** 跑的，窗口一关什么都没了 —— 上一轮排查"卡死"时
    #   就是因为没有这份日志，只能靠猜。现在菜单、倒计时、agent 的每一行
    #   诊断（[暂停]/[窗口]/[唤醒]/[退出]）都会落进 out/console_*.log。
    console_fh = None
    try:
        import run_agent
        stamp = time.strftime("%Y%m%d_%H%M%S")
        cpath = os.path.join(OUT, f"console_{stamp}.log")
        console_fh = run_agent.tee_console(cpath)
        print(f"控制台日志 -> {cpath}")
        print("   （卡住的话把这个文件发我，里面能看到每一步在干什么）")
    except Exception as exc:  # noqa: BLE001 —— 记不上日志不该挡住玩
        print(f"（控制台日志没开起来：{exc}）")

    try:
        return _main_body()
    finally:
        if console_fh is not None:
            try:
                sys.stdout.flush()
                console_fh.flush()
                console_fh.close()
            except Exception:  # noqa: BLE001
                pass


def _main_body() -> int:
    banner()
    if not preflight():
        print("\n已退出。")
        return 1

    cfg = ask()
    if cfg is None:
        print("已退出。")
        return 0

    return run(cfg)


if __name__ == "__main__":
    raise SystemExit(main())

# PvZ 杂交版 × Jev 自动对战

让 Jev 根据当前战场选择植物，用代码完成感知、战术约束、操作和结果验证。

**Windows · Python 3.10+ · 经典杂交版 v3.9.9 · MIT**

Jev 是 TypeSafe 的 System One 决策模型，在本项目中负责候选行动之间的战术选择。
项目通过只读游戏内存获取阳光、卡组、植物、僵尸和地形，再生成合法行动供 Jev 取舍。
实际种植、平台连种、回收与换种通过鼠标或键盘完成，并读回战场确认结果。

[快速开始](#快速开始) · [命令行用法](#命令行用法) · [当前能力](#当前能力) · [日志与复盘](#日志与复盘) · [常见问题](#常见问题) · [开发与验证](#开发与验证)

> 最近更新：富余阳光时持续补阵，优先补强弱路，前期经济按列种植。
> 当前版本通过 479 项离线测试；测试和快照回放不代表实战胜率。
> [查看本次修复与证据](docs/continuous-formation-2026-10-01.md)

## 快速开始

### 1. 准备环境

- Windows，运行经典《植物大战僵尸杂交版》v3.9.9。
- Python 3.10 或更新版本；当前本机验证环境为 Python 3.13.12。
- 可用的 `curl.exe` 和 TypeSafe API Key，运行时需要网络访问 Jev。

核心运行模块使用 Python 标准库，无需安装第三方 Python 包。
个别截图或研究辅助工具可能有额外依赖，不属于正常启动流程。

下载仓库并进入项目目录：

```powershell
git clone https://github.com/hzqwe/pvz-jev.git
cd pvz-jev
```

### 2. 配置 API Key

启动器支持两种方式，任选一种：

| 方式 | 设置位置 | 适合场景 |
| --- | --- | --- |
| 本地文件 | `%USERPROFILE%\.workbuddy-ai\jev_api_key` | 双击启动；文件只放 Key 本身 |
| 环境变量 | `TYPESAFE_API_KEY` | 从已配置环境的终端启动 |

例如，在 PowerShell 当前会话中设置：

```powershell
$env:TYPESAFE_API_KEY = "你的 TypeSafe API Key"
python tools/launch.py
```

当前会话的环境变量只传给从该终端启动的程序；从资源管理器双击启动时，可使用本地文件方式。

### 3. 启动自动对战

1. 打开游戏，进入关卡并选好卡组。泳池要携带合适的睡莲，屋顶要携带花盆。
2. 双击项目根目录的 `启动Jev.bat`，选择 **1：持续实战运行**；也可以执行 `python tools/launch.py`。
3. 在倒计时内点回游戏窗口，让游戏保持运行。

启动器默认采用安全模式，不主动恢复最小化窗口或抢焦点。
菜单、选卡和换局期间会等待；首次寻找游戏进程的等待默认上限为 900 秒。
检测过的游戏进程持续消失 120 秒后退出，正常对局没有 30 分钟上限。

停止方式：按 **Ctrl+C**、关闭控制台，或在项目 `out/` 目录新建名为 `STOP` 的文件。
通过启动器再次运行会清除旧的停止标记。

> `启动Jev.bat` 自动以自身所在目录为项目根目录，支持移动仓库和带空格的路径。
> 请保留它与 `tools/` 的相对位置；也可以从项目根目录使用 `python tools/launch.py`。
> 启动器会寻找可用 Python；若命令落到 WindowsApps 占位程序，请改用真实解释器的完整路径。

## 命令行用法

下面的命令在项目根目录执行。`run_agent.py` 默认只决策和记日志；加上 `--live` 才会操作游戏。
试运行仍会请求 Jev，离线测试与日志回放不会。

```powershell
# 试运行 60 秒：只决策，不点击
python tools/run_agent.py --duration 60

# 持续自动对战
python tools/run_agent.py --live

# 自动对战 5 分钟
python tools/run_agent.py --live --duration 300

# 后台点击无效时，改用真实光标点击
python tools/run_agent.py --live --foreground

# 调整决策间隔和游戏进程消失后的等待时间
python tools/run_agent.py --live --interval 3 --game-missing-timeout 180

# 指定本次决策日志
python tools/run_agent.py --live --log out/decisions_manual.jsonl

# 查看完整参数
python tools/run_agent.py --help
```

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--duration` | `0` | 0 为持续运行，正数为运行秒数 |
| `--interval` | `3` | 向 Jev 请求决策的间隔，单位为秒 |
| `--wait-play` | `900` | 首次寻找游戏进程的等待上限 |
| `--game-missing-timeout` | `120` | 已见过的游戏进程持续消失后的退出等待 |
| `--foreground` | 关闭 | 使用真实光标点击 |
| `--allow-window-ops` | 关闭 | 允许恢复窗口、抢焦点和唤醒；部分 DirectDraw 环境曾出现卡死，按需启用 |

## 当前能力

| 方向 | 已实现的行为 |
| --- | --- |
| 前期经济 | 安全时继续补充稳定产阳光植物，按列建设，兼顾开局防线和强力植物储蓄 |
| 持续补阵 | 当前防守够用后仍利用富余阳光补强；优先弱路，逐步扩展受保护的攻击位置和前排防御 |
| 近屋救场 | 综合距离、血量、护甲、实际输出和小推车状态，优先处理可应对的近屋威胁 |
| 火力与配合 | 检查射程、前后关系、跨路覆盖、追踪伤害，以及机制已登记的过火组合 |
| 泳池与屋顶 | 检查睡莲或花盆支撑，以连续事务完成平台和后续植物种植 |
| 铲除与回收 | 支持回收高坚果拾卡续种、已确认压扁威胁前的提前回收，以及满足条件的经济植物换攻击 |
| 卡组识别 | 经典 v3.9.9 按版本目录识别实际 ID；识别身份、已知机制与可执行能力分别校验 |
| 操作验证 | 点击前重新读状态，检查冷却和完整预算；操作后确认植物、格子、光标或掉落卡变化 |
| 连续运行 | 等待菜单、选卡和换局，记录运行阶段，切换对局时重新检查知识和卡组 |

**当前仍有边界：**

- 主要适配经典 v3.9.9。其他游戏版本、特殊地图与特殊植物交互需要单独验证。
- 百科中的名字和参考条目不等于完整战斗机制；陌生卡不能保证已经会用，模仿者等特殊选卡操作尚未完整支持。
- 未验证的价格、伤害和机制不会直接当作救场保证；部分评分与输出估计属于启发式。
- 游戏失焦、暂停、窗口尺寸变化或平台不足可能导致无法操作；有钱也不意味着每个位置都能安全种植。
- 项目读取游戏状态并模拟输入，不修改游戏内存，不提供无限阳光等修改器能力。

## 工作流程

```mermaid
flowchart LR
    A[读取战场] --> B[生成合法候选]
    B --> C[Jev 判断]
    C --> D[救场与预算复查]
    D --> E[点击与事务执行]
    E --> F[读回确认并记录]
    F --> A
```

代码负责身份、冷却、地形、平台、费用和可执行性；Jev 在候选之间做结构化判断。
执行层再次检查战场，必要时让近屋救场或弱路修复优先，避免模型等待掩盖可执行的建设。

运行时只发送当前卡组和战场所需的知识摘要，不逐次发送整个百科。
快速补种复用确定性建设规则，不额外请求模型；Jev API 的额度与费用仍取决于实际调用和输入。

## 日志与复盘

启动器会为每次运行生成独立日志。产物保存在 `out/`，不提交到 Git。

| 文件 | 内容 |
| --- | --- |
| `decisions_<时间戳>.jsonl` | 战场快照、候选、Jev 回答、最终决策和执行证据 |
| `decisions_<时间戳>.events.jsonl` | 会话、对局切换和运行阶段事件 |
| `console_<时间戳>.log` | 控制台诊断、暂停、异常和退出信息 |
| `report.html` | 可浏览的决策复盘，启动器结束后尝试自动打开 |
| `battle_review_*.md` | 复盘器生成的问题、证据与改进建议 |

```powershell
# 分析启动器生成的最新一份对战日志
python tools/review_battle.py

# 指定日志分析
python tools/review_battle.py out/decisions_manual.jsonl

# 把指定日志生成 HTML
python tools/run_agent.py --report --log out/decisions_manual.jsonl --report-out out/report_manual.html
```

排查一次对局时，保留同一时间段的决策日志、事件日志和控制台日志。
复盘器只提供建议，不自动修改策略。控制器代码更新后需要退出旧进程并重新启动。

## 常见问题

| 现象 | 优先检查 |
| --- | --- |
| 找不到游戏或读不到战场 | 确认游戏版本、进程和关卡；菜单中没有活动战场是正常情况。用 `python tools/probe.py --list` 检查 |
| 画面正常却一直不种 | 点回游戏窗口，检查是否失焦或暂停；看控制台的时钟与响应状态 |
| 阳光很多却等待 | 查看候选和决策原因：是否近屋危急、卡片冷却、缺平台、落点被拒或模型超时；当前版本已修复防守够用就停建的问题 |
| 泳池或屋顶种不下去 | 检查目标格的平台、支撑卡冷却，以及平台加植物的总阳光是否足够 |
| 种歪或点到相邻卡 | 先核对窗口尺寸和布局；必要时重新测量并检查网格，不能把坐标错误当成植物涨价 |
| 换卡组后植物身份不对 | 查看启动身份审计，确认版本目录和已有绑定；先运行知识审计，不要直接按卡槽顺序猜 ID |
| 启动脚本找不到项目 | 确认下载了完整仓库，`.bat` 与 `tools/` 在同一层；或从项目根目录执行 `python tools/launch.py` |

需要检查几何时，在活动关卡中运行：

```powershell
python tools/measure_layout.py --write
python tools/overlay_grid.py
```

`--write` 会更新 `data/layout.json`。这些诊断工具可能读取截图或操作窗口，建议先停止自动对战再运行。

## 开发与验证

核心代码及数据的位置：

| 路径 | 职责 |
| --- | --- |
| `pvz/board.py`、`pvz/win32.py` | 战场读取、进程、窗口和输入基础 |
| `pvz/catalog.py`、`pvz/plants.py`、`pvz/mechanics.py` | 版本身份、植物知识、机制和成本 |
| `pvz/tactics.py`、`pvz/policy.py` | 战力评估、合法候选、Jev 提问和决策合并 |
| `pvz/agent.py`、`pvz/transactions.py` | 主循环、实时复查、连种、回收与换种 |
| `pvz/geometry.py`、`pvz/ui.py` | 网格、卡槽坐标和点击验证 |
| `data/versions/classic-3.9.9/` | 固定版本目录、机制、百科资料与来源 |
| `data/hybrid_plants.json`、`data/plant_playbook.json` | 已登记植物档案和使用原则 |
| `data/layout.json`、`data/plant_ids.json`、`data/plant_costs.json` | 布局、已有绑定和成本观测 |
| `tools/`、`docs/` | 测试、诊断、离线回放、调查与修复记录 |

分类入口：[工具与测试](tools/README.md) · [知识数据](data/README.md) · [文档索引](docs/README.md) · [维护约定](CONTRIBUTING.md)

```powershell
# 全套离线回归，不请求 Jev、不操作游戏
python -m unittest discover -s tools -p 'test_*.py' -q

# 审计某组植物的身份与机制覆盖
python tools/audit_knowledge.py --game-version 3.9.9 --deck 86 16 67 66

# 检查历史快照在当前策略下是否产生非法种植候选
python tools/replay_battle_review.py --game-version 3.9.9 --log out/decisions_manual.jsonl

# 沿用历史 Jev 回答，检查等待是否改选为建设
python tools/replay_formation_waits.py --log out/decisions_manual.jsonl --output out/formation_waits_replay.json
```

回放逐个使用历史快照，不模拟新操作之后的战场演化，不能据此推断实际胜率。
核心回归之外，`selftest_decision.py` 等模型联调工具会真实调用 API，按需运行。

## 更新与资料

| 日期 / 主题 | 文档 |
| --- | --- |
| 2026-10-01：富余阳光持续补阵 | [建设规则、等待回放与边界](docs/continuous-formation-2026-10-01.md) |
| 2026-09-30：弱路火力与安全换种 | [逐路战力复盘](docs/lane-firepower-2026-09-30.md) |
| 2026-09-30：向日葵运营 | [持续经济修复](docs/economy-growth-2026-09-30.md) |
| 2026-09-30：屋顶连种与执行一致性 | [平台与炸弹](docs/roof-followup-2026-09-30.md)、[可靠性审查](docs/reliability-fixes-2026-09-30.md) |
| 2026-09-29：百科和关系扩展 | [来源与知识边界](docs/knowledge-expansion-sources-2026-09-29.md) |
| 2026-09-28：连续运行、压扁前回收 | [跨局运行](docs/battle-review-2026-09-28-continuous.md)、[回收保护](docs/crusher-recovery-2026-09-28.md) |
| 版本目录与知识来源 | [资料调查](docs/knowledge-source-research-2026-09-28.md)、[目录实施](docs/versioned-catalog-result-2026-09-28.md) |
| 视觉补充方案 | [设计说明，非已完成能力](docs/vision-integration-2026-09-29.md) |
| 整理前的完整 README | [历史开发与调研记录](docs/readme-history-2026-10-01.md) |

## 许可证与致谢

项目代码采用 [MIT License](LICENSE)。版本身份资料的来源与提交记录见
[sources.json](data/versions/classic-3.9.9/sources.json)，其上游许可证见
[LICENSE.upstream.txt](data/versions/classic-3.9.9/LICENSE.upstream.txt)。

本仓库不包含《植物大战僵尸》及杂交版游戏本体资源。
感谢经典杂交版作者「潜艇伟伟迷」、百科资料维护者与相关开源项目提供的参考。

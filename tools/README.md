# 工具与测试

[项目说明](../README.md) · [知识数据](../data/README.md) · [维护约定](../CONTRIBUTING.md)

本目录保留原有脚本路径。测试共用夹具、启动器导入和历史文档都引用这些入口，
分类由本页提供；使用时在项目根目录执行 `python tools/<脚本名>.py`。

## 启动与日常复盘

| 工具 | 用途 | 是否请求 Jev / 操作游戏 |
| --- | --- | --- |
| [launch.py](launch.py) | 与双击启动脚本相同的交互菜单 | 默认选项会请求 Jev 并操作游戏 |
| [run_agent.py](run_agent.py) | 主控制器与 HTML 报告入口 | 对战模式请求 Jev；`--live` 才操作游戏；`--report` 只生成报告 |
| [review_battle.py](review_battle.py) | 从实际日志提取问题与建议 | 离线，不请求 Jev、不操作游戏 |
| [replay_battle_review.py](replay_battle_review.py) | 检查历史快照在当前策略下的候选合法性 | 离线，不请求 Jev、不操作游戏 |
| [replay_formation_waits.py](replay_formation_waits.py) | 沿用历史回答，检查等待是否改选建设 | 离线，不请求 Jev、不操作游戏 |

常用命令：

```powershell
python tools/launch.py
python tools/run_agent.py --duration 60
python tools/review_battle.py
python tools/replay_battle_review.py --game-version 3.9.9 --log out/decisions_manual.jsonl
```

试运行 `run_agent.py --duration 60` 仍会消耗 API 调用，只是不点击。
离线回放不模拟新动作之后的战场变化，不等于胜率测试。

## 知识审计与数据维护

| 工具 | 用途 |
| --- | --- |
| [audit_knowledge.py](audit_knowledge.py) | 审计版本身份、已绑定机制、缺失字段与冲突 |
| [audit_domain.py](audit_domain.py) | 审计百科、僵尸特征和植物关系 |
| [import_version_catalog.py](import_version_catalog.py) | 从固定且经过哈希校验的上游文本导入目录；会写入输出目录 |
| [bind_cards.py](bind_cards.py) | 旧卡组模板的人工辅助绑定；`--dry` 只检查，正常模式会写 `plant_ids.json` |

```powershell
python tools/audit_knowledge.py --game-version 3.9.9 --deck 86 16 67 66
python tools/audit_domain.py
python tools/bind_cards.py --dry
```

版本目录已支持真实 ID 识别。只有核实了旧卡组模板与版本后才使用人工绑定，
不要用 `--force` 掩盖身份不一致，也不要按槽位给陌生卡猜名字。

## 诊断、几何与实验

以下工具主要为现场排查保留，部分会抓屏、恢复窗口、点击、铲除或写布局。
运行前先停止自动对战，并查看脚本说明；它们不属于离线回归命令。

| 分类 | 脚本 |
| --- | --- |
| 进程与战场 | [probe.py](probe.py)、[diag_live.py](diag_live.py)、[diag_hang.py](diag_hang.py)、[dump_plants.py](dump_plants.py)、[look_drops.py](look_drops.py) |
| 光标与暂停 | [probe_cursor.py](probe_cursor.py)、[probe_pause.py](probe_pause.py)、[exp_wake.py](exp_wake.py) |
| 截图与网格 | [look.py](look.py)、[measure_layout.py](measure_layout.py)、[overlay_grid.py](overlay_grid.py)、[overlay_cards.py](overlay_cards.py) |
| 直接种植实验 | [click_test.py](click_test.py)、[place_test.py](place_test.py)、[plant_experiment.py](plant_experiment.py)、[probe_rows.py](probe_rows.py) |
| 图像与偏移研究 | [crop.py](crop.py)、[scan_green.py](scan_green.py)、[find_gridlines.py](find_gridlines.py)、[find_strides.py](find_strides.py)、[make_doc_screenshot.py](make_doc_screenshot.py) |

`measure_layout.py --write` 会更新布局；`probe.py --jev` 会调用 API。
图像研究工具可能需要 Pillow 等额外依赖，核心控制器无需这些包。

## 测试与模型联调

**日常离线回归：**

```powershell
python -m unittest discover -s tools -p 'test_*.py' -q
```

`test_*.py` 是回归集，覆盖策略、成本、地图、关系、事务、版本目录、日志和启动器。
它们使用构造局面、模拟输入或固定日志夹具，不应连接真实 Jev 或接管真实游戏。
启动器测试只启动测试替身，验证移动目录、中文与空格路径以及参数传递。

| 其他自检 | 用途 |
| --- | --- |
| [selftest_policy.py](selftest_policy.py) | 构造局面检查候选与规则 |
| [selftest_hang.py](selftest_hang.py) | Windows 窗口/挂起相关模拟自检 |
| [selftest_decision.py](selftest_decision.py) | 合成战场调用真实 Jev；会消耗 API 调用 |
| [selftest_strategy_jev.py](selftest_strategy_jev.py) | 策略与真实 Jev 的联调；会消耗 API 调用 |

## 日志夹具

[fixtures/](fixtures/) 存放经过挑选的历史战场快照，用来复现已修复的问题。
当前包括早期经济、逐路火力、屋顶平台后续与富余建设场景。
完整对局日志继续保存在本地 `out/`；不要以完整日志替代最小回归夹具。

# 维护与贡献

项目适配 Windows 与经典杂交版 v3.9.9，核心模块使用 Python 标准库。
启动方法见 [README](README.md)，工具和数据的分类见
[tools/README.md](tools/README.md) 与 [data/README.md](data/README.md)。

## 目录约定

- `pvz/`：感知、知识、策略和操作代码。
- `data/`：版本资料、机制、已有配置和校准记录。
- `tools/`：稳定的命令行入口、诊断、自检和回归；历史路径暂不搬动。
- `tools/fixtures/`：用于复现问题的最小历史快照。
- `docs/`：调查、设计和修复结果；当前用法放在根目录 README。
- `out/`、`refs/`、`.worktrees/`：本地日志、上游参考与工作区，不提交到 Git。

完整对局日志是后续复盘证据，不作为垃圾缓存清理。
已有工作区要通过相应管理工具处理，不能按目录名字直接删除。

## 策略与操作修改

1. 从日志确定问题发生在感知、候选、模型取舍还是实际执行。
2. 保留能复现问题的最小局面，再修改对应规则。
3. 检查冷却、完整平台预算、地形、射程、近屋救场、基础经济和操作读回。
4. 回归通过后记录证据、限制和下一局需要观察的行为。

种植、回收、换种和平台后续必须以读回结果判断成功，不能只认“已点击”。
未知植物或僵尸需要显式版本身份与机制来源；名字、推定字段和已验证能力分别记录。
成本观测受产阳光、拾取、失败点击等干扰，不能直接把净阳光变化当作可靠价格。

## 验证

在 Windows 的项目根目录运行：

```powershell
python -m unittest discover -s tools -p 'test_*.py' -q
python tools/audit_knowledge.py --game-version 3.9.9 --deck 86 16 67 66
git diff --check
```

涉及具体对局的问题，再用相应日志执行 `replay_battle_review.py`。
离线回归与回放不调用 Jev、不接管真实游戏，也不说明实际胜率。
模型联调和真实操作应使用 [工具分类](tools/README.md) 中明确标注的入口。

启动脚本保持 ASCII、CRLF，并从自身位置定位仓库；Python、Markdown 和 JSON 使用 UTF-8。
本地凭据和 `.env` 文件不提交，示例配置只放占位内容。
第三方来源和许可证随对应资料保留。

## 提交

每次提交围绕一个明确问题，说明行为变化及验证。
提交前检查 `data/layout.json`、`data/plant_ids.json` 和 `data/plant_costs.json` 是否有运行时改动；
只有明确确认的校准或知识变更才与代码一起提交。
保持日志和截图在 `out/`，公共文档附件或最小测试夹具放在对应目录。

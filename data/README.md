# 知识数据

[项目说明](../README.md) · [审计工具](../tools/README.md) · [维护约定](../CONTRIBUTING.md)

数据区包括版本身份、机制参考、已确认档案和本机运行配置。
“知道植物名字”“知道部分属性”“能够正确操作和组合使用”是三种不同状态。

## 版本资料

当前版本包位于 [versions/classic-3.9.9/](versions/classic-3.9.9/)。

| 文件 | 职责 |
| --- | --- |
| [catalog.json](versions/classic-3.9.9/catalog.json) | 固定版本的植物、僵尸 ID 与身份候选 |
| [mechanics_bindings.json](versions/classic-3.9.9/mechanics_bindings.json) | 将显式版本 ID 对应到已有植物档案 |
| [mechanics.json](versions/classic-3.9.9/mechanics.json) | 部分已登记的机制与来源状态 |
| [encyclopedia.json](versions/classic-3.9.9/encyclopedia.json) | 植物、僵尸、关系和场景的百科参考 |
| [sources.json](versions/classic-3.9.9/sources.json) | 上游网址、固定提交、哈希与导入信息 |
| [LICENSE.upstream.txt](versions/classic-3.9.9/LICENSE.upstream.txt) | 身份目录上游许可证 |

修改时保留版本和来源，不以同名植物推断跨版本数值，
不把未经核实的百科字段覆盖到用户确认或运行时确认事实上。

## 植物与使用规则

| 文件 | 职责 |
| --- | --- |
| [hybrid_plants.json](hybrid_plants.json) | 按植物档案登记价格、血量、冷却、攻击和功能 |
| [plant_playbook.json](plant_playbook.json) | 使用原则、布局和植物特性说明 |
| [plant_names.json](plant_names.json) | 旧版名称与角色表 |
| [zombie_traits.json](zombie_traits.json) | 已登记的僵尸特征与默认估计 |
| [lineups.json](lineups.json) | 旧卡组模板及类型指纹，用于人工辅助绑定 |

版本目录和已有绑定之间的优先级由 `pvz/plants.py` 管理。
增加机制需要同时检查合法地图、射程、平台、动态成本和可执行交互；
仅补一条名称记录不意味着控制器已经会用这张卡。

## 本机配置与观测

| 文件 | 职责 / 更新方式 |
| --- | --- |
| [layout.json](layout.json) | 点击布局；几何测量工具可更新 |
| [plant_ids.json](plant_ids.json) | 已有名称绑定与元信息；人工绑定工具可能更新 |
| [plant_costs.json](plant_costs.json) | 已校准成本和运行时有效观测；控制器退出时可能更新 |

这些文件目前仍受 Git 跟踪，并包含现有版本的校准结果。本轮整理保留其内容。
运行后如有变化，先检查差异和证据，不能把一次失败点击或混入产阳光的差值当成新价格。
更换机器时尤其需要核对布局；不要直接删除这些文件来“清理仓库”。

API Key 不属于知识数据，放在本地环境变量或用户目录的凭据文件中。
对局日志、截图与运行事件放在 `out/`，不放进 `data/`。

## 校验

```powershell
python tools/audit_knowledge.py --game-version 3.9.9 --deck 86 16 67 66
python tools/audit_domain.py
python -m unittest discover -s tools -p 'test_*.py' -q
```

离线审计只说明数据与规则的覆盖，实际种植与技能配合仍需对应版本实测。

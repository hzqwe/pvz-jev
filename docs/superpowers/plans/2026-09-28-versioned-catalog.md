# v3.9.9 版本化身份目录 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从已确认适配经典 v3.9.9 的静态来源建立身份目录，让新卡组按真实 ID 识别，并报告机制缺失。

**Architecture:** 身份目录独立于战斗机制库；版本不匹配时不使用身份映射。保留当前已验证的 15 卡绑定与策略，由报告揭示名称、版本和来源冲突，再分批补齐机制。

**Tech Stack:** 当前项目 Python、标准库 ast/json/dataclasses/hashlib、现有 unittest；不新增第三方依赖，不执行修改器。

**Spec:** `docs/knowledge-source-research-2026-09-28.md`。

## Global Constraints

- edition 固定 classic，game_version 固定 3.9.9；其它版本明确拒绝套用。
- 上游提交固定 ce3363868d087363b1b69df656a582c29b099db5，标签 β0.66。
- 身份已知不等于机制已知；不得以身份登记使未知植物进入已确认的战斗策略。
- 实测和用户已确认事实不因名称差异被自动覆盖；新数据逐字段保存来源和适用版本。
- 这阶段不改评分、回收事务、点击坐标和 Jev 请求频率；不增加付费请求。

## Review Focus

- 相同 ID 在原版、经典杂交版和重制版有不同含义：拒绝跨版本目录。
- 实体名称别名和简写：能报告身份但保持已确认本地机制与绑定。
- 来源含占位/特殊实体：标记身份候选，不能承诺全部都可选或可种。
- 同 ID 名称表冲突或源文件混入动态代码：导入拒绝不合规值，不执行源文件。
- 新牌组没有机制知识：识别名称并报告缺失，不把普通豌豆默认规则套到它身上。

---

### Task 1: 固定来源的静态导入

**Files:** Create `tools/import_version_catalog.py`, `pvz/catalog.py`, `data/versions/classic-3.9.9/catalog.json`, `data/versions/classic-3.9.9/sources.json`, `tools/test_catalog.py`；保留上游 MIT 许可与归属信息。

**Interfaces:** `extract_literal_names(source: str, variable: str) -> tuple[str, ...]`；`CatalogIdentity(edition: str, game_version: str, kind: str, type_id: int, canonical_name: str)`；`VersionCatalog.resolve(kind: str, type_id: int) -> CatalogIdentity | None`。

- [ ] 写失败测试：植物初始数组 0=豌豆向日葵、16=豌豆睡莲、67=荷叶、86=向日葵女王、161=回收高坚果、183=雷果子；僵尸数组 5 对应冰车读报的上游名称；保留原始字符串不靠名字自动造能力。
- [ ] 写失败测试：导入器不执行数组外的函数调用；指定数组使用调用表达式而非字符串字面量时明确报错；重复顶层定义拒绝。占位和后续数组拼接不被当成初始植物可选集合。
- [ ] 从固定提交读取源文件，记录 URL、SHA、源文件内容哈希、导入日期、许可；以 ast 解析目标 Assign 和列表字符串，不 eval/import 上游文件。
- [ ] 生成身份候选目录，所有新记录标记机制状态 identity_only；导入数量用于审计，不用于宣布实物覆盖率。
- [ ] 运行 `python -m unittest discover -s tools -p test_catalog.py -v`，确认全部通过，再单独提交。

### Task 2: 版本隔离、别名和覆盖审计

**Files:** Extend `pvz/catalog.py`, `tools/test_catalog.py`；Create `tools/audit_knowledge.py`, `tools/test_knowledge_audit.py`。

**Interfaces:** `load_catalog(path: str, edition: str, game_version: str) -> VersionCatalog`；`audit_book(catalog: VersionCatalog, book: PlantBook, deck_ids: list[int]) -> dict`。报告分为 identity_known、mechanics_known、aliases、conflicts、missing_fields，不输出猜测数值。

- [ ] 写失败测试：传 classic/3.19 或 remake/0.28 加载 classic/3.9.9 时抛 ValueError；缺少版本标识也拒绝。
- [ ] 写失败测试：本地 ID 0=豌豆射手、16=睡莲时报告上游别名差异，不改变原 KBEntry 或成本缓存；现有绑定结果保持一致。
- [ ] 写失败测试：新增已知 ID 无 KBEntry 时 identity_known=True、mechanics_known=False；未知 ID 两项 False；原版推定僵尸特征不能变成 confirmed=True。
- [ ] 实现独立的目录查找与只读审计，现有 `PlantBook` 作为消费方，不改其确认语义。
- [ ] 运行 `python -m unittest discover -s tools -p test_knowledge_audit.py -v` 与目录测试，确认通过；运行审计输出当前 15 卡及未知牌组样本报告，再提交。

### Task 3: 接入新牌组的身份展示

**Files:** Modify `pvz/plants.py`, `pvz/agent.py`；Extend `tools/test_catalog.py`, `tools/test_knowledge_audit.py`；Update `README.md`。

**Interfaces:** `PlantBook.identity(type_id: int) -> CatalogIdentity | None`（目录可选）；`PlantBook.name(type_id: int) -> str` 保持原签名，已绑定名称优先，未绑定时才展示匹配版本目录的名字；`is_known/role/cost/combat` 不因只有身份而伪造机制。

- [ ] 写失败测试：相同新牌组换序不会按位置串绑；已绑定的 15 卡名字和成本不变；只识别身份的卡不会变成已确认 shooter/producer。
- [ ] 写失败测试：未配置已验证版本或版本不匹配时，保持当前未知 ID 行为；未知机制输出缺失项，不能按冷却或卡片数静默猜身份。
- [ ] 接入由已验证运行版本启用的目录，启动日志给出目录版本、SHA、身份/机制覆盖和缺失卡列表；默认保留当前绑定，避免把显示名迁移变成策略改写。
- [ ] 更新说明：第一阶段解决身份与缺口报告，任意牌组自动游玩还依赖后续机制和地图合法性阶段。
- [ ] 运行 `python -m unittest discover -s tools -p 'test_*.py' -v` 和 `git diff --check`，确认原回收/泳池/动态成本回归仍通过；完成只读卡组预演后提交。

## 后续计划的边界

机制导入、成本冲突处理、地图层与组合规则各自形成下一份可独立验收的计划。只有完成对应机制及地图验证的牌组才进入自动操作；第一阶段不宣称已掌握 318 种植物或全部地图。

## 自审

本计划覆盖调查设计的第一阶段。五类 Review Focus 已分别由上述测试覆盖；接口均定义于使用它们之前。源静态枚举与真实可用实体的区别、名字与机制的区别、版本不符与未知身份的处理均有明确结果。其余四阶段保留在调查文档路线图中，未提前承诺实现或测试通过。

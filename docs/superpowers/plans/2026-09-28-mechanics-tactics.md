# 机制证据与有效火力 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans inline, task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 将首批机制证据接入当前 v3.9.9 卡组，使火力、组合和经济判断更准确。
**Architecture:** 版本目录的字段补丁生成临时独立档案；通用目标计算供候选、威胁和救场共用；审计只读。
**Tech Stack:** Python 标准库、现有 unittest，不新增依赖或付费调用。
**Spec:** `docs/superpowers/specs/2026-09-28-mechanics-tactics-design.md`

## Global Constraints

- 只允许 classic/3.9.9；保留原用户绑定和成本；不把推定数值当实测。
- 不改坐标、回收事务或请求频率；资料和来源只发送当前需要的摘要。
- 改进先离线测试和日志重放，不自动操作正在运行的游戏。

## Review Focus

- 预先绑定的 ID 0 应得到当前版本双能力，切换版本必须恢复原档案。
- 同名 ID 228、错版本和无来源字段不得套用当前机制。
- 敌人越过射手、超射程、火炬睡眠或错行时，不应得到错误的支援/增伤。
- 穿透只计可命中敌人；追踪不可错误乘以敌人数。
- 普通僵尸仍在场的安全富阳光场景应补阵型，危险或需储蓄仍允许保留资源。

### Task 1: 版本化字段补丁

**Files:** Create `pvz/mechanics.py`, `data/versions/classic-3.9.9/mechanics.json`, `tools/test_mechanics.py`; modify `pvz/plants.py`.
**Interfaces:** `load_mechanics(catalog, profiles, path=None) -> dict[int,dict]`; `PlantBook.catalog_entry(type_id, catalog=None) -> KBEntry | None`，返回独立当前版本档案，不能写用户绑定。

- [ ] 写测试并观察失败：ID 0 产阳光和射击并存、原价格不变、原 KBEntry 不变；退出/失败版本切换恢复；ID 228 不继承；无来源和错身份补丁拒绝。
- [ ] 实现严格字段读取和独立档案叠加；0 补产能，86 隔离跨版本传闻，183 补穿透；数值冲突列候选。
- [ ] 测试通过并提交。Expected: `python -m unittest discover -s tools -p test_mechanics.py` → OK。

### Task 2: 目标、配合与经济判断

**Files:** Modify `pvz/tactics.py`, `pvz/policy.py`, `pvz/serialize.py`; create `tools/test_combat_projection.py`.
**Interfaces:** `can_hit(book,type_id,row,col,target) -> bool`; `torch_path(board,book,type_id,row,col,target=None) -> bool`; `target_dps(board,book,type_id,row,col,target) -> float`; `economy_summary(board,book) -> dict`。

- [ ] 写测试并观察失败：超射程支援为零，经过射手不计支援，睡眠/错位火炬不计配合，穿透同行群体贡献、追踪分摊，推定倍数不进确定性伤害。
- [ ] 接入威胁、垫背、救场和候选；只增加小摘要；富阳光低威胁的阵型补齐在合并侧生效。
- [ ] 测试通过并提交。Expected: 新测试和原策略测试 → OK。

### Task 3: 审计与整体验证

**Files:** Modify `pvz/knowledge.py`, `tools/test_knowledge_audit.py`, README；记录研究/测试结果。

- [ ] 写测试并观察失败：未绑定档案和运行时机制覆盖一致，区分 bound/available，不改任何运行对象；推定和冲突数值能报告。
- [ ] 审计调用同一只读档案解析函数；运行全量测试、最近两局离线重放、diff 检查并提交。
- [ ] 一次独立整体审查，重要项一次修复并回归；保存备份，同步原启动目录和 GitHub。Expected: 全量测试 OK、重放非法候选 0、仓库干净。

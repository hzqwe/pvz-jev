# Jev Reliability and Domain Knowledge Implementation Plan

> **For agentic workers:** Use executing-plans for local integration; independent runtime, tactics and sourced-data tasks may run under dispatching-parallel-agents. Each code change uses failing regression tests before implementation.

**Goal:** 修复审查中确认的可靠性问题，并让更多有来源的植物、僵尸、地图与配合事实参与 Jev 判断。

**Architecture:** 沿用 PlantBook、战术候选和执行事务。新增版本知识包、有限关系摘要与单游戏控制权；精确计算在本地，Jev 选择合法候选。

**Tech Stack:** Python 3.13 / 标准库 / Windows ctypes / unittest / 现有 Jev HTTP 接口。

**Spec:** `docs/superpowers/specs/2026-09-29-reliability-knowledge-design.md`

## Global Constraints

- 游戏内存只读，不自动启动游戏，不调用付费实战。
- 版本身份使用经典 3.9.9，固定 `ce3363868d087363b1b69df656a582c29b099db5`。
- 未知事实保持未知；来源与版本适用性必须记录；保留用户确认事实。
- 不新增每轮视觉调用；Jev 输入只保留当前相关知识。

## Review Focus

- 控制者异常退出或游戏重启后锁释放、重新获取。
- 文件损坏、删除资料及临时版本绑定后知识快照一致。
- 三行输出被重复计为每行全部 DPS。
- 水路叠层铲除导致支撑或上层误删。
- 新 Wiki 资料覆盖已有用户确认的价格和行为。

### Task 1: Runtime ownership, costs and reload

**Files:** `pvz/agent.py`, `pvz/plants.py`, `pvz/serialize.py`, new `pvz/control.py`, runtime regression tests.

**Interfaces:** PlantBook retains existing public API; adds `knowledge_revision` and `reload_data()` returns load summary. Agent acquires per-game input ownership before any live input. Encyclopedia loading accepts spec schema without changing old manual facts.

- [ ] Write and run failing tests for two controllers, clean release, known-cost pollution, already-bound reload, corrupt snapshot retention, cached doctrine reload.
- [ ] Implement ownership and trustworthy cost observation, complete snapshot reload with final effective profiles, provenance metadata.
- [ ] Run targeted regressions and full suite; record changes and limitations.

### Task 2: Sourced version knowledge expansion

**Files:** new `data/versions/classic-3.9.9/encyclopedia.json`, source research documentation. No edits to Task 1/3 code.

**Interfaces:** plants/zombies/relations/scenes as specified. Field confidence and version status distinguish current-reference facts from confirmed values.

- [ ] Search via GitHub plugin, verify pinned identity and available classic Wiki sources.
- [ ] Import as many explicit mechanism records as sources support, prioritizing current deck, common cards, terrain supports and common threats; omit unsupported numerical guesses.
- [ ] Validate identities and source paths; record counts and source limitations.

### Task 3: Ability, terrain and execution regressions

**Files:** `pvz/tactics.py`, `pvz/board.py`, `pvz/policy.py`, `pvz/transactions.py`, ability/terrain regression tests. Coordinate agent.py changes with Task 1.

**Interfaces:** normalized combat and placement dictionaries, same candidate/transaction APIs; BoardState mower diagnostics serialized through existing snapshot handling.

- [ ] Reproduce adjacent-row support, wallet-vs-sun income, event-triggered income, platform terrain and stacked upgrade mistakes in failing tests.
- [ ] Implement bounded coverage/output budgets, explicit terrain abilities, top-layer replacement checks, mower rejection diagnostics; remove unconditional unused-card bonus.
- [ ] Verify existing pool, wall recovery and calm development regressions.

### Task 4: Relevant relationships and integration

**Files:** new `pvz/relations.py`, knowledge integration tests, tools audit/replay compatibility, docs/README.

**Interfaces:** `relationship_summary(board, book, limit=8) -> list[dict]`; select only current deck/field relationships. Current code retains decision schema.

- [ ] Test relevant-only and bounded relationship summaries, source validation, old Lily Pad logs.
- [ ] Connect summaries and new version facts to actual runtime state/candidate descriptions; avoid per-tick encyclopedia traversal or external calls.
- [ ] Run all tests and yesterday's replay, check payload budget, perform fresh independent review and address meaningful findings.
- [ ] Commit verified changes and integrate into user's existing checkout; provide counts, actual behavior and remaining visual/live validation limits.

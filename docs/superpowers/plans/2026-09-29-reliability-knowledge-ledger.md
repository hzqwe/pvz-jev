# Implementation ledger

- Base: fc078ec, 312 baseline unittest cases passed (2026-09-29).
- Ruling: user explicitly approved previous audit repair order and requested direct execution; implement that scope without another approval round. Source expansion and optional vision analysis extend the same task.
- Workspace: native worktree tool returned “Not a git repository” because chat root is E:/jev; manual fallback created isolated E:/jev/pvz-jev/.worktrees/knowledge-reliability on codex/knowledge-reliability. Main checkout retained clean.
- Task 1: runtime consistency completed in `agent.py`, `plants.py`, `serialize.py`, and `control.py`: per-game input ownership, dry-run guards, atomic reload, provenance priority, isolated cost evidence, replacement metadata, and special-scene labeling.
- Task 2: sourced encyclopedia completed in `data/versions/classic-3.9.9/encyclopedia.json` and `docs/knowledge-expansion-sources-2026-09-29.md`. The loader rejects identity/schema/source conflicts and keeps the pinned β0.66 revision separate from unverified Wiki mechanics.
- Task 3: ability/terrain completed in `tactics.py`, `board.py`, `policy.py`, and `transactions.py`: explicit placement/economy capabilities, per-lane attack budgets, reachable-target checks, top-layer shovel identity, and mower diagnostics.
- Task 4: relevant relationships implemented after failing tests, then bounded and malformed-input tests passed. Old Lily Pad state-only logs retain an explicit historic alias; relationship summaries and active domain context are wired into the state. Expanded integration tests cover the real 3.9.9 encyclopedia.
- Visual ruling: Jev live docs only support textual state. Provide optional complementary pipeline design, do not add a paid vision call to every game decision.

## Verification evidence

- Full offline suite: 381 tests passed.
- `compileall` over `pvz` and `tools` passed; `git diff --check` passed.
- Twelve non-event decision logs from 2026-09-28 replayed with the pinned version: 2,158 snapshots, 13,029 candidates, 10,452 plant candidates, 0 invalid plant candidates, 0 missing reasons, and 0 overlong descriptions. `.events.jsonl` files are event streams rather than state snapshots and were excluded.
- Domain audit: 274 plant references, 111 zombie references, 162 relations, 6 scenes; the sample deck has all 11 identities and mechanics available.
- A synthetic 15-card pool with 18 current zombies serializes to 30,566 UTF-8 bytes with 4 applicable relationships and 1 bounded enemy record; the full encyclopedia is not sent per decision.

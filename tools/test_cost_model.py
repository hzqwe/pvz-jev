"""2026-09-26 决策优化批次的回归测试。

覆盖：成本模型（TTL 棘轮衰减 / 动态涨价 / 失败分诊用的归一化）、
女王开局硬约束（到账兑现落点扩展 / 救场绕过储蓄闸门）、
执行前威胁重评（escalate_emergency）、时钟推进判据、铲子被啃窗口、
Jev 超时的保守兜底。全部离线，不碰游戏、不调 API。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pvz.board import BoardState, Plant, SeedSlot, Zombie
from pvz.plants import MIN_COST_TTL, PlantBook
from pvz.ui import SunTracker
from pvz.policy import (
    RECLAIM_MAX_HP,
    RECLAIM_MAX_HP_BITTEN,
    action_is_current,
    escalate_emergency,
    generate_candidates,
    merge_decision,
)
from pvz.jev import JevAnswer, JevResponse

QUEEN, SUN, PEA, WALLNUT, STRONG = range(300, 305)
RECLAIM = 320


class CostModelTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.assertTrue(self.book.bind_one(QUEEN, '向日葵女王'))
        self.assertTrue(self.book.bind_one(PEA, '豌豆射手'))
        # 高冰果的图鉴 combat 里登记了 price_increment=100
        self.assertTrue(self.book.bind_one(STRONG, '高冰果'))

    def test_dynamic_price_scales_with_field_copies(self):
        self.assertEqual(self.book.cost(STRONG), 500)
        self.book.sync_field_copies([Plant(0, 0, 0, STRONG), Plant(1, 1, 0, STRONG)])
        self.assertEqual(self.book.cost(STRONG), 700)   # 500 + 100*2
        self.book.sync_field_copies([])
        self.assertEqual(self.book.cost(STRONG), 500)

    def test_set_real_cost_normalizes_price_increment(self):
        # 实测拿到的是"第 2 张"的总价 600（1 张基价 500 + 场上已有 1 张 +100）
        self.book.sync_field_copies([Plant(0, 0, 0, STRONG)])
        self.book.set_real_cost(STRONG, 600)
        self.assertEqual(self.book.real_cost[STRONG], 500)      # 归一化成基价
        self.assertEqual(self.book.cost(STRONG), 600)           # 查询按当前株数加回
        self.book.sync_field_copies([])
        self.assertEqual(self.book.cost(STRONG), 500)

    def test_note_unaffordable_normalizes_and_decays(self):
        self.book.sync_field_copies([Plant(0, 0, 0, STRONG)])
        self.book.note_unaffordable(STRONG, 750)   # 总价至少 751 -> 基价下界 651
        self.assertEqual(self.book.min_cost[STRONG], 651)
        self.assertEqual(self.book.cost(STRONG), 751)   # 基价 651 + 溢价 100*1
        # 场上那张没了 -> 溢价消失，但归一化的下界仍然生效
        self.book.sync_field_copies([])
        self.assertEqual(self.book.cost(STRONG), 651)
        # TTL 过期 -> 下界失效，回落到图鉴基线（不再单向棘轮锁死）
        fake_now = self.book.min_cost_ts[STRONG] + MIN_COST_TTL + 1
        with patch('pvz.plants.time.time', return_value=fake_now):
            self.assertEqual(self.book.cost(STRONG), 500)
        self.assertNotIn(STRONG, self.book.min_cost)

    def test_floor_never_forgets_real_price_after_ttl(self):
        self.book.set_real_cost(PEA, 125)
        self.book.note_unaffordable(PEA, 400)
        self.assertEqual(self.book.cost(PEA), 401)
        fake_now = self.book.min_cost_ts[PEA] + MIN_COST_TTL + 1
        with patch('pvz.plants.time.time', return_value=fake_now):
            self.assertEqual(self.book.cost(PEA), 125)   # 回到实测价，不是图鉴价


class QueenOpeningTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.assertTrue(self.book.bind_one(QUEEN, '向日葵女王'))
        self.assertTrue(self.book.bind_one(SUN, '阳光向日葵'))
        self.assertTrue(self.book.bind_one(PEA, '豌豆射手'))
        self.assertTrue(self.book.bind_one(WALLNUT, '冰冻坚果'))

    def board(self, cards, zombies=(), plants=(), sun=600):
        return BoardState(ok=True, sun=sun, rows=5, cols=9, game_clock=1000,
                          slots=[SeedSlot(i, t, 0, 1000) for i, t in enumerate(cards)],
                          plants=list(plants), zombies=list(zombies))

    def plan_active(self, b):
        from pvz.tactics import saving_plan
        return saving_plan(b, self.book)

    def test_opening_save_matures_at_column_d_when_bc_occupied(self):
        # 女王买得起、B/C 已被向日葵占住 -> 兑现落点应扩展到 D 列而不是消失
        b = self.board([QUEEN, SUN],
                       [Zombie(0, 0, 0, x=720)],
                       [Plant(0, 0, 1, SUN), Plant(1, 0, 2, SUN)])
        plan = self.plan_active(b)
        self.assertIsNotNone(plan, '开局储蓄（grace 90s）应在低威胁时保持活跃')
        self.assertTrue((b.sun or 0) >= plan['cost'])
        cands = [c for c in generate_candidates(b, self.book)
                 if c.kind == 'plant' and c.type_id == QUEEN]
        self.assertTrue(cands, '女王到账必须出候选（否则开局永远等待）')
        self.assertEqual(cands[0].col, 2)

    def test_rescue_wall_bypasses_saving_gate(self):
        # 储蓄差价中 + 有前排植物马上被啃（x=300，距前排 60px）->
        # 墙系卡允许绕过储蓄闸门救场
        b = self.board([QUEEN, WALLNUT, SUN],
                       [Zombie(0, 0, 0, x=300)],
                       [Plant(0, 0, 2, PEA)], sun=450)
        plan = self.plan_active(b)
        self.assertIsNotNone(plan)
        self.assertGreater(plan['missing_sun'], 0)
        kinds = {(c.type_id, c.kind) for c in generate_candidates(b, self.book)
                 if c.kind == 'plant'}
        self.assertIn((WALLNUT, 'plant'), kinds, '救场墙应绕过储蓄闸门出候选')
        self.assertNotIn((SUN, 'plant'), kinds, '产阳光卡不应绕过储蓄闸门')

    def test_rescue_bypass_needs_pressure(self):
        # 僵尸还在远处（low）且没有贴身拦截 -> 不绕过，继续专心攒女王
        b = self.board([QUEEN, WALLNUT, SUN],
                       [Zombie(0, 0, 0, x=700)], sun=450)
        plan = self.plan_active(b)
        self.assertIsNotNone(plan)
        kinds = {c.type_id for c in generate_candidates(b, self.book) if c.kind == 'plant'}
        self.assertNotIn(WALLNUT, kinds)
        self.assertNotIn(SUN, kinds)

    def test_rescue_bypass_is_cheap_only(self):
        # 500 级的射手不允许绕过储蓄闸门 —— 否则女王的积蓄被一次烧光（泳池局实测）
        melon = 306
        self.assertTrue(self.book.bind_one(melon, '冰瓜香蒲'))
        b = self.board([QUEEN, WALLNUT, SUN, melon],
                       [Zombie(0, 0, 0, x=300)],
                       [Plant(0, 0, 2, PEA)], sun=450)
        self.assertIsNotNone(self.plan_active(b))
        kinds = {c.type_id for c in generate_candidates(b, self.book) if c.kind == 'plant'}
        self.assertIn(WALLNUT, kinds, '便宜墙应绕过储蓄闸门')
        self.assertNotIn(melon, kinds, '500 级射手不应绕过储蓄闸门')

    def test_baseline_save_aborts_once_zombies_settle_in(self):
        # 对照基线语义：僵尸上草坪且过了 30s 开局宽限 -> 储蓄退出，转正常运营
        # （泳池局实测：90s 宽限让 agent 在 6 只僵尸进场时 0 防御硬吃第一波）
        b = self.board([QUEEN, WALLNUT, SUN],
                       [Zombie(0, 0, 0, x=700), Zombie(1, 1, 0, x=760),
                        Zombie(2, 2, 0, x=680)], sun=550)
        b.game_clock = 4000          # > 3000：开局宽限已过
        self.assertIsNone(self.plan_active(b), '僵尸在场且过宽限后应退出储蓄')

    def test_save_continues_while_calm_or_in_grace(self):
        # 平静时（无僵尸）不限时钟；开局宽限内出现远处僵尸也继续攒（基线语义）
        calm = self.board([QUEEN, SUN], [], sun=450)
        calm.game_clock = 8000
        self.assertIsNotNone(self.plan_active(calm), '无僵尸时应继续攒女王')
        grace = self.board([QUEEN, SUN], [Zombie(0, 0, 0, x=700)], sun=450)
        grace.game_clock = 2000
        self.assertIsNotNone(self.plan_active(grace), '开局宽限内应继续攒女王')


class ExecuteGuardTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.assertTrue(self.book.bind_one(PEA, '豌豆射手'))
        self.assertTrue(self.book.bind_one(WALLNUT, '冰冻坚果'))
        self.assertTrue(self.book.bind_one(RECLAIM, '回收高坚果'))

    def board(self, cards, zombies=(), plants=(), sun=600, clock=1000):
        return BoardState(ok=True, sun=sun, rows=5, cols=9, game_clock=clock,
                          slots=[SeedSlot(i, t, 0, 1000) for i, t in enumerate(cards)],
                          plants=list(plants), zombies=list(zombies))

    def test_escalate_emergency_on_fresh_snapshot(self):
        calm = self.board([PEA], [Zombie(0, 0, 0, x=700)])
        cands = [c for c in generate_candidates(calm, self.book) if c.kind == 'plant']
        self.assertTrue(cands)
        pending = cands[0]
        self.assertFalse(pending.emergency)
        # 几秒后（同一张卡还没执行）那路已经贴脸
        crisis = self.board([PEA], [Zombie(0, 0, 0, x=120)])
        esc = escalate_emergency(pending, crisis, self.book)
        self.assertIsNotNone(esc)
        self.assertTrue(esc.emergency)

    def test_escalate_ignores_already_rescue_and_quiet(self):
        quiet = self.board([PEA], [Zombie(0, 0, 0, x=700)])
        self.assertIsNone(escalate_emergency(None, quiet, self.book))
        crisis = self.board([PEA], [Zombie(0, 0, 0, x=120)])
        cands = [c for c in generate_candidates(crisis, self.book) if c.kind == 'plant']
        rescue = next(c for c in cands if c.emergency)
        self.assertIsNone(escalate_emergency(rescue, crisis, self.book))

    def test_clock_not_advancing_blocks_actions(self):
        b = self.board([PEA], [Zombie(0, 0, 0, x=300)])
        cands = [c for c in generate_candidates(b, self.book) if c.kind == 'plant']
        b.clock_advancing = False
        self.assertFalse(action_is_current(cands[0], b, self.book))
        dec = merge_decision(JevResponse(), cands + generate_candidates(b, self.book), b, self.book)
        self.assertTrue(dec.hold)

    def test_shovel_window_widened_while_bitten(self):
        eaten = self.board([], [Zombie(0, 0, 0, x=250)],
                           [Plant(0, 0, 2, RECLAIM, hp=3000, recently_eaten=True)])
        self.assertGreater(3000, RECLAIM_MAX_HP)
        self.assertLessEqual(3000, RECLAIM_MAX_HP_BITTEN)
        self.assertTrue(any(c.kind == 'shovel' for c in generate_candidates(eaten, self.book)))
        healthy = self.board([], [Zombie(0, 0, 0, x=250)],
                             [Plant(0, 0, 2, RECLAIM, hp=3000, recently_eaten=False)])
        self.assertFalse(any(c.kind == 'shovel' for c in generate_candidates(healthy, self.book)))

    def test_jev_timeout_falls_back_to_wait(self):
        # 平静且有后排火力（无缺失角色）时，超时兜底应保持等待
        b = self.board([PEA], [Zombie(0, 0, 0, x=700)], [Plant(0, 0, 6, PEA)])
        cands = generate_candidates(b, self.book)
        resp = JevResponse(error='HTTP 000: curl: (28) timed out')
        dec = merge_decision(resp, cands, b, self.book)
        self.assertTrue(dec.fallback)
        self.assertTrue(dec.hold, '超时兜底应保守等待，而不是无确认地花钱')
        self.assertIn('timed out', ' '.join(dec.notes))

    def test_jev_timeout_still_rescues_critical(self):
        b = self.board([PEA], [Zombie(0, 0, 0, x=120)])
        cands = generate_candidates(b, self.book)
        resp = JevResponse(error='HTTP 000: timed out')
        dec = merge_decision(resp, cands, b, self.book)
        self.assertTrue(dec.fallback)
        self.assertFalse(dec.hold)
        self.assertTrue(dec.candidate.emergency, '危急路仍必须被救场覆盖')


class SunTrackerTests(unittest.TestCase):
    def tracker(self):
        return SunTracker(clock=lambda: 1000.0)

    def test_persistent_misses_get_banned(self):
        t = self.tracker()
        hits = [(100, 200), (300, 400)]
        t.feedback(hits, gained=0)
        t.feedback(hits, gained=0)
        self.assertEqual(t.banned_keys(), set())     # 2 次还不够
        t.feedback(hits, gained=0)
        self.assertEqual(t.banned_keys(), {SunTracker.key_of(100, 200),
                                           SunTracker.key_of(300, 400)})

    def test_full_gain_clears_suspicion(self):
        t = self.tracker()
        hits = [(100, 200)]
        t.feedback(hits, gained=0)
        t.feedback(hits, gained=25)                  # 一颗真阳光 = 25
        self.assertEqual(t.banned_keys(), set())
        t.feedback(hits, gained=0)
        t.feedback(hits, gained=0)
        self.assertEqual(t.banned_keys(), set())     # 计数被清过，重新累计

    def test_mixed_gain_is_conservative(self):
        t = self.tracker()
        hits = [(100, 200), (300, 400)]
        t.feedback(hits, gained=25)                  # 0 < 25 < 50：无法归因
        self.assertEqual(t.banned_keys(), set())
        self.assertEqual(t._miss, {})

    def test_unknown_gain_changes_nothing(self):
        t = self.tracker()
        t.feedback([(100, 200)], gained=None)
        self.assertEqual(t._miss, {})

    def test_ban_expires(self):
        t = self.tracker()
        pos = [(100, 200)]
        for _ in range(3):
            t.feedback(pos, gained=0)
        self.assertTrue(t.banned_keys())
        t._banned_until[SunTracker.key_of(100, 200)] = 900.0   # 过期
        self.assertEqual(t.banned_keys(), set())


class ZeroDefenceLaneTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.assertTrue(self.book.bind_one(PEA, '豌豆射手'))
        self.assertTrue(self.book.bind_one(SUN, '阳光向日葵'))
        self.assertTrue(self.book.bind_one(QUEEN, '向日葵女王'))

    def board(self, cards, zombies=(), plants=(), sun=600):
        return BoardState(ok=True, sun=sun, rows=5, cols=9, game_clock=40000,
                          slots=[SeedSlot(i, t, 0, 1000) for i, t in enumerate(cards)],
                          plants=list(plants), zombies=list(zombies))

    @staticmethod
    def wait_answer(cands):
        wait_cid = next(c.cid for c in cands if c.kind == 'wait')
        return JevResponse(answers={'action': JevAnswer('action', 'choice',
                                                        {'choice': wait_cid, 'confidence': 0.6}),
                                    'hold_sun': JevAnswer('hold_sun', 'noul', {'noul': 0.1})})

    def test_model_wait_on_zero_defence_lane_is_overridden(self):
        # 复刻 2026-09-26 泳池局：僵尸 223px、整路零防御、推车未知，模型选等待
        b = self.board([PEA, SUN], [Zombie(0, 0, 0, x=223)], sun=190)
        cands = generate_candidates(b, self.book)
        dec = merge_decision(self.wait_answer(cands), cands, b, self.book)
        self.assertFalse(dec.hold, '零防线路不允许等待')
        self.assertTrue(dec.fallback)
        self.assertTrue(any('zero defenders' in n for n in dec.notes))
        self.assertTrue(set(dec.candidate.covers) & {0},
                        '兜底动作必须覆盖零防线路')

    def test_queen_save_yields_to_zero_defence_lane(self):
        # 女王储蓄期内，零防御路被压（x=300）时必须先买便宜墙 —— 保命优先，
        # 女王只推迟十几秒（配合生成侧 ≤150 的便宜绕过）
        self.assertTrue(self.book.bind_one(WALLNUT, '冰冻坚果'))
        b = self.board([QUEEN, WALLNUT, SUN], [Zombie(0, 0, 0, x=300)], sun=300)
        b.game_clock = 2000
        from pvz.tactics import saving_plan
        self.assertIsNotNone(saving_plan(b, self.book), '此时储蓄仍应活跃')
        cands = generate_candidates(b, self.book)
        dec = merge_decision(self.wait_answer(cands), cands, b, self.book)
        self.assertFalse(dec.hold, '零防线路不允许等待（即使正在攒女王）')
        self.assertTrue(set(dec.candidate.covers) & {0})

    def test_far_zombie_on_zero_defence_lane_can_wait(self):
        # 僵尸还在 865px 刚出屏：不构成禁等
        b = self.board([PEA, SUN], [Zombie(0, 0, 0, x=865)], sun=190)
        cands = generate_candidates(b, self.book)
        dec = merge_decision(self.wait_answer(cands), cands, b, self.book)
        self.assertTrue(dec.hold)


if __name__ == '__main__':
    unittest.main()

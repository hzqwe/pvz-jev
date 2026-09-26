"""进阶威胁回归（用户 2026-09-26 四点）：
远程僵尸点杀高价值射手 / 坦克大波提前交灰烬 / 撞车僵尸与防撞墙 /
快死铲掉换阳光（含压扁前预铲）。全部离线，不碰游戏不调 API。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_strategy import StrategyTests, PEA, WALL, STRONG, BOMB
from test_transactions import FakeGame
from pvz.board import BoardState, Plant, SeedSlot, Zombie
from pvz.plants import PlantBook
from pvz.policy import Candidate, generate_candidates
from pvz.tactics import lane_facts
from pvz.transactions import run_transaction
from pvz.ui import Layout

QUEEN, ICE = range(420, 422)      # 420=向日葵女王 421=高冰果
CRUSH_Z, FAST_Z = 12, 7           # zombie_traits.json 里的候选 id（crush/快啃）


class FakeGameAgent:
    """test_transactions.FakeGame 的最小 agent 替身（复用它的 FakeGame 本体）。"""

    def __init__(self, game, book):
        self.reader = game
        self.clicker = game
        self.book = book
        self.layout = Layout()
        self._geometry_error = None
        self._action_times = []


class AdvancedThreatTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        for tid, name in ((PEA, '豌豆射手'), (WALL, '冰冻坚果'),
                          (STRONG, '冰瓜香蒲'), (BOMB, '阳光炸弹'),
                          (QUEEN, '向日葵女王'), (ICE, '高冰果')):
            self.assertTrue(self.book.bind_one(tid, name))

    def board(self, cards, zombies=(), plants=(), sun=1000):
        return BoardState(ok=True, sun=sun, rows=5, cols=9, game_clock=40000,
                          slots=[SeedSlot(i, t, 0, 1000) for i, t in enumerate(cards)],
                          plants=list(plants), zombies=list(zombies))

    # -- 1) 远程僵尸：无墙保护的昂贵射手必须快速补墙 ----------------------
    def test_uncovered_expensive_shooter_gets_wall_priority(self):
        z = Zombie(0, 0, 0, x=400)
        z.stationary = True                      # 驻停 = 远程僵尸（行为观测）
        b = self.board([WALL], [z], [Plant(0, 0, 6, STRONG)])
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.type_id == WALL]
        self.assertTrue(cs, '贵射手无墙保护时应给出墙候选')
        self.assertTrue(any('expensive plant' in c.why for c in cs))
        self.assertTrue(any('ranged' in c.why for c in cs), '驻停僵尸应触发远程告警')

    def test_covered_lane_does_not_claim_unprotected(self):
        z = Zombie(0, 0, 0, x=400)
        z.stationary = True
        b = self.board([WALL], [z], [Plant(0, 0, 6, STRONG), Plant(1, 0, 4, WALL)])
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and 'expensive plant' in c.why and c.row == 0]
        self.assertFalse(cs, '已有墙在位的路不应声称"无墙保护"')

    def test_lane_facts_reports_crush_and_eat_rate(self):
        f = lane_facts(self.board([], [Zombie(0, 0, CRUSH_Z, x=500)]), 0, self.book)
        self.assertEqual(f['crush_zombies'], 1)
        self.assertEqual(f['ranged_zombies'], 0)
        f2 = lane_facts(self.board([], [Zombie(0, 0, FAST_Z, x=500)]), 0, self.book)
        self.assertEqual(f2['eat_dps'], 150.0)

    # -- 2) 坦克/大波：提前交灰烬，不等贴脸 -------------------------------
    def test_eager_burst_allowed_on_dense_mid_push(self):
        zs = [Zombie(i, i % 5, 0, x=500.0) for i in range(6)]
        b = self.board([BOMB], zs, sun=400)
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.type_id == BOMB]
        self.assertTrue(cs, '6 只僵尸的大波即使没贴脸也应有灰烬候选（eager）')

    def test_single_far_zombie_still_waits(self):
        b = self.board([BOMB], [Zombie(0, 0, 0, x=700)], sun=400)
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.type_id == BOMB]
        self.assertFalse(cs, '单只远处僵尸不该引诱交灰烬')

    def test_tanky_push_triggers_eager(self):
        z = Zombie(0, 0, 0, x=500, hp=3000, armor_hp=300)   # strength ≈ 12
        b = self.board([BOMB], [z], sun=400)
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.type_id == BOMB]
        self.assertTrue(cs, '灰烬一发打不死的坦克应在它进中圈时就交')

    # -- 3) 撞车僵尸：防撞墙优先，普通墙明示会被压扁 ----------------------
    def test_crush_zombie_prefers_crush_resistant_wall(self):
        # 2026-09-26 实战修订（23:54 冰坚果种进冰车路送死）：最近僵尸=冰车时，
        # 本路**不再生成**普通墙候选，只允许防撞墙；炸弹接管（见下一条）。
        b = self.board([WALL, ICE], [Zombie(0, 0, CRUSH_Z, x=500)], sun=1200)
        cands = generate_candidates(b, self.book)
        self.assertFalse([c for c in cands
                          if c.kind == 'plant' and c.type_id == WALL and c.row == 0],
                         '冰车是最近僵尸时不应给出普通墙候选（送死）')
        ice = next(c for c in cands
                   if c.kind == 'plant' and c.type_id == ICE and c.row == 0)
        self.assertIn('Crush-resistant', ice.why)

    def test_bomb_targets_truck_when_no_anti_crush_ready(self):
        # 雷果子/高冰果都在冷却 -> 阳光炸弹砸冰车是标准答案，加分并注明
        b = self.board([BOMB], [Zombie(0, 0, CRUSH_Z, x=500)], sun=400)
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.type_id == BOMB]
        self.assertTrue(cs, '撞车无防撞墙时应给出炸弹候选')
        self.assertTrue(any('Crushing zombie' in c.why for c in cs))

    # -- 4) 铲掉换阳光 -----------------------------------------------------
    def test_salvage_shovel_when_plant_nearly_eaten(self):
        z = Zombie(0, 0, 0, x=280)               # 贴着 col3 啃
        b = self.board([PEA], [z],
                       [Plant(0, 0, 3, PEA, hp=200, recently_eaten=True)], sun=50)
        cs = [c for c in generate_candidates(b, self.book) if c.kind == 'shovel']
        self.assertTrue(cs, '快被啃死的植物应给"铲掉换阳光"候选')
        self.assertTrue(all(c.salvage for c in cs))

    def test_healthy_plant_is_not_salvaged(self):
        z = Zombie(0, 0, 0, x=280)
        b = self.board([PEA], [z],
                       [Plant(0, 0, 3, PEA, hp=3000, recently_eaten=True)], sun=50)
        self.assertFalse([c for c in generate_candidates(b, self.book)
                          if c.kind == 'shovel' and c.salvage])

    def test_crush_preemptive_salvage_of_frontmost_non_anti_crush(self):
        # 撞车僵尸距前排不可防撞植物 ≤2 格 -> 预铲；防撞植物 -> 绝不铲
        b = self.board([STRONG], [Zombie(0, 0, CRUSH_Z, x=390)],
                       [Plant(0, 0, 2, STRONG, hp=8000)], sun=50)
        cs = [c for c in generate_candidates(b, self.book) if c.kind == 'shovel']
        self.assertTrue(cs, '撞车僵尸两格内应预铲不可防撞植物')
        self.assertTrue(all(c.salvage for c in cs))
        anti = self.board([ICE], [Zombie(0, 0, CRUSH_Z, x=390)],
                          [Plant(0, 0, 2, ICE, hp=8000)], sun=50)
        self.assertFalse([c for c in generate_candidates(anti, self.book)
                          if c.kind == 'shovel'], '防撞植物不应被预铲')

    def test_fast_eater_shrinks_salvage_window(self):
        # 黑橄榄球类（traits: eat_dps=150）：血量 450 时快 eater 撑不过 4s -> 铲；
        # 普通速度(100/s)下 450 血还能撑 >4s -> 不铲
        z = Zombie(0, 0, FAST_Z, x=280)
        b = self.board([PEA], [z],
                       [Plant(0, 0, 3, PEA, hp=450, recently_eaten=True)], sun=50)
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'shovel' and c.salvage]
        self.assertTrue(cs, '快速啃食者面前 450 血(≤150*4)也应触发抢救')
        slow = self.board([PEA], [Zombie(0, 0, 0, x=280)],
                          [Plant(0, 0, 3, PEA, hp=450, recently_eaten=True)], sun=50)
        self.assertFalse([c for c in generate_candidates(slow, self.book)
                          if c.kind == 'shovel' and c.salvage],
                         '普通啃食速度(100/s)下 450 血还能撑 >4s，不铲')

    def test_salvage_does_not_need_the_blinking_eaten_flag(self):
        # recently_eaten 是约 1s 的短倒计时位，采样经常错过（夜战实测 44 次被啃
        # 只抓到 6 次）：贴身+低血就必须触发，不能等那个标志位。
        z = Zombie(0, 0, 0, x=280)
        b = self.board([PEA], [z],
                       [Plant(0, 0, 3, PEA, hp=300, recently_eaten=False)], sun=50)
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'shovel' and c.salvage]
        self.assertTrue(cs, '贴身+血量≤咬速×4 时即使没采样到 eaten 位也应触发')

    def test_dying_salvage_escalates_to_emergency(self):
        z = Zombie(0, 0, 0, x=280)
        b = self.board([PEA], [z],
                       [Plant(0, 0, 3, PEA, hp=120, recently_eaten=True)], sun=50)
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'shovel' and c.salvage]
        self.assertTrue(cs and cs[0].emergency, '≤咬速×1.5s 的抢救应升级 emergency')

    def test_relocation_never_targets_crush_lane_with_plain_wall(self):
        # 夜战教训：回收高坚果（非防撞）被搬进撞车路 = 白给（7000 血一压就没）。
        # 撞车路必须从非防撞墙的换位目标里排除；防撞墙可以进（撞它爆胎）。
        from pvz.tactics import relocation_target
        src = Plant(0, 1, 2, 320, hp=7300)           # 回收高坚果在撞车路（lane 2）
        b = self.board([], [Zombie(0, 1, 5, x=500)], [src], sun=1000)
        dest = relocation_target(b, self.book, src)
        if dest is not None:
            self.assertNotEqual(dest[0], 1, '非防撞墙的换位目标不能是撞车路')
        anti = Plant(0, 1, 2, 421, hp=7300)          # 高冰果（crush_hits）可以进
        b2 = self.board([], [Zombie(0, 1, 5, x=500)], [anti], sun=1000)
        dest2 = relocation_target(b2, self.book, anti)
        self.assertIsNotNone(dest2, '防撞墙应有换位选择（含撞车路）')

    def test_adapt_stale_candidate_to_fresh_column(self):
        from pvz.policy import adapt_stale_candidate
        # 原候选 PEA@r0c3，快照里 c3 被占、同路 c4 可种 -> 适配到 c4
        b1 = self.board([PEA], [Zombie(0, 0, 0, x=500)])
        cands = [c for c in generate_candidates(b1, self.book)
                 if c.kind == 'plant' and c.type_id == PEA]
        pending = cands[0]
        b2 = self.board([PEA], [Zombie(0, 0, 0, x=440)],
                        [Plant(0, 0, pending.col, PEA)])
        fresh = generate_candidates(b2, self.book)
        self.assertTrue(any(c.type_id == PEA and c.row == 0 for c in fresh))
        adapted = adapt_stale_candidate(pending, b2, self.book)
        self.assertIsNotNone(adapted)
        self.assertEqual(adapted.type_id, PEA)
        self.assertEqual(adapted.row, 0)
        self.assertNotEqual(adapted.col, pending.col)

    def test_adapt_returns_none_when_no_same_type_candidate(self):
        from pvz.policy import adapt_stale_candidate
        b1 = self.board([PEA], [Zombie(0, 0, 0, x=500)])
        pending = next(c for c in generate_candidates(b1, self.book)
                       if c.kind == 'plant' and c.type_id == PEA)
        empty = self.board([PEA], [], [Plant(0, 0, 2, PEA), Plant(1, 0, 3, PEA)])
        self.assertIsNone(adapt_stale_candidate(pending, empty, self.book))


class PickDropTests(unittest.TestCase):
    """拾回掉落卡（2026-09-26）：回收事务被中断后，卡躺在草坪上必须能捡回来。"""

    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.assertTrue(self.book.bind_one(320, '回收高坚果'))

    def board(self, zombies=(), plants=(), sun=1000, drops=(), holding=False):
        b = BoardState(ok=True, sun=sun, rows=5, cols=9, game_clock=1000,
                       slots=[], plants=list(plants), zombies=list(zombies),
                       dropped_seeds=list(drops), holding=holding)
        return b

    def test_pick_candidate_for_stranded_seed(self):
        from pvz.board import DroppedSeed
        b = self.board(drops=[DroppedSeed(0, 320, x=445, y=185, width=50, height=70)])
        cs = [c for c in generate_candidates(b, self.book) if c.kind == 'pick']
        self.assertTrue(cs, '草坪上有掉落卡应给出拾回候选')
        self.assertEqual((cs[0].row, cs[0].col), (1, 5))
        self.assertIn('lying on the lawn', cs[0].describe(self.book))

    def test_no_pick_when_holding_or_no_destination(self):
        from pvz.board import DroppedSeed
        drop = [DroppedSeed(0, 320, x=445, y=185, width=50, height=70)]
        self.assertFalse([c for c in generate_candidates(
            self.board(drops=drop, holding=True), self.book) if c.kind == 'pick'],
            '手上有东西时不出拾回候选')
        # 五路全是撞车僵尸：非防撞卡无合法落点 -> 不出候选（宁躺不乱点）
        crushes = [Zombie(i, i, 5, x=500) for i in range(5)]
        self.assertFalse([c for c in generate_candidates(
            self.board(zombies=crushes, drops=drop), self.book) if c.kind == 'pick'])

    def test_pick_drop_transaction_replants(self):
        from pvz.board import DroppedSeed
        from pvz.transactions import run_transaction
        from pvz.ui import Layout
        from test_transactions import FakeGame
        g = FakeGame()
        g.state.plants = []
        g.state.dropped_seeds = [DroppedSeed(0, 161, x=100, y=150, width=50, height=70)]
        book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.assertTrue(book.bind_one(161, '回收高坚果'))
        agent = FakeGameAgent(g, book)
        c = Candidate('x', 'pick', 1, 1, type_id=161)
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(agent, g.read(), pick=c)
        self.assertTrue(result['completed'], result)
        self.assertEqual(result['kind'], 'pick_drop')
        self.assertTrue(any(p.type_id == 161 for p in g.state.plants),
                        '拾回的卡应重新种上')
        self.assertFalse(g.state.dropped_seeds, '掉落卡应消失')

    # -- 事务：salvage 走完整铲子链路 --------------------------------------
    def test_salvage_transaction_removes_plant(self):
        g = FakeGame()
        g.state.plants = [Plant(0, 0, 3, PEA, hp=200, recently_eaten=True)]
        g.state.zombies = [Zombie(0, 0, 0, x=280)]
        book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.assertTrue(book.bind_one(PEA, '豌豆射手'))
        agent = FakeGameAgent(g, book)
        c = Candidate('x', 'shovel', 0, 3, type_id=PEA, salvage=True)
        with patch('pvz.transactions.time.sleep'):
            result = run_transaction(agent, g.read(), candidate=c)
        self.assertTrue(result['completed'], result)
        self.assertEqual(result['kind'], 'salvaged')
        self.assertFalse(any(p.cell == (0, 3) for p in g.state.plants))


if __name__ == '__main__':
    unittest.main()


class StrandedPickAndPriceTests(unittest.TestCase):
    """2026-09-26 夜战 23:12 会话复盘的三项修复回归。"""

    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.assertTrue(self.book.bind_one(307, '雷果子'))
        self.assertTrue(self.book.bind_one(320, '回收高坚果'))

    def board(self, cards=(), zombies=(), plants=(), sun=1000, drops=()):
        return BoardState(ok=True, sun=sun, rows=5, cols=9, game_clock=1000,
                          slots=[SeedSlot(i, t, 0, 1000) for i, t in enumerate(cards)],
                          plants=list(plants), zombies=list(zombies),
                          dropped_seeds=list(drops))

    def test_thunder_fruit_dynamic_price(self):
        # 夜战实测：雷果子 415/440/595 阳光连续被拒 —— 每多一株 +100
        self.assertEqual(self.book.cost(307), 400)
        self.book.sync_field_copies([Plant(0, 0, 0, 307)])
        self.assertEqual(self.book.cost(307), 500)
        self.book.sync_field_copies([Plant(0, 0, 0, 307), Plant(1, 1, 0, 307)])
        self.assertEqual(self.book.cost(307), 600)

    def test_rich_rejection_does_not_ratchet_floor(self):
        # 睡莲账面价被棘轮成 2276 的教训：阳光 ≥3×模型价时的拒绝是点偏
        # —— 这里用回收高坚果验证 note_unaffordable 的分诊逻辑在 agent 侧，
        # PlantBook 层面只验证 floor 语义（正常记录仍然生效）。
        self.book.note_unaffordable(320, 60)
        self.assertEqual(self.book.cost(320), 125)   # floor 61 < 图鉴 125
        self.book.note_unaffordable(320, 200)
        self.assertEqual(self.book.cost(320), 201)   # 真实"买不起"仍然记下界

    def test_wait_yields_to_stranded_pick(self):
        from pvz.policy import generate_candidates, merge_decision
        from pvz.jev import JevAnswer, JevResponse
        from pvz.board import DroppedSeed
        b = self.board(drops=[DroppedSeed(0, 320, x=445, y=185, width=50, height=70)])
        cands = generate_candidates(b, self.book)
        wait_cid = next(c.cid for c in cands if c.kind == 'wait')
        resp = JevResponse(answers={'action': JevAnswer('action', 'choice',
                                                        {'choice': wait_cid, 'confidence': 0.9})})
        dec = merge_decision(resp, cands, b, self.book)
        self.assertEqual(dec.candidate.kind, 'pick', '等待时应优先拾回 stranded 掉落卡')
        self.assertTrue(dec.fallback)
        self.assertTrue(any('stranded seed card' in n for n in dec.notes))


if __name__ == '__main__':
    unittest.main()


class PremiumEconomyTests(unittest.TestCase):
    """金卡节奏（用户 2026-09-26）：3 株产阳光（含女王）就该允许高价值出手。"""

    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        for tid, name in ((430, '向日葵女王'), (431, '阳光向日葵'), (421, '高冰果')):
            self.assertTrue(self.book.bind_one(tid, name))

    def test_premium_allowed_with_three_producers(self):
        # 女王+2向日葵=3 producer、僵尸在中圈、阳光 900：高冰果应能出手
        plants = [Plant(0, 0, 2, 430), Plant(1, 1, 0, 431), Plant(2, 1, 1, 431)]
        b = BoardState(ok=True, sun=700, rows=5, cols=9, game_clock=40000,
                       slots=[SeedSlot(0, 421, 0, 1000)],
                       plants=plants, zombies=[Zombie(0, 0, 0, x=700)])
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.type_id == 421]
        self.assertTrue(cs, '3 株产阳光时高价值卡应允许出手')

    def test_premium_still_blocked_with_two_producers(self):
        # 只有 2 株产阳光：维持经济护栏（防死亡螺旋），高价值卡仍被拦
        plants = [Plant(0, 0, 2, 430), Plant(1, 1, 0, 431)]
        b = BoardState(ok=True, sun=700, rows=5, cols=9, game_clock=40000,
                       slots=[SeedSlot(0, 421, 0, 1000)],
                       plants=plants, zombies=[Zombie(0, 0, 0, x=700)])
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.type_id == 421]
        self.assertFalse(cs, '2 株产阳光时高价值卡仍应被经济闸拦住')


if __name__ == '__main__':
    unittest.main()

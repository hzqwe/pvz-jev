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
        b = self.board([WALL, ICE], [Zombie(0, 0, CRUSH_Z, x=500)], sun=1200)
        cands = generate_candidates(b, self.book)
        wall = next(c for c in cands
                    if c.kind == 'plant' and c.type_id == WALL and c.row == 0)
        ice = next(c for c in cands
                   if c.kind == 'plant' and c.type_id == ICE and c.row == 0)
        self.assertIn('WARNING', wall.why)
        self.assertIn('Crush-resistant', ice.why)
        self.assertGreater(ice.score, wall.score, '防撞墙应比普通墙优先')

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
        # 黑橄榄球类（traits: eat_dps=150）：血量 350 撑不过 3s -> 应该铲
        z = Zombie(0, 0, FAST_Z, x=280)
        b = self.board([PEA], [z],
                       [Plant(0, 0, 3, PEA, hp=350, recently_eaten=True)], sun=50)
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'shovel' and c.salvage]
        self.assertTrue(cs, '快速啃食者面前 350 血(≤150*3)也应触发抢救')
        slow = self.board([PEA], [Zombie(0, 0, 0, x=280)],
                          [Plant(0, 0, 3, PEA, hp=350, recently_eaten=True)], sun=50)
        self.assertFalse([c for c in generate_candidates(slow, self.book)
                          if c.kind == 'shovel' and c.salvage],
                         '普通啃食速度(100/s)下 350 血还能撑 >3s，不铲')

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

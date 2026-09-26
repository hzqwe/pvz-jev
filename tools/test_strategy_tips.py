"""攻略补充批次回归（用户 2026-09-26 第五批）：
积雪格不可种植 / 阳光充裕填满四列 / 墙前排优先 / 九宫格加成 /
女王分行 / 铲旧换新（火炬槽位升级）。全部离线。
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_strategy import StrategyTests, SUN, PEA, WALL, STRONG, BOMB
from pvz.board import BoardState, Plant, SeedSlot, Zombie
from pvz.plants import PlantBook
from pvz.policy import generate_candidates, merge_decision, snow_blocked
from pvz.jev import JevAnswer, JevResponse

QUEEN, GATLING = range(430, 432)   # 430=向日葵女王 431=狂野机枪射手


class SnowCellTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        for tid, name in ((SUN, '阳光向日葵'), (WALL, '冰冻坚果')):
            self.assertTrue(self.book.bind_one(tid, name))

    def board(self, cards, zombies=(), plants=(), sun=1000):
        b = BoardState(ok=True, sun=sun, rows=5, cols=9, game_clock=1000,
                       slots=[SeedSlot(i, t, 0, 1000) for i, t in enumerate(cards)],
                       plants=list(plants), zombies=list(zombies))
        return b

    def test_snow_cell_blocks_candidates(self):
        b = self.board([SUN], [])
        until = time.time() + 20
        b.snow_cells = {(r, 0): until for r in range(5)}     # A 列全积雪
        b.snow_cells.update({(0, 1): until, (0, 2): until})
        self.assertTrue(snow_blocked(b, 0, 0))
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.col == 0]
        self.assertFalse(cs, '积雪格（A列）不应出现任何种植候选')
        ok = [c for c in generate_candidates(b, self.book) if c.kind == 'plant']
        self.assertTrue(ok, '非积雪格照常出候选')

    def test_melted_snow_reopens(self):
        b = self.board([SUN], [])
        b.snow_cells = {(0, 0): time.time() - 1}           # 已融化
        self.assertFalse(snow_blocked(b, 0, 0))


class RichFillTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        for tid, name in ((SUN, '阳光向日葵'), (PEA, '豌豆射手'), (WALL, '冰冻坚果')):
            self.assertTrue(self.book.bind_one(tid, name))

    def board(self, cards, zombies=(), plants=(), sun=1000):
        return BoardState(ok=True, sun=sun, rows=5, cols=9, game_clock=1000,
                          slots=[SeedSlot(i, t, 0, 1000) for i, t in enumerate(cards)],
                          plants=list(plants), zombies=list(zombies))

    @staticmethod
    def wait_answer(cands):
        wait_cid = next(c.cid for c in cands if c.kind == 'wait')
        return JevResponse(answers={'action': JevAnswer('action', 'choice',
                                                        {'choice': wait_cid, 'confidence': 0.9}),
                                    'hold_sun': JevAnswer('hold_sun', 'noul', {'noul': 0.99})})

    def test_rich_calm_fills_more_producers(self):
        # 10 株向日葵（旧上限）+ 每路已有射手：旧逻辑会发呆，calm_fill 应继续铺
        plants = [Plant(i, i // 2, i % 2, SUN) for i in range(10)]
        plants += [Plant(50 + r, r, 6, PEA) for r in range(5)]
        b = self.board([SUN], [], plants, sun=1500)
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.type_id == SUN]
        self.assertTrue(cs, '阳光充裕的平静期应继续铺向日葵（填满四列）')
        # 模型发呆 -> 代码从候选里挑一个补上
        cands = generate_candidates(b, self.book)
        dec = merge_decision(self.wait_answer(cands), cands, b, self.book)
        self.assertFalse(dec.hold, 'calm_fill 下不允许发呆')
        self.assertTrue(any('Rich and calm' in n for n in dec.notes))

    def test_poor_calm_keeps_old_cap(self):
        # 阳光只有 550（未达 600 门槛）：维持旧上限，10 株后不再铺
        plants = [Plant(i, i // 2, i % 2, SUN) for i in range(10)]
        b = self.board([SUN], [], plants, sun=550)
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.type_id == SUN]
        self.assertFalse(cs, '未达充裕门槛时维持旧 producer 上限')


class TorchColumnTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        for tid, name in ((SUN, '阳光向日葵'), (PEA, '豌豆射手'),
                          (QUEEN, '向日葵女王'), (GATLING, '狂野机枪射手')):
            self.assertTrue(self.book.bind_one(tid, name))

    def board(self, cards, zombies=(), plants=(), sun=1000):
        return BoardState(ok=True, sun=sun, rows=5, cols=9, game_clock=1000,
                          slots=[SeedSlot(i, t, 0, 1000) for i, t in enumerate(cards)],
                          plants=list(plants), zombies=list(zombies))

    def test_queen_prefers_torch_free_rows(self):
        # R1 已有女王 -> 第二株女王应去别的行
        b = self.board([QUEEN], [Zombie(0, 0, 0, x=760)],
                       [Plant(0, 0, 2, QUEEN)], sun=800)
        b.game_clock = 500
        cands = generate_candidates(b, self.book)
        cs = [c for c in cands if c.kind == 'plant' and c.type_id == QUEEN]
        self.assertTrue(cs)
        self.assertNotEqual(cs[0].row, 0, '女王不应与现有女王同行')

    def test_shovel_upgrade_frees_torch_slot(self):
        # 女王 C 列、身后 B 列是向日葵；手里有狂野机枪 -> 应给"铲旧换新"候选
        b = self.board([GATLING], sun=1000,
                       plants=[Plant(0, 0, 2, QUEEN), Plant(1, 0, 1, SUN)])
        b.game_clock = 40000
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'shovel' and c.salvage]
        self.assertTrue(cs, '女王身后的廉价植物应让位给过火射手')
        self.assertEqual((cs[0].row, cs[0].col), (0, 1))
        self.assertIn('Torch-column upgrade', cs[0].why)

    def test_shovel_upgrade_needs_upgrade_card_and_sun(self):
        # 手里没有过火射手 -> 不铲（铲了没东西补位）
        b = self.board([SUN], sun=1000,
                       plants=[Plant(0, 0, 2, QUEEN), Plant(1, 0, 1, SUN)])
        b.game_clock = 40000
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'shovel' and c.salvage]
        self.assertFalse(cs, '没有替换卡时不应铲掉炬火槽位植物')


class WallFrontPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        self.assertTrue(self.book.bind_one(WALL, '冰冻坚果'))

    def test_wall_prefers_column_e_on_far_zombies(self):
        b = BoardState(ok=True, sun=600, rows=5, cols=9, game_clock=1000,
                       slots=[SeedSlot(0, WALL, 0, 1000)],
                       zombies=[Zombie(0, 0, 0, x=700)])
        cs = [c for c in generate_candidates(b, self.book)
              if c.kind == 'plant' and c.type_id == WALL]
        self.assertTrue(cs)
        self.assertGreaterEqual(cs[0].col, 4, '僵尸还远时墙应种第5列(E)及以上')


if __name__ == '__main__':
    unittest.main()

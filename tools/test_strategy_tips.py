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


class MemorySunTests(unittest.TestCase):
    """coin 池内存驱动收阳光（2026-09-27，00:11 会话日志离线确认）。"""

    def test_memory_positions_filter_and_convert(self):
        from pvz.ui import Layout, memory_sun_positions
        lay = Layout()
        lay.client_w, lay.client_h = lay.base_w, lay.base_h
        coins = [
            {"index": 0, "type": 6, "x": 400, "y": 250, "w": 50, "h": 60},   # 落地阳光
            {"index": 1, "type": 1, "x": 650, "y": 150, "w": 50, "h": 60},   # 飞行阳光
            {"index": 2, "type": 4, "x": 60, "y": 60, "w": 50, "h": 30},     # 阳光计数器
            {"index": 3, "type": 16, "x": 300, "y": 300, "w": 50, "h": 60},  # 种子卡
            {"index": 4, "type": 2, "x": 700, "y": 400, "w": 50, "h": 60},   # 未知杂物
        ]
        out = memory_sun_positions(coins, lay)
        self.assertEqual([t for _, _, t in out], [6, 1])   # 只留阳光、按 x 升序
        cx, cy, _ = out[0]
        ref = lay.cell_center(2, 4)                        # 逻辑(400,250)≈R3E 附近
        self.assertAlmostEqual(cx, ref[0], delta=40)       # x 与格中心一致
        self.assertAlmostEqual(cy, ref[1], delta=60)       # y 公式带固定偏移，容忍半格

    def test_off_lawn_coins_are_dropped(self):
        from pvz.ui import Layout, memory_sun_positions
        lay = Layout()
        lay.client_w, lay.client_h = lay.base_w, lay.base_h
        out = memory_sun_positions(
            [{"type": 6, "x": -300, "y": 250, "w": 50, "h": 60}], lay)
        self.assertEqual(out, [])


class WaterLanePadTests(unittest.TestCase):
    """受压水路的荷叶链（2026-09-27 夜战教训：荷叶后接了豌豆/阳光菇，
    而挨打的 L4 全程没有荷叶——水路被穿）。"""

    def setUp(self):
        self.book = PlantBook(hybrid_file='', cost_file='', ids_file='')
        for tid, name in ((0, '豌豆射手'), (12, '冰冻坚果'), (421, '高冰果'),
                          (16, '睡莲'), (1, '阳光向日葵')):
            self.assertTrue(self.book.bind_one(tid, name))

    def test_pressured_water_lane_pads_chain_walls(self):
        # 泳池：L3/L4 水路被压（僵尸 460/677），手里有坚果+高冰果+睡莲
        # -> 挨打水路应出"荷叶+连锁墙"候选，分数不再被 45 封顶
        b = BoardState(ok=True, sun=900, rows=6, cols=9, game_clock=40000, scene=2,
                       slots=[SeedSlot(0, 0, 0, 1000), SeedSlot(1, 12, 0, 1000),
                              SeedSlot(2, 421, 0, 1000), SeedSlot(3, 16, 0, 1000)],
                       zombies=[Zombie(0, 2, 0, x=460), Zombie(1, 3, 0, x=677)],
                       plants=[Plant(0, 0, 2, 430), Plant(1, 1, 0, 1)])
        b.row_types = {0: 1, 1: 1, 2: 2, 3: 2, 4: 1, 5: 1}
        pads = [c for c in generate_candidates(b, self.book)
                if c.kind == 'plant' and c.supports_type is not None]
        self.assertTrue(pads, '受压水路应出荷叶候选')
        pressured = [c for c in pads if c.score >= 70]
        self.assertTrue(pressured, '受压水路的荷叶不应被 45 封顶')
        best=max(pressured,key=lambda c:c.score)
        self.assertTrue(self.book.has_tag(best.supports_type, 'wall'),
                        'The best pressured-water compound must still be a blocker')
        self.assertTrue(all(c.supports_type is not None for c in pressured),
                        'Every alternative preserves its scored followup; no bare pad')

    def test_calm_water_pad_stays_cheap(self):
        # 平静期的水路荷叶维持低价（铺地基，不是救场）
        b = BoardState(ok=True, sun=900, rows=6, cols=9, game_clock=40000, scene=2,
                       slots=[SeedSlot(0, 0, 0, 1000), SeedSlot(1, 12, 0, 1000),
                              SeedSlot(2, 16, 0, 1000)],
                       zombies=[], plants=[Plant(0, 0, 2, 430)])
        b.row_types = {0: 1, 1: 1, 2: 2, 3: 2, 4: 1, 5: 1}
        pads = [c for c in generate_candidates(b, self.book)
                if c.kind == 'plant' and c.supports_type is not None]
        self.assertTrue(pads and all(c.score <= 45 for c in pads))
